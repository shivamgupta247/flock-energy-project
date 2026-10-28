import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import load_settings
from app.index import MeterIndex
from app.main import app
from app.portal import PortalNotFound, PortalUnavailable


def export_row(meter_id, status="Installed", dt="DT-001", lat=26.9, lng=75.8, circle_code="C-01"):
    return {
        "meterId": meter_id, "serialNo": f"SN{meter_id}", "make": "Genus", "phaseType": "three",
        "installStatus": status, "installType": "CT Operated", "build": "v2", "dtCode": dt,
        "hierarchy": {
            "zone": {"name": "Jaipur Zone 1", "code": "Z-01"},
            "circle": {"name": "Circle 1", "code": circle_code},
            "division": {"name": "Division 1", "code": "D-01"},
            "subdivision": {"name": "Subdivision 1", "code": "SD-01"},
            "substation": {"name": "Substation 1", "code": "SS-01"},
            "feeder": {"name": "Feeder 1", "code": "F-001"},
            "dt": {"name": "Malviya Nagar DT 1", "code": dt},
        },
        "geo": {"lat": lat, "lng": lng},
    }


class FakePortal:
    def __init__(self):
        self.export = [
            export_row("J100000"),
            export_row("J100001", status="Faulty", lat=27.5, lng=76.5),
            export_row("J100002", circle_code=""),
        ]
        self.energy_calls = 0
        self.export_calls = 0
        self.fail_export = False

    async def export_meters(self):
        self.export_calls += 1
        if self.fail_export:
            raise PortalUnavailable("down")
        return self.export

    async def list_transformers(self):
        return [{"code": "DT-001", "name": "Malviya Nagar DT 1", "feederCode": "F-001", "capacityKva": 100}]

    async def meter_detail(self, meter_id):
        if meter_id != "J100999":
            raise PortalNotFound(meter_id)
        return {"meterId": meter_id, "detail": {"classData": json.dumps({"installed_meter": {
            "MeterId": meter_id, "SerialNo": "GN1", "Make": "Genus"}})},
            "hierarchy": {"Zone": "Jaipur Zone 1 (Z-01)", "DT": "Malviya Nagar DT 1 (DT-001)"}}

    async def meter_geo(self, meter_id):
        return {"latitude": "26.9", "longitude": "75.8"}

    async def meter_energy(self, meter_id, date_from, date_to):
        self.energy_calls += 1
        await asyncio.sleep(0.05)  # long enough for concurrent requests to overlap
        if meter_id == "NOPE":
            raise PortalNotFound(meter_id)
        return [
            {"timestamp": "01/06/2026 00:00", "kwh": "100", "kvah": "110", "voltR": "230"},
            {"timestamp": "01/06/2026 00:30", "kwh": "100.5", "kvah": "110.6", "voltR": "231"},
            {"timestamp": "01/06/2026 01:00", "kwh": "101", "kvah": "111.2", "voltR": "229"},
        ]


@pytest.fixture
def client():
    with TestClient(app) as c:
        fake = FakePortal()
        app.state.portal = fake
        app.state.index = MeterIndex(fake, ttl_seconds=600)
        app.state.readings_cache = {}
        c.fake = fake
        yield c


def test_list_filters_and_paginates(client):
    body = client.get("/v1/meters", params={"install_status": "Faulty"}).json()
    assert [m["meter_id"] for m in body["data"]] == ["J100001"]
    body = client.get("/v1/meters", params={"page_size": 2, "page": 2}).json()
    assert body["page"] == {"page": 2, "page_size": 2, "total": 3, "total_pages": 2}
    assert body["freshness"]["stale"] is False


def test_proximity_search_sorted_with_distance(client):
    body = client.get("/v1/meters", params={"near_lat": 26.9, "near_lng": 75.8, "radius_km": 5}).json()
    assert [m["meter_id"] for m in body["data"]] == ["J100000", "J100002"]
    assert body["data"][0]["distance_km"] == 0


def test_meter_from_snapshot_includes_repairs(client):
    body = client.get("/v1/meters/j100002").json()
    assert body["source"] == "snapshot"
    assert body["data"]["hierarchy"]["circle"]["code"] == "C-01"
    assert body["data"]["data_quality"]


def test_meter_missing_from_snapshot_falls_back_to_live(client):
    body = client.get("/v1/meters/J100999").json()
    assert body["source"] == "live"
    assert body["data"]["serial_no"] == "GN1" and body["data"]["nameplate_format"] == "v2"
    assert client.get("/v1/meters/J100998").status_code == 404


def test_readings_raw_and_daily_and_cached(client):
    raw = client.get("/v1/meters/J100000/readings", params={"from": "2026-06-01", "to": "2026-06-01"}).json()
    assert raw["interval_minutes"] == 30 and raw["total_kwh"] == 1.0
    assert raw["readings"][1]["kwh_delta"] == 0.5
    client.get("/v1/meters/J100000/readings", params={"from": "2026-06-01", "to": "2026-06-01"})
    assert client.fake.energy_calls == 1
    daily = client.get("/v1/meters/J100000/readings", params={"resolution": "daily"}).json()
    assert daily["daily"][0]["kwh"] == 1.0 and "readings" not in daily


async def test_concurrent_identical_readings_requests_share_one_portal_call():
    fake = FakePortal()
    app.state.settings = load_settings()
    app.state.portal = fake
    app.state.readings_cache = {}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as ac:
        responses = await asyncio.gather(*[ac.get("/v1/meters/J100000/readings") for _ in range(5)])
    assert all(r.status_code == 200 for r in responses)
    assert fake.energy_calls == 1


def test_errors_have_one_shape(client):
    for path, status in [("/v1/meters/NOPE/readings", 404), ("/v1/meters?install_status=bad", 422),
                         ("/v1/meters/J1/readings?from=2026-06-02&to=2026-06-01", 422), ("/v1/transformers/X", 404)]:
        resp = client.get(path)
        assert resp.status_code == status
        assert set(resp.json()["error"]) == {"code", "message"}


def test_portal_outage_on_first_load_is_503(client):
    client.fake.fail_export = True
    resp = client.get("/v1/meters")
    assert resp.status_code == 503 and resp.json()["error"]["code"] == "portal_unavailable"


def test_stale_snapshot_is_served_while_refresh_fails_in_background(client):
    client.get("/v1/meters")
    client.fake.fail_export = True
    app.state.index._loaded_monotonic -= 10_000  # age the snapshot past its TTL
    body = client.get("/v1/meters").json()  # served immediately; refresh starts in background
    assert body["page"]["total"] == 3 and body["freshness"]["stale"] is True
    assert client.get("/health").json()["status"] == "degraded"


def test_stale_snapshot_is_replaced_by_background_refresh(client):
    client.get("/v1/meters")
    client.fake.export.append(export_row("J100003"))
    app.state.index._loaded_monotonic -= 10_000
    client.get("/v1/meters")  # triggers the refresh
    assert client.get("/v1/meters").json()["page"]["total"] == 4


def test_outage_on_first_load_fails_fast_during_cooldown(client):
    client.fake.fail_export = True
    client.fake.export_calls = 0
    assert client.get("/v1/meters").status_code == 503
    assert client.get("/v1/meters").status_code == 503
    assert client.fake.export_calls == 1  # second request didn't retry the portal


def test_meter_ids_that_are_not_id_shaped_are_rejected(client):
    for bad in ["..", "J1.0", "a" * 40]:
        resp = client.get(f"/v1/meters/{bad}/readings")
        assert resp.status_code in (404, 422), bad
    assert client.fake.energy_calls == 0


def test_web_client_is_served_at_root(client):
    assert client.get("/", follow_redirects=False).headers["location"] == "/ui/"
    page = client.get("/ui/")
    assert page.status_code == 200 and "Urja Meter Explorer" in page.text


def test_transformers_and_hierarchy(client):
    dts = client.get("/v1/transformers").json()["data"]
    assert dts[0]["meter_count"] == 3 and dts[0]["meters_by_status"] == {"Installed": 2, "Faulty": 1}
    tree = client.get("/v1/hierarchy").json()["data"]
    assert tree[0]["code"] == "Z-01" and tree[0]["meter_count"] == 3
