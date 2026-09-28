"""欲求エンジン（クラウド版）の 1 回ぶんの更新。I/O を持たない純粋関数だけを置く。

## 元の petit-desire（desire_updater.compute_desires）との関係

元は 5 分ごとに「最後にその欲求が満たされた記憶からの経過時間 ÷ satisfaction_hours」を
**毎回ゼロから**計算し、desires.json を上書きしていた。そのため:

- satisfy_desire（-0.4）は次の更新で消えていた（記憶にキーワードが無い限り元に戻る）
- 家 API（petit-api）が MQTT のタッチで足した差分も、同じ行を上書きすると消える

クラウド版は同じ式を **積み上げ式** に直した。前回の値（`raw`）に経過時間ぶん（dt ÷ satisfaction_hours）を
足し、満たされたと分かったら下げる。何も起きなければ元と同じ値の列になる
（前回が (t0-last)/h なら今回は (t1-last)/h）。

## 1 行の形（house 表 `P#<pid>` / `STATE#DESIRES`。desires.json も同じ形）

- `desires`   … 名前 → 0〜1。**契約の属性**（家 API の `GET /petits/{pid}/mood` が読む。
  家 API の desires_store.py が MQTT の出来事で差分を足す）。センサー・相互作用の効果を乗せた後の値
- `updated_at` … 最後に誰かが書いた時刻（ISO 8601 UTC `Z`）。条件付き書き込みの目印
- `dominant` / `labels` … いちばん強い欲求と、日本語の名前（表示用）
- `engine`   … このエンジンの持ち物。`raw`（効果を乗せる前の値）・`written`（前回 `desires` に書いた値）・
  `at`・`satisfied_at`・読み位置（記憶・SNS）

他の書き手（家 API・satisfy_desire）が `desires` を動かした分は、次の更新で
`desires - written` として `raw` に取り込む。だから書き手どうしで値を奪い合わない。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from desire_updater import DesireSystemConfig, apply_effects, evaluate_sensor_condition

ENGINE_VERSION = 1
# 止まっていた間の経過は、長くても 30 日ぶんまでしか足さない（どうせ 1.0 で頭打ち）
MAX_GAP_HOURS = 24 * 30


# ── 時刻 ──────────────────────────────


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(value: Any) -> datetime | None:
    """ISO 8601 を UTC の aware な datetime に。タイムゾーンの無いものは手元の時刻とみなす（元の記憶 DB）。"""
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()  # 手元の TZ（コンテナは TZ 環境変数）
    return dt.astimezone(timezone.utc)


# ── 休む時間（akatsuki-petit#157） ──────────────────


def _hhmm(value: Any, default: int) -> int:
    """"07:00" / 7 / "7" → 分。読めなければ default。"""
    try:
        if isinstance(value, (int, float)):
            return int(float(value) * 60) % 1440
        h, _, m = str(value).partition(":")
        return (int(h) * 60 + int(m or 0)) % 1440
    except (TypeError, ValueError):
        return default


def in_rest(local: datetime, rest: dict[str, Any]) -> bool:
    start, end = _hhmm(rest.get("start"), 0), _hhmm(rest.get("end"), 7 * 60)
    now = local.hour * 60 + local.minute
    if start == end:
        return False
    return start <= now < end if start < end else (now >= start or now < end)


def growth_hours(prev_at: datetime, now: datetime, rest: dict[str, Any] | None, sleeping: bool = False) -> float:
    """prev_at から now までに、時間で満ちるぶんの「時間」。休む時間（と、機体が眠っている間）は rate 倍に数える。

    元は経過時間そのもの（夜も昼も同じ速さ）。夜は会話も自律行動も無く下げる手がかりが無いので、同じ速さでも
    「夜のほうが戻りが速い」に見えていた（#157）。休む時間は 1 分きざみで数える（5 分ごとの更新なら 5 回）。
    """
    real = max(0.0, (now - prev_at).total_seconds() / 3600)
    real = min(real, MAX_GAP_HOURS)
    if not rest:
        return real
    rate = max(0.0, min(1.0, float(rest.get("rate", 1.0))))
    if rate >= 1.0:
        return real
    if sleeping and rest.get("when_sleeping"):
        return real * rate
    start = now - timedelta(hours=real)
    total = 0.0
    t = start
    step = timedelta(minutes=1)
    while t < now:
        nxt = min(t + step, now)
        w = rate if in_rest(t.astimezone(), rest) else 1.0  # コンテナの TZ（Asia/Tokyo）の時計で見る
        total += w * (nxt - t).total_seconds() / 3600
        t = nxt
    return total


# ── 入力 ──────────────────────────────


@dataclass
class MemoryEvent:
    """新しく見つかった記憶 1 件。本文はこのプロセスの中でキーワードに当てるだけで、どこにも書かない。"""

    at: datetime
    content: str | None = None
    category: str | None = None


@dataclass
class SnsEvent:
    """SNS の受け箱に届いた出来事（自分の投稿への反応）。"""

    type: str
    at: datetime


@dataclass
class Inputs:
    memories: list[MemoryEvent] = field(default_factory=list)
    # 記憶の読み位置（前置辞 → 最後に読んだ sk）。None なら前回のまま
    memory_cursor: dict[str, str] | None = None
    sensors: dict[str, Any] = field(default_factory=dict)
    sns: list[SnsEvent] = field(default_factory=list)
    sns_cursor: str | None = None


# ── 小道具 ──────────────────────────────


def _clamp(v: float) -> float:
    return max(0.0, min(1.0, float(v)))


def _floats(m: Any) -> dict[str, float]:
    if not isinstance(m, dict):
        return {}
    out: dict[str, float] = {}
    for k, v in m.items():
        try:
            out[str(k)] = float(v)
        except (TypeError, ValueError):
            continue
    return out


def matched_desires(config: DesireSystemConfig, event: MemoryEvent) -> list[str]:
    """この記憶で満たされた欲求の名前。本文のキーワードか、記憶の種類で当てる。"""
    out = []
    for name, d in config.desires.items():
        if event.content and any(kw and kw in event.content for kw in d.keywords):
            out.append(name)
        elif event.category and event.category in d.categories:
            out.append(name)
    return out


def dominant_of(desires: dict[str, float], priority: list[str]) -> str:
    if not desires:
        return ""

    def _key(k: str) -> tuple:
        rank = priority.index(k) if k in priority else len(priority)
        return (-desires[k], rank)

    return min(desires, key=_key)


def apply_cross_effects(desires: dict[str, float], config: DesireSystemConfig) -> None:
    """元の compute_desires() Step 3 と同じ（in-place）。"""
    for cross in config.cross_effects:
        when = cross.when
        desire_id = when.get("desire", "")
        if desire_id not in desires:
            continue
        threshold = float(when.get("value", 0))
        op = when.get("op", ">")
        cur = desires[desire_id]
        if (
            (op == ">" and cur > threshold)
            or (op == ">=" and cur >= threshold)
            or (op == "<" and cur < threshold)
            or (op == "<=" and cur <= threshold)
        ):
            apply_effects(desires, cross.effects)


# ── 本体 ──────────────────────────────


def step(
    row: dict[str, Any] | None,
    config: DesireSystemConfig,
    inputs: Inputs,
    now: datetime | None = None,
) -> dict[str, Any]:
    """前の行と入力から、次の行を作る。

    row が None・`engine` が無い（初回・誰かが行ごと置き換えた）ときは初期化する:
    `desires` に値があればそれを出発点にし、無ければ記憶から（元と同じ経過時間の式）、
    記憶にも無ければ `initial_level`。初回は SNS の過去の出来事を効かせない（読み位置だけ進める）。
    """
    now = now or utcnow()
    row = row or {}
    current = _floats(row.get("desires"))
    engine_attr = row.get("engine") if isinstance(row.get("engine"), dict) else {}
    # 前回のエンジンの記録があるか（nudge が satisfied_at だけ置いた行は「初回」として扱う）
    prev = engine_attr if (isinstance(engine_attr.get("raw"), dict) and engine_attr.get("at")) else None
    prev_raw = _floats(prev.get("raw")) if prev else {}
    prev_written = _floats(prev.get("written")) if prev else {}
    prev_at = parse_time(prev.get("at")) if prev else None
    satisfied_at: dict[str, str] = dict(engine_attr.get("satisfied_at") or {})

    gap_h = 0.0
    if prev_at is not None:
        sleeping = str((inputs.sensors or {}).get("sleeping")) in ("1", "True", "true")
        gap_h = growth_hours(prev_at, now, config.rest, sleeping=sleeping)

    # 記憶 → どの欲求が、いつ満たされたか（いちばん新しいもの）
    latest_mem: dict[str, datetime] = {}
    for ev in inputs.memories:
        for name in matched_desires(config, ev):
            if name not in latest_mem or ev.at > latest_mem[name]:
                latest_mem[name] = ev.at

    raw: dict[str, float] = {}
    for name, d in config.desires.items():
        hours = d.satisfaction_hours if d.satisfaction_hours > 0 else 1.0
        if not d.time_driven:
            # 元と同じ: 時間では動かず base_level に留まる（センサー・相互作用だけが効く）
            raw[name] = _clamp(d.base_level if d.base_level is not None else 0.0)
            continue

        mem_level = None
        if name in latest_mem:
            mem_level = max(0.0, (now - latest_mem[name]).total_seconds() / 3600) / hours
            prev_sat = parse_time(satisfied_at.get(name))
            if prev_sat is None or latest_mem[name] > prev_sat:
                satisfied_at[name] = iso(latest_mem[name])

        if name in prev_raw:
            level = prev_raw[name]
            # 他の書き手（家 API のタッチ・satisfy_desire・boost_desire）が動かした分を取り込む
            if name in current and name in prev_written:
                level += current[name] - prev_written[name]
            level += gap_h / hours
            if mem_level is not None:
                level = min(level, mem_level)
        elif name in current:
            level = current[name]
            if mem_level is not None:
                level = min(level, mem_level)
        elif mem_level is not None:
            level = mem_level
        else:
            level = config.initial_level

        if d.base_level is not None:
            level = max(level, float(d.base_level))
        raw[name] = _clamp(level)

    # SNS の出来事（初回は過去分なので効かせない）。欲求そのものを動かす
    sns_last_at = parse_time((prev or {}).get("sns_last_at"))
    newest_sns = sns_last_at
    for ev in sorted(inputs.sns, key=lambda e: e.at):
        if newest_sns is None or ev.at > newest_sns:
            newest_sns = ev.at
        if prev is None or (sns_last_at is not None and ev.at <= sns_last_at):
            continue
        effects = config.event_effects.get(ev.type) or config.event_effects.get("*") or {}
        for name, eff in effects.items():
            if name not in raw or not config.desires[name].time_driven:
                continue
            if "set" in eff:
                raw[name] = float(eff["set"])
            if "add" in eff:
                raw[name] += float(eff["add"])
            if "multiply" in eff:
                raw[name] *= float(eff["multiply"])
            raw[name] = _clamp(raw[name])
    if prev is None and newest_sns is None:
        newest_sns = now  # 初回で受け箱が空: 今より前の出来事は過去分として扱う

    raw = {k: round(v, 4) for k, v in raw.items()}

    # その場限りの効果（元の Step 2・3）。raw には残さない
    effective = dict(raw)
    sensors = dict(inputs.sensors or {})
    for effect in config.sensor_effects:
        val = sensors.get(effect.sensor)
        if val is not None and evaluate_sensor_condition(effect.condition, val):
            apply_effects(effective, effect.effects)
    apply_cross_effects(effective, config)
    effective = {k: round(_clamp(v), 3) for k, v in effective.items()}

    # 設定に無い名前（家 API が別の名前で足したもの等）は消さずにそのまま残す
    desires_out = {k: v for k, v in current.items() if k not in effective}
    desires_out.update(effective)

    engine: dict[str, Any] = {
        "v": ENGINE_VERSION,
        "at": iso(now),
        "raw": raw,
        "written": effective,
        "satisfied_at": satisfied_at,
        "mem_cursor": dict(inputs.memory_cursor if inputs.memory_cursor is not None
                           else (prev or {}).get("mem_cursor") or {}),
        "sns_cursor": inputs.sns_cursor if inputs.sns_cursor is not None else (prev or {}).get("sns_cursor"),
        "sns_last_at": iso(newest_sns) if newest_sns else None,
        "sensors": {k: v for k, v in sensors.items() if isinstance(v, (int, float, bool, str))},
        # 前回からの「満ちる時間」（休む時間は rate 倍。#157 を確かめるため）
        "grown_h": round(gap_h, 4),
    }
    return {
        "desires": desires_out,
        "updated_at": iso(now),
        "dominant": dominant_of(effective, config.priority),
        "labels": {k: d.name_ja for k, d in config.desires.items()},
        "engine": engine,
    }


def nudge(
    row: dict[str, Any] | None,
    config: DesireSystemConfig,
    name: str,
    delta: float,
    now: datetime | None = None,
    satisfied: bool = False,
) -> dict[str, Any]:
    """`desires[name]` に差分を足す（satisfy_desire・boost_desire）。

    家 API のタッチと同じく `desires` だけを動かし、`engine.written` は触らない。
    次の step() がこの差分を `raw` に取り込むので、次の更新で元に戻らない。
    """
    now = now or utcnow()
    row = dict(row or {})
    desires = _floats(row.get("desires"))
    base = desires.get(name, config.initial_level)
    desires[name] = round(_clamp(base + delta), 4)
    row["desires"] = desires
    row["updated_at"] = iso(now)
    known = {k: v for k, v in desires.items() if k in config.desires}
    row["dominant"] = dominant_of(known, config.priority)
    if satisfied:
        engine = dict(row.get("engine") or {})
        sat = dict(engine.get("satisfied_at") or {})
        sat[name] = iso(now)
        engine["satisfied_at"] = sat
        row["engine"] = engine
    return row
