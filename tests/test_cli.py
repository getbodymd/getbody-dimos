import json

from getbody_dimos.cli import main

from .fake_dimos import FakeDimos

CONFIG = "examples/unitree-go2/config.yaml"


def test_plan_matches_the_committed_example(tmp_path):
    out = tmp_path / "plan.json"
    assert main(["plan", "--config", CONFIG, "-o", str(out)]) == 0
    assert out.read_bytes() == open("examples/unitree-go2/plan.json", "rb").read()


def test_schemas(capsys):
    assert main(["schemas", "--config", CONFIG]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["offered_feeds"] == ["camera", "task"]
    assert out["command_schemas"]["turn"]["required"] == ["degrees"]


def test_check_against_fake_dimos(capsys):
    with FakeDimos() as dimos:
        assert main(["check", "--config", CONFIG, "--mcp-url", dimos.url]) == 0
    assert "stop tools ['stop_navigation', 'end_exploration']" in capsys.readouterr().out


def test_check_fails_when_dimos_is_down(capsys):
    assert main(["check", "--config", CONFIG, "--mcp-url", "http://127.0.0.1:9/mcp"]) == 1
    assert "cannot reach dimos" in capsys.readouterr().err
