"""GetBody control bridge: the GetBody side of a robot's connection, ready-made.

Download: https://getbody.md/bridge/getbody_bridge.py   (needs Python 3.10+,
`pip install websockets cryptography`).

A robot is listed on GetBody by its owner's own process holding the body-interface
WebSocket (wss://getbody.md/getbody/ws/bodies/<id>/interface) and acting on what
arrives there. This file does everything on the GetBody side of that, the same for
every robot:

- signs the connection as the body's owner agent (DID / Ed25519),
- reconnects after drops, heartbeats twice a second,
- runs renter commands one at a time and replies with command_result,
- never runs the same invocation_id twice (a renter retry gets the first result),
- answers feed reads with feed_result,
- on a kill: stops the robot at once, replies kill_ack, and refuses every
  command until a human re-arms it (type `rearm` in the bridge's terminal),
- teleop (direct real-time control), if your listing offers it: hands your robot
  only the latest frame (never a backlog), streams back the state you return, and
  holds the robot when the stream stops, including its own watchdog if frames stop
  arriving at all (network loss).

You write only the robot side: a subclass of `Robot` with three methods (five
if you offer teleop).

    from getbody_bridge import Robot, main

    class MyRobot(Robot):
        def command(self, action, params):
            # Do it on the robot. Enforce your safe limits here: refuse
            # anything outside them with {"status": "fault", "error": "..."}.
            return {"status": "ok"}

        def feed(self, name):
            return {"battery": 0.82}          # the value renters read

        def stop(self, running):
            # Go to a safe state NOW. `running` is the command in progress
            # ({"action", "params"}) or None. Must not wait on that command.
            return {"halted": True, "outcome": "partial", "state": {}}

        # Only if you offer teleop:
        def teleop(self, action, params):
            # Apply one direct-control frame NOW (e.g. set velocities), clamped to your
            # safe limits. Called up to ~50 times a second; must return quickly.
            return {"pose": [0, 0, 0]}        # optional state streamed back to the renter

        def hold(self, reason):
            # Stop moving and hold still: the stream stopped (deadman, renter stop,
            # session closed, or this bridge's watchdog). Not a kill: teleop may resume.
            pass

    if __name__ == "__main__":
        main(MyRobot())

Then:

    python my_robot.py standin --plan plan.json     # terminal 1: local stand-in for GetBody
    python my_robot.py run --url ws://127.0.0.1:8765 # terminal 2: the bridge, against it
    python my_robot.py run --body-id 42 --key agent_key.json   # for real, once approved

`standin` walks through the pre-listing checklist with a human watching the robot:
heartbeats, every command in the plan, a repeated invocation_id, every feed, teleop
(streaming, holding when the stream stops, teleop_stop) if the plan has it, a kill
(and that nothing runs until re-armed). plan.json:

    {"commands": [{"action": "move", "params": {"vx": 0.05}}], "feeds": ["battery"],
     "teleop": {"action": "drive", "params": {"vx": 0.05}}}

agent_key.json: {"private_key_hex": "<64 hex chars: your agent's Ed25519 private key>"}
"""
import argparse
import asyncio
import hashlib
import json
import os
import secrets
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

__version__ = "1.1.0"

HEARTBEAT_EVERY_S = 0.5
RECONNECT_MAX_S = 30
COMMAND_DEADLINE_S = 5  # GetBody's default relay timeout; slower replies are lost
TELEOP_WATCHDOG_S = 0.5  # hold the robot if teleop frames stop for this long, whatever GetBody says
TELEOP_STATE_MAX_HZ = 20
_SEEN_LIMIT = 2000


class Robot:
    """The robot side. Subclass it; every method runs off the network thread, so
    blocking calls are fine (but a command must reply within 5 seconds)."""

    def command(self, action: str, params: dict) -> dict:
        return {"status": "fault", "error": f"Command {action!r} is not implemented."}

    def feed(self, name: str) -> dict:
        return {"status": "fault", "error": f"Feed {name!r} is not implemented."}

    def stop(self, running) -> dict:
        """Called on a kill, while a command may still be running. Return
        {"halted": bool, "outcome": "completed"|"partial"|"not_started", "state": {...}}."""
        raise NotImplementedError("stop() must put the robot in a safe state.")

    def teleop(self, action: str, params: dict):
        """Teleop only: apply one direct-control frame now, within your safe limits.
        Only the latest frame is passed (older ones are dropped); keep it fast. Return
        a small state dict to stream back to the renter, or None."""
        raise NotImplementedError("This robot doesn't offer teleop.")

    def hold(self, reason: str) -> None:
        """Teleop only: stop moving and hold still (the stream stopped). Not a kill.
        A robot that offers teleop MUST implement this."""


# --------------------------------------------------------------------------
# signing (same canonical form GetBody verifies, getbody/ws_auth.py)
# --------------------------------------------------------------------------

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58(data: bytes) -> str:
    n, out = int.from_bytes(data, "big"), ""
    while n:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    return "1" * (len(data) - len(data.lstrip(b"\x00"))) + out


class AgentKey:
    def __init__(self, private_key_hex: str):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        self._key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(private_key_hex))
        public = self._key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.agent_id = "did:key:z" + _b58(bytes([0xED, 0x01]) + public)

    @classmethod
    def load(cls, path: str) -> "AgentKey":
        with open(path, encoding="utf-8") as f:
            return cls(json.load(f)["private_key_hex"])

    def ws_headers(self, path: str) -> dict:
        nonce = secrets.token_hex(16)
        expiry = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        canonical = f"WS-CONNECT\n{path}\n{nonce}\n{expiry}\n{hashlib.sha256(b'').hexdigest()}".encode()
        return {
            "X-Getbody-Agent-Id": self.agent_id, "X-Getbody-Nonce": nonce,
            "X-Getbody-Expiry": expiry, "X-Getbody-Signature": self._key.sign(canonical).hex(),
        }


async def _connect(url, headers):
    import websockets

    try:
        return await websockets.connect(url, additional_headers=headers)
    except TypeError:  # websockets < 13
        return await websockets.connect(url, extra_headers=headers)


# --------------------------------------------------------------------------
# the bridge
# --------------------------------------------------------------------------

class Bridge:
    def __init__(self, robot: Robot, url: str, key: AgentKey = None, path: str = "", state_file: str = None,
                 log=print):
        self.robot, self.url, self.key, self.path, self.log = robot, url, key, path, log
        self.state_file = state_file
        self.halted = False
        self.rearm_requested = False
        self._commands = ThreadPoolExecutor(max_workers=1, thread_name_prefix="getbody-command")
        self._other = ThreadPoolExecutor(max_workers=4, thread_name_prefix="getbody-io")
        self._lock = threading.Lock()
        self._results = {}      # invocation_id -> result of a finished command
        self._inflight = {}     # invocation_id -> asyncio.Future of a running/queued command
        self._running = None    # {"invocation_id", "action", "params"} while robot.command runs
        self._teleop = ThreadPoolExecutor(max_workers=1, thread_name_prefix="getbody-teleop")
        self._teleop_latest = None   # newest frame not yet handed to the robot
        self._teleop_busy = False
        self._teleop_active = False  # streaming, so the watchdog applies
        self._teleop_seq = None
        self._last_teleop = 0.0
        self._last_state_sent = 0.0
        self._ws = None
        self._stopping = False
        self._load_state()

    # ---- persistence of finished invocation results (dedup across restarts)
    def _load_state(self):
        if self.state_file and os.path.exists(self.state_file):
            with open(self.state_file, encoding="utf-8") as f:
                self._results = json.load(f).get("results", {})

    def _save_state(self):
        if not self.state_file:
            return
        items = list(self._results.items())[-_SEEN_LIMIT:]
        self._results = dict(items)
        tmp = self.state_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"results": self._results}, f)
        os.replace(tmp, self.state_file)

    # ---- connection loop
    async def run_forever(self):
        delay = 1
        while not self._stopping:
            try:
                headers = self.key.ws_headers(self.path) if self.key else {}
                ws = await _connect(self.url, headers)
                delay = 1
                await self._session(ws)
            except Exception as exc:  # network errors, server closes
                if self._stopping:
                    break
                self.log(f"connection lost ({exc}); retrying in {delay}s")
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_MAX_S)

    async def _session(self, ws):
        self._ws = ws
        beat = asyncio.create_task(self._heartbeat(ws))
        watchdog = asyncio.create_task(self._teleop_watchdog(ws))
        try:
            async for raw in ws:
                try:
                    frame = json.loads(raw)
                except ValueError:
                    continue
                await self._on_frame(ws, frame)
        finally:
            beat.cancel()
            watchdog.cancel()
            self._ws = None
            if self._teleop_active:
                await self._hold(None, "disconnected")

    async def _heartbeat(self, ws):
        while True:
            await ws.send(json.dumps({"type": "heartbeat"}))
            if self.rearm_requested:
                self.rearm_requested = False
                await ws.send(json.dumps({"type": "rearm"}))
            await asyncio.sleep(HEARTBEAT_EVERY_S)

    async def _on_frame(self, ws, frame):
        kind = frame.get("type")
        if kind == "heartbeat_ack":
            return
        if frame.get("status") == "connected":
            self.halted = bool(frame.get("halted"))
            self.log(f"connected (body {frame.get('body_id')})" + ("; HALTED: type `rearm` once it is safe" if self.halted else ""))
            return
        if kind == "command":
            asyncio.create_task(self._on_command(ws, frame))
        elif kind == "read_feed":
            asyncio.create_task(self._on_read_feed(ws, frame))
        elif kind == "teleop":
            self._on_teleop(ws, frame)
        elif kind == "teleop_stop":
            await self._hold(ws, frame.get("reason") or "stop")
        elif kind == "kill":
            await self._on_kill(ws, frame)
        elif kind == "rearmed":
            self.halted = False
            self.log("re-armed: commands accepted again")
        elif kind == "rearm_refused":
            self.log(f"re-arm refused: open kills {frame.get('kill_ids')}")
        elif kind == "kill_ack_received":
            self.log(f"kill {frame.get('kill_id')} acknowledged by GetBody ({frame.get('status')})")
        elif "approval_state" in frame:
            self.log(f"not approved yet ({frame.get('code')}); will keep retrying")
        elif "error" in frame or kind == "error":
            self.log(f"GetBody: {frame}")

    async def _on_command(self, ws, frame):
        inv = str(frame.get("invocation_id"))
        loop = asyncio.get_running_loop()
        with self._lock:
            done = self._results.get(inv)
            fut = self._inflight.get(inv)
            if done is None and fut is None:
                fut = loop.create_future()
                self._inflight[inv] = fut
                first = True
            else:
                first = False
        if done is not None:
            result = done
        elif not first:
            result = await fut  # the same invocation is already running: answer with its result
        else:
            action, params = frame.get("action"), frame.get("params") or {}
            result = await loop.run_in_executor(self._commands, self._run_command, inv, action, params)
            with self._lock:
                self._results[inv] = result
                self._inflight.pop(inv, None)
                self._save_state()
            fut.set_result(result)
        await ws.send(json.dumps({"type": "command_result", "invocation_id": inv, "result": result}))

    def _run_command(self, inv, action, params):
        if self.halted:
            return {"status": "fault", "error": "Body is halted after a kill; not run."}
        self._running = {"invocation_id": inv, "action": action, "params": params}
        try:
            result = self.robot.command(action, params)
            return result if isinstance(result, dict) else {"status": "ok", "value": result}
        except Exception as exc:
            return {"status": "fault", "error": f"{type(exc).__name__}: {exc}"}
        finally:
            self._running = None

    async def _on_read_feed(self, ws, frame):
        loop = asyncio.get_running_loop()

        def read():
            try:
                value = self.robot.feed(frame.get("feed"))
                return value if isinstance(value, dict) else {"value": value}
            except Exception as exc:
                return {"status": "fault", "error": f"{type(exc).__name__}: {exc}"}

        result = await loop.run_in_executor(self._other, read)
        await ws.send(json.dumps({"type": "feed_result", "invocation_id": frame.get("invocation_id"), "result": result}))

    # ---- teleop: latest frame wins, state streamed back, hold when it stops
    def _on_teleop(self, ws, frame):
        if self.halted:
            return
        seq = frame.get("seq")
        if isinstance(seq, int) and self._teleop_seq is not None and seq <= self._teleop_seq:
            return
        self._teleop_seq = seq if isinstance(seq, int) else self._teleop_seq
        self._last_teleop = time.monotonic()
        self._teleop_active = True
        self._teleop_latest = frame
        if not self._teleop_busy:
            self._teleop_busy = True
            asyncio.create_task(self._drain_teleop(ws))

    async def _drain_teleop(self, ws):
        loop = asyncio.get_running_loop()
        try:
            while self._teleop_latest is not None and self._teleop_active and not self.halted:
                frame, self._teleop_latest = self._teleop_latest, None

                def apply(f=frame):
                    try:
                        return self.robot.teleop(f.get("action"), f.get("params") or {})
                    except Exception as exc:
                        return {"error": f"{type(exc).__name__}: {exc}"}

                state = await loop.run_in_executor(self._teleop, apply)
                now = time.monotonic()
                if isinstance(state, dict) and state and now - self._last_state_sent >= 1 / TELEOP_STATE_MAX_HZ:
                    self._last_state_sent = now
                    await ws.send(json.dumps({"type": "teleop_state", "lease_id": frame.get("lease_id"),
                                              "seq": frame.get("seq"), "state": state}))
        finally:
            self._teleop_busy = False

    async def _hold(self, ws, reason):
        self._teleop_active = False
        self._teleop_latest = None
        loop = asyncio.get_running_loop()

        def hold():
            try:
                self.robot.hold(reason)
            except Exception as exc:
                self.log(f"hold() failed: {type(exc).__name__}: {exc}")

        await loop.run_in_executor(self._other, hold)
        self.log(f"teleop stopped ({reason}): holding")

    async def _teleop_watchdog(self, ws):
        while True:
            await asyncio.sleep(0.05)
            if self._teleop_active and time.monotonic() - self._last_teleop > TELEOP_WATCHDOG_S:
                await self._hold(ws, "watchdog")

    async def _on_kill(self, ws, frame):
        self.halted = True  # before anything else: no queued command may start
        self._teleop_active = False
        self._teleop_latest = None
        running = self._running
        self.log(f"KILL ({frame.get('reason')}): stopping")
        loop = asyncio.get_running_loop()

        def stop():
            try:
                return self.robot.stop({"action": running["action"], "params": running["params"]} if running else None) or {}
            except Exception as exc:
                return {"halted": False, "state": {"error": f"{type(exc).__name__}: {exc}"}}

        out = await loop.run_in_executor(self._other, stop)
        interrupted = None
        if running:
            outcome = out.get("outcome") if out.get("outcome") in ("completed", "partial", "not_started") else "partial"
            interrupted = {"invocation_id": running["invocation_id"], "action": running["action"], "outcome": outcome}
        halted = out.get("halted") is True
        await ws.send(json.dumps({
            "type": "kill_ack", "kill_id": frame.get("kill_id"), "halted": halted,
            "interrupted": interrupted, "state": out.get("state") if isinstance(out.get("state"), dict) else {},
        }))
        self.log("halted; type `rearm` once the robot is safe to use" if halted else "NOT HALTED: check the robot now")

    def request_rearm(self):
        """Ask GetBody to re-arm (sent with the next heartbeat). A human decision."""
        self.rearm_requested = True

    def close(self):
        self._stopping = True


def _stdin_rearm(bridge: Bridge):
    for line in sys.stdin:
        if line.strip().lower() == "rearm":
            bridge.request_rearm()
            print("re-arm requested")


# --------------------------------------------------------------------------
# local stand-in for GetBody (pre-listing checklist)
# --------------------------------------------------------------------------

class StandIn:
    """A local WebSocket server that sends what GetBody sends, for testing a bridge
    against the real robot before registering. `ask` is how it gets a human's
    yes/no (input() by default)."""

    def __init__(self, plan: dict, ask=None, log=print):
        self.plan, self.log = plan, log
        self.ask = ask or (lambda q: input(q + " [y/n] ").strip().lower().startswith("y"))
        self.results = []   # (check, passed, detail)
        self.finished = asyncio.Event()

    def _check(self, name, passed, detail=""):
        self.results.append((name, passed, detail))
        self.log(f"{'PASS' if passed else 'FAIL'}  {name}" + (f": {detail}" if detail else ""))

    async def handler(self, ws, *_):
        inbox = asyncio.Queue()
        beats = 0

        async def reader():
            nonlocal beats
            async for raw in ws:
                frame = json.loads(raw)
                if frame.get("type") == "heartbeat":
                    beats += 1
                    await ws.send(json.dumps({"type": "heartbeat_ack"}))
                elif frame.get("type") == "rearm":
                    await ws.send(json.dumps({"type": "rearmed", "body_id": 0}))
                    await inbox.put(frame)
                else:
                    await inbox.put(frame)

        async def expect(kind, inv=None, timeout=COMMAND_DEADLINE_S):
            loop = asyncio.get_running_loop()
            end = loop.time() + timeout
            while True:
                left = end - loop.time()
                if left <= 0:
                    return None
                try:
                    frame = await asyncio.wait_for(inbox.get(), left)
                except asyncio.TimeoutError:
                    return None
                if frame.get("type") == kind and (inv is None or frame.get("invocation_id") == inv):
                    return frame

        read_task = asyncio.create_task(reader())
        try:
            await ws.send(json.dumps({"status": "connected", "body_id": 0, "halted": False}))
            await asyncio.sleep(3)
            self._check("connects and heartbeats", beats >= 3, f"{beats} heartbeats in 3s")

            commands = self.plan.get("commands") or []
            first_inv = None
            for cmd in commands:
                inv = str(uuid.uuid4())
                first_inv = first_inv or (inv, cmd)
                if not self.ask(f"About to send {cmd['action']} {json.dumps(cmd.get('params') or {})}. "
                                "Is someone at the robot with its stop within reach?"):
                    self._check(f"command {cmd['action']}", False, "skipped: nobody at the robot")
                    continue
                await ws.send(json.dumps({"type": "command", "invocation_id": inv, "action": cmd["action"],
                                          "params": cmd.get("params") or {}}))
                reply = await expect("command_result", inv)
                if reply is None:
                    self._check(f"command {cmd['action']}", False, f"no command_result within {COMMAND_DEADLINE_S}s")
                    continue
                ok = self.ask(f"Result {json.dumps(reply.get('result'))}. Did the robot do that correctly?")
                self._check(f"command {cmd['action']}", ok, json.dumps(reply.get("result")))

            if first_inv:
                inv, cmd = first_inv
                await ws.send(json.dumps({"type": "command", "invocation_id": inv, "action": cmd["action"],
                                          "params": cmd.get("params") or {}}))
                reply = await expect("command_result", inv)
                ok = reply is not None and self.ask(f"Sent {cmd['action']} again with the same invocation_id. "
                                                    "Did the robot stay still this time?")
                self._check("repeated invocation_id runs once", ok)

            for name in self.plan.get("feeds") or []:
                inv = str(uuid.uuid4())
                await ws.send(json.dumps({"type": "read_feed", "invocation_id": inv, "feed": name}))
                reply = await expect("feed_result", inv)
                ok = reply is not None and isinstance(reply.get("result"), dict) and \
                    self.ask(f"Feed {name}: {json.dumps(reply.get('result') if reply else None)}. Is that real data?")
                self._check(f"feed {name}", ok)

            teleop = self.plan.get("teleop")
            if teleop:
                action, tparams = teleop["action"], teleop.get("params") or {}
                rate = max(1, int(teleop.get("rate_hz", 20)))
                seq = 0

                async def stream(frames):
                    nonlocal seq
                    for _ in range(frames):
                        seq += 1
                        await ws.send(json.dumps({"type": "teleop", "lease_id": 0, "seq": seq,
                                                  "action": action, "params": tparams}))
                        await asyncio.sleep(1 / rate)

                if self.ask(f"Next: teleop {action} {json.dumps(tparams)} streamed for 2 seconds. "
                            "Is someone at the robot with its stop within reach?"):
                    await stream(rate * 2)
                    self._check("teleop streams", self.ask("Did the robot move smoothly for about 2 seconds?"))
                    await asyncio.sleep(1.0)    # stream stops: the bridge's own watchdog must hold the robot
                    self._check("teleop holds when frames stop",
                                self.ask("Did it stop and hold still on its own, within about half a second?"))
                    await stream(rate)
                    await ws.send(json.dumps({"type": "teleop_stop", "lease_id": 0, "reason": "renter"}))
                    self._check("teleop_stop stops it", self.ask("Did it stop at once when told to?"))
                else:
                    self._check("teleop", False, "skipped: nobody at the robot")

            if commands:
                cmd = commands[0]
                self.ask(f"Next: {cmd['action']}, then a KILL while it runs. Ready, with the stop within reach?")
                await ws.send(json.dumps({"type": "command", "invocation_id": str(uuid.uuid4()),
                                          "action": cmd["action"], "params": cmd.get("params") or {}}))
                await asyncio.sleep(0.2)
            kill_id = str(uuid.uuid4())
            await ws.send(json.dumps({"type": "kill", "kill_id": kill_id, "lease_id": None, "reason": "stand-in test"}))
            ack = await expect("kill_ack", timeout=10)
            good = ack is not None and ack.get("kill_id") == kill_id and ack.get("halted") is True
            ok = good and self.ask("Did the robot stop at once and stay safe?")
            self._check("kill stops the robot and acks", ok, json.dumps(ack) if ack else "no kill_ack within 10s")

            if commands:
                inv = str(uuid.uuid4())
                cmd = commands[0]
                await ws.send(json.dumps({"type": "command", "invocation_id": inv, "action": cmd["action"],
                                          "params": cmd.get("params") or {}}))
                reply = await expect("command_result", inv)
                refused = reply is not None and (reply.get("result") or {}).get("status") == "fault"
                self._check("nothing runs while halted", refused and self.ask("Did the robot stay still?"))

            self.log("Now type `rearm` in the bridge's terminal (only once the robot is safe).")
            rearm = await expect("rearm", timeout=600)
            self._check("re-arm", rearm is not None)
        finally:
            read_task.cancel()
            passed = sum(1 for _, p, _ in self.results if p)
            self.log(f"\n{passed}/{len(self.results)} checks passed" +
                     ("" if passed == len(self.results) else ": fix the failures and run again before registering"))
            self.finished.set()

    async def serve(self, host="127.0.0.1", port=8765):
        import websockets

        async with websockets.serve(self.handler, host, port):
            self.log(f"stand-in listening on ws://{host}:{port}; start the bridge with --url ws://{host}:{port}")
            await self.finished.wait()


# --------------------------------------------------------------------------
# command line
# --------------------------------------------------------------------------

def main(robot: Robot, argv=None):
    p = argparse.ArgumentParser(description="GetBody control bridge")
    sub = p.add_subparsers(dest="mode", required=True)
    r = sub.add_parser("run", help="run the bridge")
    r.add_argument("--body-id", type=int, help="your body's id on GetBody")
    r.add_argument("--key", help="agent_key.json with private_key_hex")
    r.add_argument("--host", default="getbody.md")
    r.add_argument("--url", help="connect here instead (e.g. the local stand-in)")
    r.add_argument("--state", default="getbody_bridge_state.json", help="where finished invocations are kept")
    s = sub.add_parser("standin", help="local stand-in for GetBody, to test before registering")
    s.add_argument("--plan", required=True, help="plan.json: {commands: [{action, params}], feeds: [...]}")
    s.add_argument("--port", type=int, default=8765)
    args = p.parse_args(argv)

    if args.mode == "standin":
        with open(args.plan, encoding="utf-8") as f:
            plan = json.load(f)
        asyncio.run(StandIn(plan).serve(port=args.port))
        return

    if args.url:
        url, path, key = args.url, "", (AgentKey.load(args.key) if args.key else None)
    else:
        if not (args.body_id and args.key):
            p.error("run needs --body-id and --key (or --url for the stand-in)")
        path = f"/getbody/ws/bodies/{args.body_id}/interface"
        url, key = f"wss://{args.host}{path}", AgentKey.load(args.key)
    bridge = Bridge(robot, url, key=key, path=path, state_file=args.state)
    threading.Thread(target=_stdin_rearm, args=(bridge,), daemon=True).start()
    asyncio.run(bridge.run_forever())


if __name__ == "__main__":
    class _DryRun(Robot):
        """Prints instead of moving anything: `python getbody_bridge.py run --url ...`."""

        def command(self, action, params):
            print(f"[dry-run] {action} {params}")
            return {"status": "ok"}

        def feed(self, name):
            return {"dry_run": True, "feed": name}

        def stop(self, running):
            print("[dry-run] stop")
            return {"halted": True, "outcome": "partial", "state": {}}

    main(_DryRun())
