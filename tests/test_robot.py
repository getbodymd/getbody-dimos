import time

import pytest

from getbody_dimos.config import parse
from getbody_dimos.mcp_client import McpClient
from getbody_dimos.robot import DimosRobot, StartupError

from .fake_dimos import JPEG, FakeDimos

RAW = {
    "ack_after_s": 0.3,
    "pose_regex": r"Robot is at x=(?P<x>-?[\d.]+) y=(?P<y>-?[\d.]+) heading=(?P<heading>-?[\d.]+)deg",
    "commands": {
        "move": {
            "tool": "move_to", "mode": "background", "max_runtime_s": 5,
            "fixed": {"relative": True},
            "params": {"x": {"type": "number", "min": -1.0, "max": 1.0, "default": 0.0},
                       "y": {"type": "number", "min": -1.0, "max": 1.0, "default": 0.0}},
            "fault_patterns": ["cancelled or failed", "timed out"],
        },
        "tag_location": {
            "tool": "tag_location",
            "params": {"location_name": {"type": "string", "required": True, "max_length": 32, "pattern": "[a-z ]+"}},
            "example": {"location_name": "desk"},
        },
        "stop": {"builtin": "stop"},
    },
    "feeds": {
        "camera": {"kind": "image", "tool": "observe"},
        "battery": {"kind": "number", "tool": "get_battery_soc"},
        "status": {"kind": "json", "tool": "server_status"},
        "task": {"kind": "task"},
    },
    "stop": {"tools": ["stop_navigation", "end_exploration", "stop_patrol"], "required": ["stop_navigation"],
             "timeout_s": 1.0},
}


@pytest.fixture
def dimos():
    with FakeDimos(move_duration_s=1.0, start_delay_s=0.0, settle_s=0.2) as fake:
        yield fake


def make_robot(dimos, raw=RAW, motion=None):
    cfg = parse(raw)
    cfg.mcp_url = dimos.url
    robot = DimosRobot(cfg, McpClient(dimos.url, timeout_s=cfg.mcp_timeout_s), motion=motion)
    robot.check()
    return robot


def test_check_skips_stop_tools_dimos_does_not_offer(dimos):
    raw = {**RAW, "stop": {**RAW["stop"], "tools": RAW["stop"]["tools"] + ["stop_following"]}}
    robot = make_robot(dimos, raw)
    assert robot.stop_tools == ["stop_navigation", "end_exploration", "stop_patrol"]  # no PersonFollow module


def test_check_refuses_missing_tools(dimos):
    raw = {**RAW, "commands": {**RAW["commands"], "dance": {"tool": "dance"}}}
    with pytest.raises(StartupError, match="does not offer tool 'dance'"):
        make_robot(dimos, raw)


def test_out_of_range_is_refused_before_dimos_is_called(dimos):
    robot = make_robot(dimos)
    result = robot.command("move", {"x": 2.0})
    assert result["status"] == "fault" and "above the limit" in result["error"]
    assert dimos.count("move_to") == 0


def test_unknown_command_is_refused(dimos):
    assert make_robot(dimos).command("backflip", {})["status"] == "fault"


def test_sync_command(dimos):
    result = make_robot(dimos).command("tag_location", {"location_name": "desk"})
    assert dimos.calls[-1] == ("tag_location", {"location_name": "desk"})
    assert result == {"status": "ok", "result": "Tagged 'desk': (0.0,0.0)."}


def test_background_command_replies_started_then_task_feed_shows_done(dimos):
    robot = make_robot(dimos)
    t0 = time.monotonic()
    result = robot.command("move", {"x": 0.5})
    assert time.monotonic() - t0 < 1.0
    assert result["status"] == "ok" and result["state"] == "started"
    assert dimos.calls[-1] == ("move_to", {"x": 0.5, "y": 0.0, "relative": True})
    assert robot.feed("task")["task"]["state"] == "running"
    time.sleep(1.2)
    feed = robot.feed("task")
    assert feed["task"]["state"] == "done"
    assert feed["last_pose"]["x"] == 0.5


def test_fast_background_command_replies_with_its_result(dimos):
    dimos.move_duration_s = 0.05
    result = make_robot(dimos).command("move", {"x": 0.1})
    assert result["status"] == "ok" and result["result"].startswith("Navigation goal reached")


def test_second_move_is_refused_while_one_runs(dimos):
    robot = make_robot(dimos)
    robot.command("move", {"x": 0.5})
    result = robot.command("move", {"x": 0.5})
    assert result["status"] == "fault" and "Busy" in result["error"]
    assert dimos.count("move_to") == 1


def test_renter_stop_cancels_the_task(dimos):
    robot = make_robot(dimos)
    robot.command("move", {"x": 0.5})
    result = robot.command("stop", {})
    assert result["status"] == "ok"
    assert result["stop_tools"] == {"stop_navigation": "ok", "end_exploration": "ok", "stop_patrol": "ok"}
    assert result["task"]["state"] == "stopped"
    time.sleep(0.3)
    assert not dimos.is_moving()


def test_watchdog_stops_a_task_that_runs_too_long(dimos):
    dimos.move_duration_s = 10
    raw = {**RAW, "commands": {**RAW["commands"], "move": {**RAW["commands"]["move"], "max_runtime_s": 0.5}}}
    robot = make_robot(dimos, raw)
    robot.command("move", {"x": 0.5})
    time.sleep(1.0)
    assert robot.feed("task")["task"]["state"] == "timed_out"
    assert dimos.count("stop_navigation") == 1
    assert not dimos.is_moving()


def test_kill_interrupts_the_task_and_halts(dimos):
    robot = make_robot(dimos)
    robot.command("move", {"x": 0.5})
    out = robot.stop(None)
    assert out["halted"] is True
    assert out["outcome"] == "partial"
    assert out["state"]["verified"] is False
    assert out["state"]["task"]["state"] == "interrupted"
    time.sleep(0.3)
    assert not dimos.is_moving()
    # dimos's late "cancelled" reply does not overwrite the interrupted state
    assert robot.feed("task")["task"]["state"] == "interrupted"


def test_kill_with_nothing_running(dimos):
    out = make_robot(dimos).stop(None)
    assert out["halted"] is True and out["outcome"] == "not_started"


def test_kill_reports_not_halted_when_stop_fails(dimos):
    robot = make_robot(dimos)
    dimos.fail_stop = True
    out = robot.stop(None)
    assert out["halted"] is False
    assert out["state"]["stop_tools"]["stop_navigation"].startswith("error")


def test_kill_reports_not_halted_when_stop_hangs(dimos):
    robot = make_robot(dimos)
    dimos.stop_delay_s = 3
    t0 = time.monotonic()
    out = robot.stop(None)
    assert time.monotonic() - t0 < 2.5
    assert out["halted"] is False


class FakeMotion:
    def __init__(self, stopped):
        self.stopped = stopped

    def wait_stopped(self):
        return self.stopped, {"speed": 0.0 if self.stopped else 0.4}

    def latest(self):
        return {"x": 0, "y": 0}


def test_kill_with_odometry_reports_measured_halt(dimos):
    out = make_robot(dimos, motion=FakeMotion(stopped=False)).stop(None)
    assert out["halted"] is False and out["state"]["verified"] is True
    out = make_robot(dimos, motion=FakeMotion(stopped=True)).stop(None)
    assert out["halted"] is True


def test_feeds(dimos):
    robot = make_robot(dimos)
    camera = robot.feed("camera")
    assert camera["mime_type"] == "image/jpeg" and camera["data"] == JPEG
    assert robot.feed("battery") == {"value": None, "available": False}
    assert robot.feed("status")["value"]["pid"] == 4242
    assert robot.feed("task") == {"task": None, "last_pose": None}
    assert robot.feed("lidar")["status"] == "fault"


def test_dimos_down_gives_a_fault_not_an_exception(dimos):
    robot = make_robot(dimos)
    robot.client.url = "http://127.0.0.1:9/mcp"
    robot.client.timeout_s = 0.5
    assert robot.command("tag_location", {"location_name": "desk"})["status"] == "fault"
    assert robot.feed("camera")["status"] == "fault"


def test_kill_also_ends_exploration(dimos):
    """stop_navigation alone would leave the explorer picking new goals."""
    robot = make_robot(dimos)
    robot.client.call_tool("begin_exploration")
    assert dimos.exploring
    out = robot.stop(None)
    assert out["halted"] is True
    assert out["state"]["stop_tools"]["end_exploration"] == "ok"
    assert not dimos.is_moving()


def test_busy_capability_refusal_is_a_fault(dimos):
    raw = {**RAW, "commands": {**RAW["commands"], "goto": {
        "tool": "navigate_with_text",
        "params": {"query": {"type": "string", "required": True, "max_length": 40, "pattern": "[a-z ]+"}},
        "example": {"query": "kitchen"}}}}
    robot = make_robot(dimos, raw)
    robot.client.call_tool("begin_exploration")  # holds the movement capability
    result = robot.command("goto", {"query": "kitchen"})
    assert result["status"] == "fault"
    assert "capability 'movement' is held by 'begin_exploration'" in result["error"]


def test_camera_timeout_is_a_fault(dimos):
    dimos.camera_running = False
    result = make_robot(dimos).feed("camera")
    assert result["status"] == "fault" and "No camera frame received" in result["error"]


def test_kill_survives_a_broken_odometry_check(dimos):
    class Broken(FakeMotion):
        def wait_stopped(self):
            raise RuntimeError("bus down")

    out = make_robot(dimos, motion=Broken(stopped=True)).stop(None)
    assert out["halted"] is False
    assert "bus down" in out["state"]["motion"]["error"]


def test_kill_with_no_required_stop_tool_offered_is_not_halted(dimos):
    robot = make_robot(dimos)
    robot.stop_tools = ["end_exploration"]  # stop_navigation (required) missing
    assert robot.stop(None)["halted"] is False
