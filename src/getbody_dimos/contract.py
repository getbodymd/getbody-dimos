"""Check a config against the tools a dimos McpServer offers.

Used at startup against the live tools/list, and in tests against the
contract pinned in tests/contract/. Catches a config that names a tool dimos
doesn't have, passes an argument the tool doesn't take, passes the wrong
type, or can leave out an argument the tool requires.
"""

from __future__ import annotations

from typing import Any

from .config import Config, Param

_JSON_TYPES = {"number": {"number"}, "integer": {"integer", "number"}, "string": {"string"}, "boolean": {"boolean"}}


def _schema_types(prop: dict[str, Any]) -> set[str]:
    types: set[str] = set()
    for part in [prop, *prop.get("anyOf", []), *prop.get("oneOf", [])]:
        t = part.get("type")
        if isinstance(t, str):
            types.add(t)
        elif isinstance(t, list):
            types.update(t)
    return types


def _value_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    return type(value).__name__


def _fits(sent: set[str], accepted: set[str]) -> bool:
    if not accepted:  # untyped property: anything goes
        return True
    if "number" in accepted:
        accepted = accepted | {"integer"}
    return sent <= accepted


def problems(config: Config, tools: dict[str, dict[str, Any]]) -> list[str]:
    out: list[str] = []
    for tool in sorted(config.tools_used() - set(tools)):
        out.append(f"dimos does not offer tool {tool!r}")
    for name, cmd in config.commands.items():
        if not cmd.tool or cmd.tool not in tools:
            continue
        schema = tools[cmd.tool].get("inputSchema") or {}
        props: dict[str, Any] = schema.get("properties") or {}
        required = set(schema.get("required") or [])
        where = f"command {name} ({cmd.tool})"
        for arg, p in cmd.params.items():
            if arg not in props:
                out.append(f"{where}: {cmd.tool} takes no argument {arg!r}")
            elif not _fits(_JSON_TYPES[p.type], _schema_types(props[arg])):
                out.append(f"{where}: {arg} is a {p.type} here but {cmd.tool} takes {sorted(_schema_types(props[arg]))}")
        for arg, value in cmd.fixed.items():
            if arg not in props:
                out.append(f"{where}: {cmd.tool} takes no argument {arg!r}")
            elif not _fits({_value_type(value)}, _schema_types(props[arg])):
                out.append(f"{where}: fixed {arg}={value!r} is not a {sorted(_schema_types(props[arg]))}")
        for arg in sorted(required):
            param: Param | None = cmd.params.get(arg)
            if arg in cmd.fixed or (param is not None and (param.required or param.has_default)):
                continue
            out.append(f"{where}: {cmd.tool} requires {arg!r}; make it required, give it a default, or fix it")
    for name, feed in config.feeds.items():
        if feed.tool in tools and (tools[feed.tool].get("inputSchema") or {}).get("required"):
            out.append(f"feed {name} ({feed.tool}): feeds call tools with no arguments, but it requires some")
    for tool in config.stop.tools:
        if tool in tools and (tools[tool].get("inputSchema") or {}).get("required"):
            out.append(f"stop tool {tool}: stop tools are called with no arguments, but it requires some")
    return out
