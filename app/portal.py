"""Low-level client for the Urja Meter Ops portal.

Everything that knows about the portal's wire format lives here: the
form-based login, the session cookie, the HMAC-signed export, the SvelteKit
`__data.json` encoding and the rate limit. The rest of the service only sees
plain Python dicts. See PROTOCOL.md for how each of these was discovered.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import time
from typing import Any, Dict, List, Optional

import httpx

from .config import Settings

log = logging.getLogger(__name__)

SESSION_COOKIE = "__Secure-better-auth.session_token"


class PortalError(Exception):
    """The portal failed or behaved unexpectedly."""


class PortalAuthError(PortalError):
    """We could not log in with the configured credentials."""


class PortalNotFound(PortalError):
    """The portal says the requested resource does not exist."""


class PortalUnavailable(PortalError):
    """The portal kept failing (rate limit, 5xx, network) after retries."""


class RateLimiter:
    """Spaces requests evenly so we never burst past the portal's limit.

    The portal returns 429 after ~120 requests/minute and sends no
    Retry-After header, so the cheapest strategy is to never trigger it.
    """

    def __init__(self, per_minute: int):
        self._interval = 60.0 / max(per_minute, 1)
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            delay = self._next - now
            self._next = max(now, self._next) + self._interval
        if delay > 0:
            await asyncio.sleep(delay)


def decode_devalue(payload: List[Any]) -> Any:
    """Decode SvelteKit's `devalue` flat encoding used by `__data.json`.

    The payload is a flat list: index 0 is the root, and every object value /
    array element is an index into the list. Negative numbers are sentinels.
    """
    sentinels = {-1: None, -2: None, -3: float("nan"), -4: float("inf"), -5: float("-inf"), -6: -0.0}
    cache: Dict[int, Any] = {}

    def hydrate(i: int) -> Any:
        if i in sentinels:
            return sentinels[i]
        if i in cache:
            return cache[i]
        value = payload[i]
        if isinstance(value, list):
            if value and isinstance(value[0], str):  # typed value, e.g. ["Date", "..."]
                result = value[1] if len(value) > 1 else None
            else:
                result = [hydrate(j) for j in value]
        elif isinstance(value, dict):
            result = {k: hydrate(j) for k, j in value.items()}
        else:
            result = value
        cache[i] = result
        return result

    return hydrate(0)


class PortalClient:
    MAX_ATTEMPTS = 5

    def __init__(self, settings: Settings, transport: Optional[httpx.AsyncBaseTransport] = None):
        self._settings = settings
        self._http = httpx.AsyncClient(
            base_url=settings.portal_url,
            timeout=settings.request_timeout_seconds,
            transport=transport,
            follow_redirects=False,
        )
        self._limiter = RateLimiter(settings.portal_requests_per_minute)
        self._login_lock = asyncio.Lock()
        # Incremented on every successful login. A request that saw the session
        # expire only logs in again if nobody else has done so since, so N
        # concurrent requests hitting an expired session cause one login, not N.
        self._session = 0

    async def close(self) -> None:
        await self._http.aclose()

    # -- session -----------------------------------------------------------

    async def login(self, expired_session: Optional[int] = None) -> None:
        async with self._login_lock:
            if self._session and self._session != expired_session:
                return  # already logged in, or someone else re-logged in meanwhile
            await self._limiter.wait()
            # SvelteKit form actions reject cross-origin POSTs, so send Origin.
            resp = await self._http.post(
                "/login",
                data={"email": self._settings.portal_email, "password": self._settings.portal_password},
                headers={"Origin": self._settings.portal_url, "Accept": "application/json"},
            )
            body = _json_or_none(resp)
            if SESSION_COOKIE not in self._http.cookies or not body or body.get("type") != "redirect":
                raise PortalAuthError(f"Portal login failed (HTTP {resp.status_code}).")
            self._session += 1
            log.info("Logged in to portal")

    async def _request(self, path: str, params: Optional[Dict[str, Any]] = None, signed: bool = False) -> Any:
        """GET a portal path, handling auth, rate limiting and transient errors."""
        reauthed = False
        failures = 0
        for attempt in range(self.MAX_ATTEMPTS):
            if failures and attempt:  # back off after failures, not after a re-login
                await asyncio.sleep(_backoff(failures - 1))
            await self.login()  # no-op when a session exists
            session = self._session
            headers = await self._sign(path, params) if signed else {}
            await self._limiter.wait()
            try:
                resp = await self._http.get(path, params=params, headers=headers)
            except httpx.TransportError as exc:
                log.warning("Portal network error on %s: %s", path, exc)
                failures += 1
                continue

            body = _json_or_none(resp)
            error = body.get("error") if isinstance(body, dict) else None
            # Expired session: JSON endpoints answer 401 "unauthorized", SvelteKit
            # data endpoints answer 200 with a redirect to /login.
            expired = (resp.status_code == 401 and error == "unauthorized") or (
                isinstance(body, dict) and body.get("type") == "redirect" and body.get("location") == "/login"
            )
            if expired:
                if reauthed:
                    raise PortalAuthError("Portal rejected a freshly created session.")
                reauthed = True
                await self.login(expired_session=session)
                continue
            if resp.status_code == 404:
                raise PortalNotFound(path)
            # A rejected signature is usually clock skew between us and the
            # portal; a fresh timestamp on the next attempt is the fix.
            if resp.status_code == 429 or resp.status_code >= 500 or error == "signature_invalid":
                log.warning("Portal returned %s on %s (attempt %d)", resp.status_code, path, attempt + 1)
                failures += 1
                continue
            if resp.status_code != 200 or body is None:
                raise PortalError(f"Unexpected portal response {resp.status_code} on {path}")
            return body
        raise PortalUnavailable(f"Portal kept failing on {path}")

    async def _sign(self, path: str, params: Optional[Dict[str, Any]]) -> Dict[str, str]:
        """Build the x-timestamp / x-signature headers the export endpoint requires.

        signature = hex(HMAC-SHA256(secret, "GET\\n<path>\\n<query>\\n<unix seconds>"))
        The secret is per-session, served by /portal/keys, so fetch it each time.
        """
        secret = (await self._request("/portal/keys"))["data"]["signingSecret"]
        query = str(httpx.QueryParams(params or {}))
        timestamp = str(int(time.time()))
        message = "\n".join(["GET", path, query, timestamp]).encode()
        signature = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
        return {"x-timestamp": timestamp, "x-signature": signature}

    # -- endpoints -----------------------------------------------------------

    async def export_meters(self) -> List[Dict[str, Any]]:
        """Every meter with hierarchy and geo, in one signed call (the bulk path)."""
        body = await self._request("/portal/export", {"page": 1}, signed=True)
        meters = body["data"]
        if body.get("total") is not None and body["total"] != len(meters):
            log.warning("Export returned %d of %d meters", len(meters), body["total"])
        return meters

    async def list_transformers(self) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        page = 1
        while True:
            body = await self._request("/portal/dts", {"page": page})
            items.extend(body["data"])
            if not body["data"] or len(items) >= body["total"]:
                return items
            page += 1

    async def meter_detail(self, meter_id: str) -> Dict[str, Any]:
        """Nameplate + hierarchy, from the meter page's SvelteKit server load."""
        body = await self._request(f"/meters/{meter_id}/__data.json")
        if body.get("type") == "error" or body.get("status") == 404:
            raise PortalNotFound(meter_id)
        node = body["nodes"][-1]
        if not node or node.get("type") != "data":
            raise PortalNotFound(meter_id)
        return decode_devalue(node["data"])

    async def meter_geo(self, meter_id: str) -> Dict[str, Any]:
        return (await self._request(f"/portal/meters/{meter_id}/geo"))["data"]

    async def meter_energy(self, meter_id: str, date_from: Optional[str], date_to: Optional[str]) -> List[Dict[str, Any]]:
        params = {k: v for k, v in (("from", date_from), ("to", date_to)) if v}
        return (await self._request(f"/portal/meters/{meter_id}/energy", params))["data"]


def _json_or_none(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return None


def _backoff(attempt: int) -> float:
    return min(2 ** attempt, 30)
