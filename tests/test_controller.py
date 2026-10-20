import asyncio

import pytest

from bambuddy_dragonbreath import controller as controller_mod
from bambuddy_dragonbreath.config import Config, PrinterConfig
from bambuddy_dragonbreath.controller import PrinterController
from bambuddy_dragonbreath.dragonbreath import DragonBreathError

PID = 3


class FakeBambuddy:
    def __init__(self):
        self.status = {
            "connected": True,
            "state": "IDLE",
            "ams": [{"id": 0, "tray": [{"id": 0, "tray_type": "PLA"}, {"id": 1, "tray_type": "ABS"}]}],
            "vt_tray": [],
            "current_archive_id": None,
        }
        self.status["temperatures"] = {"bed_target": 0}
        self.targets_raw = ""
        self.archives: dict[int, dict] = {}
        self.queue: list[dict] = []
        self.fail = False

    async def get_status(self, printer_id):
        assert printer_id == PID
        if self.fail:
            raise ConnectionError("down")
        return self.status

    async def get_preheat_targets_raw(self):
        return self.targets_raw

    async def get_pending_queue(self, printer_id):
        return [dict(i) for i in self.queue]

    async def get_archive(self, archive_id):
        return self.archives.get(archive_id, {})


class FakeHeater:
    dry_run = False
    base_url = "http://db"

    def __init__(self):
        self.calls: list[tuple] = []
        self.mode = "off"
        self.owner = None
        self.fault = False
        self.stale_next_heartbeat = False
        self.leases = 0

    def state(self):
        return {
            "state_revision": 1,
            "mode": self.mode,
            "target": {"maximum_c": 70},
            "config": {"comms_ms": 300000},
            "control": {"lease": {"active": self.mode == "power_on", "owner": self.owner}},
            "safety": {"fault_latched": self.fault, "inhibited": False, "reason": "emergency" if self.fault else None},
        }

    async def get_state(self):
        return self.state()

    async def power_on(self, target_c):
        if self.fault:
            raise DragonBreathError("fault_latched")
        self.calls.append(("power_on", target_c))
        self.mode, self.owner = "power_on", "bambuddy"
        self.leases += 1
        return f"{self.leases:032d}"

    async def heartbeat(self, lease_id):
        self.calls.append(("heartbeat", lease_id))
        if self.stale_next_heartbeat:
            self.stale_next_heartbeat = False
            raise DragonBreathError("stale_lease", status=409)
        return 300000

    async def off(self):
        self.calls.append(("off",))
        self.mode, self.owner = "off", None

    async def clear_fault(self):
        self.calls.append(("clear_fault",))
        self.fault = False

    def commands(self):
        return [c for c in self.calls if c[0] != "heartbeat"]


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(controller_mod, "RESOLVE_RETRY_S", 0.01)
    monkeypatch.setattr(controller_mod, "RESOLVE_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(controller_mod, "HEARTBEAT_MIN_S", 0.01)


def make(**cfg_kw):
    cfg = Config(printers=[PrinterConfig(printer_id=PID, dragonbreath_url="http://db")], **cfg_kw)
    b, h = FakeBambuddy(), FakeHeater()
    c = PrinterController(cfg, cfg.printers[0], b, h)
    # comms_ms 300000 / 3 is too slow for tests; HEARTBEAT_MIN_S only raises it.
    h_state = h.state

    def state():
        s = h_state()
        s["config"]["comms_ms"] = 30
        return s

    h.state = state
    return c, b, h


def start(mapping, pid=PID):
    return {"type": "print_start", "printer_id": pid, "data": {"filename": "x.gcode", "ams_mapping": mapping}}


def complete(status="completed"):
    return {"type": "print_complete", "printer_id": PID, "data": {"status": status}}


async def test_abs_print_engages_heartbeats_and_turns_off():
    c, b, h = make()
    b.status["state"] = "PREPARE"
    await c.handle_event(start([1]))
    assert h.commands() == [("power_on", 45)]
    await asyncio.sleep(0.05)
    assert any(call[0] == "heartbeat" for call in h.calls)
    await c.handle_event(complete())
    assert h.commands() == [("power_on", 45), ("off",)]
    assert c.lease_id is None


@pytest.mark.parametrize("status", ["failed", "aborted", "cancelled"])
async def test_off_on_any_terminal_status(status):
    c, b, h = make()
    await c.handle_event(start([1]))
    await c.handle_event(complete(status))
    assert h.commands()[-1] == ("off",)


async def test_pla_print_does_not_heat():
    c, b, h = make()
    await c.handle_event(start([0]))
    assert h.commands() == []
    await c.handle_event(complete())
    assert h.commands() == []


async def test_other_printer_ignored():
    c, b, h = make()
    await c.handle_event(start([1], pid=99))
    assert h.commands() == []


async def test_bambuddy_map_and_overrides_used():
    c, b, h = make(overrides={"ABS": 60})
    await c.handle_event(start([1]))
    assert h.commands() == [("power_on", 60)]

    c, b, h = make()
    b.targets_raw = '{"ABS": 20}'
    await c.handle_event(start([1]))
    assert h.commands() == [("power_on", 30)]  # raised to the device minimum


async def test_no_mapping_falls_back_to_archive_after_it_appears():
    c, b, h = make()
    b.status["state"] = "RUNNING"
    await c.handle_event(start(None))
    assert h.commands() == []
    b.status["current_archive_id"] = 12
    b.archives[12] = {"filament_type": "PLA, ASA"}
    await asyncio.sleep(0.05)
    assert h.commands() == [("power_on", 45)]


async def test_no_information_gives_up_without_heating():
    c, b, h = make()
    await c.handle_event(start(None))
    await asyncio.sleep(0.1)
    assert c.resolved
    assert h.commands() == []


async def test_delayed_off_cancelled_by_new_print():
    c, b, h = make(off_delay_s=0.05)
    await c.handle_event(start([1]))
    await c.handle_event(complete())
    assert h.commands() == [("power_on", 45)]
    await c.handle_event(start([1]))
    await asyncio.sleep(0.1)
    assert ("off",) not in h.commands()
    await c.handle_event(complete())
    await asyncio.sleep(0.1)
    assert h.commands()[-1] == ("off",)


async def test_reconcile_picks_up_print_after_restart():
    c, b, h = make()
    b.status.update(state="RUNNING", current_archive_id=5)
    b.archives[5] = {"filament_type": "ABS"}
    await c.reconcile()
    await asyncio.sleep(0.05)
    assert h.commands() == [("power_on", 45)]


async def test_reconcile_turns_off_when_print_complete_missed():
    c, b, h = make()
    b.status["state"] = "RUNNING"
    await c.handle_event(start([1]))
    b.status["state"] = "FINISH"
    await c.reconcile()
    assert h.commands() == [("power_on", 45), ("off",)]


async def test_reconcile_clears_stale_lease_from_previous_run():
    c, b, h = make()
    h.mode, h.owner = "power_on", "bambuddy"
    await c.reconcile()
    assert h.commands() == [("off",)]


async def test_reconcile_leaves_someone_elses_heating_alone():
    c, b, h = make()
    h.mode, h.owner = "power_on", "someone"
    await c.reconcile()
    assert h.commands() == []


async def test_user_turning_heater_off_is_respected():
    c, b, h = make()
    b.status["state"] = "RUNNING"
    await c.handle_event(start([1]))
    h.mode, h.owner = "off", None  # turned off on the device
    await c.reconcile()
    await c.reconcile()
    assert h.commands() == [("power_on", 45)]
    assert c.lease_id is None


async def test_stale_lease_reengages_while_printing():
    c, b, h = make()
    b.status["state"] = "RUNNING"
    await c.handle_event(start([1]))
    h.stale_next_heartbeat = True
    await asyncio.sleep(0.05)
    assert h.commands() == [("power_on", 45), ("power_on", 45)]
    assert c.lease_id == f"{2:032d}"


async def test_fault_latched_blocks_heating_unless_auto_clear():
    c, b, h = make()
    h.fault = True
    await c.handle_event(start([1]))
    assert h.commands() == []

    c, b, h = make(auto_clear_fault=True)
    h.fault = True
    await c.handle_event(start([1]))
    assert h.commands() == [("clear_fault",), ("power_on", 45)]


async def test_bambuddy_lost_turns_heater_off():
    now = [0.0]
    c, b, h = make(bambuddy_lost_off_s=60)
    c.clock = lambda: now[0]
    c.last_bambuddy_ok = 0
    b.status["state"] = "RUNNING"
    await c.handle_event(start([1]))
    b.fail = True
    now[0] = 30
    await c.reconcile()
    assert ("off",) not in h.commands()
    now[0] = 61
    await c.reconcile()
    assert h.commands()[-1] == ("off",)


async def test_max_engaged_hours():
    now = [0.0]
    c, b, h = make(max_engaged_hours=1)
    c.clock = lambda: now[0]
    b.status["state"] = "RUNNING"
    await c.handle_event(start([1]))
    now[0] = 3601
    await c.reconcile()
    assert h.commands()[-1] == ("off",)


async def test_check_heater_logs_and_warns(caplog):
    c, b, h = make()
    s = h.state()
    s["environment"] = {"control_source": "bambu"}
    h.get_state = lambda: _value(s)
    with caplog.at_level("INFO"):
        await c.check_heater()
    assert "control source is 'bambu'" in caplog.text

    async def boom():
        raise ConnectionError("no route")

    h.get_state = boom
    await c.check_heater()
    assert "unreachable" in caplog.text
    assert h.commands() == []


async def _value(v):
    return v


async def test_shutdown_turns_off_our_heating():
    c, b, h = make()
    await c.handle_event(start([1]))
    await c.shutdown()
    assert h.commands()[-1] == ("off",)


def queue_item(id=82, mapping=(-1, -1, 1), **kw):
    return {"id": id, "status": "pending", "position": 1, "manual_start": False,
            "ams_mapping": list(mapping), "filament_type": "ABS", "archive_name": "part", **kw}


def preheating(b, item):
    b.status["state"] = "FINISH"
    b.status["temperatures"]["bed_target"] = 100
    b.queue = [item]


async def test_queue_preheat_engages_and_print_start_keeps_heating():
    c, b, h = make()
    preheating(b, queue_item())
    await c.reconcile()
    assert h.commands() == [("power_on", 45)]
    await c.reconcile()
    assert h.commands() == [("power_on", 45)]  # no re-issue while preheat continues
    b.status["state"] = "PREPARE"
    b.queue = []
    await c.handle_event(start([-1, -1, 1]))
    assert h.commands() == [("power_on", 45)]  # same target: lease kept
    assert c.preheat_item is None
    await c.handle_event(complete())
    assert h.commands()[-1] == ("off",)


async def test_queue_preheat_abandoned_turns_off_after_two_misses():
    c, b, h = make()
    preheating(b, queue_item())
    await c.reconcile()
    b.queue = []
    b.status["temperatures"]["bed_target"] = 0
    await c.reconcile()
    assert ("off",) not in h.commands()
    await c.reconcile()
    assert h.commands() == [("power_on", 45), ("off",)]


async def test_queue_preheat_pla_does_not_heat():
    c, b, h = make()
    preheating(b, queue_item(mapping=(0,), filament_type="PLA"))
    await c.reconcile()
    await c.reconcile()
    assert h.commands() == []


async def test_queue_preheat_uses_item_override_and_slicer_fallback():
    c, b, h = make()
    preheating(b, queue_item(preheat_chamber_target_override=55))
    await c.reconcile()
    assert h.commands() == [("power_on", 55)]

    c, b, h = make()
    preheating(b, queue_item(mapping=(-1,), filament_type="ASA"))
    await c.reconcile()
    assert h.commands() == [("power_on", 45)]


async def test_no_preheat_without_bed_target_or_for_manual_items():
    c, b, h = make()
    preheating(b, queue_item())
    b.status["temperatures"]["bed_target"] = 0
    await c.reconcile()
    b.status["temperatures"]["bed_target"] = 100
    b.queue = [queue_item(manual_start=True)]
    await c.reconcile()
    assert h.commands() == []


async def test_queue_preheat_time_cap():
    now = [0.0]
    c, b, h = make(preheat_max_minutes=10)
    c.clock = lambda: now[0]
    preheating(b, queue_item())
    await c.reconcile()
    now[0] = 601
    await c.reconcile()
    assert h.commands() == [("power_on", 45), ("off",)]
    now[0] = 700
    await c.reconcile()
    assert h.commands() == [("power_on", 45), ("off",)]  # not re-engaged for the same item
