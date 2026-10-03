"""最初から載せる道具（PETIT_PRELOAD_TOOLS・2026-10-03）。

claude は MCP の道具が多いと、説明を最初には載せず要るときに ToolSearch で探す（1回に1ターン増える）。
env の PETIT_PRELOAD_TOOLS（カンマ区切りの道具の名前）に書いた道具にだけ、`_meta` の
`anthropic/alwaysLoad: true` を付けて最初から載せる。一覧は petit-env の scripts/preload-tools.txt に
1か所（4つの MCP サーバーぶん）にあり、gen-mcp-config.sh がサーバーごとに分けてこの env に入れる。
空・未設定なら何も付けない。実在しない名前は標準エラーに1回だけ出す（道具の名前を変えたのに一覧が古い）。
"""

from __future__ import annotations

import os
import sys

from mcp.types import Tool

ALWAYS_LOAD = "anthropic/alwaysLoad"
_warned: set[str] = set()


def preload_names(raw: str | None = None) -> list[str]:
    raw = os.environ.get("PETIT_PRELOAD_TOOLS", "") if raw is None else raw
    return [n.strip() for n in raw.split(",") if n.strip()]


def mark_preload(tools: list[Tool], raw: str | None = None) -> list[Tool]:
    """tools のうち一覧にある道具に alwaysLoad を付けて、そのまま返す。"""
    want = set(preload_names(raw))
    for name in sorted(want - {t.name for t in tools} - _warned):
        _warned.add(name)
        print(f"[desire-system] PETIT_PRELOAD_TOOLS の {name} という道具は無い", file=sys.stderr)
    for tool in tools:
        if tool.name in want:
            tool.meta = {**(tool.meta or {}), ALWAYS_LOAD: True}
    return tools
