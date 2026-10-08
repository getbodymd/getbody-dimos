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
import sys
import threading

from . import __version__
from .bridge import DimosBridge
from .config import ConfigError, load
from .robot import DimosRobot, StartupError
from .vendor import getbody_bridge as gb


def _robot(args) -> DimosRobot:
    cfg = load(args.config)
    if args.mcp_url:
        cfg.mcp_url = args.mcp_url
    motion = None
    if cfg.odom is not None and not args.no_odom:
        from .motion import make_probe

        motion = make_probe(cfg.odom)
    robot = DimosRobot(cfg, motion=motion)
    tools = robot.check()
    print(f"dimos at {cfg.mcp_url}: {len(tools)} tools; stop tools {robot.stop_tools}"
          + ("; odometry check on" if motion else "; odometry check off (halted is not measured)"))
    return robot


def cmd_check(args) -> int:
    robot = _robot(args)
    cfg = robot.config
    for name, c in cfg.commands.items():
        target = "built-in stop" if c.builtin else f"{c.tool} ({c.mode})"
        print(f"command {name:16} -> {target}")
    for name, f in cfg.feeds.items():
        print(f"feed    {name:16} -> {f.tool or f.kind}")
    print("ok")
    return 0


def cmd_plan(args) -> int:
    plan = json.dumps(load(args.config).plan(), indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8", newline="\n") as f:
            f.write(plan + "\n")
        print(f"wrote {args.output}")
    else:
        print(plan)
    return 0


def cmd_schemas(args) -> int:
    cfg = load(args.config)
    print(json.dumps({"command_schemas": cfg.schemas(), "offered_feeds": list(cfg.feeds)}, indent=2))
    return 0


def cmd_standin(args) -> int:
    with open(args.plan, encoding="utf-8") as f:
        plan = json.load(f)
    asyncio.run(gb.StandIn(plan).serve(port=args.port))
    return 0


def cmd_run(args) -> int:
    robot = _robot(args)
    if args.url:
        url, path, key = args.url, "", (gb.AgentKey.load(args.key) if args.key else None)
    else:
        if not (args.body_id and args.key):
            print("run needs --url (e.g. the local stand-in), or --body-id and --key", file=sys.stderr)
            return 2
        path = f"/getbody/ws/bodies/{args.body_id}/interface"
        url, key = f"wss://{args.host}{path}", gb.AgentKey.load(args.key)
    bridge = DimosBridge(robot, url, key=key, path=path, state_file=args.state)
    threading.Thread(target=gb._stdin_rearm, args=(bridge,), daemon=True).start()
    print(f"bridge connecting to {url}; type `rearm` here after a kill, once the robot is safe")
    try:
        asyncio.run(bridge.run_forever())
    except KeyboardInterrupt:
        pass
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="getbody-dimos", description="List a DimOS robot on GetBody (unofficial).")
    p.add_argument("--version", action="version", version=f"getbody-dimos {__version__} (bridge {gb.__version__})")
    sub = p.add_subparsers(dest="mode", required=True)

    def with_config(sp, dimos=True):
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

    handlers = {"check": cmd_check, "plan": cmd_plan, "schemas": cmd_schemas, "standin": cmd_standin, "run": cmd_run}
    try:
        return handlers[args.mode](args)
    except (ConfigError, StartupError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
