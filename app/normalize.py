"""Turn the portal's inconsistent payloads into one clean shape.

Pure functions only (no I/O), so they are easy to test. Quirks handled here:

* Nameplates come in two shapes: `legacy` meters send a list of
  {parameterName, parameterValue}; `v2` meters send a JSON *string* with
  PascalCase keys under `installed_meter`.
* Hierarchy levels sometimes have a blank code or a blank name. We repair them
  from the rest of the fleet and record what we did in `data_quality`.
* Readings are strings, timestamps are DD/MM/YYYY HH:MM local time, the
  cadence differs per meter (30 min or daily), and rows can be duplicated with
  blank values.
"""

import json
import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

# Jaipur. India has no DST, so a fixed offset is exact.
PORTAL_TZ = timezone(timedelta(hours=5, minutes=30))

LEVELS = ["zone", "circle", "division", "subdivision", "substation", "feeder", "dt"]
# Labels the meter page uses for each level.
LEVEL_LABELS = dict(zip(LEVELS, ["Zone", "Circle", "Division", "Subdivision", "Sub Station", "Feeder", "DT"]))

# Both nameplate shapes mapped onto our field names.
_LEGACY_FIELDS = {
    "Meter ID": "meter_id",
    "Serial No": "serial_no",
    "Make": "make",
    "Phase Type": "phase_type",
    "Installation Status": "install_status",
    "Installation Type": "install_type",
}
_V2_FIELDS = {
    "MeterId": "meter_id",
    "SerialNo": "serial_no",
    "Make": "make",
    "PhaseType": "phase_type",
    "InstallationStatus": "install_status",
    "InstallationType": "install_type",
}


# -- meters --------------------------------------------------------------------


def parse_nameplate(detail: Dict[str, Any]) -> Dict[str, Optional[str]]:
    """Normalise either nameplate shape from the meter page into one dict."""
    if "classData" in detail:
        raw = json.loads(detail["classData"] or "{}").get("installed_meter") or {}
        mapping = _V2_FIELDS
    else:
        raw = {p.get("parameterName"): p.get("parameterValue") for p in detail.get("data") or []}
        mapping = _LEGACY_FIELDS
    return {ours: _clean(raw.get(theirs)) for theirs, ours in mapping.items()}


_LABEL_RE = re.compile(r"^(?P<name>.*?)\s*\((?P<code>[^()]*)\)\s*$")


def parse_level_label(label: Optional[str]) -> Dict[str, str]:
    """'Jaipur Zone 1 (Z-01)' -> {'name': 'Jaipur Zone 1', 'code': 'Z-01'}."""
    label = (label or "").strip()
    match = _LABEL_RE.match(label)
    if not match:
        return {"name": label, "code": ""}
    return {"name": match.group("name").strip(), "code": match.group("code").strip()}


def hierarchy_from_detail(hierarchy: Dict[str, Any]) -> Dict[str, Dict[str, str]]:
    return {level: parse_level_label(hierarchy.get(label)) for level, label in LEVEL_LABELS.items()}


def meter_from_export(raw: Dict[str, Any]) -> Dict[str, Any]:
    geo = raw.get("geo") or {}
    return {
        "meter_id": raw["meterId"],
        "serial_no": _clean(raw.get("serialNo")),
        "make": _clean(raw.get("make")),
        "phase_type": _clean(raw.get("phaseType")),
        "install_status": _clean(raw.get("installStatus")),
        "install_type": _clean(raw.get("installType")),
        "nameplate_format": _clean(raw.get("build")),
        "hierarchy": {
            level: {
                "code": _clean((raw.get("hierarchy") or {}).get(level, {}).get("code")) or "",
                "name": _clean((raw.get("hierarchy") or {}).get(level, {}).get("name")) or "",
            }
            for level in LEVELS
        },
        "location": _location(geo.get("lat"), geo.get("lng")),
        "data_quality": [],
    }


def meter_from_detail(meter_id: str, detail: Dict[str, Any], geo: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Build the same meter shape from the per-meter page + geo endpoint."""
    nameplate = parse_nameplate(detail.get("detail") or {})
    return {
        **nameplate,
        "meter_id": nameplate.get("meter_id") or meter_id,
        "nameplate_format": "v2" if "classData" in (detail.get("detail") or {}) else "legacy",
        "hierarchy": hierarchy_from_detail(detail.get("hierarchy") or {}),
        "location": _location((geo or {}).get("latitude"), (geo or {}).get("longitude")),
        "data_quality": [],
    }


def repair_hierarchy(meters: List[Dict[str, Any]], transformers: Iterable[Dict[str, Any]] = ()) -> None:
    """Fill blank hierarchy codes/names in place, using the rest of the fleet.

    For each level we learn code<->name from rows where both are present
    (majority vote). Transformer names come from the transformer register,
    which is the authoritative source; names that differ from it are kept but
    flagged.
    """
    code_to_name: Dict[str, Dict[str, str]] = {}
    name_to_code: Dict[str, Dict[str, str]] = {}
    for level in LEVELS:
        pairs = Counter(
            (m["hierarchy"][level]["code"], m["hierarchy"][level]["name"])
            for m in meters
            if m["hierarchy"][level]["code"] and m["hierarchy"][level]["name"]
        )
        by_code: Dict[str, Counter] = defaultdict(Counter)
        by_name: Dict[str, Counter] = defaultdict(Counter)
        for (code, name), n in pairs.items():
            by_code[code][name] += n
            by_name[name][code] += n
        code_to_name[level] = {c: names.most_common(1)[0][0] for c, names in by_code.items()}
        name_to_code[level] = {n: codes.most_common(1)[0][0] for n, codes in by_name.items()}

    registry = {t["code"]: t["name"] for t in transformers}
    code_to_name["dt"].update(registry)

    for m in meters:
        for level in LEVELS:
            node = m["hierarchy"][level]
            if not node["code"] and node["name"] in name_to_code[level]:
                node["code"] = name_to_code[level][node["name"]]
                m["data_quality"].append(f"{level}.code was blank; inferred '{node['code']}' from name")
            elif not node["name"] and node["code"] in code_to_name[level]:
                node["name"] = code_to_name[level][node["code"]]
                m["data_quality"].append(f"{level}.name was blank; inferred '{node['name']}' from code")
            elif not node["code"] or not node["name"]:
                m["data_quality"].append(f"{level} is incomplete and could not be repaired")
        dt = m["hierarchy"]["dt"]
        if dt["code"] in registry and dt["name"] != registry[dt["code"]]:
            m["data_quality"].append(
                f"dt.name '{dt['name']}' differs from transformer register '{registry[dt['code']]}'"
            )


# -- readings ------------------------------------------------------------------


def parse_timestamp(value: str) -> datetime:
    return datetime.strptime(value.strip(), "%d/%m/%Y %H:%M").replace(tzinfo=PORTAL_TZ)


def normalize_readings(rows: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Type, sort and de-duplicate raw readings and derive per-interval consumption.

    kWh/kVAh are cumulative register values, so consumption for an interval
    is the difference between consecutive readings. Each derived value is
    attached to the reading that closes the interval.
    """
    issues: List[str] = []
    by_ts: Dict[datetime, Dict[str, Any]] = {}
    for row in rows:
        try:
            ts = parse_timestamp(row["timestamp"])
        except (KeyError, ValueError):
            issues.append(f"dropped reading with unparseable timestamp {row.get('timestamp')!r}")
            continue
        reading = {
            "timestamp": ts,
            "kwh": _number(row.get("kwh")),
            "kvah": _number(row.get("kvah")),
            "voltage_r": _number(row.get("voltR")),
        }
        if ts in by_ts:
            issues.append(f"duplicate reading at {ts.isoformat()}; kept the most complete one")
            if _completeness(reading) <= _completeness(by_ts[ts]):
                continue
        by_ts[ts] = reading

    readings = [by_ts[ts] for ts in sorted(by_ts)]
    prev: Optional[Dict[str, Any]] = None
    for r in readings:
        r["kwh_delta"] = r["kvah_delta"] = None
        if prev is not None:
            r["kwh_delta"] = _delta(prev["kwh"], r["kwh"])
            r["kvah_delta"] = _delta(prev["kvah"], r["kvah"])
            if r["kwh_delta"] is not None and r["kwh_delta"] < 0:
                issues.append(f"kWh register went backwards at {r['timestamp'].isoformat()} (reset or bad read)")
                r["kwh_delta"] = None
            if r["kvah_delta"] is not None and r["kvah_delta"] < 0:
                r["kvah_delta"] = None
        prev = r
    missing = sum(1 for r in readings if r["kwh"] is None)
    if missing:
        issues.append(f"{missing} reading(s) have no kWh value")
    return readings, issues


def detect_interval_minutes(readings: List[Dict[str, Any]]) -> Optional[int]:
    if len(readings) < 2:
        return None
    gaps = [
        (b["timestamp"] - a["timestamp"]).total_seconds() / 60 for a, b in zip(readings, readings[1:])
    ]
    return int(statistics.median(gaps))


def daily_summary(readings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Consumption per calendar day (local time), whatever the native cadence.

    Each delta is attributed to the day its interval *started*, so a daily
    meter's 02/06 00:00 reading counts towards 1 June.
    """
    days: Dict[str, Dict[str, Any]] = {}
    prev_ts: Optional[datetime] = None
    for r in readings:
        if prev_ts is not None and r["kwh_delta"] is not None:
            day = days.setdefault(
                prev_ts.date().isoformat(),
                {"kwh": 0.0, "kvah": 0.0, "intervals": 0, "volts": []},
            )
            day["kwh"] += r["kwh_delta"]
            day["kvah"] += r["kvah_delta"] or 0.0
            day["intervals"] += 1
        if r["voltage_r"] is not None:
            days.setdefault(
                r["timestamp"].date().isoformat(), {"kwh": 0.0, "kvah": 0.0, "intervals": 0, "volts": []}
            )["volts"].append(r["voltage_r"])
        prev_ts = r["timestamp"]

    out = []
    for date, d in sorted(days.items()):
        if not d["intervals"]:
            continue
        volts = d["volts"]
        out.append(
            {
                "date": date,
                "kwh": round(d["kwh"], 3),
                "kvah": round(d["kvah"], 3),
                "power_factor": round(d["kwh"] / d["kvah"], 3) if d["kvah"] else None,
                "intervals": d["intervals"],
                "voltage_r_min": min(volts) if volts else None,
                "voltage_r_max": max(volts) if volts else None,
                "voltage_r_avg": round(sum(volts) / len(volts), 1) if volts else None,
            }
        )
    return out


# -- helpers -------------------------------------------------------------------


def _clean(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _number(value: Any) -> Optional[float]:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _location(lat: Any, lng: Any) -> Optional[Dict[str, float]]:
    lat, lng = _number(lat), _number(lng)
    if lat is None or lng is None:
        return None
    return {"latitude": lat, "longitude": lng}


def _delta(a: Optional[float], b: Optional[float]) -> Optional[float]:
    return round(b - a, 3) if a is not None and b is not None else None


def _completeness(reading: Dict[str, Any]) -> int:
    return sum(reading[k] is not None for k in ("kwh", "kvah", "voltage_r"))
