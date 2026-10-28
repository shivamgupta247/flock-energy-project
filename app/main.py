"""HTTP layer: routes, validation, error mapping. Run with `uvicorn app.main:app`."""

import asyncio
import logging
import math
import pathlib
import time
from contextlib import asynccontextmanager
from datetime import date
from enum import Enum
from typing import Any, Dict, Optional, Tuple

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__
from .config import load_settings
from .index import MeterIndex
from .normalize import daily_summary, detect_interval_minutes, meter_from_detail, normalize_readings
from .portal import PortalAuthError, PortalClient, PortalError, PortalNotFound, PortalUnavailable
from .schemas import (
    Error, Freshness, Health, HierarchyResponse, MeterList, MeterResponse, ReadingsResponse,
    SummaryResponse, Transformer, TransformerList,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

DESCRIPTION = """
A clean, read-only API over the **Urja Meter Ops** portal: smart meters, their
place in the distribution network, their location and their energy readings.

* Meter, transformer and hierarchy queries are answered from a fleet snapshot
  pulled from the portal's bulk export and refreshed every few minutes. Every
  response says how fresh it is.
* Readings are fetched live (with a short cache) and normalised: numbers are
  numbers, timestamps are ISO 8601 with offset, duplicates are removed and
  per-interval consumption is derived from the cumulative registers.
* Problems in the portal's data are repaired where possible and always
  reported in `data_quality` rather than silently hidden.
"""

ERRORS: Dict[int, Dict[str, Any]] = {
    404: {"model": Error, "description": "Not found"},
    422: {"model": Error, "description": "Invalid parameters"},
    502: {"model": Error, "description": "Portal error"},
    503: {"model": Error, "description": "Portal unavailable"},
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    portal = PortalClient(settings)
    app.state.settings = settings
    app.state.portal = portal
    app.state.index = MeterIndex(portal, settings.index_ttl_seconds)
    app.state.readings_cache = {}
    warm = asyncio.create_task(_warm(app.state.index)) if settings.warm_on_startup else None
    yield
    if warm:
        warm.cancel()
    await portal.close()


async def _warm(index: MeterIndex) -> None:
    """Load the snapshot at startup so the first request doesn't pay for it."""
    try:
        await index.ensure_fresh()
    except PortalError as exc:
        logging.getLogger(__name__).warning("Startup snapshot load failed (will retry on demand): %s", exc)


app = FastAPI(
    title="Urja Meter API",
    version=__version__,
    description=DESCRIPTION,
    lifespan=lifespan,
    responses={k: v for k, v in ERRORS.items() if k >= 500},
)

# The web client: a static page that only talks to the /v1 API below.
STATIC_DIR = pathlib.Path(__file__).parent / "static"
app.mount("/ui", StaticFiles(directory=STATIC_DIR, html=True), name="ui")


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse("/ui/")


# -- errors --------------------------------------------------------------------


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


@app.exception_handler(StarletteHTTPException)
async def http_error(_: Request, exc: StarletteHTTPException):
    code = {404: "not_found", 422: "invalid_request"}.get(exc.status_code, "error")
    return _error(exc.status_code, code, str(exc.detail))


@app.exception_handler(PortalUnavailable)
async def portal_unavailable(_: Request, exc: PortalUnavailable):
    return _error(503, "portal_unavailable", "The upstream portal is not responding; try again shortly.")


@app.exception_handler(PortalAuthError)
async def portal_auth(_: Request, exc: PortalAuthError):
    return _error(502, "portal_auth_failed", "Could not authenticate with the upstream portal.")


@app.exception_handler(PortalError)
async def portal_error(_: Request, exc: PortalError):
    return _error(502, "portal_error", "The upstream portal returned an unexpected response.")


@app.exception_handler(Exception)
async def unexpected(_: Request, exc: Exception):
    logging.getLogger(__name__).exception("Unhandled error")
    return _error(500, "internal_error", "Something went wrong.")


@app.exception_handler(RequestValidationError)
async def validation_error(_: Request, exc: RequestValidationError):
    parts = ["{}: {}".format(".".join(str(p) for p in e["loc"][1:]), e["msg"]) for e in exc.errors()]
    return _error(422, "invalid_request", "; ".join(parts))


# -- dependencies --------------------------------------------------------------


async def fresh_index(request: Request) -> MeterIndex:
    index: MeterIndex = request.app.state.index
    await index.ensure_fresh()
    return index


def freshness(index: MeterIndex) -> Freshness:
    return Freshness(snapshot_loaded_at=index.loaded_at, stale=index.is_stale)


class InstallStatus(str, Enum):
    installed = "Installed"
    faulty = "Faulty"
    decommissioned = "Decommissioned"


class PhaseType(str, Enum):
    single = "single"
    three = "three"


class InstallType(str, Enum):
    whole_current = "Whole Current"
    ct_operated = "CT Operated"


class Resolution(str, Enum):
    raw = "raw"
    daily = "daily"


# Meter ids are interpolated into portal URLs, so only allow id-shaped values
# (otherwise e.g. ".." would reach other portal paths).
METER_ID = Path(pattern=r"^[A-Za-z0-9-]{1,32}$", description="Meter id, e.g. J100001 (case-insensitive).")


# -- routes --------------------------------------------------------------------


@app.get("/health", response_model=Health, tags=["meta"], summary="Service health")
async def health(request: Request):
    index: MeterIndex = request.app.state.index
    try:
        await index.ensure_fresh()
        status = "ok" if not index.last_error else "degraded"
    except PortalError:
        status = "unavailable"
    return Health(status=status, snapshot_loaded_at=index.loaded_at, meters=len(index.meters),
                  last_refresh_error=index.last_error)


@app.get("/v1/meters", response_model=MeterList, tags=["meters"], summary="List and search meters",
         responses={422: ERRORS[422]})
async def list_meters(
    q: Optional[str] = Query(None, description="Case-insensitive substring of meter id or serial number."),
    install_status: Optional[InstallStatus] = None,
    make: Optional[str] = Query(None, description="Manufacturer, e.g. Genus, HPL, 'L&T'."),
    phase_type: Optional[PhaseType] = None,
    install_type: Optional[InstallType] = None,
    zone: Optional[str] = Query(None, description="Zone code, e.g. Z-01."),
    circle: Optional[str] = Query(None, description="Circle code, e.g. C-04."),
    division: Optional[str] = None,
    subdivision: Optional[str] = None,
    substation: Optional[str] = None,
    feeder: Optional[str] = Query(None, description="Feeder code, e.g. F-007."),
    dt: Optional[str] = Query(None, description="Distribution transformer code, e.g. DT-007."),
    near_lat: Optional[float] = Query(None, ge=-90, le=90, description="Proximity search: latitude."),
    near_lng: Optional[float] = Query(None, ge=-180, le=180, description="Proximity search: longitude."),
    radius_km: float = Query(1.0, gt=0, le=100, description="Proximity search radius. Results sorted nearest first."),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    index: MeterIndex = Depends(fresh_index),
):
    if (near_lat is None) != (near_lng is None):
        raise HTTPException(422, "near_lat and near_lng must be given together")
    near = {"latitude": near_lat, "longitude": near_lng, "radius_km": radius_km} if near_lat is not None else None
    results = index.search(
        q=q,
        filters={
            "install_status": install_status.value if install_status else None,
            "make": make,
            "phase_type": phase_type.value if phase_type else None,
            "install_type": install_type.value if install_type else None,
        },
        hierarchy={"zone": zone, "circle": circle, "division": division, "subdivision": subdivision,
                   "substation": substation, "feeder": feeder, "dt": dt},
        near=near,
    )
    start = (page - 1) * page_size
    return MeterList(
        data=results[start:start + page_size],
        page={"page": page, "page_size": page_size, "total": len(results),
              "total_pages": max(1, math.ceil(len(results) / page_size))},
        freshness=freshness(index),
    )


@app.get("/v1/meters/{meter_id}", response_model=MeterResponse, tags=["meters"], summary="Get one meter",
         responses={404: ERRORS[404]})
async def get_meter(request: Request, meter_id: str = METER_ID, index: MeterIndex = Depends(fresh_index)):
    meter_id = meter_id.upper()
    if meter_id in index.by_id:
        return MeterResponse(data=index.by_id[meter_id], source="snapshot", freshness=freshness(index))
    # Not in the snapshot: it may have been added since. Ask the portal directly.
    portal: PortalClient = request.app.state.portal
    try:
        detail = await portal.meter_detail(meter_id)
    except PortalNotFound:
        raise HTTPException(404, f"Meter {meter_id} not found")
    try:
        geo = await portal.meter_geo(meter_id)
    except PortalNotFound:
        geo = None
    return MeterResponse(data=meter_from_detail(meter_id, detail, geo), source="live", freshness=freshness(index))


@app.get("/v1/meters/{meter_id}/readings", response_model=ReadingsResponse, response_model_exclude_none=True,
         tags=["meters"], summary="Energy readings for a meter", responses={404: ERRORS[404], 422: ERRORS[422]})
async def get_readings(
    request: Request,
    meter_id: str = METER_ID,
    from_: Optional[date] = Query(None, alias="from", description="First day (inclusive), YYYY-MM-DD."),
    to: Optional[date] = Query(None, description="Last day (inclusive), YYYY-MM-DD."),
    resolution: Resolution = Query(Resolution.raw, description="'raw' readings or 'daily' consumption totals."),
):
    """Readings in a date window. With no window, the portal's default applies
    (the last 7 days of available data). The current portal only holds June 2026."""
    if from_ and to and from_ > to:
        raise HTTPException(422, "'from' must not be after 'to'")
    meter_id = meter_id.upper()
    rows = await _cached_energy(request, meter_id, from_, to)
    readings, issues = normalize_readings(rows)
    response: Dict[str, Any] = {
        "meter_id": meter_id,
        "resolution": resolution.value,
        "interval_minutes": detect_interval_minutes(readings),
        "window_start": readings[0]["timestamp"] if readings else None,
        "window_end": readings[-1]["timestamp"] if readings else None,
        "total_kwh": round(sum(r["kwh_delta"] or 0 for r in readings), 3) if readings else None,
        "data_quality": issues,
    }
    if resolution is Resolution.daily:
        response["daily"] = daily_summary(readings)
    else:
        response["readings"] = readings
    return response


async def _cached_energy(request: Request, meter_id: str, from_: Optional[date], to: Optional[date]):
    """Fetch raw readings, cached briefly.

    The cache holds the in-flight task rather than the result, so identical
    requests arriving together share one portal call instead of each spending
    rate-limit budget. Failed fetches are not cached.
    """
    cache: Dict[Tuple, Tuple[float, "asyncio.Task"]] = request.app.state.readings_cache
    ttl = request.app.state.settings.readings_ttl_seconds
    key = (meter_id, from_, to)
    hit = cache.get(key)
    if not hit or time.monotonic() - hit[0] >= ttl:
        if len(cache) > 2000:  # crude bound; this is a cache, not a store
            cache.clear()
        task = asyncio.ensure_future(request.app.state.portal.meter_energy(
            meter_id, from_.isoformat() if from_ else None, to.isoformat() if to else None
        ))
        hit = cache[key] = (time.monotonic(), task)
    try:
        return await asyncio.shield(hit[1])
    except Exception as exc:
        if cache.get(key) is hit:
            del cache[key]
        if isinstance(exc, PortalNotFound):
            raise HTTPException(404, f"Meter {meter_id} not found")
        raise


@app.get("/v1/transformers", response_model=TransformerList, tags=["network"],
         summary="Distribution transformers with meter counts")
async def list_transformers(feeder: Optional[str] = Query(None, description="Feeder code, e.g. F-001."),
                            index: MeterIndex = Depends(fresh_index)):
    items = sorted(index.transformers.values(), key=lambda t: t["code"])
    if feeder:
        items = [t for t in items if (t["feeder_code"] or "").lower() == feeder.lower()]
    return TransformerList(data=items, freshness=freshness(index))


@app.get("/v1/transformers/{code}", response_model=Transformer, tags=["network"], summary="Get one transformer",
         responses={404: ERRORS[404]})
async def get_transformer(code: str, index: MeterIndex = Depends(fresh_index)):
    item = index.transformers.get(code.strip().upper())
    if not item:
        raise HTTPException(404, f"Transformer {code} not found")
    return item


@app.get("/v1/hierarchy", response_model=HierarchyResponse, response_model_exclude_none=True, tags=["network"],
         summary="Network hierarchy tree (zone → circle → division → subdivision → substation → feeder → DT)")
async def hierarchy(index: MeterIndex = Depends(fresh_index)):
    return HierarchyResponse(data=index.hierarchy_tree(), freshness=freshness(index))


@app.get("/v1/summary", response_model=SummaryResponse, tags=["network"], summary="Fleet-wide counts")
async def summary(index: MeterIndex = Depends(fresh_index)):
    return SummaryResponse(data=index.summary(), freshness=freshness(index))
