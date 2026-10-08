"""A small client for the dimos McpServer.

dimos serves MCP as plain JSON-RPC over HTTP POST (default
http://127.0.0.1:9990/mcp), with no session handshake. This client speaks just
the three methods the server implements: initialize, tools/list and tools/call.
"""

from __future__ import annotations

import itertools
import threading
from dataclasses import dataclass, field
from typing import Any

import httpx

DEFAULT_URL = "http://127.0.0.1:9990/mcp"

# dimos reports these as ordinary (non-isError) text results; they are failures.
_REFUSAL_PREFIXES = ("Tool not found:", "Cannot start '", "Error running tool '")


class McpError(Exception):
    """The server could not be reached, or answered with a JSON-RPC error."""


@dataclass
class ToolResult:
    text: str = ""
    images: list[dict[str, str]] = field(default_factory=list)  # [{"mime_type", "data" (base64)}]
    is_error: bool = False


def parse_content(result: dict[str, Any]) -> ToolResult:
    """Turn a tools/call result into text and images.

    Accepts MCP image parts ({"type": "image", "data", "mimeType"}) and the
    OpenAI-style parts dimos returns today ({"type": "image_url",
    "image_url": {"url": "data:image/jpeg;base64,..."}}).
    """
    out = ToolResult(is_error=bool(result.get("isError")))
    texts = []
    for part in result.get("content") or []:
        kind = part.get("type")
        if kind == "text":
            texts.append(str(part.get("text", "")))
        elif kind == "image" and part.get("data"):
            out.images.append({"mime_type": part.get("mimeType") or "image/jpeg", "data": part["data"]})
        elif kind == "image_url":
            url = (part.get("image_url") or {}).get("url", "")
            if url.startswith("data:") and ";base64," in url:
                header, data = url.split(";base64,", 1)
                out.images.append({"mime_type": header[len("data:"):] or "image/jpeg", "data": data})
    out.text = "\n".join(texts)
    if not out.is_error and out.text.startswith(_REFUSAL_PREFIXES):
        out.is_error = True
    return out


class McpClient:
    """Thread-safe: the bridge calls tools from several threads at once."""

    def __init__(self, url: str = DEFAULT_URL, timeout_s: float = 4.0, transport=None):
        self.url = url
        self.timeout_s = timeout_s
        self._http = httpx.Client(timeout=timeout_s, transport=transport)
        self._ids = itertools.count(1)
        self._ids_lock = threading.Lock()

    def close(self) -> None:
        self._http.close()

    def _request(self, method: str, params: dict | None = None, timeout_s: float | None = None) -> Any:
        with self._ids_lock:
            req_id = next(self._ids)
        body = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}
        timeout = self.timeout_s if timeout_s is None else timeout_s
        try:
            resp = self._http.post(self.url, json=body, timeout=timeout,
                                   headers={"Accept": "application/json, text/event-stream"})
            resp.raise_for_status()
            msg = resp.json()
        except httpx.ConnectTimeout as exc:
            raise McpError(f"{method} failed: cannot reach dimos at {self.url}") from exc
        except httpx.TimeoutException as exc:
            raise TimeoutError(f"dimos did not answer {method} within {timeout}s") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise McpError(f"{method} failed: {type(exc).__name__}: {exc}") from exc
        if "error" in msg:
            err = msg["error"] or {}
            raise McpError(f"{method}: {err.get('message', err)}")
        return msg.get("result")

    def initialize(self) -> dict:
        result = self._request("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "getbody-dimos", "version": "0.1.0"},
        })
        # A notification: dimos answers 204 with no body.
        try:
            self._http.post(self.url, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        except httpx.HTTPError:
            pass
        return result or {}

    def list_tools(self) -> dict[str, dict]:
        """Tool name -> {"inputSchema", "description", "_meta"}."""
        result = self._request("tools/list") or {}
        return {t["name"]: t for t in result.get("tools", [])}

    def call_tool(self, name: str, arguments: dict | None = None, timeout_s: float | None = None) -> ToolResult:
        result = self._request("tools/call", {"name": name, "arguments": arguments or {}}, timeout_s)
        return parse_content(result or {})
