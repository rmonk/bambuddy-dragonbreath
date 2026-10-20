"""Bambuddy REST + WebSocket client."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx
import websockets

logger = logging.getLogger(__name__)

# Bambuddy's ACTIVE_PRINT_STATES (services/printer_manager.py).
ACTIVE_PRINT_STATES = frozenset({"PREPARE", "SLICING", "RUNNING", "PAUSE"})

# Close code Bambuddy uses for a missing/expired WS token.
_WS_CLOSE_UNAUTHORIZED = 4401

# Synthetic event yielded after every (re)connect so the consumer can reconcile
# whatever it missed while disconnected.
CONNECTED = {"type": "_connected"}


class BambuddyClient:
    def __init__(self, base_url: str, api_key: str = "", *, client: httpx.AsyncClient | None = None):
        self.base_url = base_url.rstrip("/")
        headers = {"X-API-Key": api_key} if api_key else {}
        self._http = client or httpx.AsyncClient(base_url=self.base_url, headers=headers, timeout=15)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _get(self, path: str) -> Any:
        r = await self._http.get(path)
        r.raise_for_status()
        return r.json()

    async def get_status(self, printer_id: int) -> dict[str, Any]:
        return await self._get(f"/api/v1/printers/{printer_id}/status")

    async def get_preheat_targets_raw(self) -> str:
        settings = await self._get("/api/v1/settings")
        return settings.get("preheat_filament_targets") or ""

    async def get_archive(self, archive_id: int) -> dict[str, Any]:
        return await self._get(f"/api/v1/archives/{archive_id}")

    async def get_pending_queue(self, printer_id: int) -> list[dict[str, Any]]:
        items = await self._get(f"/api/v1/queue/?printer_id={printer_id}&status=pending")
        return items if isinstance(items, list) else []

    async def ws_token(self) -> str:
        r = await self._http.post("/api/v1/auth/ws-token")
        r.raise_for_status()
        return r.json()["token"]

    def _ws_url(self, token: str) -> str:
        scheme = "wss" if self.base_url.startswith("https") else "ws"
        host = self.base_url.split("://", 1)[1]
        return f"{scheme}://{host}/api/v1/ws?token={token}"

    async def events(self, ping_interval: float = 30) -> AsyncIterator[dict[str, Any]]:
        """Yield WS messages forever, reconnecting with backoff. A fresh token is
        minted on every connect (they last 60 min). ``CONNECTED`` is yielded
        after each successful connect."""
        backoff = 1.0
        while True:
            try:
                token = await self.ws_token()
                async with websockets.connect(self._ws_url(token), ping_interval=None, max_size=None) as ws:
                    logger.info("Connected to Bambuddy WebSocket")
                    backoff = 1.0
                    yield CONNECTED
                    pinger = asyncio.create_task(self._ping(ws, ping_interval))
                    try:
                        async for raw in ws:
                            try:
                                msg = json.loads(raw)
                            except ValueError:
                                continue
                            if isinstance(msg, dict):
                                yield msg
                    finally:
                        pinger.cancel()
            except asyncio.CancelledError:
                raise
            except websockets.ConnectionClosed as e:
                if e.rcvd and e.rcvd.code == _WS_CLOSE_UNAUTHORIZED:
                    logger.warning("Bambuddy refused the WebSocket token; check the API key permissions")
                else:
                    logger.warning("Bambuddy WebSocket closed: %s", e)
            except Exception as e:  # network errors, HTTP errors from ws-token, etc.
                logger.warning("Bambuddy WebSocket error: %s", e)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    @staticmethod
    async def _ping(ws, interval: float) -> None:
        # Bambuddy answers an application-level {"type":"ping"} with pong; this
        # keeps idle proxies from dropping the socket.
        while True:
            await asyncio.sleep(interval)
            await ws.send(json.dumps({"type": "ping"}))
