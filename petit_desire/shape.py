"""ぷちが自分で決める欲求の形（akatsuki-petit#106）。

設定ファイル（desire_config.json・無ければ defaults.py）は里親・開発側が置く「生まれつきの形」。
その上に、ぷち自身が決めた形を欲求の行（house 表 `STATE#DESIRES`／desires.json）の属性 `shape` に重ねる。

    shape = {
      "<欲求の名前>": {                   # 新しい欲求を足す／生まれつきの欲求の形を変える
        "name_ja": "確かめたい",          # 表示の名前（省けば名前そのまま）
        "description": "…",              # どんな気持ちか（自律行動・get_desires に出る）
        "satisfaction_hours": 6,          # 0 から 1 まで満ちるのにかかる時間（時間）
        "satisfy_amount": 0.2,            # satisfy_desire 1 回で下がる量
        "keywords": ["確かめた"],         # 記憶にこの言葉があれば満たされたと数える
        "by": "petit", "at": "…Z",
      },
      "<名前>": {"retired": true, ...},   # 手放した（計算にも表示にも出さない。define しなおせば戻る）
    }

行に置くので、コンテナを作り直しても・cron と MCP のどちらから読んでも同じ形になる。
"""

from __future__ import annotations

import copy
import re
from typing import Any

from desire_updater import DesireConfig, DesireSystemConfig

NAME_MAX = 24
# 名前は日本語でよい（「確かめたい」）。表の Map のキーに使うので、空白と記号の一部だけ断る
_BAD_NAME = re.compile(r"[\s.#$/\\\"'`{}\[\]<>]")
HOURS_MIN, HOURS_MAX = 0.25, 24.0 * 14
SATISFY_MIN, SATISFY_MAX = 0.05, 1.0
MAX_DESIRES = 12  # 欲求の数の上限（生まれつきのものも含む）。多すぎると自律行動のプロンプトが膨らむ
DEFAULT_SATISFY = 0.4  # 元の server.py と同じ


class ShapeError(ValueError):
    """ぷちに返す理由（日本語）。"""


def check_name(name: str) -> str:
    name = (name or "").strip()
    if not name or len(name) > NAME_MAX:
        raise ShapeError(f"名前は 1〜{NAME_MAX} 字")
    if _BAD_NAME.search(name):
        raise ShapeError("名前に空白や記号（. # $ / \\ \" ' ` { } [ ] < >）は使えない")
    return name


def shape_of(row: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    shape = (row or {}).get("shape")
    return {str(k): dict(v) for k, v in shape.items() if isinstance(v, dict)} if isinstance(shape, dict) else {}


def apply_shape(config: DesireSystemConfig, shape: dict[str, dict[str, Any]] | None) -> DesireSystemConfig:
    """生まれつきの設定に、ぷちが決めた形を重ねた設定（元の config は変えない）。"""
    if not shape:
        return config
    cfg = copy.copy(config)
    desires = dict(config.desires)
    priority = list(config.priority)
    for name, spec in shape.items():
        if spec.get("retired"):
            desires.pop(name, None)
            continue
        base = copy.copy(desires[name]) if name in desires else DesireConfig(
            name_ja=name, description="", satisfaction_hours=3.0, keywords=[])
        if spec.get("name_ja"):
            base.name_ja = str(spec["name_ja"])
        if spec.get("description") is not None:
            base.description = str(spec["description"])
        if spec.get("satisfaction_hours") is not None:
            base.satisfaction_hours = float(spec["satisfaction_hours"])
        if spec.get("satisfy_amount") is not None:
            base.satisfy_amount = float(spec["satisfy_amount"])
        if spec.get("keywords") is not None:
            base.keywords = [str(k) for k in spec["keywords"] if str(k).strip()]
        desires[name] = base
    cfg.desires = desires
    cfg.priority = [p for p in priority if p in desires] + [n for n in desires if n not in priority]
    return cfg


def satisfy_amount_of(d: DesireConfig) -> float:
    v = getattr(d, "satisfy_amount", None)
    return float(v) if v is not None else DEFAULT_SATISFY


def new_spec(*, name_ja: str | None, description: str | None, satisfaction_hours: float | None,
             satisfy_amount: float | None, keywords: list[str] | None) -> dict[str, Any]:
    """道具の引数から shape の 1 件を作る（範囲外は断る。省いたものは入れない＝前の値のまま）。"""
    spec: dict[str, Any] = {}
    if name_ja is not None and str(name_ja).strip():
        if len(str(name_ja)) > NAME_MAX:
            raise ShapeError(f"name_ja は {NAME_MAX} 字まで")
        spec["name_ja"] = str(name_ja).strip()
    if description is not None and str(description).strip():
        if len(str(description)) > 200:
            raise ShapeError("description は 200 字まで")
        spec["description"] = str(description).strip()
    if satisfaction_hours is not None:
        h = float(satisfaction_hours)
        if not HOURS_MIN <= h <= HOURS_MAX:
            raise ShapeError(f"satisfaction_hours は {HOURS_MIN}〜{HOURS_MAX:g} 時間")
        spec["satisfaction_hours"] = h
    if satisfy_amount is not None:
        a = float(satisfy_amount)
        if not SATISFY_MIN <= a <= SATISFY_MAX:
            raise ShapeError(f"satisfy_amount は {SATISFY_MIN}〜{SATISFY_MAX}")
        spec["satisfy_amount"] = a
    if keywords is not None:
        kws = [str(k).strip() for k in keywords if str(k).strip()]
        if len(kws) > 20:
            raise ShapeError("keywords は 20 個まで")
        spec["keywords"] = kws
    return spec
