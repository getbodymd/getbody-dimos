"""Derive the MCP contract of the getbody-dimos.unitree-go2-mcp blueprint from a
dimos checkout, without installing or importing dimos.

dimos builds each tool's inputSchema in Module.get_skills() as

    json.dumps(tool(attr).args_schema.model_json_schema())      # langchain_core.tools.tool

and McpServer._handle_tools_list() then pops "description" (moving it to the
tool) and "title", and adds _meta {"dimos/uses", "dimos/lifecycle"} when a
skill uses a capability or is not instant.

This script reads each @skill method's signature and docstring from the dimos
source with `ast`, rebuilds a stub with the same signature and docstring
behind the same wrappers (@skill's functools.wraps wrapper, and @rpc's sync
dispatcher for async skills), and runs the same langchain_core call on it.
Run it with the langchain-core and pydantic versions pinned in dimos's uv.lock
and the Python dimos runs on:

    uv run --no-project -p 3.12 --with langchain-core==1.3.3 --with pydantic==2.12.5 \
        python tools/derive_contract.py /path/to/dimos tests/contract/dimos-<sha>.json
"""

from __future__ import annotations

import ast
import functools
import json
import platform
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

# The modules in getbody_dimos/blueprints/unitree_go2.py, by source file and class.
# unitree_go2 = unitree_go2_basic (vis_module, GO2Connection) + VoxelGridMapper,
# CostMapper, ReplanningAStarPlanner, WavefrontFrontierExplorer, PatrollingModule,
# MovementManager. Only the classes below define @skill methods; the others
# (and their base classes) define none.
BLUEPRINT_MODULES = [
    ("dimos/robot/unitree/go2/connection.py", "GO2Connection"),
    ("dimos/navigation/experimental/frontier_exploration/wavefront_frontier_goal_selector.py",
     "WavefrontFrontierExplorer"),
    ("dimos/navigation/experimental/patrolling/module.py", "PatrollingModule"),
    ("dimos/agents/mcp/mcp_server.py", "McpServer"),
    ("dimos/agents/skills/navigation.py", "NavigationSkillContainer"),
    ("dimos/agents/skills/observe_skill.py", "ObserveSkill"),
    ("dimos/robot/unitree/unitree_skill_container.py", "UnitreeSkillContainer"),
]
# Also in the blueprint, checked to have no @skill methods (nor their bases).
NO_SKILL_MODULES = [
    ("dimos/mapping/voxels/module.py", "VoxelGridMapper"),
    ("dimos/mapping/costmapper.py", "CostMapper"),
    ("dimos/navigation/go2/replanning_a_star/module.py", "ReplanningAStarPlanner"),
    ("dimos/navigation/movement_manager/movement_manager.py", "MovementManager"),
    ("dimos/perception/experimental/spatial_perception.py", "SpatialMemory"),
    ("dimos/visualization/rerun/bridge.py", "RerunBridgeModule"),
    ("dimos/memory/module.py", "StreamModule"),
    ("dimos/core/module.py", "ModuleBase"),
    ("dimos/core/module.py", "Module"),
]
CAPABILITY_NAMES = {"CAP_MOVEMENT": "movement"}  # dimos/agents/capabilities.py

_ALLOWED_NAMES = {"float", "int", "str", "bool", "None", "list", "dict", "tuple", "Any", "Literal", "Optional"}


def _check_annotation(node: ast.expr, where: str) -> None:
    """Only plain types: these get evaluated to build the stub."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and sub.id not in _ALLOWED_NAMES:
            raise SystemExit(f"{where}: annotation uses {sub.id!r}; add it to the allowlist if it is a plain type")
        if not isinstance(sub, (ast.Name, ast.Constant, ast.BinOp, ast.BitOr, ast.Subscript, ast.Tuple, ast.Load)):
            raise SystemExit(f"{where}: unsupported annotation {ast.unparse(node)!r}")


def _skill_decorator(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[list[str], str] | None:
    for dec in fn.decorator_list:
        if isinstance(dec, ast.Name) and dec.id == "skill":
            return [], "instant"
        if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Name) and dec.func.id == "skill":
            uses, lifecycle = [], "instant"
            for kw in dec.keywords:
                if kw.arg == "uses":
                    uses = [CAPABILITY_NAMES[e.id] if isinstance(e, ast.Name) else ast.literal_eval(e)
                            for e in kw.value.elts]
                elif kw.arg == "lifecycle":
                    lifecycle = ast.literal_eval(kw.value)
            return uses, lifecycle
    return None


def _class(tree: ast.Module, name: str) -> ast.ClassDef:
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise SystemExit(f"class {name} not found")


def _sport_command_doc(tree: ast.Module) -> str:
    """UnitreeSkillContainer.execute_sport_command.__doc__ is set after the class,
    from UNITREE_WEBRTC_CONTROLS (unitree_skill_container.py)."""
    controls = None
    template = None
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else [getattr(node, "target", None)]
        for t in targets:
            if isinstance(t, ast.Name) and t.id == "UNITREE_WEBRTC_CONTROLS":
                controls = ast.literal_eval(node.value)
            if isinstance(t, ast.Attribute) and t.attr == "__doc__" and ast.unparse(t.value).endswith(
                    "execute_sport_command"):
                template = node.value
    if controls is None or not isinstance(template, ast.JoinedStr):
        raise SystemExit("execute_sport_command docstring source not found")
    commands = {n: (i, d) for n, i, d in controls if n not in ["Reverse", "Spin"]}
    rendered = "\n".join(f'- "{n}": {d}' for n, (_, d) in commands.items())
    parts = []
    for v in template.values:
        if isinstance(v, ast.Constant):
            parts.append(v.value)
        elif isinstance(v, ast.FormattedValue) and isinstance(v.value, ast.Name) and v.value.id == "_commands":
            parts.append(rendered)
        else:
            raise SystemExit("unexpected part in execute_sport_command docstring")
    return "".join(parts)


def _stub(fn: ast.FunctionDef | ast.AsyncFunctionDef, doc: str | None, where: str):
    args = fn.args
    if args.vararg or args.kwarg or args.posonlyargs or args.kwonlyargs:
        raise SystemExit(f"{where}: unsupported signature")
    params = args.args[1:]  # drop self
    defaults = [None] * (len(params) - len(args.defaults)) + list(args.defaults)
    pieces = ["self"]
    for p, d in zip(params, defaults, strict=True):
        piece = p.arg
        if p.annotation is not None:
            _check_annotation(p.annotation, where)
            piece += f": {ast.unparse(p.annotation)}"
        if d is not None:
            piece += f" = {ast.literal_eval(d)!r}"
        pieces.append(piece)
    kind = "async def" if isinstance(fn, ast.AsyncFunctionDef) else "def"
    from typing import Any, Literal, Optional  # noqa: F401  (names the stubs may use)

    ns: dict = {"Any": Any, "Literal": Literal, "Optional": Optional}
    exec(f"{kind} {fn.name}({', '.join(pieces)}):\n    pass\n", ns)  # signature built from checked parts
    func = ns[fn.name]
    func.__doc__ = doc
    func.__module__ = "dimos_stub"

    # @skill: functools.wraps wrapper (async for async skills), then @rpc.
    if isinstance(fn, ast.AsyncFunctionDef):
        @functools.wraps(func)
        async def async_context_wrapper(*a, **k):
            return await func(*a, **k)

        @functools.wraps(async_context_wrapper)
        def rpc_wrapper(self, *a, **k):  # @rpc's sync dispatcher for async methods
            raise NotImplementedError

        return rpc_wrapper

    @functools.wraps(func)
    def sync_context_wrapper(*a, **k):
        return func(*a, **k)

    return sync_context_wrapper


def derive(dimos_root: Path) -> dict:
    from langchain_core.tools import tool

    tools = []
    modules: dict[str, list[str]] = {}
    for rel, cls_name in NO_SKILL_MODULES:
        cls = _class(ast.parse((dimos_root / rel).read_text(encoding="utf-8")), cls_name)
        found = [n.name for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and _skill_decorator(n)]
        if found:
            raise SystemExit(f"{cls_name} now has skills {found}; add it to BLUEPRINT_MODULES")
    for rel, cls_name in BLUEPRINT_MODULES:
        tree = ast.parse((dimos_root / rel).read_text(encoding="utf-8"))
        cls = _class(tree, cls_name)
        methods = {}
        for node in cls.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            meta = _skill_decorator(node)
            if meta is None:
                continue
            doc = ast.get_docstring(node, clean=False)
            if cls_name == "UnitreeSkillContainer" and node.name == "execute_sport_command":
                doc = _sport_command_doc(tree)
            methods[node.name] = (_stub(node, doc, f"{rel}:{node.lineno}"), meta)

        stub_cls = type(cls_name, (), {name: f for name, (f, _) in methods.items()})
        instance = stub_cls()
        modules[cls_name] = sorted(methods)
        for name in sorted(methods):  # get_skills() walks dir(self): alphabetical
            uses, lifecycle = methods[name][1]
            schema = json.loads(json.dumps(tool(getattr(instance, name)).args_schema.model_json_schema()))
            description = schema.pop("description", None)  # McpServer._handle_tools_list
            schema.pop("title", None)
            entry: dict = {"name": name, "inputSchema": schema}
            if description:
                entry["description"] = description
            if uses or lifecycle != "instant":
                entry["_meta"] = {"dimos/uses": list(uses), "dimos/lifecycle": lifecycle}
            tools.append(entry)

    commit = subprocess.run(["git", "-C", str(dimos_root), "rev-parse", "HEAD"], capture_output=True, text=True,
                            check=True).stdout.strip()
    pyproject = (dimos_root / "pyproject.toml").read_text(encoding="utf-8")
    dimos_version = next(line.split('"')[1] for line in pyproject.splitlines() if line.startswith("version ="))
    return {
        "dimos": {"repo": "https://github.com/dimensionalOS/dimos", "commit": commit, "version": dimos_version},
        "derived_with": {"python": platform.python_version(), "langchain_core": version("langchain-core"),
                         "pydantic": version("pydantic")},
        "blueprint": "getbody-dimos.unitree-go2-mcp",
        "modules": modules,
        # McpServer._handle_initialize
        "initialize": {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}, "logging": {}},
                       "serverInfo": {"name": "dimensional", "version": "1.0.0"}},
        "tools": sorted(tools, key=lambda t: t["name"]),
    }


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: derive_contract.py <dimos checkout> <output.json>")
    contract = derive(Path(sys.argv[1]))
    Path(sys.argv[2]).parent.mkdir(parents=True, exist_ok=True)
    with open(sys.argv[2], "w", encoding="utf-8", newline="\n") as f:
        json.dump(contract, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"{len(contract['tools'])} tools from dimos {contract['dimos']['commit'][:7]} -> {sys.argv[2]}")
