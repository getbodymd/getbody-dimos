"""The config and the fake dimos against the dimos contract pinned in tests/contract/.

The contract is derived from dimos's source by tools/derive_contract.py; see
tests/contract/README.md. If dimos changes a tool our config uses, re-derive
the contract and these tests show what broke.
"""

import copy
import json
import re

import pytest

from getbody_dimos import contract
from getbody_dimos.config import load, parse
from getbody_dimos.mcp_client import McpClient

from .fake_dimos import CONTRACT, CONTRACT_PATH, FakeDimos

CONFIG = "examples/unitree-go2/config.yaml"
TOOLS = {t["name"]: t for t in CONTRACT["tools"]}
PINNED_COMMIT = "dc80d89b89558e9ef3bcccc7949b20aa3e2b4d3b"


def test_contract_is_pinned_to_the_documented_dimos_commit():
    assert CONTRACT["dimos"]["commit"] == PINNED_COMMIT
    assert CONTRACT_PATH.name == f"dimos-{PINNED_COMMIT[:7]}.json"
    assert CONTRACT["blueprint"] == "getbody-dimos.unitree-go2-mcp"
    readme = (CONTRACT_PATH.parent / "README.md").read_text(encoding="utf-8")
    assert PINNED_COMMIT in readme


def test_example_config_matches_the_contract():
    assert contract.problems(load(CONFIG), TOOLS) == []


def test_every_tool_the_example_config_names_is_in_the_contract():
    cfg = load(CONFIG)
    named = cfg.tools_used() | set(cfg.stop.tools)
    assert named <= set(TOOLS), f"not in dimos {PINNED_COMMIT[:7]}: {sorted(named - set(TOOLS))}"


def test_a_tool_missing_from_the_contract_is_caught():
    raw = _raw_config()
    raw["commands"]["move"]["tool"] = "relative_move"  # an older dimos name (PR #945's README); not in this dimos
    assert "dimos does not offer tool 'relative_move'" in contract.problems(parse(raw), TOOLS)


def test_an_argument_the_tool_does_not_take_is_caught():
    raw = _raw_config()
    raw["commands"]["move"]["params"]["speed"] = {"type": "number", "min": 0, "max": 1, "default": 0.5}
    assert any("takes no argument 'speed'" in p for p in contract.problems(parse(raw), TOOLS))


def test_a_wrongly_typed_argument_is_caught():
    raw = _raw_config()
    raw["commands"]["move"]["fixed"]["relative"] = "yes"
    assert any("fixed relative='yes'" in p for p in contract.problems(parse(raw), TOOLS))


def test_a_required_argument_the_renter_can_omit_is_caught():
    raw = _raw_config()
    raw["commands"]["tag_location"]["params"]["location_name"]["required"] = False
    assert any("requires 'location_name'" in p for p in contract.problems(parse(raw), TOOLS))


def test_fake_dimos_serves_exactly_the_contract():
    with FakeDimos() as dimos:
        client = McpClient(dimos.url)
        assert client.initialize() == CONTRACT["initialize"]
        assert client.list_tools() == TOOLS
        for name in TOOLS:  # the fake implements every tool in the contract
            assert hasattr(dimos, name), name


def test_contract_shapes_dimos_produces():
    # Spot checks of what langchain-core 1.3.3 / pydantic 2.12.5 produce for these signatures.
    move = TOOLS["move_to"]["inputSchema"]
    assert move["properties"]["degrees"] == {"anyOf": [{"type": "number"}, {"type": "null"}], "default": None,
                                             "title": "Degrees"}
    assert "required" not in move
    assert TOOLS["tag_location"]["inputSchema"]["required"] == ["location_name"]
    assert TOOLS["begin_exploration"]["_meta"] == {"dimos/uses": ["movement"], "dimos/lifecycle": "background"}
    assert "_meta" not in TOOLS["move_to"]  # move_to takes no capability in dimos
    for name in ("stop_navigation", "end_exploration", "stop_patrol", "observe"):
        assert TOOLS[name]["inputSchema"] == {"properties": {}, "type": "object"}


@pytest.mark.parametrize("command", ["move", "turn", "tag_location"])
def test_limit_corners_produce_arguments_the_tool_accepts(command):
    """Every in-limit corner of every param, plus fixed args, fits the tool's schema."""
    cmd = load(CONFIG).commands[command]
    props = TOOLS[cmd.tool]["inputSchema"]["properties"]
    samples = [cmd.example]
    for name, p in cmd.params.items():
        if p.type == "number":
            samples += [{**cmd.example, name: p.min}, {**cmd.example, name: p.max}]
    for params in samples:
        args = cmd.arguments(params)
        assert set(args) <= set(props)
        for k, v in args.items():
            types = {props[k].get("type")} | {a.get("type") for a in props[k].get("anyOf", [])}
            assert (isinstance(v, bool) and "boolean" in types) or (isinstance(v, (int, float)) and not isinstance(
                v, bool) and "number" in types) or (isinstance(v, str) and "string" in types), (k, v, types)


def test_move_to_reply_matches_the_pose_regex():
    cfg = load(CONFIG)
    with FakeDimos(move_duration_s=0.05, start_delay_s=0, settle_s=0) as dimos:
        text = McpClient(dimos.url).call_tool("move_to", {"x": 0.5, "relative": True}).text
    m = re.search(cfg.pose_regex, text)
    assert m and float(m["x"]) == 0.5


def _raw_config():
    import yaml

    with open(CONFIG, encoding="utf-8") as f:
        return copy.deepcopy(yaml.safe_load(f))


def test_contract_file_is_stable_json():
    text = CONTRACT_PATH.read_text(encoding="utf-8")
    assert text.endswith("\n") and json.loads(text) == CONTRACT
