"""Optional: confirm a halt by reading odometry straight off the dimos bus.

dimos has no MCP tool that reports the robot's pose or velocity, so after a
kill the bridge can only say the stop tools returned ok. With this enabled,
kill_ack's `halted` is measured instead: the robot's speed from odometry has
to drop to near zero.

What dimos publishes (checked against dimos dc80d89):

- GO2Connection has `odom: Out[PoseStamped]`. Every `odom` stream in the
  getbody-dimos.unitree-go2-mcp blueprint is a PoseStamped, so the module
  coordinator names the channel `/odom` (module_coordinator._get_transport_for).
- Payload: the LCM encoding of geometry_msgs.PoseStamped
  (dimos/msgs/geometry_msgs/PoseStamped.py lcm_encode → dimos_lcm's
  PoseStamped.lcm_encode), on both transports.
- zenoh (dimos's default transport): key `dimos/odom/geometry_msgs.PoseStamped`
  (transport_factory.transport_topic + zenohpubsub.Topic.key_expr). dimos's
  sessions are peers that scout by multicast on the loopback interface only and
  listen on tcp/127.0.0.1 (protocol/service/zenohservice.py); this probe opens
  the same kind of session.
- LCM (DIMOS_TRANSPORT=lcm): channel `/odom#geometry_msgs.PoseStamped`
  (lcmpubsub.Topic.__str__) on LCM_DEFAULT_URL, by default
  udpm://239.255.76.67:7667?ttl=0 (protocol/service/lcmservice.py).

Speed is measured from receive times, not message stamps, so clock offsets
between processes don't matter.
"""

from __future__ import annotations

import json
import logging
import math
import os
import platform
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from .config import OdomConfig

log = logging.getLogger(__name__)

DIMOS_LCM_URL = "udpm://239.255.76.67:7667?ttl=0"
LOOPBACK_INTERFACE = "lo0" if platform.system() == "Darwin" else "lo"


def _yaw(qx: float, qy: float, qz: float, qw: float) -> float:
    return math.atan2(2 * (qw * qz + qx * qy), 1 - 2 * (qy * qy + qz * qz))


def _angle_diff(a: float, b: float) -> float:
    return (a - b + math.pi) % (2 * math.pi) - math.pi


def decode_pose(data: bytes) -> tuple[float, float, float]:
    """x, y, yaw from an LCM-encoded geometry_msgs.PoseStamped."""
    from dimos_lcm.geometry_msgs import PoseStamped

    msg = PoseStamped.lcm_decode(data)
    p, q = msg.pose.position, msg.pose.orientation
    return p.x, p.y, _yaw(q.x, q.y, q.z, q.w)


class MotionProbe:
    """Keeps recent odometry samples and judges whether the robot is still."""

    def __init__(self, config: OdomConfig, clock: Callable[[], float] = time.monotonic):
        self.config = config
        self.clock = clock
        self._samples: deque[tuple[float, float, float, float]] = deque(maxlen=400)
        self._lock = threading.Lock()
        self.received = 0
        self.decode_errors = 0

    def start(self) -> None:
        """Subclasses subscribe to the bus here."""

    def close(self) -> None:
        """Subclasses release the bus here."""

    def add(self, x: float, y: float, yaw: float, t: float | None = None) -> None:
        with self._lock:
            self._samples.append((self.clock() if t is None else t, x, y, yaw))
            self.received += 1

    def add_encoded(self, data: bytes) -> None:
        try:
            self.add(*decode_pose(data))
        except Exception:
            self.decode_errors += 1
            if self.decode_errors in (1, 100) or self.decode_errors % 1000 == 0:
                log.exception("could not decode odometry on %s (%d so far)", self.config.topic, self.decode_errors)

    def latest(self) -> dict[str, Any]:
        with self._lock:
            if not self._samples:
                return {"status": "fault", "error": f"no odometry received yet on {self.config.topic}"}
            t, x, y, yaw = self._samples[-1]
        return {"x": x, "y": y, "heading_deg": math.degrees(yaw), "age_s": round(self.clock() - t, 3),
                **self.motion()}

    def motion(self) -> dict[str, Any]:
        """Speed over the last window_s, from the oldest and newest samples in it."""
        now = self.clock()
        with self._lock:
            window = [s for s in self._samples if now - s[0] <= self.config.window_s]
        if not window:
            return {"speed": None, "yaw_rate": None, "fresh": False}
        newest, oldest = window[-1], window[0]
        fresh = now - newest[0] <= self.config.stale_s
        dt = newest[0] - oldest[0]
        if dt < self.config.window_s / 2:
            return {"speed": None, "yaw_rate": None, "fresh": fresh}
        speed = math.hypot(newest[1] - oldest[1], newest[2] - oldest[2]) / dt
        yaw_rate = abs(_angle_diff(newest[3], oldest[3])) / dt
        return {"speed": round(speed, 4), "yaw_rate": round(yaw_rate, 4), "fresh": fresh}

    def wait_stopped(self) -> tuple[bool, dict[str, Any]]:
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
                    m["error"] = f"no fresh odometry on {self.config.topic}; cannot confirm the robot stopped"
                elif m["speed"] is None:
                    m["error"] = "not enough odometry in the window to measure speed"
                else:
                    m["error"] = "still moving when the check timed out"
                return False, m
            time.sleep(0.05)


def zenoh_session_config(config: OdomConfig) -> dict[str, Any]:
    """The zenoh settings dimos's own sessions use by default
    (zenohservice.ZenohConfig + _zenoh_config), with this config's overrides."""
    settings: dict[str, Any] = {
        "mode": "peer",
        "scouting/multicast/enabled": True,
        "scouting/multicast/interface": LOOPBACK_INTERFACE,
        "scouting/gossip/enabled": True,
        "transport/shared_memory/enabled": False,
    }
    if config.zenoh_connect:
        settings["connect/endpoints"] = list(config.zenoh_connect)
    else:
        settings["listen/endpoints"] = ["tcp/127.0.0.1:0"]
    settings.update(config.zenoh_config or {})
    return settings


class ZenohMotionProbe(MotionProbe):
    def start(self) -> None:
        import zenoh

        zconfig = zenoh.Config()
        for key, value in zenoh_session_config(self.config).items():
            zconfig.insert_json5(key, json.dumps(value))
        self._session: Any = zenoh.open(zconfig)
        self._sub = self._session.declare_subscriber(
            self.config.topic, lambda sample: self.add_encoded(sample.payload.to_bytes()))
        log.info("odometry: subscribed to zenoh key %s", self.config.topic)

    def close(self) -> None:
        self._session.close()


class LcmMotionProbe(MotionProbe):
    def start(self) -> None:
        import lcm  # from lcm-dimos-fork, which dimos-lcm depends on

        url = self.config.lcm_url or os.environ.get("LCM_DEFAULT_URL") or DIMOS_LCM_URL
        self._lc = lcm.LCM(url)
        self._lc.subscribe(self.config.topic, lambda _channel, data: self.add_encoded(data))
        self._stop = threading.Event()
        threading.Thread(target=self._loop, daemon=True, name="odom-lcm").start()
        log.info("odometry: subscribed to LCM channel %s on %s", self.config.topic, url)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._lc.handle_timeout(100)
            except Exception:
                log.exception("LCM receive failed")
                time.sleep(0.5)

    def close(self) -> None:
        self._stop.set()


def make_probe(config: OdomConfig) -> MotionProbe:
    probe: MotionProbe = {"zenoh": ZenohMotionProbe, "lcm": LcmMotionProbe}[config.backend](config)
    probe.start()
    return probe
