"""The odometry probe against a real zenoh session publishing the way dimos does:
an LCM-encoded geometry_msgs.PoseStamped on dimos/odom/geometry_msgs.PoseStamped.

Needs eclipse-zenoh and dimos-lcm (`pip install 'getbody-dimos[odom-zenoh]'`);
skipped otherwise. This checks the probe's zenoh and decoding code, not that a
running dimos publishes there (see README: Not yet tested on real dimos).
"""

import json
import socket
import threading
import time

import pytest

from getbody_dimos.config import DIMOS_ODOM_TOPICS, OdomConfig
from getbody_dimos.motion import make_probe, zenoh_session_config

zenoh = pytest.importorskip("zenoh")
geometry_msgs = pytest.importorskip("dimos_lcm.geometry_msgs")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def dimos_like_publisher(port):
    """A peer session like dimos's (zenohservice._zenoh_config), listening on a known port."""
    cfg = zenoh.Config()
    for key, value in {"mode": "peer", "listen/endpoints": [f"tcp/127.0.0.1:{port}"],
                       "scouting/multicast/enabled": False, "transport/shared_memory/enabled": False}.items():
        cfg.insert_json5(key, json.dumps(value))
    session = zenoh.open(cfg)
    return session, session.declare_publisher(DIMOS_ODOM_TOPICS["zenoh"])


def encode(x, y):
    msg = geometry_msgs.PoseStamped()
    msg.header.frame_id = "world"
    msg.pose.position.x, msg.pose.position.y = x, y
    msg.pose.orientation.w = 1.0
    return msg.lcm_encode()


def test_probe_measures_speed_from_zenoh_odometry():
    port = free_port()
    session, pub = dimos_like_publisher(port)
    probe = make_probe(OdomConfig(backend="zenoh", topic=DIMOS_ODOM_TOPICS["zenoh"], timeout_s=1.0,
                                  zenoh_connect=[f"tcp/127.0.0.1:{port}"],
                                  zenoh_config={"scouting/multicast/enabled": False}))
    speed = {"v": 0.4}
    stop = threading.Event()

    def publish():
        x = 0.0
        while not stop.is_set():
            x += speed["v"] / 20
            pub.put(encode(x, 0.0))
            time.sleep(0.05)

    threading.Thread(target=publish, daemon=True).start()
    try:
        deadline = time.monotonic() + 5
        while probe.received < 15 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert probe.received >= 15, "no odometry arrived over zenoh"
        assert probe.decode_errors == 0
        moving, detail = probe.wait_stopped()
        assert not moving and detail["speed"] == pytest.approx(0.4, rel=0.25)

        speed["v"] = 0.0
        stopped, detail = probe.wait_stopped()
        assert stopped and detail["speed"] < 0.05
        assert probe.latest()["x"] > 0
    finally:
        stop.set()
        probe.close()
        session.close()


def test_default_session_mirrors_dimos():
    settings = zenoh_session_config(OdomConfig(backend="zenoh", topic=DIMOS_ODOM_TOPICS["zenoh"]))
    assert settings["mode"] == "peer"
    assert settings["listen/endpoints"] == ["tcp/127.0.0.1:0"]
    assert settings["scouting/multicast/enabled"] is True
    assert settings["scouting/multicast/interface"] in ("lo", "lo0")
    assert settings["scouting/gossip/enabled"] is True
    assert settings["transport/shared_memory/enabled"] is False
