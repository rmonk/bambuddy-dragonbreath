"""Minimal DragonBreath API v2 client (see docs/api-v2.md in the firmware repo)."""

from __future__ import annotations

import logging
import uuid
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# PB_MIN_MODE_TARGET_C in the firmware: power_on below this is rejected.
MIN_TARGET_C = 30


class DragonBreathError(Exception):
    def __init__(self, code: str, message: str = "", status: int = 0, state: dict | None = None):
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.status = status
        self.state = state or {}


class DragonBreathClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        actor_id: str = "bambuddy",
        *,
        dry_run: bool = False,
        client: httpx.AsyncClient | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.actor_id = actor_id
        self.dry_run = dry_run
        self._http = client or httpx.AsyncClient(timeout=10)
        self._headers = {"X-DragonBreath-Auth": token or "bambuddy"}

    async def aclose(self) -> None:
        await self._http.aclose()

    async def get_state(self) -> dict[str, Any]:
        r = await self._http.get(f"{self.base_url}/api/v2/state")
        r.raise_for_status()
        return r.json()

    async def _post(self, path: str, body: dict) -> dict[str, Any]:
        r = await self._http.post(f"{self.base_url}{path}", json=body, headers=self._headers)
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code >= 400 or data.get("ok") is False:
            raise DragonBreathError(
                data.get("error") or f"http_{r.status_code}",
                data.get("message", ""),
                r.status_code,
                data.get("state"),
            )
        return data

    async def command(self, command: dict, expected_revision: int | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "api_version": 2,
            "request_id": uuid.uuid4().hex,
            "actor": {"kind": "web", "id": self.actor_id},
            "command": command,
        }
        if expected_revision is not None:
            body["expected_revision"] = expected_revision
        if self.dry_run:
            logger.info("[dry-run] would POST %s/api/v2/command %s", self.base_url, command)
            return {"ok": True, "lease_id": "dryrun" + "0" * 26, "state": {}}
        return await self._post("/api/v2/command", body)

    async def power_on(self, target_c: int) -> str:
        """Heat to ``target_c`` under a fresh lease; returns the lease id.
        Retries once on a revision race."""
        for attempt in (1, 2):
            state = await self.get_state()
            try:
                resp = await self.command(
                    {"name": "power_on", "target_c": int(target_c)},
                    expected_revision=int(state.get("state_revision", 0)),
                )
            except DragonBreathError as e:
                if e.code == "revision_conflict" and attempt == 1:
                    continue
                raise
            return resp["lease_id"]
        raise AssertionError("unreachable")

    async def heartbeat(self, lease_id: str) -> int:
        """Refresh our lease; returns ms until it expires."""
        if self.dry_run:
            return 300_000
        resp = await self._post("/api/v2/heartbeat", {"api_version": 2, "lease_id": lease_id})
        return int(resp.get("expires_in_ms", 0))

    async def off(self) -> None:
        await self.command({"name": "off"})

    async def clear_fault(self) -> None:
        state = await self.get_state()
        await self.command({"name": "clear_fault"}, expected_revision=int(state.get("state_revision", 0)))
