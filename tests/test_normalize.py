import json

from app.normalize import (
    daily_summary, detect_interval_minutes, meter_from_export, normalize_readings,
    parse_level_label, parse_nameplate, repair_hierarchy,
)

LEGACY_DETAIL = {"data": [
    {"parameterName": "Meter ID", "parameterValue": "J100000"},
    {"parameterName": "Serial No", "parameterValue": "SE33962"},
    {"parameterName": "Make", "parameterValue": "HPL"},
    {"parameterName": "Phase Type", "parameterValue": "single"},
    {"parameterName": "Installation Status", "parameterValue": "Decommissioned"},
    {"parameterName": "Installation Type", "parameterValue": "Whole Current"},
]}
V2_DETAIL = {"classData": json.dumps({"installed_meter": {
    "MeterId": "J100004", "SerialNo": "SE65293", "Make": "Genus", "PhaseType": "single",
    "InstallationStatus": "Faulty", "InstallationType": "CT Operated",
}})}


def test_both_nameplate_shapes_normalise_to_the_same_fields():
    legacy, v2 = parse_nameplate(LEGACY_DETAIL), parse_nameplate(V2_DETAIL)
    assert legacy.keys() == v2.keys()
    assert legacy["serial_no"] == "SE33962" and legacy["install_type"] == "Whole Current"
    assert v2["meter_id"] == "J100004" and v2["install_status"] == "Faulty"


def test_parse_level_label():
    assert parse_level_label("Jaipur Zone 1 (Z-01)") == {"name": "Jaipur Zone 1", "code": "Z-01"}
    assert parse_level_label("Feeder 3 ()") == {"name": "Feeder 3", "code": ""}
    assert parse_level_label(None) == {"name": "", "code": ""}


def _export(meter_id, circle, dt=("DT-001", "Malviya Nagar DT 1")):
    levels = {
        "zone": {"name": "Jaipur Zone 1", "code": "Z-01"},
        "circle": {"name": circle[1], "code": circle[0]},
        "division": {"name": "Division 1", "code": "D-01"},
        "subdivision": {"name": "Subdivision 1", "code": "SD-01"},
        "substation": {"name": "Substation 1", "code": "SS-01"},
        "feeder": {"name": "Feeder 1", "code": "F-001"},
        "dt": {"name": dt[1], "code": dt[0]},
    }
    return meter_from_export({"meterId": meter_id, "serialNo": "X", "make": "HPL", "phaseType": "single",
                              "installStatus": "Installed", "installType": "CT Operated", "build": "legacy",
                              "dtCode": dt[0], "hierarchy": levels, "geo": {"lat": 26.9, "lng": 75.8}})


def test_repair_fills_blank_code_and_name_and_flags_it():
    meters = [
        _export("A", ("C-01", "Circle 1")),
        _export("B", ("", "Circle 1")),
        _export("C", ("C-01", "")),
        _export("D", ("", "")),
    ]
    repair_hierarchy(meters)
    assert meters[1]["hierarchy"]["circle"]["code"] == "C-01"
    assert meters[2]["hierarchy"]["circle"]["name"] == "Circle 1"
    assert meters[0]["data_quality"] == []
    assert "inferred" in meters[1]["data_quality"][0]
    assert "could not be repaired" in meters[3]["data_quality"][0]


def test_transformer_register_name_wins_and_alias_is_flagged():
    meters = [_export("A", ("C-01", "Circle 1"), dt=("DT-007", "Old Malviya Nagar Xfmr"))]
    repair_hierarchy(meters, [{"code": "DT-007", "name": "Sanganer DT 7"}])
    assert meters[0]["hierarchy"]["dt"]["name"] == "Old Malviya Nagar Xfmr"  # as reported, not rewritten
    assert "differs from transformer register" in meters[0]["data_quality"][0]


def test_readings_are_typed_sorted_deduplicated_and_deltas_derived():
    rows = [
        {"timestamp": "02/06/2026 00:00", "kwh": "110.5", "kvah": "120", "voltR": "230"},
        {"timestamp": "01/06/2026 00:00", "kwh": "100", "kvah": "109", "voltR": "231"},
        {"timestamp": "02/06/2026 00:00", "kwh": "", "kvah": "120", "voltR": ""},  # duplicate, less complete
    ]
    readings, issues = normalize_readings(rows)
    assert [r["timestamp"].isoformat() for r in readings] == ["2026-06-01T00:00:00+05:30", "2026-06-02T00:00:00+05:30"]
    assert readings[1]["kwh"] == 110.5 and readings[1]["kwh_delta"] == 10.5
    assert readings[0]["kwh_delta"] is None
    assert any("duplicate" in i for i in issues)
    assert detect_interval_minutes(readings) == 1440


def test_register_going_backwards_is_not_counted_as_consumption():
    rows = [
        {"timestamp": "01/06/2026 00:00", "kwh": "100", "kvah": "110", "voltR": "230"},
        {"timestamp": "01/06/2026 00:30", "kwh": "5", "kvah": "6", "voltR": "230"},
    ]
    readings, issues = normalize_readings(rows)
    assert readings[1]["kwh_delta"] is None
    assert any("backwards" in i for i in issues)


def test_daily_summary_attributes_consumption_to_the_interval_start_day():
    rows = [{"timestamp": f"0{d}/06/2026 00:00", "kwh": str(100 + d * 10), "kvah": str(110 + d * 11), "voltR": "230"}
            for d in (1, 2, 3)]
    readings, _ = normalize_readings(rows)
    days = daily_summary(readings)
    assert [d["date"] for d in days] == ["2026-06-01", "2026-06-02"]
    assert days[0]["kwh"] == 10 and days[0]["power_factor"] == round(10 / 11, 3)
