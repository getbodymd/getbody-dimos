"""The vendored GetBody bridge is unmodified and matches its type stub."""

import ast
import hashlib
import re
from pathlib import Path

from getbody_dimos.vendor import getbody_bridge

VENDOR = Path(getbody_bridge.__file__).parent


def test_checksum_matches_vendor_readme():
    recorded = re.search(r"SHA-256 \| `([0-9a-f]{64})`", (VENDOR / "README.md").read_text(encoding="utf-8"))
    assert recorded, "SHA-256 missing from vendor/README.md"
    assert hashlib.sha256((VENDOR / "getbody_bridge.py").read_bytes()).hexdigest() == recorded[1]


def test_version_matches_vendor_readme():
    assert f"| Version | {getbody_bridge.__version__} " in (VENDOR / "README.md").read_text(encoding="utf-8")


def test_stub_names_exist_in_the_bridge():
    stub = ast.parse((VENDOR / "getbody_bridge.pyi").read_text(encoding="utf-8"))
    for node in stub.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef)):
            assert hasattr(getbody_bridge, node.name), node.name
            if isinstance(node, ast.ClassDef):
                cls = getattr(getbody_bridge, node.name)
                for member in node.body:
                    if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        assert hasattr(cls, member.name), f"{node.name}.{member.name}"
