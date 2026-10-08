"""Check getbody_dimos/blueprints/unitree_go2.py against a dimos checkout, without
importing dimos:

1. every `from dimos... import Name` resolves to a top-level definition in
   that dimos source tree;
2. the modules it composes are unitree-go2-agentic's, minus exactly the ones
   it means to drop (the LLM agent, web input, speech, person-follow and the
   perception loop);
3. the external-blueprint name it registers is one dimos will accept.

    python tools/check_blueprint.py /path/to/dimos
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BLUEPRINT = ROOT / "src/getbody_dimos/blueprints/unitree_go2.py"
DROPPED = {"McpClient", "WebInput", "SpeakSkill", "PersonFollowSkillContainer", "PerceiveLoopSkill"}
# dimos/robot/external_blueprints.py
LOCAL_BLUEPRINT_NAME_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def module_file(dimos: Path, module: str) -> Path:
    base = dimos.joinpath(*module.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.exists():
            return candidate
    raise SystemExit(f"FAIL: module {module} not found in {dimos}")


def top_level_names(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.ImportFrom):
            names.update(a.asname or a.name for a in node.names)
    return names


def dimos_imports(path: Path) -> dict[str, str]:
    """name -> module, for every `from dimos.x import name` in the file."""
    out = {}
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("dimos."):
            for alias in node.names:
                out[alias.asname or alias.name] = node.module or ""
    return out


def autoconnect_parts(path: Path, var: str) -> list[str]:
    """Names passed to autoconnect(...) in `var = autoconnect(...)...`. X.blueprint() -> X."""
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == var for t in node.targets):
            call = node.value
            while isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute):  # .global_config(...)
                call = call.func.value
            assert isinstance(call, ast.Call) and getattr(call.func, "id", None) == "autoconnect", var
            parts = []
            for arg in call.args:
                if isinstance(arg, ast.Name):
                    parts.append(arg.id)
                elif isinstance(arg, ast.Call) and isinstance(arg.func, ast.Attribute):
                    parts.append(ast.unparse(arg.func.value))
            return parts
    raise SystemExit(f"FAIL: {var} = autoconnect(...) not found in {path}")


def flatten(dimos: Path, path: Path, var: str) -> set[str]:
    """Module classes in a blueprint, expanding blueprint variables it imports."""
    imports = dimos_imports(path)
    out: set[str] = set()
    for part in autoconnect_parts(path, var):
        if part[:1].islower() or part.startswith("_"):  # a blueprint variable
            out |= flatten(dimos, module_file(dimos, imports[part]) if part in imports else path, part)
        else:
            out.add(part)
    return out


def main(dimos: Path) -> int:
    failures = []
    imports = dimos_imports(BLUEPRINT)
    for name, module in sorted(imports.items()):
        if name not in top_level_names(module_file(dimos, module)):
            failures.append(f"{module} has no {name}")
        else:
            print(f"ok    from {module} import {name}")

    stop_at = {"unitree_go2"}  # shared base: compare above it
    ours = {p for p in autoconnect_parts(BLUEPRINT, "unitree_go2_mcp") if p not in stop_at}
    agentic_file = module_file(dimos, "dimos.robot.unitree.go2.blueprints.agentic.unitree_go2_agentic")
    agentic = flatten(dimos, agentic_file, "unitree_go2_agentic")
    base = flatten(dimos, module_file(dimos, "dimos.robot.unitree.go2.blueprints.smart.unitree_go2"), "unitree_go2")
    expected = (agentic - base) - DROPPED
    if ours != expected:
        failures.append(f"modules above unitree_go2: ours {sorted(ours)}, expected agentic minus dropped "
                        f"{sorted(expected)}")
    else:
        print(f"ok    modules = unitree-go2-agentic - {sorted(DROPPED)}: {sorted(ours)}")
    missing_drops = DROPPED - (agentic - base)
    if missing_drops:
        failures.append(f"{sorted(missing_drops)} are no longer in unitree-go2-agentic; update DROPPED")

    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'\[project\.entry-points\."dimos\.blueprints"\]\s*\n(?:#.*\n)*([\w-]+)\s*=\s*"([^"]+)"', pyproject)
    if not m:
        failures.append("no dimos.blueprints entry point in pyproject.toml")
    else:
        name, target = m.groups()
        if not LOCAL_BLUEPRINT_NAME_PATTERN.fullmatch(name):
            failures.append(f"entry point name {name!r} is not lowercase kebab-case")
        if target != "getbody_dimos.blueprints.unitree_go2:unitree_go2_mcp":
            failures.append(f"entry point target {target!r}")
        print(f"ok    entry point dimos.blueprints: {name} = {target}  ->  dimos run getbody-dimos.{name}")

    for f in failures:
        print(f"FAIL  {f}")
    return 1 if failures else 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: check_blueprint.py <dimos checkout>")
    sys.exit(main(Path(sys.argv[1])))
