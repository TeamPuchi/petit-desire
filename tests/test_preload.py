"""最初から載せる道具（PETIT_PRELOAD_TOOLS）の印（2026-10-03）。"""

from mcp.types import Tool

from petit_desire import preload

ALWAYS = "anthropic/alwaysLoad"


def test_unknown_name_is_reported_once(capsys):
    preload._warned.clear()
    tools = [Tool(name="satisfy_desire", inputSchema={"type": "object"}),
             Tool(name="get_desires", inputSchema={"type": "object"})]
    preload.mark_preload(tools, " satisfy_desire , no_such_tool")
    assert tools[0].meta == {ALWAYS: True} and tools[1].meta is None
    assert "no_such_tool" in capsys.readouterr().err
    preload.mark_preload(tools, "no_such_tool")
    assert capsys.readouterr().err == ""
