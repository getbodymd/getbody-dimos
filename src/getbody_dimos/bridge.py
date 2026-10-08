"""GetBody's bridge with two additions for background commands.

The vendored Bridge only knows about the command running inside
robot.command(). Here a long command replies "started" and keeps going in the
background, so on a kill that background task is the one interrupted: this
subclass hands it to the stock kill handling, so kill_ack reports it under
`interrupted` with the outcome from DimosRobot.stop() ("partial").
"""

from __future__ import annotations

from typing import Any

from .robot import DimosRobot
from .vendor.getbody_bridge import Bridge


class DimosBridge(Bridge):
    robot: DimosRobot

    def _run_command(self, inv: str, action: str, params: dict[str, Any]) -> dict[str, Any]:
        # Commands run one at a time, so this is the invocation robot.command() sees.
        self.robot.invocation_id = inv
        try:
            return super()._run_command(inv, action, params)
        finally:
            self.robot.invocation_id = None

    async def _on_kill(self, ws: Any, frame: dict[str, Any]) -> None:
        borrowed = False
        if self._running is None:
            task = self.robot.running_task()
            if task is not None:
                self._running = {"invocation_id": task.invocation_id, "action": task.action, "params": task.params}
                borrowed = True
        try:
            await super()._on_kill(ws, frame)
        finally:
            if borrowed:
                self._running = None
