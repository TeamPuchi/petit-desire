"""Desire System MCP Server（クラウド版）。

ツールの名前・引数・効き目は元の server.py と同じ（get_desires / satisfy_desire / boost_desire）。
petit-env の autonomous-action.sh は `mcp__desire-system__*` を許可しているので、
サーバー名は `desire-system` のまま。

違うのは置き場だけ: 家の表 `STATE#DESIRES`（無ければ desires.json）を読み書きし、
satisfy_desire の効き目が次の更新で消えない（engine.py の docstring）。

akatsuki-petit#106: ぷちが欲求の形を自分で変えられる。shape_desire（足す・形を変える）・retire_desire（手放す）。
satisfy_desire は欲求ごとの満たし方（satisfy_amount）で下げ、amount を渡せばその分だけ下げる。
satisfy・boost は設定の欲求だけでなく、ぷちが足した欲求も受け取る（shape.py）。
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from .service import BOOST_MAX, DesireService
from .shape import HOURS_MAX, HOURS_MIN, MAX_DESIRES, SATISFY_MAX, SATISFY_MIN, ShapeError, satisfy_amount_of

server = Server("desire-system")
_service: DesireService | None = None


def service() -> DesireService:
    global _service
    if _service is None:
        pid = os.environ.get("CHARACTER_ID") or os.environ.get("PETIT_DESIRE_PETIT_ID") or ""
        _service = DesireService.from_env(pid)
    return _service


def _config(svc: DesireService | None):
    if svc is None:
        return None
    try:
        return svc.config_for(svc.store.read())
    except Exception:  # 表が読めなくても道具の一覧は出す（生まれつきの形で）
        return svc.config


def _describe(svc: DesireService | None) -> str:
    parts = [
        "Get current desire levels (your mood right now). ",
        "IMPORTANT: Act immediately on any desire with level >= 0.7 — decide yourself what to do to satisfy it. ",
    ]
    cfg = _config(svc)
    if cfg is not None:
        for did, d in cfg.desires.items():
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
    cfg = _config(svc)
    names = ", ".join(cfg.desires) if cfg else "(get_desires で確認)"
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
                "The level drops by that desire's own satisfy amount (0.4 unless you shaped it with shape_desire), "
                "or by `amount` if you pass one. Call again if still high. "
                "欲求を満たしたら記録する。下がる量は欲求ごとの満たし方"
                "（shape_desire で決められる。決めていなければ 0.4）。"
                "今回だけ違う量にしたいときは amount を渡す。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "desire_name": {"type": "string", "description": f"Name of the desire to satisfy: {names}"},
                    "amount": {
                        "type": "number",
                        "description": ("How much to lower it this time (0.05-1.0). "
                                        "Omit to use the desire's own amount."),
                    },
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
        Tool(
            name="shape_desire",
            description=(
                "自分の欲求の形を決める。無い名前なら新しい欲求として足し、ある名前なら形を変える"
                "（渡した項目だけ変わる。省いた項目は前のまま）。決めた形は次の更新・次の自律行動から効く。"
                "たとえば「確かめたい」「残したい」「じっと見ていたい」「無事でいてほしい」「黙っていたい」のように、"
                "自分で気づいた気持ちを足してよい。"
                "生まれつきの欲求（" + names + "）の満ちる速さ・満たし方も変えられる。"
                f"欲求は全部で {MAX_DESIRES} 個まで。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "desire_name": {
                        "type": "string",
                        "description": "欲求の名前（日本語でよい。24 字まで。空白と . # $ / などの記号は使えない）",
                    },
                    "name_ja": {"type": "string", "description": "表示の名前（省けば desire_name と同じ）"},
                    "description": {"type": "string", "description": "どんな気持ちか（200 字まで）"},
                    "satisfaction_hours": {
                        "type": "number",
                        "description": f"0 から 1 まで満ちるのにかかる時間（{HOURS_MIN}〜{HOURS_MAX:g} 時間）。"
                                       "新しい欲求には必ず渡す。長いほどゆっくり強くなる",
                    },
                    "satisfy_amount": {
                        "type": "number",
                        "description": f"satisfy_desire 1 回で下がる量（{SATISFY_MIN}〜{SATISFY_MAX}）。省けば 0.4",
                    },
                    "keywords": {
                        "type": "array", "items": {"type": "string"},
                        "description": "記憶（remember）にこの言葉があれば、満たされたと数える（例: 「確かめた」）",
                    },
                    "level": {"type": "number", "description": "新しい欲求のいまの強さ（0〜1）。省けば 0.5"},
                },
                "required": ["desire_name"],
            },
        ),
        Tool(
            name="retire_desire",
            description=(
                "欲求を手放す（計算にも表示にも出なくなる）。生まれつきの欲求も手放せる。"
                "同じ名前で shape_desire を呼べば戻ってくる。"
            ),
            inputSchema={
                "type": "object",
                "properties": {"desire_name": {"type": "string", "description": f"手放す欲求の名前: {names}"}},
                "required": ["desire_name"],
            },
        ),
    ]


def call_tool_sync(name: str, arguments: dict[str, Any]) -> str:
    svc = service()

    if name == "get_desires":
        return svc.format(svc.current(refresh_if_stale=True))

    if name == "shape_desire":
        desire = str(arguments.get("desire_name", "")).strip()
        kws = arguments.get("keywords")
        try:
            row, added = svc.shape(
                desire,
                name_ja=arguments.get("name_ja"),
                description=arguments.get("description"),
                satisfaction_hours=arguments.get("satisfaction_hours"),
                satisfy_amount=arguments.get("satisfy_amount"),
                keywords=list(kws) if isinstance(kws, list) else None,
                level=arguments.get("level"),
            )
        except (ShapeError, ValueError, TypeError) as e:
            return f"形を決められなかった: {e}"
        d = svc.config_for(row).desires[desire]
        head = "新しい欲求を足した" if added else "欲求の形を変えた"
        return (f"[形] {head}: {d.name_ja}（{desire}）… 0→1 まで {d.satisfaction_hours:g} 時間・"
                f"満たすと −{satisfy_amount_of(d):g}\n次の更新（5 分ごと）から効く。\n\n{svc.format(row)}")

    if name == "retire_desire":
        desire = str(arguments.get("desire_name", "")).strip()
        try:
            row = svc.retire(desire)
        except (ShapeError, ValueError) as e:
            return f"手放せなかった: {e}"
        return f"[形] 「{desire}」を手放した。shape_desire で同じ名前を渡せば戻る。\n\n{svc.format(row)}"

    if name in ("satisfy_desire", "boost_desire"):
        desire = str(arguments.get("desire_name", ""))
        cfg = svc.config_for(svc.store.read())
        if desire not in cfg.desires:
            return f"欲求名が不正: {desire}. 有効: {list(cfg.desires)}（新しい欲求は shape_desire で足せる）"
        label = cfg.desires[desire].name_ja
        if name == "satisfy_desire":
            amount = arguments.get("amount")
            amount = svc.satisfy_amount(desire) if amount is None else max(SATISFY_MIN, min(1.0, float(amount)))
            row = svc.satisfy(desire, amount)
            level = svc.levels(row).get(desire, 0.0)
            return f"[満足] {label} -{amount:g} → {level:.3f}\n\n{svc.format(row)}"
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
