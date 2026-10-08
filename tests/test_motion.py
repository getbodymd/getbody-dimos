import math

from getbody_dimos.config import OdomConfig
from getbody_dimos.motion import MotionProbe


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def probe(**kw):
    clock = Clock()
    cfg = OdomConfig(backend="zenoh", topic="dimos/odom/geometry_msgs.PoseStamped", timeout_s=0.0, **kw)
    return MotionProbe(cfg, clock=clock), clock


def feed(p, clock, vx=0.0, wz=0.0, seconds=1.0, hz=10):
    x = yaw = 0.0
    for _ in range(int(seconds * hz)):
        clock.t += 1 / hz
        x += vx / hz
        yaw += wz / hz
        p.add(x, 0.0, yaw)


def test_still_robot_is_stopped():
    p, clock = probe()
    feed(p, clock)
    stopped, detail = p.wait_stopped()
    assert stopped and detail["speed"] == 0.0


def test_moving_robot_is_not_stopped():
    p, clock = probe()
    feed(p, clock, vx=0.3)
    stopped, detail = p.wait_stopped()
    assert not stopped and math.isclose(detail["speed"], 0.3, rel_tol=0.05)


def test_turning_in_place_is_not_stopped():
    p, clock = probe()
    feed(p, clock, wz=0.5)
    assert not p.wait_stopped()[0]


def test_stale_odometry_cannot_confirm_a_stop():
    p, clock = probe()
    feed(p, clock)
    clock.t += 5
    stopped, detail = p.wait_stopped()
    assert not stopped and "no fresh odometry" in detail["error"]


def test_no_odometry_at_all():
    p, _ = probe()
    assert not p.wait_stopped()[0]
    assert p.latest()["status"] == "fault"
