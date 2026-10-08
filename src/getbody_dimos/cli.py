"""getbody-dimos command line.

    getbody-dimos check   --config config.yaml           # dimos reachable, every mapped tool present
    getbody-dimos plan    --config config.yaml -o plan.json
    getbody-dimos standin --plan plan.json               # GetBody's local stand-in
    getbody-dimos run     --config config.yaml --url ws://127.0.0.1:8765
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import threading

from . import __version__
from .bridge import DimosBridge
from .config import ConfigError, load
from .robot import DimosRobot, StartupError
from .vendor import getbody_bridge as gb

log = logging.getLogger("getbody_dimos")


def _robot(args: argparse.Namespace) -> DimosRobot:
    cfg = load(args.config)
    if args.mcp_url:
        cfg.mcp_url = args.mcp_url
    motion = None
    if cfg.odom is not None and not args.no_odom:
        from .motion import make_probe

        motion = make_probe(cfg.odom)
    robot = DimosRobot(cfg, motion=motion)
    robot.check()
    if motion:
        log.info("odometry check on: kill_ack.halted is measured from %s", cfg.odom.topic if cfg.odom else "?")
    else:
        log.warning("odometry check off: kill_ack.halted means the stop tools returned ok, not measured motion")
    return robot


def cmd_check(args: argparse.Namespace) -> int:
    robot = _robot(args)
    cfg = robot.config
    for name, c in cfg.commands.items():
        target = "built-in stop" if c.builtin else f"{c.tool} ({c.mode})"
        print(f"command {name:16} -> {target}")
    for name, f in cfg.feeds.items():
        print(f"feed    {name:16} -> {f.tool or f.kind}")
    print(f"stop tools: {', '.join(robot.stop_tools)}")
    print("ok")
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    plan = json.dumps(load(args.config).plan(), indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8", newline="\n") as f:
            f.write(plan + "\n")
        print(f"wrote {args.output}")
    else:
        print(plan)
    return 0


def cmd_schemas(args: argparse.Namespace) -> int:
    cfg = load(args.config)
    print(json.dumps({"command_schemas": cfg.schemas(), "offered_feeds": list(cfg.feeds)}, indent=2))
    return 0


def cmd_standin(args: argparse.Namespace) -> int:
    with open(args.plan, encoding="utf-8") as f:
        plan = json.load(f)
    asyncio.run(gb.StandIn(plan).serve(port=args.port))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    robot = _robot(args)
    if args.url:
        url, path, key = args.url, "", (gb.AgentKey.load(args.key) if args.key else None)
    else:
        if not (args.body_id and args.key):
            print("run needs --url (e.g. the local stand-in), or --body-id and --key", file=sys.stderr)
            return 2
        path = f"/getbody/ws/bodies/{args.body_id}/interface"
        url, key = f"wss://{args.host}{path}", gb.AgentKey.load(args.key)
    bridge_log = logging.getLogger("getbody_dimos.bridge")
    bridge = DimosBridge(robot, url, key=key, path=path, state_file=args.state, log=bridge_log.warning)
    threading.Thread(target=gb._stdin_rearm, args=(bridge,), daemon=True).start()
    log.info("bridge connecting to %s; type `rearm` here after a kill, once the robot is safe", url)
    try:
        asyncio.run(bridge.run_forever())
    except KeyboardInterrupt:
        pass
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="getbody-dimos", description="List a DimOS robot on GetBody (unofficial).")
    p.add_argument("--version", action="version", version=f"getbody-dimos {__version__} (bridge {gb.__version__})")
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    sub = p.add_subparsers(dest="mode", required=True)

    def with_config(sp: argparse.ArgumentParser, dimos: bool = True) -> None:
        sp.add_argument("--config", required=True, help="config.yaml")
        if dimos:
            sp.add_argument("--mcp-url", help="dimos MCP endpoint (overrides the config)")
            sp.add_argument("--no-odom", action="store_true", help="skip the odometry check even if configured")

    with_config(sub.add_parser("check", help="check dimos is up and offers every mapped tool"))
    sp = sub.add_parser("plan", help="write plan.json for the stand-in")
    with_config(sp, dimos=False)
    sp.add_argument("-o", "--output")
    with_config(sub.add_parser("schemas", help="print command_schemas for the listing"), dimos=False)
    sp = sub.add_parser("standin", help="GetBody's local stand-in, to test before registering")
    sp.add_argument("--plan", required=True)
    sp.add_argument("--port", type=int, default=8765)
    sp = sub.add_parser("run", help="run the bridge")
    with_config(sp)
    sp.add_argument("--url", help="connect here instead of getbody.md (e.g. ws://127.0.0.1:8765)")
    sp.add_argument("--body-id", type=int)
    sp.add_argument("--key", help="agent_key.json")
    sp.add_argument("--host", default="getbody.md")
    sp.add_argument("--state", default="getbody_bridge_state.json")
    args = p.parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
                        stream=sys.stderr, force=True)

    handlers = {"check": cmd_check, "plan": cmd_plan, "schemas": cmd_schemas, "standin": cmd_standin, "run": cmd_run}
    try:
        return handlers[args.mode](args)
    except (ConfigError, StartupError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
