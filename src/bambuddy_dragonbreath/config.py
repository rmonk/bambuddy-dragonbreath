"""YAML configuration. ``${VAR}`` references are expanded from the environment so
secrets can live in the compose ``environment:`` / an env file instead of the
YAML itself."""

from __future__ import annotations

import os
import re
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class BambuddyConfig(BaseModel):
    url: str = "http://bambuddy:8000"
    # API key with "Read status" permission only. Empty when Bambuddy auth is off.
    api_key: str = ""

    @field_validator("url")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        return v.rstrip("/")


class PrinterConfig(BaseModel):
    printer_id: int
    name: str = ""
    dragonbreath_url: str
    # The control token set on the device (POST /api/v2/token). If none is set,
    # any non-empty value is accepted by the firmware.
    dragonbreath_token: str = "bambuddy"

    @field_validator("dragonbreath_url")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @property
    def label(self) -> str:
        return self.name or f"printer {self.printer_id}"


class Config(BaseModel):
    bambuddy: BambuddyConfig = Field(default_factory=BambuddyConfig)
    printers: list[PrinterConfig]
    # Filament → °C entries that win over Bambuddy's preheat_filament_targets.
    overrides: dict[str, int] = Field(default_factory=dict)
    # Seconds to keep heating after a print ends (0 = off immediately).
    off_delay_s: float = 0
    # Send clear_fault before engaging when the device has a latched fault.
    # Off by default: clearing a safety latch should be a human decision.
    auto_clear_fault: bool = False
    # How often to re-check printer and heater state independently of events.
    reconcile_interval_s: float = 30
    # Turn the heater off if Bambuddy can't be reached for this long.
    bambuddy_lost_off_s: float = 600
    # Hard cap on one continuous heating session, whatever Bambuddy says.
    max_engaged_hours: float = 24
    # actor.id sent to DragonBreath (≤31 chars); also how we recognise our own lease.
    actor_id: str = Field(default="bambuddy", max_length=31)
    dry_run: bool = False
    log_level: str = "INFO"


def _expand_env(text: str) -> str:
    def sub(m: re.Match) -> str:
        name, default = m.group(1), m.group(2)
        value = os.environ.get(name)
        if value is None:
            if default is None:
                raise ValueError(f"config references ${{{name}}} but it is not set")
            return default
        return value

    return _ENV_REF.sub(sub, text)


def _config_from_env() -> dict:
    """Single-printer config from environment variables, for running without a
    config file (e.g. everything in a compose ``environment:`` block)."""
    env = os.environ
    raw: dict = {
        "bambuddy": {"url": env["BAMBUDDY_URL"], "api_key": env.get("BAMBUDDY_API_KEY", "")},
        "printers": [
            {
                "printer_id": env["PRINTER_ID"],
                "name": env.get("PRINTER_NAME", ""),
                "dragonbreath_url": env["DRAGONBREATH_URL"],
                "dragonbreath_token": env.get("DRAGONBREATH_TOKEN") or "bambuddy",
            }
        ],
    }
    # OVERRIDES="ABS=55,PETG=0"
    if env.get("OVERRIDES"):
        pairs = (item.split("=", 1) for item in env["OVERRIDES"].split(",") if "=" in item)
        raw["overrides"] = {k.strip(): v.strip() for k, v in pairs}
    for name in (
        "off_delay_s",
        "auto_clear_fault",
        "reconcile_interval_s",
        "bambuddy_lost_off_s",
        "max_engaged_hours",
        "dry_run",
        "log_level",
    ):
        if env.get(name.upper()):
            raw[name] = env[name.upper()]
    return raw


def load_config(path: str | Path) -> Config:
    """Load ``path``; if it doesn't exist, fall back to environment variables
    (BAMBUDDY_URL, PRINTER_ID and DRAGONBREATH_URL are required)."""
    p = Path(path)
    if not p.exists():
        missing = [v for v in ("BAMBUDDY_URL", "PRINTER_ID", "DRAGONBREATH_URL") if not os.environ.get(v)]
        if missing:
            raise SystemExit(f"no config file at {p} and {', '.join(missing)} not set")
        return Config.model_validate(_config_from_env())
    raw = yaml.safe_load(_expand_env(p.read_text())) or {}
    return Config.model_validate(raw)
