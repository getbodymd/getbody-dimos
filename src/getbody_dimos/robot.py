"""The robot side of the GetBody bridge, for a robot run by dimos.

Commands and feeds call dimos MCP tools as the config allows. Commands that
take longer than GetBody's 5 s reply limit run in the background: the command
replies "started" and the `task` feed reports how it went. A kill calls every
stop tool dimos offers and, if configured, checks odometry to confirm the
robot has stopped moving.
"""

from __future__ import annotations

import json
import math
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any

from .config import Command, Config, ParamError
from .mcp_client import McpClient, McpError, ToolResult
from .vendor.getbody_bridge import Robot

# Task states that are final.
DONE, FAILED, STOPPED, INTERRUPTED, TIMED_OUT = "done", "failed", "stopped", "interrupted", "timed_out"
RUNNING = "running"


class StartupError(RuntimeError):
    pass


@dataclass
class Task:
    """A background command: the dimos call runs in its own thread."""

    action: str
    params: dict
    invocation_id: str | None
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    state: str = RUNNING
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None
    result: str | None = None
    error: str | None = None
    done: threading.Event = field(default_factory=threading.Event)

    def finish(self, state: str, result: str | None = None, error: str | None = None) -> bool:
        """Set the final state once; later calls (e.g. the dimos reply after a kill) are ignored."""
        if self.done.is_set():
            return False
        self.state, self.result, self.error, self.ended_at = state, result, error, time.time()
        self.done.set()
        return True

    def view(self) -> dict:
        out = {"id": self.id, "action": self.action, "params": self.params, "state": self.state,
               "started_at": self.started_at, "ended_at": self.ended_at}
        if self.result is not None:
            out["result"] = self.result
        if self.error is not None:
            out["error"] = self.error
        return out


class DimosRobot(Robot):
    def __init__(self, config: Config, client: McpClient | None = None, motion=None, log=print):
        self.config = config
        self.client = client or McpClient(config.mcp_url, timeout_s=config.mcp_timeout_s)
        self.motion = motion          # optional MotionProbe (odometry) for a measured halt
        self.log = log
        self.stop_tools: list[str] = []
        self.invocation_id: str | None = None  # set by the bridge before each command
        self._task: Task | None = None
        self._last_pose: dict | None = None
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="dimos-call")
        self._pose_re = re.compile(config.pose_regex) if config.pose_regex else None

    # ---- startup
    def check(self) -> dict[str, dict]:
        """Make sure dimos is up and offers every tool the config uses.
        Returns dimos's tool list. Raises StartupError otherwise."""
        try:
            self.client.initialize()
            tools = self.client.list_tools()
        except (McpError, TimeoutError) as exc:
            raise StartupError(f"cannot reach dimos MCP at {self.config.mcp_url}: {exc}") from exc
        missing = sorted(self.config.tools_used() - set(tools))
        if missing:
            raise StartupError(f"dimos does not offer {missing}; is the right blueprint running?")
        self.stop_tools = [t for t in self.config.stop.tools if t in tools]
        if not self.stop_tools:
            raise StartupError(f"dimos offers none of the stop tools {self.config.stop.tools}")
        return tools

    # ---- commands
    def command(self, action: str, params: dict) -> dict:
        cmd = self.config.commands.get(action)
        if cmd is None:
            return _fault(f"Command {action!r} is not offered by this robot.")
        if cmd.builtin == "stop":
            return self._renter_stop()
        try:
            args = cmd.arguments(params or {})
        except ParamError as exc:
            return _fault(f"Refused: {exc}.")
        if cmd.mode == "background":
            return self._start_background(cmd, params or {}, args)
        return self._result(cmd, self._call(cmd.tool, args, self.config.mcp_timeout_s))

    def _call(self, tool: str, args: dict, timeout_s: float) -> ToolResult:
        try:
            return self.client.call_tool(tool, args, timeout_s=timeout_s)
        except TimeoutError as exc:
            return ToolResult(text=str(exc), is_error=True)
        except McpError as exc:
            return ToolResult(text=str(exc), is_error=True)

    def _faulted(self, cmd: Command, res: ToolResult) -> bool:
        return res.is_error or any(re.search(p, res.text) for p in cmd.fault_patterns)

    def _result(self, cmd: Command, res: ToolResult) -> dict:
        self._note_pose(res.text)
        if self._faulted(cmd, res):
            return _fault(res.text or "dimos reported an error.")
        return {"status": "ok", "result": res.text}

    def _note_pose(self, text: str) -> None:
        m = self._pose_re.search(text or "") if self._pose_re else None
        if m:
            self._last_pose = {"x": float(m["x"]), "y": float(m["y"]), "heading_deg": float(m["heading"]),
                               "at": time.time()}

    # ---- background commands
    def _start_background(self, cmd: Command, params: dict, args: dict) -> dict:
        with self._lock:
            if self._task and not self._task.done.is_set():
                return _fault(f"Busy: {self._task.action} is still running (task {self._task.id}). "
                              "Send stop first, or wait for the task feed to show it finished.")
            task = Task(action=cmd.name, params=params, invocation_id=self.invocation_id)
            self._task = task
        threading.Thread(target=self._run_task, args=(cmd, task, args), daemon=True,
                         name=f"task-{task.id}").start()
        threading.Thread(target=self._watch_task, args=(task, cmd.max_runtime_s), daemon=True).start()
        if task.done.wait(self.config.ack_after_s):
            if task.state == DONE:
                return {"status": "ok", "result": task.result, "task": task.view()}
            return _fault(task.error or task.result or task.state, task=task.view())
        return {"status": "ok", "state": "started", "task": task.view(),
                "note": "Still running; read the task feed for progress. Send stop to cancel."}

    def _run_task(self, cmd: Command, task: Task, args: dict) -> None:
        # The dimos call may outlive max_runtime_s by a little: the watchdog
        # stops the robot, and dimos then returns the call.
        res = self._call(cmd.tool, args, cmd.max_runtime_s + 10)
        self._note_pose(res.text)
        if self._faulted(cmd, res):
            task.finish(FAILED, result=res.text, error=res.text or "dimos reported an error.")
        else:
            task.finish(DONE, result=res.text)

    def _watch_task(self, task: Task, max_runtime_s: float) -> None:
        if task.done.wait(max_runtime_s):
            return
        if task.finish(TIMED_OUT, error=f"ran longer than {max_runtime_s}s; stopped by the bridge"):
            self.log(f"task {task.id} ({task.action}) hit its {max_runtime_s}s limit: stopping the robot")
            self._stop_all()

    def running_task(self) -> Task | None:
        task = self._task
        return task if task and not task.done.is_set() else None

    def _renter_stop(self) -> dict:
        task = self.running_task()
        if task:  # before the stop tools: dimos answers the cancelled call at once
            task.finish(STOPPED, error="stopped by the renter")
        stopped = self._stop_all()
        ok = self._stop_ok(stopped)
        out = {"status": "ok" if ok else "fault", "stop_tools": stopped}
        if task:
            out["task"] = task.view()
        if not ok:
            out["error"] = "a stop tool failed; see stop_tools"
        return out

    # ---- stopping
    def _stop_all(self) -> dict[str, str]:
        """Call every stop tool at once. Returns tool -> "ok" or the error."""
        timeout = self.config.stop.timeout_s
        futures = {self._pool.submit(self._call, t, {}, timeout): t for t in self.stop_tools}
        done, _ = wait(futures, timeout=timeout + 0.5)
        out = {}
        for fut, tool in futures.items():
            if fut not in done:
                out[tool] = f"no reply within {timeout}s"
                continue
            res = fut.result()
            out[tool] = f"error: {res.text}" if res.is_error else "ok"
        return out

    def _stop_ok(self, stopped: dict[str, str]) -> bool:
        required = self.config.stop.required or self.stop_tools
        return all(stopped.get(t) == "ok" for t in required if t in self.stop_tools)

    def stop(self, running) -> dict:
        """Kill: stop everything now and say truthfully whether the robot halted."""
        task = self.running_task()
        if task:
            task.finish(INTERRUPTED, error="interrupted by a kill")
        stopped = self._stop_all()
        state: dict[str, Any] = {"stop_tools": stopped}
        if task:
            state["task"] = task.view()
        if self.motion is not None:
            halted, detail = self.motion.wait_stopped()
            state["verified"] = True
            state["motion"] = detail
        else:
            halted = self._stop_ok(stopped)
            state["verified"] = False
            state["note"] = "halted means every required stop tool returned ok; motion was not measured"
        outcome = "partial" if (task or running) else "not_started"
        return {"halted": halted, "outcome": outcome, "state": state}

    # ---- feeds
    def feed(self, name: str) -> dict:
        feed = self.config.feeds.get(name)
        if feed is None:
            return _fault(f"Feed {name!r} is not offered by this robot.")
        if feed.kind == "task":
            task = self._task
            return {"task": task.view() if task else None, "last_pose": self._last_pose}
        if feed.kind == "odom":
            return self.motion.latest() if self.motion else _fault("odometry is not configured")
        res = self._call(feed.tool, {}, self.config.mcp_timeout_s)
        if res.is_error:
            return _fault(res.text or "dimos reported an error.")
        if feed.kind == "image":
            if not res.images:
                return _fault(f"{feed.tool} returned no image")
            image = res.images[0]
            if feed.max_bytes and len(image["data"]) > feed.max_bytes:
                return _fault(f"frame is {len(image['data'])} bytes, over the {feed.max_bytes} limit")
            return {"mime_type": image["mime_type"], "encoding": "base64", "data": image["data"], "at": time.time()}
        if feed.kind == "json":
            try:
                return {"value": json.loads(res.text)}
            except ValueError:
                return _fault(f"{feed.tool} did not return JSON")
        if feed.kind == "number":
            try:
                value = float(res.text)
                value = value if math.isfinite(value) else None
            except ValueError:
                value = None  # e.g. dimos returns "None" before the first reading
            return {"value": value, "available": value is not None}
        return {"value": res.text}


def _fault(error: str, **extra) -> dict:
    return {"status": "fault", "error": error, **extra}
