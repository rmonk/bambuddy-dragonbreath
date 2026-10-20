"""Per-printer state machine: engage the heater when a print that needs chamber
heat starts, keep its lease alive, and turn it off when the print ends."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

from .bambuddy import ACTIVE_PRINT_STATES, BambuddyClient
from .config import Config, PrinterConfig
from .dragonbreath import MIN_TARGET_C, DragonBreathClient, DragonBreathError
from .filament import (
    Resolution,
    clamp_for_device,
    parse_targets,
    resolve_from_trays,
    resolve_from_types,
    split_slicer_types,
)

logger = logging.getLogger(__name__)

# Retrying the filament lookup after a print starts: the archive (slicer
# metadata) is often created a few seconds after the print_start event.
RESOLVE_RETRY_S = 10
RESOLVE_MAX_ATTEMPTS = 18
# Never heartbeat more often than this, whatever comms_ms says.
HEARTBEAT_MIN_S = 5.0


class _PrinterLog(logging.LoggerAdapter):
    def process(self, msg, kwargs):
        return f"[{self.extra['printer']}] {msg}", kwargs


class PrinterController:
    def __init__(
        self,
        cfg: Config,
        printer: PrinterConfig,
        bambuddy: BambuddyClient,
        heater: DragonBreathClient,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.cfg = cfg
        self.printer = printer
        self.bambuddy = bambuddy
        self.heater = heater
        self.clock = clock
        self.log = _PrinterLog(logger, {"printer": printer.label})

        self.job_active = False
        self.resolved = False
        self.ams_mapping: Any = None
        self.lease_id: str | None = None
        self.target_c = 0
        self.engaged_at: float | None = None
        self.last_bambuddy_ok = clock()
        # Bambuddy's queue preheat (bed heating before the upload) we're heating for.
        self.preheat_item: int | None = None
        self.preheat_started: float | None = None
        self._preheat_misses = 0
        self._preheat_done: set[int] = set()

        self._lock = asyncio.Lock()
        self._heartbeat_task: asyncio.Task | None = None
        self._off_task: asyncio.Task | None = None
        self._resolver_task: asyncio.Task | None = None

    async def check_heater(self) -> None:
        """Log what we're talking to at startup, so a wrong URL or a device
        still running its own AUTO logic shows up before the first print."""
        try:
            state = await self.heater.get_state()
        except Exception as e:
            self.log.error("DragonBreath at %s unreachable: %s", self.heater.base_url, e)
            return
        env = state.get("environment") or {}
        safety = state.get("safety") or {}
        self.log.info(
            "DragonBreath %s firmware %s: mode=%s, control source=%s, chamber=%s°C",
            state.get("device_id"),
            state.get("firmware"),
            state.get("mode"),
            env.get("control_source"),
            ((state.get("sensors") or {}).get("chamber") or {}).get("temperature_c"),
        )
        if env.get("control_source") not in (None, "none"):
            self.log.warning(
                "DragonBreath control source is %r; set it to None so its own AUTO logic can't also drive the heater",
                env.get("control_source"),
            )
        if state.get("mode") == "auto":
            self.log.warning("DragonBreath AUTO mode is armed; it will act on its own filament detection")
        if safety.get("fault_latched") or safety.get("inhibited"):
            self.log.error("DragonBreath has a latched fault/inhibit (%s)", safety.get("reason"))

    # ── events ────────────────────────────────────────────────────────────

    async def handle_event(self, msg: dict) -> None:
        if msg.get("printer_id") != self.printer.printer_id:
            return
        kind = msg.get("type")
        data = msg.get("data") or {}
        if kind == "print_start":
            await self.on_print_start(data)
        elif kind == "print_complete":
            await self.on_print_complete(data)

    async def on_print_start(self, data: dict) -> None:
        async with self._lock:
            self._cancel(self._off_task)
            self._off_task = None
            self.job_active = True
            self.resolved = False
            self.ams_mapping = data.get("ams_mapping")
            if self.lease_id is None:
                self.engaged_at = None
            self._end_preheat()
            self.log.info(
                "Print started: %s (ams_mapping=%s)",
                data.get("subtask_name") or data.get("filename"),
                self.ams_mapping,
            )
            await self._try_decide()
            if not self.resolved:
                self._start_resolver()

    async def on_print_complete(self, data: dict) -> None:
        async with self._lock:
            status = data.get("status", "?")
            self.log.info("Print ended (%s)", status)
            self.job_active = False
            self.resolved = False
            self._cancel(self._resolver_task)
            if self.lease_id is None:
                return
            if self.cfg.off_delay_s > 0:
                self.log.info("Turning heater off in %ss", self.cfg.off_delay_s)
                self._off_task = asyncio.create_task(self._delayed_off(f"print {status}"))
            else:
                await self._off(f"print {status}")

    # ── reconcile ─────────────────────────────────────────────────────────

    async def reconcile(self) -> None:
        """Bring our view in line with Bambuddy and the device. Covers sidecar
        or Bambuddy restarts, missed WS events, and safety time limits."""
        async with self._lock:
            now = self.clock()
            try:
                status = await self.bambuddy.get_status(self.printer.printer_id)
            except Exception as e:
                self.log.warning("Bambuddy status unavailable: %s", e)
                status = None
            if status is None or not status.get("connected"):
                if self.lease_id and now - self.last_bambuddy_ok > self.cfg.bambuddy_lost_off_s:
                    await self._off(f"no printer status from Bambuddy for {self.cfg.bambuddy_lost_off_s:.0f}s")
                    self.resolved = True  # don't re-engage this job on our own
                return
            self.last_bambuddy_ok = now

            printing = status.get("state") in ACTIVE_PRINT_STATES
            if printing:
                if not self.job_active:
                    self.log.info("Found a print in progress (%s)", status.get("subtask_name") or status.get("gcode_file"))
                    self.job_active = True
                    self.resolved = False
                    self.ams_mapping = None
                    self.engaged_at = None
                    self._start_resolver()
                if self.lease_id:
                    await self._check_still_ours()
                if self.lease_id and self.engaged_at is not None and now - self.engaged_at > self.cfg.max_engaged_hours * 3600:
                    await self._off(f"heated for over {self.cfg.max_engaged_hours}h")
            else:
                if await self._reconcile_preheat(status, now):
                    return
                if self.job_active:
                    self.log.info("Printer is %s; treating the print as ended", status.get("state"))
                    self.job_active = False
                    self.resolved = False
                    self._cancel(self._resolver_task)
                    if self.lease_id and self._off_task is None:
                        await self._off("printer no longer printing")
                elif self.lease_id is None and self._off_task is None:
                    await self._off_if_stale_lease()

    async def _reconcile_preheat(self, status: dict, now: float) -> bool:
        """Heat while Bambuddy's queue preheat warms the bed ahead of uploading
        a print. Bambuddy doesn't expose that stage directly, so it's inferred:
        the printer is idle, its bed has a target, and a queue item for it is
        pending. Returns True while a preheat is being handled."""
        bed_target = (status.get("temperatures") or {}).get("bed_target") or 0
        item = None
        if bed_target > 0 and not self.job_active:
            try:
                item = await self._next_queue_item()
            except Exception as e:
                self.log.warning("Bambuddy queue unavailable: %s", e)
                return self.preheat_item is not None
        if item is None:
            if self.preheat_item is None:
                return False
            # Two misses in a row: a single odd reading shouldn't stop the heater.
            self._preheat_misses += 1
            if self._preheat_misses < 2:
                return True
            self.log.info("Bambuddy preheat for queue item %s ended without a print starting", self.preheat_item)
            self._end_preheat()
            if self.lease_id:
                await self._off("preheat abandoned")
            return False

        self._preheat_misses = 0
        item_id = item.get("id")
        if item_id in self._preheat_done:
            return True
        if self.preheat_item != item_id:
            self.preheat_item = item_id
            self.preheat_started = now
            self.log.info(
                "Bambuddy is preheating for queue item %s (%s, bed target %s°C)",
                item_id,
                item.get("archive_name") or item.get("library_file_name"),
                bed_target,
            )
            res = await self.resolve_queue_item(item, status)
            if res is None:
                self.log.info("Filament for queue item %s not known; not heating", item_id)
                self._preheat_done.add(item_id)
                return True
            self.log.info("Filament decision: %s", res.describe())
            await self._apply_target(res.target_c)
            if not self.lease_id:
                self._preheat_done.add(item_id)
            return True
        if self.lease_id:
            await self._check_still_ours()
        if self.lease_id and self.preheat_started is not None and now - self.preheat_started > self.cfg.preheat_max_minutes * 60:
            self._preheat_done.add(item_id)
            await self._off(f"Bambuddy preheat ran over {self.cfg.preheat_max_minutes} min")
        return True

    async def _next_queue_item(self) -> dict | None:
        items = await self.bambuddy.get_pending_queue(self.printer.printer_id)
        items = [i for i in items if i.get("status") == "pending" and not i.get("manual_start")]
        return min(items, key=lambda i: (i.get("position") or 0, i.get("id") or 0)) if items else None

    def _end_preheat(self) -> None:
        self.preheat_item = None
        self.preheat_started = None
        self._preheat_misses = 0

    async def _check_still_ours(self) -> None:
        """If someone turned the heater off or changed its mode on the device or
        its web UI, stop managing it for this print rather than fight them."""
        try:
            state = await self.heater.get_state()
        except Exception as e:
            self.log.warning("DragonBreath state unavailable: %s", e)
            return
        if self.heater.dry_run:
            return
        lease = (state.get("control") or {}).get("lease") or {}
        if state.get("mode") != "power_on" or lease.get("owner") != self.cfg.actor_id or not lease.get("active"):
            self.log.warning(
                "Heater is no longer under our lease (mode=%s, owner=%s, fault=%s); leaving it alone for this print",
                state.get("mode"),
                lease.get("owner"),
                (state.get("safety") or {}).get("fault_latched"),
            )
            self.lease_id = None
            self._cancel(self._heartbeat_task)
            self.resolved = True

    async def _off_if_stale_lease(self) -> None:
        """A lease left behind by a previous run of the sidecar (crash, kill -9)
        would expire into a latched fault. Turn it off while it's still live."""
        try:
            state = await self.heater.get_state()
        except Exception:
            return
        lease = (state.get("control") or {}).get("lease") or {}
        if state.get("mode") == "power_on" and lease.get("active") and lease.get("owner") == self.cfg.actor_id:
            self.log.info("Heater still holds a lease from a previous run while idle; turning it off")
            await self._off("stale lease, printer idle")

    # ── decision ──────────────────────────────────────────────────────────

    async def resolve(self, ams_mapping: Any) -> Resolution | None:
        status = await self.bambuddy.get_status(self.printer.printer_id)
        targets = parse_targets(await self.bambuddy.get_preheat_targets_raw(), self.cfg.overrides)
        res = resolve_from_trays(status, ams_mapping, targets)
        if res is None and status.get("current_archive_id"):
            archive = await self.bambuddy.get_archive(status["current_archive_id"])
            res = resolve_from_types(split_slicer_types(archive.get("filament_type")), targets)
        return res

    async def resolve_queue_item(self, item: dict, status: dict) -> Resolution | None:
        """Same precedence as Bambuddy's preheat: the item's own chamber target,
        then the trays its ams_mapping names, then its slicer filament."""
        override = item.get("preheat_chamber_target_override")
        if override is not None:
            return Resolution(int(override), "queue item override")
        targets = parse_targets(await self.bambuddy.get_preheat_targets_raw(), self.cfg.overrides)
        res = resolve_from_trays(status, item.get("ams_mapping"), targets)
        if res is None:
            res = resolve_from_types(split_slicer_types(item.get("filament_type")), targets, "queue item")
        return res

    async def _try_decide(self) -> None:
        """Resolve the filament and engage/disengage. Leaves ``resolved`` False
        when there isn't enough information yet."""
        try:
            res = await self.resolve(self.ams_mapping)
        except Exception as e:
            self.log.warning("Filament lookup failed: %s", e)
            return
        if res is None:
            self.log.info("Filament for this print not known yet")
            return
        self.resolved = True
        self.log.info("Filament decision: %s", res.describe())
        await self._apply_target(res.target_c)

    def _start_resolver(self) -> None:
        if self._resolver_task is None or self._resolver_task.done():
            self._resolver_task = asyncio.create_task(self._resolver())

    async def _resolver(self) -> None:
        for _ in range(RESOLVE_MAX_ATTEMPTS):
            await asyncio.sleep(RESOLVE_RETRY_S)
            async with self._lock:
                if not self.job_active or self.resolved:
                    return
                await self._try_decide()
                if self.resolved:
                    return
        async with self._lock:
            if self.job_active and not self.resolved:
                self.log.warning(
                    "Couldn't determine this print's filament after %ss; not heating",
                    RESOLVE_RETRY_S * RESOLVE_MAX_ATTEMPTS,
                )
                self.resolved = True

    # ── heater control ────────────────────────────────────────────────────

    async def _apply_target(self, wanted_c: int) -> None:
        if wanted_c <= 0:
            if self.lease_id:
                await self._off("print needs no chamber heat")
            else:
                self.log.info("Print needs no chamber heat; heater stays off")
            return
        try:
            state = await self.heater.get_state()
        except Exception as e:
            self.log.error("DragonBreath unreachable, can't engage: %s", e)
            return
        safety = state.get("safety") or {}
        if safety.get("fault_latched") or safety.get("inhibited"):
            if safety.get("fault_latched") and not safety.get("inhibited") and self.cfg.auto_clear_fault:
                self.log.warning("Clearing latched DragonBreath fault (%s) before engaging", safety.get("reason"))
                try:
                    await self.heater.clear_fault()
                except DragonBreathError as e:
                    self.log.error("clear_fault refused: %s", e)
                    return
            else:
                self.log.error(
                    "DragonBreath has a latched fault/inhibit (%s); not heating. Clear it on the device.",
                    safety.get("reason"),
                )
                return
        maximum = (state.get("target") or {}).get("maximum_c") or 70
        target = clamp_for_device(wanted_c, MIN_TARGET_C, maximum)
        if self.lease_id and target == self.target_c:
            self.log.info("Already heating at %s°C", target)
            return
        if target != wanted_c:
            self.log.info("Target %s°C adjusted to %s°C for the device's range", wanted_c, target)
        comms_ms = (state.get("config") or {}).get("comms_ms") or 300_000
        try:
            self.lease_id = await self.heater.power_on(target)
        except DragonBreathError as e:
            self.log.error("power_on %s°C refused: %s", target, e)
            return
        except Exception as e:
            self.log.error("power_on %s°C failed: %s", target, e)
            return
        self.target_c = target
        if self.engaged_at is None:
            self.engaged_at = self.clock()
        self.log.info("Heater engaged at %s°C", target)
        self._cancel(self._heartbeat_task)
        self._heartbeat_task = asyncio.create_task(self._heartbeat(max(HEARTBEAT_MIN_S, comms_ms / 1000 / 3)))

    async def _heartbeat(self, interval: float) -> None:
        while True:
            await asyncio.sleep(interval)
            lease = self.lease_id
            if lease is None:
                return
            try:
                await self.heater.heartbeat(lease)
            except DragonBreathError as e:
                if e.code != "stale_lease":
                    self.log.warning("Heartbeat refused: %s", e)
                    continue
                async with self._lock:
                    if self.lease_id != lease:
                        return
                    self.lease_id = None
                    if self.job_active and self.target_c:
                        self.log.warning("Lease went stale; re-engaging")
                        self._heartbeat_task = None  # _apply_target starts a new one
                        await self._apply_target(self.target_c)
                    return
            except Exception as e:
                self.log.warning("Heartbeat failed: %s", e)

    async def _delayed_off(self, reason: str) -> None:
        await asyncio.sleep(self.cfg.off_delay_s)
        async with self._lock:
            self._off_task = None
            if self.lease_id and not self.job_active:
                await self._off(reason)

    async def _off(self, reason: str) -> None:
        self.lease_id = None
        self.engaged_at = None
        self._cancel(self._heartbeat_task)
        self._heartbeat_task = None
        for attempt in range(3):
            try:
                await self.heater.off()
                self.log.info("Heater off (%s)", reason)
                return
            except Exception as e:
                self.log.warning("off failed (attempt %d): %s", attempt + 1, e)
                await asyncio.sleep(2)
        self.log.error("Couldn't turn the heater off; its lease will expire and the firmware will cut it")

    @staticmethod
    def _cancel(task: asyncio.Task | None) -> None:
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()

    async def shutdown(self) -> None:
        for task in (self._resolver_task, self._off_task):
            self._cancel(task)
        if self.lease_id:
            await self._off("sidecar shutting down")
        else:
            self._cancel(self._heartbeat_task)
