"""A fake dimos McpServer for tests, following the dimos source it is pinned to.

Pinned to dimos commit dc80d89 (version 0.0.14). The tool list (names, input
schemas, descriptions, _meta) is loaded from tests/contract/dimos-dc80d89.json,
which tools/derive_contract.py derives from that commit. The HTTP/JSON-RPC
behaviour and every reply text below follow these files at that commit:

  dimos/agents/mcp/mcp_server.py           JSON-RPC handling, errors, capabilities, images
  dimos/robot/unitree/unitree_skill_container.py   move_to, wait, current_time, execute_sport_command
  dimos/agents/skills/navigation.py        tag_location, navigate_with_text, stop_navigation
  dimos/agents/skills/observe_skill.py     observe
  dimos/navigation/experimental/frontier_exploration/wavefront_frontier_goal_selector.py
                                           begin_exploration, end_exploration
  dimos/navigation/experimental/patrolling/module.py   start_patrol, stop_patrol
  dimos/robot/unitree/go2/connection.py    get_battery_soc (None in MuJoCo: no lowstate stream)
  dimos/msgs/sensor_msgs/Image.py          Image.agent_encode (OpenAI-style image_url part)

What it does not model: real physics, the planner's exact timing, the VLM path
of navigate_with_text, and how dimos's RPC layer words exceptions raised inside
a module worker.
"""

from __future__ import annotations

import base64
import json
import math
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CONTRACT_PATH = Path(__file__).parent / "contract" / "dimos-dc80d89.json"
CONTRACT = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
TOOLS = {t["name"]: t for t in CONTRACT["tools"]}
MODULE_OF = {skill: module for module, skills in CONTRACT["modules"].items() for skill in skills}

JPEG = base64.b64encode(b"\xff\xd8\xff\xe0fake-jpeg\xff\xd9").decode()

# unitree_skill_container.UNITREE_WEBRTC_CONTROLS names (the fake only needs the names)
SPORT_COMMANDS = {"BalanceStand", "StandUp", "StandDown", "RecoveryStand", "Sit", "RiseSit", "SwitchGait", "Trigger",
                  "BodyHeight", "FootRaiseHeight", "SpeedLevel", "Hello", "Stretch", "TrajectoryFollow",
                  "ContinuousGait", "Content", "Wallow", "Dance1", "Dance2", "GetBodyHeight", "GetFootRaiseHeight",
                  "GetSpeedLevel", "SwitchJoystick", "Pose", "Scrape", "FrontFlip", "FrontJump", "FrontPounce",
                  "WiggleHips", "GetState", "EconomicGait", "FingerHeart", "Handstand", "CrossStep", "OnesidedStep",
                  "Bound", "MoonWalk", "LeftFlip", "RightFlip", "Backflip"}


class SkillError(Exception):
    """Raised inside a fake skill; reported like dimos reports skill exceptions."""

    def __init__(self, exc: Exception):
        self.exc = exc


def _pose_text(x: float, y: float, yaw_deg: float) -> str:
    # unitree_skill_container._pose_text
    return f"x={x:.2f} y={y:.2f} heading={yaw_deg:.0f}deg"


def _wrap_deg(d: float) -> float:
    return math.degrees(math.atan2(math.sin(math.radians(d)), math.cos(math.radians(d))))


class FakeDimos:
    """Tunable timing so tests stay fast; defaults are dimos's own constants."""

    def __init__(self, move_duration_s: float = 3.0, start_delay_s: float = 1.0, settle_s: float = 2.0,
                 camera_running: bool = True):
        self.move_duration_s = move_duration_s  # how long the fake robot takes to reach a goal
        self.start_delay_s = start_delay_s      # move_to's time.sleep(1.0) before it checks progress
        self.settle_s = settle_s                # _wait_for_goal's settle: idle this long = cancelled
        self.camera_running = camera_running
        self.calls: list[tuple[str, dict]] = []
        self.fail_stop = False                  # stop_navigation raises
        self.stop_delay_s = 0.0
        self.pose = [0.0, 0.0, 0.0]             # x, y, heading in degrees (world frame)
        self.tags: dict[str, tuple[float, float]] = {}
        self._goal_cancel = threading.Event()
        self._navigating = threading.Event()
        self.exploring = False
        self.patrolling = False
        self._caps: dict[str, str] = {}         # capability -> holder tool (CapabilityRegistry)
        self._lock = threading.Lock()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}/mcp"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._goal_cancel.set()
        self._server.shutdown()
        self._server.server_close()

    def count(self, name: str) -> int:
        return sum(1 for n, _ in self.calls if n == name)

    def is_moving(self) -> bool:
        return self._navigating.is_set() or self.exploring or self.patrolling

    # ---- skills (each returns what the dimos skill returns, or raises SkillError)
    def move_to(self, x=0.0, y=0.0, degrees=None, relative=False):
        x, y = float(x), float(y)
        degrees = None if degrees is None else float(degrees)
        px, py, yaw = self.pose
        if relative:
            r = math.radians(yaw)
            gx, gy = px + x * math.cos(r) - y * math.sin(r), py + x * math.sin(r) + y * math.cos(r)
            gyaw = _wrap_deg(yaw + (degrees or 0.0))
        else:
            gx, gy, gyaw = x, y, (yaw if degrees is None else _wrap_deg(degrees))
        self._goal_cancel.clear()
        self._navigating.set()
        cancelled = self._goal_cancel.wait(self.move_duration_s)
        self._navigating.clear()
        if cancelled:
            time.sleep(self.settle_s)  # _wait_for_goal gives up after `settle` seconds idle
            outcome = "Navigation was cancelled or failed"
        else:
            self.pose = [gx, gy, gyaw]
            outcome = "Navigation goal reached"
        return f"{outcome}. Robot is at {_pose_text(*self.pose)}; goal was {_pose_text(gx, gy, gyaw)}."

    def stop_navigation(self):
        if self.stop_delay_s:
            time.sleep(self.stop_delay_s)
        if self.fail_stop:
            raise SkillError(RuntimeError("boom"))
        self._goal_cancel.set()  # ReplanningAStarPlanner.cancel_goal
        return "Stopped"

    def tag_location(self, location_name):
        self.tags[location_name] = (self.pose[0], self.pose[1])
        return f"Tagged '{location_name}': ({float(self.pose[0])},{float(self.pose[1])})."

    def navigate_with_text(self, query):
        if query in self.tags:
            return (f"Found a tagged location called '{query}'.. Started navigating to that position. "
                    "To cancel movement call the 'stop_navigation' tool.")
        return (f"No tagged location called '{query}'. No object in view matching '{query}'. "
                f"No matching location found in semantic map for '{query}'.")

    def observe(self):
        if not self.camera_running:
            raise SkillError(TimeoutError("No camera frame received within 5.0 seconds; the camera may not be running."))
        return {"agent_encode": [{"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{JPEG}"}}]}

    def get_battery_soc(self):
        return None  # MuJoCo has no lowstate stream

    def begin_exploration(self):
        if self.exploring:
            return "Exploration skill is already active. Use end_exploration to stop before starting again."
        self.exploring = True
        return "Started exploration skill. The robot is now moving. Use end_exploration to stop."

    def end_exploration(self):
        stopped, self.exploring = self.exploring, False
        self._release("begin_exploration")
        if stopped:
            return "Stopped exploration. The robot has stopped moving."
        return "Exploration skill was not active, so nothing was stopped."

    def start_patrol(self):
        if self.patrolling:
            return "Patrol is already running. Use `stop_patrol` to stop."
        self.patrolling = True
        return "Patrol started. Use `stop_patrol` to stop."

    def stop_patrol(self):
        self.patrolling = False
        self._release("start_patrol")
        return "Patrol stopped."

    def wait(self, seconds):
        time.sleep(seconds)
        return f"Wait completed with length={seconds}s"

    def current_time(self):
        return str(datetime.now())

    def execute_sport_command(self, command_name):
        if command_name not in SPORT_COMMANDS:
            return f"There's no '{command_name}' command. Did you mean: []"
        return f"'{command_name}' command executed successfully."  # MuJoCo: publish_request only prints

    def server_status(self):
        modules = list(dict.fromkeys(MODULE_OF[n] for n in TOOLS))
        return json.dumps({"pid": 4242, "modules": modules, "skills": list(TOOLS)})

    def list_modules(self):
        return json.dumps({"modules": CONTRACT["modules"]})

    def agent_send(self, message):
        if not message:
            raise SkillError(ValueError("Message cannot be empty"))
        return f"Message sent to agent: {message[:100]}"

    # ---- McpServer._handle_tools_call
    def _release(self, holder: str) -> None:
        with self._lock:
            for cap in [c for c, h in self._caps.items() if h == holder]:
                del self._caps[cap]

    def _call(self, name: str, args: dict) -> dict:
        with self._lock:
            self.calls.append((name, args))
        tool = TOOLS.get(name)
        if tool is None:
            return _text(f"Tool not found: {name}")
        meta = tool.get("_meta") or {}
        uses, lifecycle = meta.get("dimos/uses", []), meta.get("dimos/lifecycle", "instant")
        with self._lock:
            for cap in uses:
                holder = self._caps.get(cap)
                if holder is not None:
                    holder_lifecycle = (TOOLS[holder].get("_meta") or {}).get("dimos/lifecycle", "instant")
                    advice = ("Call the appropriate stop tool first, then retry." if holder_lifecycle == "background"
                              else "It is taking longer than expected; wait a moment and then retry.")
                    return _text(f"Cannot start '{name}': capability '{cap}' is held by '{holder}'. {advice}")
            for cap in uses:
                self._caps[cap] = name
        props, required = tool["inputSchema"].get("properties", {}), tool["inputSchema"].get("required", [])
        try:
            unexpected = sorted(set(args) - set(props))
            if unexpected:
                raise SkillError(TypeError(f"{MODULE_OF[name]}.{name}() got an unexpected keyword argument "
                                           f"'{unexpected[0]}'"))
            missing = [r for r in required if r not in args]
            if missing:
                raise SkillError(TypeError(f"{MODULE_OF[name]}.{name}() missing 1 required positional argument: "
                                           f"'{missing[0]}'"))
            result = getattr(self, name)(**args)
        except SkillError as err:
            e = err.exc
            self._release(name)
            return {"content": [{"type": "text", "text": f"Error running tool '{name}': {type(e).__name__}: {e}"}],
                    "isError": True}
        if lifecycle != "background":
            self._release(name)  # instant holders release when they return
        if isinstance(result, dict) and "agent_encode" in result:
            return {"content": result["agent_encode"]}
        return _text(str(result))

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                try:
                    body = json.loads(raw)
                except ValueError:
                    return self._send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
                                      status=400)
                if "id" not in body:  # notification
                    self.send_response(204)
                    self.end_headers()
                    return
                method, params, req_id = body.get("method", ""), body.get("params", {}) or {}, body.get("id")
                if method == "initialize":
                    result = CONTRACT["initialize"]
                elif method == "tools/list":
                    result = {"tools": CONTRACT["tools"]}
                elif method == "tools/call":
                    result = fake._call(params.get("name", ""), params.get("arguments") or {})
                else:
                    return self._send({"jsonrpc": "2.0", "id": req_id,
                                       "error": {"code": -32601, "message": f"Unknown: {method}"}})
                self._send({"jsonrpc": "2.0", "id": req_id, "result": result})

            def _send(self, msg, status=200):
                data = json.dumps(msg).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        return Handler


def _text(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}]}
