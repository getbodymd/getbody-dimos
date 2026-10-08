"""The bridge config: which GetBody commands and feeds exist, which dimos tool
each one calls, and the limits the bridge enforces before calling it.

Nothing is exposed unless it is listed here.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

import yaml

from .mcp_client import DEFAULT_URL

PARAM_TYPES = ("number", "integer", "string", "boolean")
COMMAND_MODES = ("sync", "background")
FEED_KINDS = ("text", "json", "number", "image", "task", "odom")


class ConfigError(ValueError):
    pass


class ParamError(ValueError):
    """A renter's params are outside what the config allows."""


@dataclass
class Param:
    name: str
    type: str
    min: float | None = None
    max: float | None = None
    enum: list | None = None
    max_length: int | None = None
    pattern: str | None = None
    required: bool = False
    default: Any = None
    has_default: bool = False

    def check(self, value: Any) -> Any:
        n = self.name
        if self.type in ("number", "integer"):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ParamError(f"{n} must be a {self.type}")
            if self.type == "integer" and not float(value).is_integer():
                raise ParamError(f"{n} must be an integer")
            if not math.isfinite(value):
                raise ParamError(f"{n} must be finite")
            if self.min is not None and value < self.min:
                raise ParamError(f"{n}={value} is below the limit {self.min}")
            if self.max is not None and value > self.max:
                raise ParamError(f"{n}={value} is above the limit {self.max}")
            value = int(value) if self.type == "integer" else float(value)
        elif self.type == "string":
            if not isinstance(value, str):
                raise ParamError(f"{n} must be a string")
            if self.max_length is not None and len(value) > self.max_length:
                raise ParamError(f"{n} is longer than {self.max_length} characters")
            if self.pattern is not None and not re.fullmatch(self.pattern, value):
                raise ParamError(f"{n} has characters that are not allowed")
        elif self.type == "boolean":
            if not isinstance(value, bool):
                raise ParamError(f"{n} must be true or false")
        if self.enum is not None and value not in self.enum:
            raise ParamError(f"{n} must be one of {self.enum}")
        return value

    def schema(self) -> dict:
        """JSON Schema for GetBody's command_schemas."""
        s: dict[str, Any] = {"type": self.type}
        if self.min is not None:
            s["minimum"] = self.min
        if self.max is not None:
            s["maximum"] = self.max
        if self.enum is not None:
            s["enum"] = list(self.enum)
        if self.max_length is not None:
            s["maxLength"] = self.max_length
        if self.pattern is not None:
            s["pattern"] = self.pattern
        if self.has_default:
            s["default"] = self.default
        return s


@dataclass
class Command:
    name: str
    tool: str | None             # None for the built-in stop
    builtin: str | None = None   # "stop"
    mode: str = "sync"
    params: dict[str, Param] = field(default_factory=dict)
    fixed: dict[str, Any] = field(default_factory=dict)
    max_runtime_s: float | None = None
    fault_patterns: list[str] = field(default_factory=list)
    example: dict[str, Any] = field(default_factory=dict)

    def arguments(self, params: dict) -> dict:
        """Check a renter's params against the limits; return the tool arguments.
        Raises ParamError for anything unknown, missing or out of range."""
        if not isinstance(params, dict):
            raise ParamError("params must be an object")
        unknown = sorted(set(params) - set(self.params))
        if unknown:
            raise ParamError(f"unknown params: {', '.join(unknown)}")
        args = {}
        for p in self.params.values():
            if p.name in params:
                args[p.name] = p.check(params[p.name])
            elif p.required:
                raise ParamError(f"{p.name} is required")
            elif p.has_default:
                args[p.name] = p.default
        args.update(self.fixed)  # never overridable by the renter
        return args

    def schema(self) -> dict:
        props = {name: p.schema() for name, p in self.params.items()}
        required = [name for name, p in self.params.items() if p.required]
        out: dict[str, Any] = {"type": "object", "properties": props, "additionalProperties": False}
        if required:
            out["required"] = required
        return out


@dataclass
class Feed:
    name: str
    kind: str
    tool: str | None = None
    max_bytes: int | None = None  # images: refuse frames bigger than this (base64 length)


@dataclass
class StopConfig:
    tools: list[str]
    required: list[str]
    timeout_s: float = 2.0


@dataclass
class OdomConfig:
    backend: str                # "zenoh" or "lcm"
    topic: str
    max_speed: float = 0.05     # m/s; at or below this counts as stopped
    max_yaw_rate: float = 0.1   # rad/s
    window_s: float = 0.5       # measure speed over this long
    timeout_s: float = 3.0      # give up waiting for a stop after this long
    stale_s: float = 1.0        # odom older than this is not trusted


@dataclass
class Config:
    mcp_url: str
    mcp_timeout_s: float
    ack_after_s: float
    commands: dict[str, Command]
    feeds: dict[str, Feed]
    stop: StopConfig
    odom: OdomConfig | None = None

    def tools_used(self) -> set[str]:
        tools = {c.tool for c in self.commands.values() if c.tool}
        tools |= {f.tool for f in self.feeds.values() if f.tool}
        return tools | set(self.stop.required)

    def plan(self) -> dict:
        """plan.json for GetBody's stand-in: every command (with its example
        params) and every feed."""
        return {
            "commands": [{"action": c.name, "params": c.example} for c in self.commands.values()],
            "feeds": list(self.feeds),
        }

    def schemas(self) -> dict:
        """command_schemas for the GetBody listing."""
        return {name: c.schema() for name, c in self.commands.items()}


def _param(command: str, name: str, raw: dict) -> Param:
    label = f"{command}.{name}"
    if not isinstance(raw, dict) or raw.get("type") not in PARAM_TYPES:
        raise ConfigError(f"param {label}: type must be one of {PARAM_TYPES}")
    p = Param(name=name, type=raw["type"], min=raw.get("min"), max=raw.get("max"), enum=raw.get("enum"),
              max_length=raw.get("max_length"), pattern=raw.get("pattern"), required=bool(raw.get("required")),
              default=raw.get("default"), has_default="default" in raw)
    if p.type in ("number", "integer") and (p.min is None or p.max is None) and p.enum is None:
        raise ConfigError(f"param {label}: numbers need both min and max")
    if p.type == "string" and p.enum is None and (p.max_length is None or p.pattern is None):
        raise ConfigError(f"param {label}: strings need an enum, or both max_length and pattern")
    if p.has_default:
        try:
            p.default = p.check(p.default)
        except ParamError as exc:
            raise ConfigError(f"param {label}: default is outside its own limits ({exc})") from exc
    return p


def _command(name: str, raw: dict) -> Command:
    raw = raw or {}
    builtin = raw.get("builtin")
    if builtin not in (None, "stop"):
        raise ConfigError(f"command {name}: unknown builtin {builtin!r}")
    tool = raw.get("tool")
    if not builtin and not tool:
        raise ConfigError(f"command {name}: needs a tool (or builtin: stop)")
    mode = raw.get("mode", "sync")
    if mode not in COMMAND_MODES:
        raise ConfigError(f"command {name}: mode must be one of {COMMAND_MODES}")
    params = {pname: _param(name, pname, praw) for pname, praw in (raw.get("params") or {}).items()}
    fixed = raw.get("fixed") or {}
    clash = set(fixed) & set(params)
    if clash:
        raise ConfigError(f"command {name}: {sorted(clash)} are both fixed and renter params")
    cmd = Command(name=name, tool=tool, builtin=builtin, mode=mode, params=params, fixed=fixed,
                  max_runtime_s=raw.get("max_runtime_s"), fault_patterns=list(raw.get("fault_patterns") or []),
                  example=raw.get("example") or {})
    if mode == "background" and not cmd.max_runtime_s:
        raise ConfigError(f"command {name}: background commands need max_runtime_s")
    try:
        cmd.arguments(cmd.example)
    except ParamError as exc:
        raise ConfigError(f"command {name}: example params are invalid ({exc})") from exc
    return cmd


def _feed(name: str, raw: dict) -> Feed:
    raw = raw or {}
    kind = raw.get("kind")
    if kind not in FEED_KINDS:
        raise ConfigError(f"feed {name}: kind must be one of {FEED_KINDS}")
    tool = raw.get("tool")
    if kind in ("task", "odom"):
        tool = None
    elif not tool:
        raise ConfigError(f"feed {name}: needs a tool")
    return Feed(name=name, kind=kind, tool=tool, max_bytes=raw.get("max_bytes"))


def parse(raw: dict) -> Config:
    if not isinstance(raw, dict):
        raise ConfigError("config must be a mapping")
    mcp = raw.get("mcp") or {}
    commands = {name: _command(name, c) for name, c in (raw.get("commands") or {}).items()}
    feeds = {name: _feed(name, f) for name, f in (raw.get("feeds") or {}).items()}
    stop_raw = raw.get("stop") or {}
    stop = StopConfig(tools=list(stop_raw.get("tools") or []), required=list(stop_raw.get("required") or []),
                      timeout_s=float(stop_raw.get("timeout_s", 2.0)))
    if not stop.tools:
        raise ConfigError("stop.tools must list at least one dimos tool that stops the robot")
    if not set(stop.required) <= set(stop.tools):
        raise ConfigError("stop.required must be a subset of stop.tools")
    odom = None
    if raw.get("odom"):
        o = raw["odom"]
        if o.get("backend") not in ("zenoh", "lcm") or not o.get("topic"):
            raise ConfigError("odom needs backend (zenoh or lcm) and topic")
        odom = OdomConfig(**{k: v for k, v in o.items()})
    if any(f.kind == "odom" for f in feeds.values()) and odom is None:
        raise ConfigError("an odom feed needs the odom section")
    cfg = Config(mcp_url=mcp.get("url", DEFAULT_URL), mcp_timeout_s=float(mcp.get("timeout_s", 4.0)),
                 ack_after_s=float(raw.get("ack_after_s", 1.0)), commands=commands, feeds=feeds, stop=stop, odom=odom)
    if not 0 < cfg.mcp_timeout_s <= 4.5:
        raise ConfigError("mcp.timeout_s must be above 0 and at most 4.5 (GetBody drops replies after 5 s)")
    if not 0 < cfg.ack_after_s < cfg.mcp_timeout_s:
        raise ConfigError("ack_after_s must be above 0 and below mcp.timeout_s")
    return cfg


def load(path: str) -> Config:
    with open(path, encoding="utf-8") as f:
        return parse(yaml.safe_load(f))
