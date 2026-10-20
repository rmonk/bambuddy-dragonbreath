"""Work out the chamber target for a print from the filaments it uses.

This is a port of Bambuddy's preheat derivation (``services/print_scheduler.py``:
``_derive_chamber_target``, ``_target_for_tray_type``, ``_resolve_filament_key``,
``_normalize_filament_type`` and the tray-id helpers) so the sidecar picks the
same temperature Bambuddy's own preheat would. One deliberate difference: when
the print's ``ams_mapping`` is unknown, Bambuddy scans every loaded tray, and
here we return None instead so the caller can fall back to the slicer metadata
(or not heat). One ASA spool parked in the AMS should not heat a PLA print.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Bambuddy's DEFAULT_PREHEAT_FILAMENT_TARGETS, used when its
# preheat_filament_targets setting is empty or unparseable.
DEFAULT_TARGETS: dict[str, int] = {
    "PLA": 0,
    "PETG": 0,
    "PETG-CF": 40,
    "ABS": 45,
    "ASA": 45,
    "PA": 50,
    "PA-CF": 55,
    "PC": 50,
    "PC-FR": 50,
    "TPU": 0,
    "PVA": 0,
    "DEFAULT": 0,
}

FILAMENT_KEY_ALIASES: dict[str, str] = {
    "NYLON": "PA",
    "PA6": "PA",
    "PA11": "PA",
    "PA12": "PA",
    "PAHT": "PA",
    "PPA": "PA",
}

# Global tray ids at or above this are the external spool(s): 254 deputy, 255 main.
EXTERNAL_TRAY_ID_MIN = 254


@dataclass
class Resolution:
    """What the sidecar decided and why, for logging."""

    target_c: int
    source: str  # "ams_mapping" or "slicer"
    filaments: list[tuple[str, str, int]] = field(default_factory=list)  # (where, type, target)

    def describe(self) -> str:
        parts = ", ".join(f"{where}={ftype}→{t}°C" for where, ftype, t in self.filaments) or "none"
        return f"target {self.target_c}°C from {self.source} [{parts}]"


def parse_targets(raw: str | None, overrides: Mapping[str, int] | None = None) -> dict[str, int]:
    """Mirror of ``_get_preheat_filament_targets``: the user's JSON map with keys
    upper-cased, or the bundled defaults when it is empty or malformed. A user
    map replaces the defaults rather than merging with them, as in Bambuddy.
    Sidecar ``overrides`` are applied last."""
    targets: dict[str, int] = dict(DEFAULT_TARGETS)
    if raw:
        try:
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise ValueError("not an object")
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("preheat_filament_targets unparseable, using defaults: %s", exc)
        else:
            targets = {}
            for key, value in parsed.items():
                try:
                    targets[str(key).upper()] = int(value)
                except (TypeError, ValueError):
                    continue
            targets.setdefault("DEFAULT", DEFAULT_TARGETS["DEFAULT"])
    for key, value in (overrides or {}).items():
        targets[str(key).upper()] = int(value)
    return targets


def normalize_filament_type(tray_type: str | None) -> str:
    """"PLA Basic" → "PLA"; "PA-CF" stays "PA-CF"."""
    words = (tray_type or "").split()
    return words[0].upper() if words else ""


def resolve_filament_key(tray_type: str | None, table: Mapping[str, object]) -> str | None:
    """Exact match, then the type without its suffix, then the alias map."""
    raw = normalize_filament_type(tray_type)
    if not raw:
        return None
    for candidate in (raw, raw.split("-")[0]):
        key = FILAMENT_KEY_ALIASES.get(candidate, candidate)
        if key in table:
            return key
    return None


def target_for_type(tray_type: str | None, targets: Mapping[str, int]) -> int:
    """Chamber target for one filament type; 0 for an empty type, DEFAULT for an
    unlisted one."""
    if not normalize_filament_type(tray_type):
        return 0
    key = resolve_filament_key(tray_type, targets)
    if key is not None:
        return targets[key]
    return targets.get("DEFAULT", 0)


def _int_or(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def global_tray_id(ams_id: int, tray_id: int) -> int:
    """``ams_id * 4 + tray_id`` for a four-slot AMS; the bare unit id for an
    AMS-HT (ids from 128, one tray each)."""
    return ams_id if ams_id >= 128 else ams_id * 4 + tray_id


def used_tray_ids(mapping: object) -> set[int] | None:
    """The global tray ids a print's ``ams_mapping`` names, or None if the
    mapping is missing, unparseable or entirely unresolved (all -1)."""
    if not mapping:
        return None
    if isinstance(mapping, str):
        try:
            mapping = json.loads(mapping)
        except (json.JSONDecodeError, TypeError):
            return None
    if not isinstance(mapping, list):
        return None
    used = {t for t in mapping if isinstance(t, int) and not isinstance(t, bool) and t >= 0}
    return used or None


def _ams_units(status: Mapping) -> list:
    ams = status.get("ams")
    # Older firmware nests AMS as {"ams": {"ams": [...]}}.
    if isinstance(ams, dict):
        ams = ams.get("ams") or []
    return ams if isinstance(ams, list) else []


def _vt_trays(status: Mapping) -> list:
    vt = status.get("vt_tray")
    if isinstance(vt, dict):
        vt = [vt]
    return vt if isinstance(vt, list) else []


def resolve_from_trays(status: Mapping, ams_mapping: object, targets: Mapping[str, int]) -> Resolution | None:
    """Max target over the trays the print's ``ams_mapping`` names, read against
    the printer's live AMS / external spool state. None when the mapping is
    unknown or names no tray that reports a filament type."""
    used = used_tray_ids(ams_mapping)
    if used is None:
        return None
    found: list[tuple[str, str, int]] = []
    for unit in _ams_units(status):
        if not isinstance(unit, dict):
            continue
        ams_id = _int_or(unit.get("id"), 0)
        for tray in unit.get("tray") or []:
            if not isinstance(tray, dict):
                continue
            gid = global_tray_id(ams_id, _int_or(tray.get("id"), 0))
            if gid not in used or not normalize_filament_type(tray.get("tray_type")):
                continue
            found.append((f"tray{gid}", tray["tray_type"], target_for_type(tray["tray_type"], targets)))
    if any(t >= EXTERNAL_TRAY_ID_MIN for t in used):
        for vt in _vt_trays(status):
            if not isinstance(vt, dict):
                continue
            vid = _int_or(vt.get("id"), EXTERNAL_TRAY_ID_MIN)
            if vid not in used or not normalize_filament_type(vt.get("tray_type")):
                continue
            found.append((f"ext{vid}", vt["tray_type"], target_for_type(vt["tray_type"], targets)))
    if not found:
        return None
    return Resolution(max(t for _, _, t in found), "ams_mapping", found)


def split_slicer_types(filament_type: str | None) -> list[str]:
    """An archive's ``filament_type`` is the slicer's types joined with ", "."""
    return [t.strip() for t in (filament_type or "").split(",") if t.strip()]


def resolve_from_types(types: Iterable[str], targets: Mapping[str, int], source: str = "slicer") -> Resolution | None:
    found = [(source, t, target_for_type(t, targets)) for t in types if normalize_filament_type(t)]
    if not found:
        return None
    return Resolution(max(t for _, _, t in found), source, found)


def clamp_for_device(target_c: int, minimum_c: int = 30, maximum_c: float = 70) -> int:
    """0 (or less) stays 0 = don't heat. DragonBreath rejects power_on below 30 °C,
    so small positive targets are raised to its minimum; anything above the
    device max is capped (the firmware would clamp it anyway)."""
    if target_c <= 0:
        return 0
    return int(min(max(target_c, minimum_c), maximum_c))
