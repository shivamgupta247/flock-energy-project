"""In-memory snapshot of the fleet, built from the portal's bulk export.

Why a snapshot: the portal can only search by meter id / serial and allows
~120 requests a minute, so answering "all faulty three-phase meters on feeder
F-007" or "meters within 2 km of here" live would be slow or impossible. The
whole fleet arrives in one signed export call, so we load it, repair it, and
answer queries locally. When it is older than INDEX_TTL_SECONDS it is
refreshed in the background while the old one keeps being served; if a
refresh fails we keep serving the previous snapshot and say so.
"""

import asyncio
import logging
import math
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .normalize import LEVELS, meter_from_export, repair_hierarchy
from .portal import PortalClient, PortalError, PortalUnavailable

log = logging.getLogger(__name__)


class MeterIndex:
    # After a failed refresh, wait this long before trying again, so a portal
    # outage doesn't turn every incoming request into a refresh attempt.
    RETRY_COOLDOWN_SECONDS = 30

    def __init__(self, portal: PortalClient, ttl_seconds: int):
        self._portal = portal
        self._ttl = ttl_seconds
        self._lock = asyncio.Lock()
        self._loaded_monotonic: Optional[float] = None
        self._next_attempt = 0.0
        self._refresh_task: Optional[asyncio.Task] = None
        self.loaded_at: Optional[datetime] = None
        self.last_error: Optional[str] = None
        self.meters: List[Dict[str, Any]] = []
        self.by_id: Dict[str, Dict[str, Any]] = {}
        self.transformers: Dict[str, Dict[str, Any]] = {}
        self.tree: List[Dict[str, Any]] = []

    @property
    def is_stale(self) -> bool:
        return self._loaded_monotonic is None or time.monotonic() - self._loaded_monotonic > self._ttl

    async def ensure_fresh(self) -> None:
        """Make sure there is a snapshot to serve.

        With no snapshot yet, the caller waits for the first load (and gets the
        error if it fails). Once a snapshot exists, a stale one is served
        immediately while a single background task refreshes it.
        """
        if not self.meters:
            async with self._lock:
                if self.meters:  # loaded by another request while we waited
                    return
                if time.monotonic() < self._next_attempt:
                    raise PortalUnavailable(f"Snapshot unavailable; last error: {self.last_error}")
                await self._refresh(raise_errors=True)
            return
        refreshing = self._refresh_task is not None and not self._refresh_task.done()
        if self.is_stale and not refreshing and time.monotonic() >= self._next_attempt:
            self._refresh_task = asyncio.create_task(self._refresh_locked())

    async def _refresh_locked(self) -> None:
        async with self._lock:
            await self._refresh()

    async def _refresh(self, raise_errors: bool = False) -> None:
        try:
            await self._load()
            self.last_error = None
        except PortalError as exc:
            self.last_error = str(exc) or exc.__class__.__name__
            self._next_attempt = time.monotonic() + self.RETRY_COOLDOWN_SECONDS
            log.warning("Snapshot refresh failed: %s", self.last_error)
            if raise_errors:
                raise

    async def _load(self) -> None:
        raw_meters = await self._portal.export_meters()
        raw_dts = await self._portal.list_transformers()
        meters = [meter_from_export(m) for m in raw_meters]
        repair_hierarchy(meters, raw_dts)
        meters.sort(key=lambda m: m["meter_id"])

        counts: Dict[str, Counter] = {}
        for m in meters:
            counts.setdefault(m["hierarchy"]["dt"]["code"], Counter())[m["install_status"]] += 1
        transformers = {
            t["code"]: {
                "code": t["code"],
                "name": t["name"],
                "feeder_code": t.get("feederCode"),
                "capacity_kva": t.get("capacityKva"),
                "meter_count": sum(counts.get(t["code"], Counter()).values()),
                "meters_by_status": dict(counts.get(t["code"], Counter())),
            }
            for t in raw_dts
        }

        self.meters, self.transformers = meters, transformers
        self.by_id = {m["meter_id"]: m for m in meters}
        self.tree = build_tree(meters)  # only changes with the snapshot, so build it once
        self._loaded_monotonic = time.monotonic()
        self.loaded_at = datetime.now(timezone.utc)
        log.info("Snapshot loaded: %d meters, %d transformers", len(meters), len(transformers))

    # -- queries ---------------------------------------------------------------

    def search(
        self,
        q: Optional[str] = None,
        filters: Optional[Dict[str, Optional[str]]] = None,
        hierarchy: Optional[Dict[str, Optional[str]]] = None,
        near: Optional[Dict[str, float]] = None,
        has_data_quality: Optional[bool] = None,
    ) -> List[Dict[str, Any]]:
        """Filter the snapshot. Text match is case-insensitive on id and serial."""
        results = self.meters
        if has_data_quality is not None:
            results = [m for m in results if bool(m["data_quality"]) == has_data_quality]
        if q:
            needle = q.strip().lower()
            results = [
                m for m in results
                if needle in m["meter_id"].lower() or needle in (m["serial_no"] or "").lower()
            ]
        for field, value in (filters or {}).items():
            if value:
                results = [m for m in results if (m[field] or "").lower() == value.lower()]
        for level, code in (hierarchy or {}).items():
            if code:
                results = [m for m in results if m["hierarchy"][level]["code"].lower() == code.lower()]
        if near:
            with_distance = []
            for m in results:
                if m["location"]:
                    d = haversine_km(near["latitude"], near["longitude"], m["location"]["latitude"], m["location"]["longitude"])
                    if d <= near["radius_km"]:
                        with_distance.append({**m, "distance_km": round(d, 3)})
            results = sorted(with_distance, key=lambda m: m["distance_km"])
        return results

    def summary(self) -> Dict[str, Any]:
        def count(field: str) -> Dict[str, int]:
            return dict(Counter(m[field] or "unknown" for m in self.meters).most_common())

        return {
            "meters": len(self.meters),
            "transformers": len(self.transformers),
            "by_install_status": count("install_status"),
            "by_make": count("make"),
            "by_phase_type": count("phase_type"),
            "by_install_type": count("install_type"),
            "meters_with_data_quality_issues": sum(1 for m in self.meters if m["data_quality"]),
        }


def build_tree(meters: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Zone -> ... -> DT tree, built from each meter's path.

    Codes below circle are reused under several parents (D-01 sits under
    C-01, C-03 and C-05), so a node is identified by its full path, not by
    its code alone. Each node reports how many meters sit beneath it.
    """
    root: Dict[str, Any] = {"children": {}}
    for m in meters:
        node = root
        for level in LEVELS:
            h = m["hierarchy"][level]
            key = h["code"] or f"(unknown {level})"
            child = node["children"].get(key)
            if child is None:
                child = node["children"][key] = {
                    "level": level, "code": h["code"] or None, "name": h["name"] or None,
                    "meter_count": 0, "children": {},
                }
            child["meter_count"] += 1
            node = child

    def finish(node: Dict[str, Any]) -> List[Dict[str, Any]]:
        out = []
        for key in sorted(node["children"]):
            child = node["children"][key]
            children = finish(child)
            item = {k: v for k, v in child.items() if k != "children"}
            if children:
                item["children"] = children
            out.append(item)
        return out

    return finish(root)


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))
