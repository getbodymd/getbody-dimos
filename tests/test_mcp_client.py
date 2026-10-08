import pytest

from getbody_dimos.mcp_client import McpClient, McpError, parse_content

from .fake_dimos import JPEG, FakeDimos


@pytest.fixture
def dimos():
    with FakeDimos(move_duration_s=0.2, start_delay_s=0.0, settle_s=0.1) as fake:
        yield fake


def test_initialize_and_list_tools(dimos):
    client = McpClient(dimos.url)
    assert client.initialize()["serverInfo"]["name"] == "dimensional"
    tools = client.list_tools()
    assert "move_to" in tools and "stop_navigation" in tools


def test_call_tool_text(dimos):
    result = McpClient(dimos.url).call_tool("tag_location", {"location_name": "desk"})
    assert not result.is_error
    assert result.text.startswith("Tagged 'desk'")


def test_unknown_tool_is_an_error_even_without_isError(dimos):
    result = McpClient(dimos.url).call_tool("does_not_exist")
    assert result.is_error
    assert "Tool not found" in result.text


def test_tool_exception_is_an_error(dimos):
    dimos.fail_stop = True
    assert McpClient(dimos.url).call_tool("stop_navigation").is_error


def test_openai_style_image_is_decoded(dimos):
    result = McpClient(dimos.url).call_tool("observe")
    assert result.images == [{"mime_type": "image/jpeg", "data": JPEG}]


def test_mcp_style_image_is_decoded():
    result = parse_content({"content": [{"type": "image", "data": "abc", "mimeType": "image/png"}]})
    assert result.images == [{"mime_type": "image/png", "data": "abc"}]


def test_busy_capability_refusal_is_an_error():
    text = "Cannot start 'move_to': capability 'movement' is held by 'begin_exploration'."
    assert parse_content({"content": [{"type": "text", "text": text}]}).is_error


def test_timeout_raises_timeout_error(dimos):
    dimos.move_duration_s = 2.0
    with pytest.raises(TimeoutError):
        McpClient(dimos.url).call_tool("move_to", {"x": 0.1}, timeout_s=0.3)


def test_unreachable_server_raises():
    with pytest.raises(McpError):
        McpClient("http://127.0.0.1:9/mcp", timeout_s=0.5).list_tools()
