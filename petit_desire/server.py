"""Desire System MCP Server（クラウド版）。

ツールの名前・引数・効き目は元の server.py と同じ（get_desires / satisfy_desire / boost_desire）。
petit-env の autonomous-action.sh は `mcp__desire-system__*` を許可しているので、
サーバー名は `desire-system` のまま。

違うのは置き場だけ: 家の表 `STATE#DESIRES`（無ければ desires.json）を読み書きし、
satisfy_desire の効き目が次の更新で消えない（engine.py の docstring）。
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from .service import BOOST_MAX, SATISFY_AMOUNT, DesireService

server = Server("desire-system")
_service: DesireService | None = None


def service() -> DesireService:
    global _service
    if _service is None:
        pid = os.environ.get("CHARACTER_ID") or os.environ.get("PETIT_DESIRE_PETIT_ID") or ""
        _service = DesireService.from_env(pid)
    return _service


def _describe(svc: DesireService | None) -> str:
    parts = [
        "Get current desire levels (your mood right now). ",
        "IMPORTANT: Act immediately on any desire with level >= 0.7 — decide yourself what to do to satisfy it. ",
    ]
    if svc is not None:
        for did, d in svc.config.desires.items():
            parts.append(f"{did}({d.name_ja}): {d.description}; ")
    parts.append(
        "After acting, call satisfy_desire to record satisfaction. "
        "Keep acting until level drops below 0.5. "
        "Desires drive behavior - do not just report them."
    )
    return "".join(parts)


@server.list_tools()
async def list_tools() -> list[Tool]:
    try:
        svc: DesireService | None = service()
    except Exception:
        svc = None
    names = ", ".join(svc.config.desires) if svc else "(get_desires で確認)"
    return [
        Tool(
            name="get_desires",
            description=_describe(svc),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="satisfy_desire",
            description=(
                "Satisfy a desire after taking an action. Pass the desire_name you just acted on. "
                f"The level drops by {SATISFY_AMOUNT}. Call again if still high."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "desire_name": {"type": "string", "description": f"Name of the desire to satisfy: {names}"},
                },
                "required": ["desire_name"],
            },
        ),
        Tool(
            name="boost_desire",
            description=(
                "Boost a desire level due to novelty/surprise (dopamine/prediction error). "
                "Call when you feel surprised or encounter unexpected info."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "desire_name": {"type": "string", "description": f"Name of the desire to boost: {names}"},
                    "amount": {
                        "type": "number",
                        "description": f"Amount to boost (0.1-{BOOST_MAX}). 0.2=mild surprise, 0.5=strong.",
                    },
                },
                "required": ["desire_name", "amount"],
            },
        ),
    ]


def call_tool_sync(name: str, arguments: dict[str, Any]) -> str:
    svc = service()
    labels = {k: d.name_ja for k, d in svc.config.desires.items()}

    if name == "get_desires":
        return svc.format(svc.current(refresh_if_stale=True))

    if name in ("satisfy_desire", "boost_desire"):
        desire = str(arguments.get("desire_name", ""))
        if desire not in svc.config.desires:
            return f"欲求名が不正: {desire}. 有効: {list(svc.config.desires)}"
        label = labels.get(desire, desire)
        if name == "satisfy_desire":
            row = svc.satisfy(desire)
            level = svc.levels(row).get(desire, 0.0)
            return f"[満足] {label} -{SATISFY_AMOUNT:.1f} → {level:.3f}\n\n{svc.format(row)}"
        row, amount = svc.boost(desire, float(arguments.get("amount", 0.2)))
        level = svc.levels(row).get(desire, 0.0)
        return f"[ドーパミン] {label} +{amount:.1f} → {level:.3f}"

    return f"Unknown tool: {name}"


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    try:
        text = await asyncio.to_thread(call_tool_sync, name, arguments or {})
    except Exception as e:  # 表・SNS が落ちていてもツールとしてはエラー文を返す（本文は含めない）
        text = f"欲求システムに届かなかった: {type(e).__name__}"
        print(f"[desire-system] {name} failed: {e!r}", file=sys.stderr)
    return [TextContent(type="text", text=text)]


async def run_server() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    asyncio.run(run_server())


if __name__ == "__main__":
    main()
