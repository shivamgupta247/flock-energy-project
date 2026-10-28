import asyncio
import hashlib
import hmac

import httpx
import pytest

from app import portal as portal_module
from app.config import Settings
from app.portal import PortalClient, PortalNotFound, PortalUnavailable, decode_devalue

BASE = "https://portal.test"
SETTINGS = Settings(portal_url=BASE, portal_email="e", portal_password="p", portal_requests_per_minute=60000,
                    index_ttl_seconds=600, readings_ttl_seconds=300, request_timeout_seconds=5,
                    warm_on_startup=False)


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    monkeypatch.setattr(portal_module, "_backoff", lambda attempt: 0)


class FakePortal:
    """Just enough of the portal's behaviour to exercise the client."""

    def __init__(self):
        self.logins = 0
        self.valid_token = None
        self.calls = []
        self.fail_with = []  # status codes to return before succeeding

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(path)
        if path == "/login":
            self.logins += 1
            self.valid_token = f"tok{self.logins}"
            return httpx.Response(200, json={"type": "redirect", "status": 303, "location": "/meters"},
                                  headers={"set-cookie": f"__Secure-better-auth.session_token={self.valid_token}; Path=/; Secure"})
        if self.valid_token not in request.headers.get("cookie", ""):
            if path.endswith("__data.json"):
                return httpx.Response(200, json={"type": "redirect", "location": "/login"})
            return httpx.Response(401, json={"error": "unauthorized"})
        if self.fail_with:
            return httpx.Response(self.fail_with.pop(0), json={"error": "rate_limited"})
        if path == "/portal/keys":
            return httpx.Response(200, json={"data": {"signingSecret": "s3cret"}})
        if path == "/portal/export":
            query = request.url.query.decode()
            expected = hmac.new(b"s3cret", "\n".join(["GET", path, query, request.headers.get("x-timestamp", "")]).encode(),
                                hashlib.sha256).hexdigest()
            if request.headers.get("x-signature") != expected:
                return httpx.Response(401, json={"error": "signature_invalid"})
            return httpx.Response(200, json={"data": [{"meterId": "J1"}], "total": 1})
        if path == "/portal/meters/MISSING/geo":
            return httpx.Response(404, json={"error": "not_found"})
        if path == "/meters/MISSING/__data.json":
            return httpx.Response(200, json={"type": "data", "nodes": [None, {"type": "data"},
                                                                      {"type": "error", "status": 404}]})
        if path == "/portal/meters/J1/geo":
            return httpx.Response(200, json={"data": {"latitude": "1", "longitude": "2"}})
        return httpx.Response(404, json={"error": "not_found"})


def make_client(fake):
    return PortalClient(SETTINGS, transport=httpx.MockTransport(fake))


async def test_logs_in_lazily_and_reuses_session():
    fake = FakePortal()
    client = make_client(fake)
    await client.meter_geo("J1")
    await client.meter_geo("J1")
    assert fake.logins == 1


async def test_relogs_in_when_session_expires():
    fake = FakePortal()
    client = make_client(fake)
    await client.meter_geo("J1")
    fake.valid_token = "rotated"  # server-side expiry
    assert (await client.meter_geo("J1"))["latitude"] == "1"
    assert fake.logins == 2


async def test_concurrent_requests_on_an_expired_session_log_in_once():
    fake = FakePortal()
    client = make_client(fake)
    await client.meter_geo("J1")
    fake.valid_token = "rotated"
    await asyncio.gather(*[client.meter_geo("J1") for _ in range(10)])
    assert fake.logins == 2


async def test_signed_export_uses_hmac_of_method_path_query_timestamp():
    client = make_client(FakePortal())
    assert await client.export_meters() == [{"meterId": "J1"}]


async def test_retries_rate_limit_then_succeeds():
    fake = FakePortal()
    fake.fail_with = [429, 429, 502]
    client = make_client(fake)
    assert (await client.meter_geo("J1"))["longitude"] == "2"


async def test_gives_up_after_repeated_failures():
    fake = FakePortal()
    fake.fail_with = [429] * 10
    with pytest.raises(PortalUnavailable):
        await make_client(fake).meter_geo("J1")


async def test_not_found_in_both_endpoint_styles():
    client = make_client(FakePortal())
    with pytest.raises(PortalNotFound):
        await client.meter_geo("MISSING")
    with pytest.raises(PortalNotFound):
        await client.meter_detail("MISSING")


def test_decode_devalue():
    payload = [{"meterId": 1, "detail": 2, "none": -1}, "J100000", {"data": 3}, [4], {"parameterName": 5}, "Make"]
    assert decode_devalue(payload) == {"meterId": "J100000", "detail": {"data": [{"parameterName": "Make"}]}, "none": None}
