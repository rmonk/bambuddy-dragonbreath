"""Entry point: ``bambuddy-dragonbreath [--config PATH] {run,status,resolve,test,off}``."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import signal
import sys

from .bambuddy import BambuddyClient
from .config import Config, PrinterConfig, load_config
from .controller import PrinterController
from .dragonbreath import DragonBreathClient

logger = logging.getLogger("bambuddy_dragonbreath")


def _heater(cfg: Config, p: PrinterConfig, dry_run: bool = False) -> DragonBreathClient:
    return DragonBreathClient(p.dragonbreath_url, p.dragonbreath_token, cfg.actor_id, dry_run=dry_run)


async def run(cfg: Config) -> None:
    bambuddy = BambuddyClient(cfg.bambuddy.url, cfg.bambuddy.api_key)
    controllers = [PrinterController(cfg, p, bambuddy, _heater(cfg, p, cfg.dry_run)) for p in cfg.printers]
    if cfg.dry_run:
        logger.warning("Dry run: heater commands are logged, not sent")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    async def consume_events() -> None:
        async for msg in bambuddy.events():
            if msg.get("type") == "_connected":
                for c in controllers:
                    asyncio.create_task(c.reconcile())
                continue
            for c in controllers:
                try:
                    await c.handle_event(msg)
                except Exception:
                    logger.exception("Error handling %s", msg.get("type"))

    async def reconcile_loop(c: PrinterController) -> None:
        while True:
            try:
                await c.reconcile()
            except Exception:
                logger.exception("Reconcile failed")
            await asyncio.sleep(cfg.reconcile_interval_s)

    for c in controllers:
        await c.check_heater()
    tasks = [asyncio.create_task(consume_events())] + [asyncio.create_task(reconcile_loop(c)) for c in controllers]
    logger.info("Watching %d printer(s) via %s", len(controllers), cfg.bambuddy.url)
    await stop.wait()
    logger.info("Shutting down")
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    for c in controllers:
        await c.shutdown()
        await c.heater.aclose()
    await bambuddy.aclose()


def _pick(cfg: Config, printer_id: int | None) -> PrinterConfig:
    if printer_id is None:
        return cfg.printers[0]
    for p in cfg.printers:
        if p.printer_id == printer_id:
            return p
    sys.exit(f"printer {printer_id} is not in the config")


async def cmd_status(cfg: Config, p: PrinterConfig) -> None:
    heater = _heater(cfg, p)
    state = await heater.get_state()
    await heater.aclose()
    keys = ("mode", "target", "sensors", "environment", "control", "safety", "config")
    print(json.dumps({k: state.get(k) for k in keys}, indent=2))


async def cmd_resolve(cfg: Config, p: PrinterConfig) -> None:
    """Show what the sidecar would pick for the printer's current print. The
    print's ams_mapping only arrives with the live print_start event, so this
    uses the slicer metadata fallback."""
    bambuddy = BambuddyClient(cfg.bambuddy.url, cfg.bambuddy.api_key)
    c = PrinterController(cfg, p, bambuddy, _heater(cfg, p, dry_run=True))
    status = await bambuddy.get_status(p.printer_id)
    print(f"printer state: {status.get('state')}  file: {status.get('subtask_name') or status.get('gcode_file')}")
    res = await c.resolve(None)
    print(res.describe() if res else "no filament information available")
    await bambuddy.aclose()


async def cmd_test(cfg: Config, p: PrinterConfig, target: int, hold: float) -> None:
    heater = _heater(cfg, p)
    lease = await heater.power_on(target)
    print(f"power_on {target}°C accepted (lease {lease[:8]}…); holding {hold:.0f}s, Ctrl-C to stop early")
    try:
        deadline = asyncio.get_running_loop().time() + hold
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(min(20, max(0.0, deadline - asyncio.get_running_loop().time())))
            ms = await heater.heartbeat(lease)
            state = await heater.get_state()
            chamber = ((state.get("sensors") or {}).get("chamber") or {}).get("temperature_c")
            print(f"  mode={state.get('mode')} chamber={chamber}°C lease expires in {ms / 1000:.0f}s")
    finally:
        await heater.off()
        print("off sent")
        await heater.aclose()


async def cmd_off(cfg: Config, p: PrinterConfig) -> None:
    heater = _heater(cfg, p)
    await heater.off()
    await heater.aclose()
    print("off sent")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="bambuddy-dragonbreath")
    ap.add_argument("--config", default=os.environ.get("BDB_CONFIG", "/config/config.yaml"))
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("run", help="run the service (default)")
    r.add_argument("--dry-run", action="store_true", help="log heater commands instead of sending them")
    for name, help_ in (
        ("status", "show the heater's state"),
        ("resolve", "show the filament decision for the current print"),
        ("off", "turn the heater off"),
    ):
        sub.add_parser(name, help=help_).add_argument("--printer", type=int)
    t = sub.add_parser("test", help="heat to a target for a while, then turn off")
    t.add_argument("--printer", type=int)
    t.add_argument("--target", type=int, default=40)
    t.add_argument("--hold", type=float, default=60, help="seconds to hold before turning off")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    logging.basicConfig(
        level=cfg.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    cmd = args.cmd or "run"
    if cmd == "run":
        if getattr(args, "dry_run", False):
            cfg.dry_run = True
        asyncio.run(run(cfg))
        return
    p = _pick(cfg, args.printer)
    with contextlib.suppress(KeyboardInterrupt):
        if cmd == "status":
            asyncio.run(cmd_status(cfg, p))
        elif cmd == "resolve":
            asyncio.run(cmd_resolve(cfg, p))
        elif cmd == "test":
            asyncio.run(cmd_test(cfg, p, args.target, args.hold))
        elif cmd == "off":
            asyncio.run(cmd_off(cfg, p))


if __name__ == "__main__":
    main()
