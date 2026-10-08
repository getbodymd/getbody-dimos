"""The robot side of the GetBody bridge, for a robot run by dimos.

Commands and feeds call dimos MCP tools as the config allows. Commands that
take longer than GetBody's 5 s reply limit run in the background: the command
replies "started" and the `task` feed reports how it went. A kill calls every
stop tool dimos offers and, if configured, checks odometry to confirm the
robot has stopped moving.
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from . import contract
from .config import Command, Config, ParamError
from .mcp_client import McpClient, McpError, ToolResult
from .vendor.getbody_bridge import Robot

if TYPE_CHECKING:
    from .motion import MotionProbe

log = logging.getLogger(__name__)

# Task states. Every state but RUNNING is final.
RUNNING, DONE, FAILED, STOPPED, INTERRUPTED, TIMED_OUT = "running", "done", "failed", "stopped", "interrupted", "timed_out"


class StartupError(RuntimeError):
    pass


@dataclass
class Task:
    """A background command: the dimos call runs in its own thread."""

    action: str
    params: dict[str, Any]
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

    def view(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "action": self.action, "params": self.params, "state": self.state,
                               "started_at": self.started_at, "ended_at": self.ended_at}
        if self.result is not None:
            out["result"] = self.result
        if self.error is not None:
            out["error"] = self.error
        return out


class DimosRobot(Robot):
    def __init__(self, config: Config, client: McpClient | None = None, motion: MotionProbe | None = None):
        self.config = config
        self.client = client or McpClient(config.mcp_url, timeout_s=config.mcp_timeout_s)
        self.motion = motion          # optional odometry probe for a measured halt
        self.stop_tools: list[str] = list(config.stop.tools)  # narrowed by check() to what dimos offers
        self.invocation_id: str | None = None  # set by the bridge before each command
        self._task: Task | None = None
        self._last_pose: dict[str, float] | None = None
        self._lock = threading.Lock()
        # Stop calls only, so feeds or a hung command can never hold up a kill.
        self._stop_pool = ThreadPoolExecutor(max_workers=max(4, 2 * len(config.stop.tools)),
                                             thread_name_prefix="dimos-stop")
        self._pose_re = re.compile(config.pose_regex) if config.pose_regex else None

    # ---- startup
    def check(self) -> dict[str, dict[str, Any]]:
        """Make sure dimos is up and its tools match the config.
        Returns dimos's tool list. Raises StartupError otherwise."""
        try:
            self.client.initialize()
            tools = self.client.list_tools()
        except (McpError, TimeoutError) as exc:
            raise StartupError(f"cannot reach dimos MCP at {self.config.mcp_url}: {exc}") from exc
        found = contract.problems(self.config, tools)
        if found:
            raise StartupError("the config does not match this dimos:\n  - " + "\n  - ".join(found)
                               + "\nIs the right blueprint running?")
        self.stop_tools = [t for t in self.config.stop.tools if t in tools]
        skipped = [t for t in self.config.stop.tools if t not in tools]
        if not self.stop_tools:
            raise StartupError(f"dimos offers none of the stop tools {self.config.stop.tools}")
        if skipped:
            log.warning("stop tools not offered by this dimos, skipped: %s", skipped)
        log.info("dimos at %s: %d tools; stop tools %s", self.config.mcp_url, len(tools), self.stop_tools)
        return tools

    # ---- commands
    def command(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        cmd = self.config.commands.get(action)
        if cmd is None:
            log.info("refused %s: not offered", action)
            return _fault(f"Command {action!r} is not offered by this robot.")
        if cmd.builtin == "stop":
            return self._renter_stop()
        try:
            args = cmd.arguments(params or {})
        except ParamError as exc:
            log.info("refused %s %s: %s", action, params, exc)
            return _fault(f"Refused: {exc}.")
        assert cmd.tool is not None
        if cmd.mode == "background":
            return self._start_background(cmd, params or {}, args)
        log.info("command %s -> %s(%s)", action, cmd.tool, args)
        return self._result(cmd, self._call(cmd.tool, args, self.config.mcp_timeout_s))

    def _call(self, tool: str, args: dict[str, Any], timeout_s: float) -> ToolResult:
        """Call a dimos tool. Never raises: failures come back as an error result."""
        try:
            return self.client.call_tool(tool, args, timeout_s=timeout_s)
        except (TimeoutError, McpError) as exc:
            log.warning("dimos %s failed: %s", tool, exc)
            return ToolResult(text=str(exc), is_error=True)
        except Exception as exc:  # a bug or an unexpected reply; still not fatal for the bridge
            log.exception("dimos %s failed unexpectedly", tool)
            return ToolResult(text=f"{type(exc).__name__}: {exc}", is_error=True)

    def _faulted(self, cmd: Command, res: ToolResult) -> bool:
        return res.is_error or any(re.search(p, res.text) for p in cmd.fault_patterns)

    def _result(self, cmd: Command, res: ToolResult) -> dict[str, Any]:
        self._note_pose(res.text)
        if self._faulted(cmd, res):
            log.warning("%s faulted: %s", cmd.name, res.text)
            return _fault(res.text or "dimos reported an error.")
        return {"status": "ok", "result": res.text}

    def _note_pose(self, text: str) -> None:
        m = self._pose_re.search(text or "") if self._pose_re else None
        if m:
            self._last_pose = {"x": float(m["x"]), "y": float(m["y"]), "heading_deg": float(m["heading"]),
                               "at": time.time()}

    # ---- background commands
    def _start_background(self, cmd: Command, params: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if self._task and not self._task.done.is_set():
                log.info("refused %s: %s (task %s) still running", cmd.name, self._task.action, self._task.id)
                return _fault(f"Busy: {self._task.action} is still running (task {self._task.id}). "
                              "Send stop first, or wait for the task feed to show it finished.")
            task = Task(action=cmd.name, params=params, invocation_id=self.invocation_id)
            self._task = task
        log.info("task %s: %s -> %s(%s), limit %ss", task.id, cmd.name, cmd.tool, args, cmd.max_runtime_s)
        threading.Thread(target=self._run_task, args=(cmd, task, args), daemon=True, name=f"task-{task.id}").start()
        threading.Thread(target=self._watch_task, args=(task, cmd.max_runtime_s), daemon=True,
                         name=f"watch-{task.id}").start()
        if task.done.wait(self.config.ack_after_s):
            if task.state == DONE:
                return {"status": "ok", "result": task.result, "task": task.view()}
            return _fault(task.error or task.result or task.state, task=task.view())
        return {"status": "ok", "state": "started", "task": task.view(),
                "note": "Still running; read the task feed for progress. Send stop to cancel."}

    def _run_task(self, cmd: Command, task: Task, args: dict[str, Any]) -> None:
        assert cmd.tool is not None and cmd.max_runtime_s is not None
        # The dimos call may outlive max_runtime_s by a little: the watchdog
        # stops the robot, and dimos then returns the call.
        res = self._call(cmd.tool, args, cmd.max_runtime_s + 10)
        self._note_pose(res.text)
        if self._faulted(cmd, res):
            finished = task.finish(FAILED, result=res.text, error=res.text or "dimos reported an error.")
        else:
            finished = task.finish(DONE, result=res.text)
        if finished:
            log.info("task %s: %s: %s", task.id, task.state, res.text)
        else:  # already stopped, interrupted or timed out; this is dimos's late reply
            log.info("task %s (%s): dimos replied after the end: %s", task.id, task.state, res.text)

    def _watch_task(self, task: Task, max_runtime_s: float) -> None:
        if task.done.wait(max_runtime_s):
            return
        if task.finish(TIMED_OUT, error=f"ran longer than {max_runtime_s}s; stopped by the bridge"):
            log.warning("task %s (%s) hit its %ss limit: stopping the robot", task.id, task.action, max_runtime_s)
            stopped = self._stop_all()
            if not self._stop_ok(stopped):
                log.error("task %s: stopping after the time limit FAILED %s; the robot may still be moving",
                          task.id, stopped)

    def running_task(self) -> Task | None:
        task = self._task
        return task if task and not task.done.is_set() else None

    def _renter_stop(self) -> dict[str, Any]:
        task = self.running_task()
        if task:  # before the stop tools: dimos answers the cancelled call at once
            task.finish(STOPPED, error="stopped by the renter")
        log.info("renter stop%s", f" (task {task.id})" if task else "")
        stopped = self._stop_all()
        ok = self._stop_ok(stopped)
        out: dict[str, Any] = {"status": "ok" if ok else "fault", "stop_tools": stopped}
        if task:
            out["task"] = task.view()
        if not ok:
            out["error"] = "a stop tool failed; see stop_tools"
            log.error("renter stop FAILED: %s", stopped)
        return out

    # ---- stopping
    def _stop_all(self) -> dict[str, str]:
        """Call every stop tool at once. Returns tool -> "ok" or what went wrong. Never raises."""
        timeout = self.config.stop.timeout_s
        futures: dict[Future[ToolResult], str] = {}
        out: dict[str, str] = {}
        for tool in self.stop_tools:
            try:
                futures[self._stop_pool.submit(self._call, tool, {}, timeout)] = tool
            except Exception as exc:  # e.g. the pool is shutting down
                out[tool] = f"not called: {type(exc).__name__}: {exc}"
        done, _ = wait(futures, timeout=timeout + 0.5)
        for fut, tool in futures.items():
            if fut not in done:
                out[tool] = f"no reply within {timeout}s"
                continue
            res = fut.result()
            out[tool] = f"error: {res.text}" if res.is_error else "ok"
        for tool, result in out.items():
            (log.info if result == "ok" else log.error)("stop tool %s: %s", tool, result)
        return out

    def _stop_ok(self, stopped: dict[str, str]) -> bool:
        required = [t for t in (self.config.stop.required or self.stop_tools) if t in self.stop_tools]
        return bool(required) and all(stopped.get(t) == "ok" for t in required)

    def stop(self, running: dict[str, Any] | None) -> dict[str, Any]:
        """Kill: stop everything now and say truthfully whether the robot halted. Never raises."""
        task = self.running_task()
        outcome = "partial" if (task or running) else "not_started"
        state: dict[str, Any] = {}
        halted = False
        try:
            if task:
                task.finish(INTERRUPTED, error="interrupted by a kill")
                state["task"] = task.view()
            log.warning("KILL: calling stop tools %s%s", self.stop_tools,
                        f"; interrupting task {task.id} ({task.action})" if task else "")
            stopped = self._stop_all()
            state["stop_tools"] = stopped
            if self.motion is not None:
                try:
                    halted, detail = self.motion.wait_stopped()
                except Exception as exc:
                    log.exception("KILL: odometry check failed")
                    halted, detail = False, {"error": f"odometry check failed: {type(exc).__name__}: {exc}"}
                state["verified"] = True
                state["motion"] = detail
            else:
                halted = self._stop_ok(stopped)
                state["verified"] = False
                state["note"] = "halted means every required stop tool returned ok; motion was not measured"
        except Exception as exc:
            log.exception("KILL: stopping failed")
            halted = False
            state["error"] = f"{type(exc).__name__}: {exc}"
        if halted:
            log.warning("KILL: halted (%s)", "measured" if state.get("verified") else "stop tools ok; not measured")
        else:
            log.critical("KILL: NOT HALTED; check the robot now. %s", state)
        return {"halted": halted, "outcome": outcome, "state": state}

    # ---- feeds
    def feed(self, name: str) -> dict[str, Any]:
        feed = self.config.feeds.get(name)
        if feed is None:
            return _fault(f"Feed {name!r} is not offered by this robot.")
        if feed.kind == "task":
            task = self._task
            return {"task": task.view() if task else None, "last_pose": self._last_pose}
        if feed.kind == "odom":
            return self.motion.latest() if self.motion else _fault("odometry is not configured")
        assert feed.tool is not None
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
            value: float | None
            try:
                value = float(res.text)
                value = value if math.isfinite(value) else None
            except ValueError:
                value = None  # dimos returns "None" before the first reading (and always, in MuJoCo, for battery)
            return {"value": value, "available": value is not None}
        return {"value": res.text}


def _fault(error: str, **extra: Any) -> dict[str, Any]:
    return {"status": "fault", "error": error, **extra}
