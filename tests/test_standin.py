"""GetBody's own stand-in checklist, run against DimosBridge and a fake dimos.

The stand-in asks a human y/n questions; here the answers come from what the
fake dimos actually saw (how many times move_to ran, whether it is moving).
"""

import asyncio
import socket
import threading
import time

from getbody_dimos.bridge import DimosBridge
from getbody_dimos.config import load
from getbody_dimos.mcp_client import McpClient
from getbody_dimos.robot import DimosRobot
from getbody_dimos.vendor.getbody_bridge import StandIn

from .fake_dimos import FakeDimos

CONFIG = "examples/unitree-go2/config.yaml"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_standin_checklist_passes():
    with FakeDimos(move_duration_s=1.5, start_delay_s=0.0, settle_s=0.3) as dimos:
        cfg = load(CONFIG)
        robot = DimosRobot(cfg, McpClient(dimos.url, timeout_s=cfg.mcp_timeout_s))
        robot.check()
        port = free_port()
        bridge = DimosBridge(robot, f"ws://127.0.0.1:{port}", log=lambda *_: None)
        log, moves_before = [], {}

        def wait_idle():
            deadline = time.monotonic() + 5
            while robot.running_task() and time.monotonic() < deadline:
                time.sleep(0.05)

        def ask(question):
            log.append(question)
            if question.startswith("About to send") or question.startswith("Next:"):
                wait_idle()
                moves_before["n"] = dimos.count("move_to")
                return True
            if "Did the robot do that correctly" in question:
                return '"status": "ok"' in question
            if "same invocation_id" in question:
                return dimos.count("move_to") == moves_before["n"]
            if "Is that real data" in question:
                return '"status": "fault"' not in question
            if "stop at once" in question:
                time.sleep(0.3)
                return not dimos.is_moving()
            if "stay still" in question:
                time.sleep(0.3)
                ok = not dimos.is_moving() and dimos.count("move_to") == moves_before["n"] + 1
                bridge.request_rearm()   # the human types `rearm`
                return ok
            raise AssertionError(f"unexpected question: {question}")

        standin = StandIn(cfg.plan(), ask=ask, log=log.append)
        threading.Thread(target=lambda: asyncio.run(bridge.run_forever()), daemon=True).start()
        asyncio.run(asyncio.wait_for(standin.serve(port=port), timeout=60))
        bridge.close()

        failed = [r for r in standin.results if not r[1]]
        assert not failed, "\n".join(log)
        names = [r[0] for r in standin.results]
        assert names == ["connects and heartbeats", "command move", "command turn", "command stop",
                         "command tag_location", "repeated invocation_id runs once", "feed camera", "feed task",
                         "kill stops the robot and acks", "nothing runs while halted", "re-arm"]
        kill = next(r for r in standin.results if r[0] == "kill stops the robot and acks")
        assert '"halted": true' in kill[2]
        assert '"action": "move"' in kill[2] and '"outcome": "partial"' in kill[2]
