from bambuddy_dragonbreath.filament import (
    DEFAULT_TARGETS,
    clamp_for_device,
    parse_targets,
    resolve_from_trays,
    resolve_from_types,
    split_slicer_types,
    target_for_type,
    used_tray_ids,
)


def status(*units, vt=None):
    return {
        "ams": [{"id": uid, "tray": [{"id": i, "tray_type": t} for i, t in enumerate(trays)]} for uid, trays in units],
        "vt_tray": vt or [],
    }


T = parse_targets("")


def test_defaults_when_setting_empty_or_bad():
    assert parse_targets("") == DEFAULT_TARGETS
    assert parse_targets("not json") == DEFAULT_TARGETS
    assert parse_targets("[1,2]") == DEFAULT_TARGETS


def test_user_map_replaces_defaults_and_overrides_win():
    t = parse_targets('{"asa": 50, "pla": "x"}', overrides={"petg": 35})
    assert t == {"ASA": 50, "DEFAULT": 0, "PETG": 35}


def test_type_lookup():
    assert target_for_type("ABS", T) == 45
    assert target_for_type("PLA Basic", T) == 0
    assert target_for_type("ASA-GF", T) == 45  # suffix dropped
    assert target_for_type("PETG-CF", T) == 40  # exact row wins
    assert target_for_type("PA6-CF", T) == 50  # alias
    assert target_for_type("PPA-CF", T) == 50
    assert target_for_type("HIPS", {**T, "DEFAULT": 33}) == 33
    assert target_for_type("", T) == 0
    assert target_for_type(None, T) == 0


def test_used_tray_ids():
    assert used_tray_ids(None) is None
    assert used_tray_ids([]) is None
    assert used_tray_ids([-1, -1]) is None
    assert used_tray_ids([2, -1, 5]) == {2, 5}
    assert used_tray_ids("[1, 254]") == {1, 254}
    assert used_tray_ids("garbage") is None


def test_only_mapped_trays_count():
    # ASA parked in slot 0, the print uses PLA in slot 1.
    s = status((0, ["ASA", "PLA", "", "PETG"]))
    res = resolve_from_trays(s, [1], T)
    assert res.target_c == 0
    assert res.filaments == [("tray1", "PLA", 0)]


def test_max_across_used_trays():
    s = status((0, ["PLA", "ABS", "PETG", "PLA"]))
    res = resolve_from_trays(s, [0, 1, -1], T)
    assert res.target_c == 45
    assert res.source == "ams_mapping"


def test_second_ams_and_ams_ht():
    s = status((0, ["PLA"] * 4), (1, ["PLA", "PLA", "PC", "PLA"]), (128, ["PA-CF"]))
    assert resolve_from_trays(s, [6], T).target_c == 50  # ams 1 slot 2
    assert resolve_from_trays(s, [128], T).target_c == 55


def test_external_spool():
    s = status((0, ["PLA"] * 4), vt=[{"id": 254, "tray_type": "ASA"}])
    assert resolve_from_trays(s, [254], T).target_c == 45
    # vt not consulted unless the mapping names it
    assert resolve_from_trays(s, [0], T).target_c == 0


def test_unknown_mapping_or_empty_trays_returns_none():
    s = status((0, ["ABS", "", "", ""]))
    assert resolve_from_trays(s, None, T) is None
    assert resolve_from_trays(s, [2], T) is None


def test_nested_ams_shape():
    s = {"ams": {"ams": [{"id": "0", "tray": [{"id": "0", "tray_type": "ABS"}]}]}}
    assert resolve_from_trays(s, [0], T).target_c == 45


def test_slicer_types():
    assert split_slicer_types("PLA, PETG") == ["PLA", "PETG"]
    assert split_slicer_types(None) == []
    res = resolve_from_types(["PLA", "ASA"], T)
    assert res.target_c == 45 and res.source == "slicer"
    assert resolve_from_types([], T) is None


def test_clamp():
    assert clamp_for_device(0) == 0
    assert clamp_for_device(-5) == 0
    assert clamp_for_device(20) == 30
    assert clamp_for_device(45) == 45
    assert clamp_for_device(90, maximum_c=70) == 70
