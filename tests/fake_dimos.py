"""A stand-in for the dimos McpServer, for tests.

It behaves the way dimos/agents/mcp/mcp_server.py does: plain JSON-RPC over
HTTP POST, unknown tools and busy capabilities answered as ordinary text, tool
exceptions as isError, images as OpenAI-style image_url parts. move_to blocks
like UnitreeSkillContainer.move_to until the goal is reached or cancelled.
"""

from __future__ import annotations

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

JPEG = base64.b64encode(b"\xff\xd8\xff\xe0fake-jpeg\xff\xd9").decode()

TOOLS = [
    {"name": "move_to", "inputSchema": {"type": "object", "properties": {
        "x": {"type": "number", "default": 0.0}, "y": {"type": "number", "default": 0.0},
        "degrees": {"anyOf": [{"type": "number"}, {"type": "null"}], "default": None},
        "relative": {"type": "boolean", "default": False}}}},
    {"name": "stop_navigation", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "tag_location", "inputSchema": {"type": "object", "properties": {
        "location_name": {"type": "string"}}, "required": ["location_name"]}},
    {"name": "observe", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "get_battery_soc", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "server_status", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "end_exploration", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "execute_sport_command", "inputSchema": {"type": "object", "properties": {
        "command_name": {"type": "string"}}, "required": ["command_name"]}},
]


class FakeDimos:
    def __init__(self, move_duration_s: float = 3.0):
        self.move_duration_s = move_duration_s
        self.calls: list[tuple[str, dict]] = []
        self.moving = threading.Event()
        self.fail_stop = False          # stop_navigation raises (isError)
        self.stop_delay_s = 0.0
        self.pose = [0.0, 0.0, 0.0]
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}/mcp"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._cancel.set()
        self._server.shutdown()
        self._server.server_close()

    def count(self, name: str) -> int:
        return sum(1 for n, _ in self.calls if n == name)

    # ---- tools
    def _move_to(self, x=0.0, y=0.0, degrees=None, relative=False):
        self._cancel.clear()
        self.moving.set()
        try:
            arrived = not self._cancel.wait(self.move_duration_s)
        finally:
            self.moving.clear()
        if arrived:
            self.pose = [self.pose[0] + x, self.pose[1] + y, self.pose[2] + (degrees or 0.0)]
            outcome = "Navigation goal reached"
        else:
            outcome = "Navigation was cancelled or failed"
        p = self.pose
        return f"{outcome}. Robot is at x={p[0]:.2f} y={p[1]:.2f} heading={p[2]:.0f}deg; goal was x=0 y=0 heading=0deg."

    def _call(self, name: str, args: dict):
        with self._lock:
            self.calls.append((name, args))
        if name == "move_to":
            return {"content": [{"type": "text", "text": self._move_to(**args)}]}
        if name == "stop_navigation":
            if self.stop_delay_s:
                threading.Event().wait(self.stop_delay_s)
            if self.fail_stop:
                return {"content": [{"type": "text", "text": "Error running tool 'stop_navigation': boom"}],
                        "isError": True}
            self._cancel.set()
            return {"content": [{"type": "text", "text": "Stopped"}]}
        if name == "end_exploration":
            return {"content": [{"type": "text", "text": "Exploration skill was not active, so nothing was stopped."}]}
        if name == "tag_location":
            return {"content": [{"type": "text", "text": f"Tagged '{args['location_name']}': (0.0,0.0)."}]}
        if name == "observe":
            return {"content": [{"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{JPEG}"}}]}
        if name == "get_battery_soc":
            return {"content": [{"type": "text", "text": "None"}]}
        if name == "server_status":
            return {"content": [{"type": "text", "text": json.dumps({"pid": 1, "modules": [], "skills": []})}]}
        if name == "execute_sport_command":
            return {"content": [{"type": "text", "text": f"'{args['command_name']}' command executed successfully."}]}
        return {"content": [{"type": "text", "text": f"Tool not found: {name}"}]}

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if "id" not in body:
                    self.send_response(204)
                    self.end_headers()
                    return
                method, params = body.get("method"), body.get("params") or {}
                if method == "initialize":
                    result = {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}},
                              "serverInfo": {"name": "dimensional", "version": "1.0.0"}}
                elif method == "tools/list":
                    result = {"tools": TOOLS}
                elif method == "tools/call":
                    result = fake._call(params.get("name", ""), params.get("arguments") or {})
                else:
                    reply = {"jsonrpc": "2.0", "id": body["id"], "error": {"code": -32601, "message": f"Unknown: {method}"}}
                    return self._send(reply)
                self._send({"jsonrpc": "2.0", "id": body["id"], "result": result})

            def _send(self, msg):
                data = json.dumps(msg).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        return Handler
