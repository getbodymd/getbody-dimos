import copy

import pytest

from getbody_dimos.config import ConfigError, ParamError, parse

BASE = {
    "commands": {
        "move": {
            "tool": "move_to", "mode": "background", "max_runtime_s": 30,
            "fixed": {"relative": True},
            "params": {
                "x": {"type": "number", "min": -1.0, "max": 1.0, "default": 0.0},
                "y": {"type": "number", "min": -1.0, "max": 1.0, "default": 0.0},
            },
            "example": {"x": 0.3},
        },
        "tag": {
            "tool": "tag_location",
            "params": {"location_name": {"type": "string", "required": True, "max_length": 8,
                                         "pattern": "[a-z ]+"}},
            "example": {"location_name": "desk"},
        },
        "stop": {"builtin": "stop"},
    },
    "feeds": {"camera": {"kind": "image", "tool": "observe"}, "task": {"kind": "task"}},
    "stop": {"tools": ["stop_navigation", "end_exploration"], "required": ["stop_navigation"]},
}


def cfg(**changes):
    raw = copy.deepcopy(BASE)
    raw.update(changes)
    return parse(raw)


def test_in_range_params_pass_and_fixed_args_are_added():
    assert cfg().commands["move"].arguments({"x": 0.5}) == {"x": 0.5, "y": 0.0, "relative": True}


@pytest.mark.parametrize("params, message", [
    ({"x": 1.5}, "above the limit"),
    ({"x": -1.01}, "below the limit"),
    ({"x": float("nan")}, "finite"),
    ({"x": "0.5"}, "must be a number"),
    ({"x": True}, "must be a number"),
    ({"speed": 1}, "unknown params"),
    ({"relative": False}, "unknown params"),   # fixed args can't be overridden
])
def test_out_of_range_params_are_refused(params, message):
    with pytest.raises(ParamError, match=message):
        cfg().commands["move"].arguments(params)


def test_string_limits():
    tag = cfg().commands["tag"]
    assert tag.arguments({"location_name": "desk"}) == {"location_name": "desk"}
    with pytest.raises(ParamError, match="required"):
        tag.arguments({})
    with pytest.raises(ParamError, match="longer"):
        tag.arguments({"location_name": "a" * 9})
    with pytest.raises(ParamError, match="not allowed"):
        tag.arguments({"location_name": "desk;rm"})


def test_numbers_need_limits():
    raw = copy.deepcopy(BASE)
    del raw["commands"]["move"]["params"]["x"]["max"]
    with pytest.raises(ConfigError, match="min and max"):
        parse(raw)


def test_strings_need_limits():
    raw = copy.deepcopy(BASE)
    del raw["commands"]["tag"]["params"]["location_name"]["pattern"]
    with pytest.raises(ConfigError, match="max_length and pattern"):
        parse(raw)


def test_background_commands_need_a_runtime_cap():
    raw = copy.deepcopy(BASE)
    del raw["commands"]["move"]["max_runtime_s"]
    with pytest.raises(ConfigError, match="max_runtime_s"):
        parse(raw)


def test_invalid_example_is_rejected():
    raw = copy.deepcopy(BASE)
    raw["commands"]["move"]["example"] = {"x": 3}
    with pytest.raises(ConfigError, match="example"):
        parse(raw)


def test_stop_tools_are_required():
    with pytest.raises(ConfigError, match="stop.tools"):
        cfg(stop={"tools": []})


def test_timeout_must_fit_getbody_deadline():
    with pytest.raises(ConfigError, match="4.5"):
        cfg(mcp={"timeout_s": 6})


def test_plan_and_schemas():
    c = cfg()
    assert c.plan() == {
        "commands": [{"action": "move", "params": {"x": 0.3}},
                     {"action": "tag", "params": {"location_name": "desk"}},
                     {"action": "stop", "params": {}}],
        "feeds": ["camera", "task"],
    }
    schema = c.schemas()["move"]
    assert schema["properties"]["x"] == {"type": "number", "minimum": -1.0, "maximum": 1.0, "default": 0.0}
    assert schema["additionalProperties"] is False
    assert c.tools_used() == {"move_to", "tag_location", "observe", "stop_navigation"}
