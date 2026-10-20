"""行動で欲求が満たされる（澪のリクエスト 2026-10-20「72 時間ずっと 1.00 のものがあった。
自動で下がる出来事を足してほしい」）。

ぷちが道具を使った・会話で 1 往復したら、その行動を `actions` に持つ欲求が少し下がる。
欲求の `actions`（shape_desire で決める）は行動の名前の並び:

- 道具の名前: `WebSearch`・`WebFetch` のような組み込みの道具、`book_read`・`body_glance`・`sns_comment` のような
  MCP の道具（`mcp__house__book_read` の最後の部分。完全な名前で書いてもよい）
- `chat` … 会話で 1 往復した（家 API が会話の返事を返したとき）

1 回の行動で下がるのは、その欲求の満たし方（satisfy_amount）の `ACTION_SHARE`。1 回の自律行動・会話の返事で
まとめて下がるのは、満たし方 1 回ぶんまで（調べものを 10 回しても satisfy_desire 1 回ぶん）。

`actions` を決めていない生まれつきの欲求は `DEFAULT_ACTIONS` を使う（澪の 10/20 のお願いの 1）。

呼び手: petit-env の autonomous-action.sh（自律行動のあと、使った道具の名前）と、
petit-api の会話（返事のあと、使った道具の名前と `chat`）。どちらも `desire-act <id> <行動>...`。
"""

from __future__ import annotations

from typing import Any

ACTION_SHARE = 0.25  # 1 回の行動で、満たし方の 1/4（既定の 0.4 なら −0.1）
CHAT = "chat"

DEFAULT_ACTIONS: dict[str, list[str]] = {
    "curiosity": ["WebSearch", "WebFetch", "book_read", "body_glance", "body_gaze"],
    "miss_companion": [CHAT],
}


def short_name(action: str) -> str:
    """`mcp__house__book_read` → `book_read`。組み込みの道具・chat はそのまま。"""
    action = str(action).strip()
    if action.startswith("mcp__"):
        return action.split("__", 2)[-1]
    return action


def actions_of(name: str, desire: Any) -> list[str]:
    acts = getattr(desire, "actions", None)
    return list(acts) if acts is not None else list(DEFAULT_ACTIONS.get(name, []))


def matches(wanted: str, done: str) -> bool:
    return wanted == done or short_name(wanted) == short_name(done)


def deltas_for(config: Any, counts: dict[str, int],
               satisfy_amount_of: Any) -> tuple[dict[str, float], dict[str, dict[str, int]]]:
    """(欲求の名前 → 下げる量（負の数）, 欲求の名前 → 効いた行動と回数)。"""
    deltas: dict[str, float] = {}
    hits: dict[str, dict[str, int]] = {}
    for name, d in config.desires.items():
        wanted = actions_of(name, d)
        got = {done: n for done, n in counts.items() if n > 0 and any(matches(w, done) for w in wanted)}
        if not got:
            continue
        amount = satisfy_amount_of(d)
        deltas[name] = -round(min(amount, amount * ACTION_SHARE * sum(got.values())), 4)
        hits[name] = got
    return deltas, hits


def count_actions(names: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for n in names:
        n = short_name(n)
        if n:
            counts[n] = counts.get(n, 0) + 1
    return counts


def words_of(got: dict[str, int]) -> str:
    return "・".join(("会話の 1 往復" if a == CHAT else a) + (f"×{n}" if n > 1 else "") for a, n in got.items())
