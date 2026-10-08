"""Optional: confirm a halt by reading odometry straight off the dimos bus.

dimos has no MCP tool that reports the robot's pose or velocity, so after a
kill the bridge can only say the stop tools returned ok. With this enabled,
kill_ack's `halted` is measured instead: the robot's speed from odometry has
to drop to near zero.

dimos publishes odometry as an LCM-encoded geometry_msgs.PoseStamped. Its
default transport is zenoh (key `dimos/odom/geometry_msgs.PoseStamped`); with
DIMOS_TRANSPORT=lcm it is the LCM channel `/odom#geometry_msgs.PoseStamped`.
Decoding needs the `dimos-lcm` message package, not dimos itself.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque

from .config import OdomConfig


def _yaw(qx: float, qy: float, qz: float, qw: float) -> float:
    return math.atan2(2 * (qw * qz + qx * qy), 1 - 2 * (qy * qy + qz * qz))


def _angle_diff(a: float, b: float) -> float:
    return (a - b + math.pi) % (2 * math.pi) - math.pi


class MotionProbe:
    """Keeps recent odometry samples and judges whether the robot is still."""

    def __init__(self, config: OdomConfig, clock=time.monotonic):
        self.config = config
        self.clock = clock
        self._samples: deque[tuple[float, float, float, float]] = deque(maxlen=400)
        self._lock = threading.Lock()

    def start(self) -> None:
        """Subclasses subscribe to the bus here."""

    def add(self, x: float, y: float, yaw: float, t: float | None = None) -> None:
        with self._lock:
            self._samples.append((self.clock() if t is None else t, x, y, yaw))

    def add_encoded(self, data: bytes) -> None:
        from dimos_lcm.geometry_msgs import PoseStamped

        decode = getattr(PoseStamped, "lcm_decode", None) or PoseStamped.decode
        msg = decode(data)
        p, q = msg.pose.position, msg.pose.orientation
        self.add(p.x, p.y, _yaw(q.x, q.y, q.z, q.w))

    def latest(self) -> dict:
        with self._lock:
            if not self._samples:
                return {"status": "fault", "error": "no odometry received yet"}
            t, x, y, yaw = self._samples[-1]
        return {"x": x, "y": y, "heading_deg": math.degrees(yaw), "age_s": round(self.clock() - t, 3),
                **self.motion()}

    def motion(self) -> dict:
        """Speed over the last window_s, from the oldest and newest samples in it."""
        now = self.clock()
        with self._lock:
            window = [s for s in self._samples if now - s[0] <= self.config.window_s]
        if not window:
            return {"speed": None, "yaw_rate": None, "fresh": False}
        newest = window[-1]
        fresh = now - newest[0] <= self.config.stale_s
        oldest = window[0]
        dt = newest[0] - oldest[0]
        if dt < self.config.window_s / 2:
            return {"speed": None, "yaw_rate": None, "fresh": fresh}
        speed = math.hypot(newest[1] - oldest[1], newest[2] - oldest[2]) / dt
        yaw_rate = abs(_angle_diff(newest[3], oldest[3])) / dt
        return {"speed": round(speed, 4), "yaw_rate": round(yaw_rate, 4), "fresh": fresh}

    def wait_stopped(self) -> tuple[bool, dict]:
        """Block until the robot is measured still, or timeout_s passes."""
        deadline = self.clock() + self.config.timeout_s
        while True:
            m = self.motion()
            still = (m["fresh"] and m["speed"] is not None and m["speed"] <= self.config.max_speed
                     and m["yaw_rate"] <= self.config.max_yaw_rate)
            if still:
                return True, m
            if self.clock() >= deadline:
                if not m["fresh"]:
                    m["error"] = "no fresh odometry; cannot confirm the robot stopped"
                return False, m
            time.sleep(0.05)


class ZenohMotionProbe(MotionProbe):
    def start(self) -> None:
        import zenoh

        self._session = zenoh.open(zenoh.Config())
        self._sub = self._session.declare_subscriber(
            self.config.topic, lambda sample: self.add_encoded(sample.payload.to_bytes()))


class LcmMotionProbe(MotionProbe):
    def start(self) -> None:
        import lcm

        self._lc = lcm.LCM()
        self._lc.subscribe(self.config.topic, lambda _channel, data: self.add_encoded(data))
        threading.Thread(target=self._loop, daemon=True, name="odom-lcm").start()

    def _loop(self) -> None:
        while True:
            self._lc.handle_timeout(100)


def make_probe(config: OdomConfig) -> MotionProbe:
    probe = {"zenoh": ZenohMotionProbe, "lcm": LcmMotionProbe}[config.backend](config)
    probe.start()
    return probe
