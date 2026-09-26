"""
Desire Updater v2 - キャラクター別コンフィグ駆動の欲求システム。

desire_config.json から全欲求定義を読み込み、3段階で計算:
  Step 1: 時間ベース計算（memory DBからキーワード検索 → 経過時間）
  Step 2: センサー効果の適用（M5の /sensors エンドポイントにHTTPリクエスト）
  Step 3: 欲求間の相互作用（cross_effects を評価）

cronで5分ごとに実行:
  */5 * * * * cd /path/to/desire-system && uv run python desire_updater.py <character_id>
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

load_dotenv()  # カレントディレクトリの .env
load_dotenv(Path(os.getenv("PETIT_DATA_DIR", str(Path.home() / "petit_data"))) / ".env")

logger = logging.getLogger("desire-updater")

# キャラクターID（コマンドライン引数 or 環境変数）
CHARACTER_ID = sys.argv[1] if len(sys.argv) > 1 else os.getenv("CHARACTER_ID", "default")
DATA_DIR = Path(os.getenv("PETIT_DATA_DIR", str(Path.home() / "petit_data")))

# SQLite DB パス（memory-mcp が使うパス）
_default_memory_db = str(Path.home() / ".claude" / "memories" / CHARACTER_ID / "memory.db")
MEMORY_DB_PATH = Path(os.getenv("MEMORY_DB_PATH", _default_memory_db))

# 欲求レベル出力先（キャラクター別）
_default_desires_path = str(DATA_DIR / "characters" / CHARACTER_ID / "data" / "desires.json")
DESIRES_PATH = Path(os.getenv("DESIRES_PATH", _default_desires_path))

# 一緒にいる人の名前（miss_companion 欲求で使う）。無ければ自律行動と同じ PETIT_USER_NAME
COMPANION_NAME = os.getenv("COMPANION_NAME") or os.getenv("PETIT_USER_NAME") or "あなた"


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

@dataclass
class DesireConfig:
    """1つの欲求の設定。"""
    name_ja: str
    description: str
    satisfaction_hours: float
    keywords: list[str]
    color: str = "#cab8d9"
    base_level: float | None = None
    time_driven: bool = True
    # クラウド版（petit_desire.engine）: 本文を読めない記憶でも、種類（category）で満たされたと数える
    categories: list[str] = field(default_factory=list)


@dataclass
class SensorEffect:
    """センサー値に基づく欲求への効果。"""
    sensor: str
    condition: dict[str, Any]
    effects: dict[str, dict[str, float]]
    description: str = ""


@dataclass
class CrossEffect:
    """欲求間の相互作用。"""
    when: dict[str, Any]
    effects: dict[str, dict[str, float]]
    description: str = ""


@dataclass
class DesireSystemConfig:
    """欲求システム全体の設定。"""
    desires: dict[str, DesireConfig]
    sensor_effects: list[SensorEffect]
    cross_effects: list[CrossEffect]
    priority: list[str]
    # クラウド版: SNS の受け箱に届いた出来事（comment / like / touch / snack / "*"）→ 欲求の効果。
    # センサー効果と違い、その場限りではなく欲求そのものを動かす（触れ合いの差分と同じ扱い）
    event_effects: dict[str, dict[str, dict[str, float]]] = field(default_factory=dict)
    # クラウド版: 記憶にも手がかりが無い欲求の出発点（家 API の desires_store.DEFAULT_BASE と同じ 0.5）
    initial_level: float = 0.5


def load_desire_config(char_id: str, data_dir: Path | None = None) -> DesireSystemConfig:
    """desire_config.json を読み込む。"""
    if data_dir is None:
        data_dir = DATA_DIR
    config_path = data_dir / "characters" / char_id / "config" / "desire_config.json"
    if not config_path.exists():
        raise FileNotFoundError(f"desire_config.json が見つかりません: {config_path}")

    raw = json.loads(config_path.read_text(encoding="utf-8"))
    return parse_desire_config(raw)


def parse_desire_config(raw: dict[str, Any], companion_name: str | None = None) -> DesireSystemConfig:
    """desire_config.json の中身（dict）から設定を作る。companion_name が無ければ COMPANION_NAME。"""
    companion = companion_name or COMPANION_NAME
    desires: dict[str, DesireConfig] = {}
    for desire_id, d in raw.get("desires", {}).items():
        keywords = list(d.get("keywords", []))
        # miss_companion のキーワードを COMPANION_NAME から自動生成
        if desire_id == "miss_companion" and not keywords:
            keywords = [
                f"{companion}と話した", f"{companion}に伝えた",
                f"{companion}と会話", f"{companion}と話す",
                f"{companion}が来た", f"{companion}がいた",
            ]
        desires[desire_id] = DesireConfig(
            name_ja=d["name_ja"],
            description=d.get("description", ""),
            satisfaction_hours=float(d["satisfaction_hours"]),
            keywords=keywords,
            color=d.get("color", "#cab8d9"),
            base_level=d.get("base_level"),
            time_driven=d.get("time_driven", True),
            categories=list(d.get("categories", [])),
        )

    sensor_effects = [
        SensorEffect(
            sensor=s["sensor"],
            condition=s["condition"],
            effects=s["effects"],
            description=s.get("description", ""),
        )
        for s in raw.get("sensor_effects", [])
    ]

    cross_effects = [
        CrossEffect(
            when=c["when"],
            effects=c["effects"],
            description=c.get("description", ""),
        )
        for c in raw.get("cross_effects", [])
    ]

    priority = raw.get("priority", list(desires.keys()))

    return DesireSystemConfig(
        desires=desires,
        sensor_effects=sensor_effects,
        cross_effects=cross_effects,
        priority=priority,
        event_effects=dict(raw.get("event_effects", {})),
        initial_level=float(raw.get("initial_level", 0.5)),
    )


# ---------------------------------------------------------------------------
# Desire state
# ---------------------------------------------------------------------------

@dataclass
class DesireState:
    """現在の欲求状態。"""

    updated_at: str
    desires: dict[str, float] = field(default_factory=dict)
    dominant: str = ""
    labels: dict[str, str] = field(default_factory=dict)
    colors: dict[str, str] = field(default_factory=dict)
    sensor_snapshot: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d: dict[str, Any] = {
            "updated_at": self.updated_at,
            "desires": self.desires,
            "labels": self.labels,
            "colors": self.colors,
            "dominant": self.dominant,
        }
        if self.sensor_snapshot:
            d["sensor_snapshot"] = self.sensor_snapshot
        return d


# ---------------------------------------------------------------------------
# Memory DB query
# ---------------------------------------------------------------------------

def get_latest_memory_timestamp(
    db_path: Path,
    keywords: list[str],
) -> datetime | None:
    """
    SQLiteのmemoriesテーブルからキーワードに一致する最新記憶のタイムスタンプを返す。
    一致なければ None。
    """
    if not db_path.exists() or not keywords:
        return None

    try:
        conn = sqlite3.connect(str(db_path))
        c = conn.cursor()
        conditions = " OR ".join(["content LIKE ?" for _ in keywords])
        params = [f"%{kw}%" for kw in keywords]
        c.execute(f"SELECT MAX(timestamp) FROM memories WHERE {conditions}", params)
        row = c.fetchone()
        conn.close()
    except Exception:
        return None

    if not row or not row[0]:
        return None

    try:
        return datetime.fromisoformat(row[0])
    except ValueError:
        return None


def calculate_desire_level(
    last_satisfied: datetime | None,
    satisfaction_hours: float,
    now: datetime | None = None,
) -> float:
    """
    欲求レベルを 0.0〜1.0 で計算する。
    last_satisfied が None（一度も満たされてない）なら 1.0。
    """
    if now is None:
        now = datetime.now()

    if last_satisfied is None:
        return 1.0

    # 両方タイムゾーンなしで統一
    if hasattr(last_satisfied, 'tzinfo') and last_satisfied.tzinfo is not None:
        last_satisfied = last_satisfied.replace(tzinfo=None)
    if hasattr(now, 'tzinfo') and now.tzinfo is not None:
        now = now.replace(tzinfo=None)

    elapsed_hours = (now - last_satisfied).total_seconds() / 3600
    return max(0.0, min(1.0, elapsed_hours / satisfaction_hours))


# ---------------------------------------------------------------------------
# Sensor fetch
# ---------------------------------------------------------------------------

def fetch_sensor_data(char_id: str, data_dir: Path | None = None) -> dict[str, Any]:
    """
    M5の /sensors エンドポイントにHTTPリクエストしてセンサーデータを取得。
    失敗時は空dict（ログに警告）。
    """
    if data_dir is None:
        data_dir = DATA_DIR
    config_path = data_dir / "characters" / char_id / "config" / "config.json"
    if not config_path.exists():
        return {}

    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return {}

    # m5_hosts (リスト) があれば順に試す、なければ m5_host (単体) を使う
    m5_hosts = config.get("m5_hosts")
    if isinstance(m5_hosts, list):
        hosts = [h for h in m5_hosts if h]
    else:
        h = config.get("m5_host", "")
        hosts = [h] if h else []
    if not hosts:
        return {}

    m5_port = config.get("m5_port", 80)
    for host in hosts:
        url = f"http://{host}:{m5_port}/sensors"
        try:
            resp = httpx.get(url, timeout=3.0)
            resp.raise_for_status()
            return resp.json()
        except Exception:
            continue
    logger.warning(f"センサー取得失敗: 全ホスト応答なし {hosts}")
    return {}


# ---------------------------------------------------------------------------
# Effect application
# ---------------------------------------------------------------------------

def evaluate_sensor_condition(
    condition: dict[str, Any],
    sensor_value: Any,
) -> bool:
    """センサー条件を評価する。"""
    op = condition.get("op", "")

    if op == "recent":
        # sensor_value はエポック秒のタイムスタンプ（lastTouchEventTime等）
        if sensor_value is None or sensor_value == 0:
            return False
        within = condition.get("within_seconds", 60)
        try:
            elapsed = time.time() - float(sensor_value) / 1000  # ms → s
            return elapsed <= within
        except (TypeError, ValueError):
            return False

    if sensor_value is None:
        return False

    try:
        val = float(sensor_value)
        threshold = float(condition.get("value", 0))
    except (TypeError, ValueError):
        return False

    if op == "range":
        # {"op": "range", "min": 1, "max": 20} → min <= val <= max
        try:
            lo = float(condition.get("min", 0))
            hi = float(condition.get("max", float("inf")))
        except (TypeError, ValueError):
            return False
        return lo <= val <= hi
    if op == "<":
        return val < threshold
    if op == ">":
        return val > threshold
    if op == "<=":
        return val <= threshold
    if op == ">=":
        return val >= threshold
    if op == "==":
        return val == threshold

    return False


def apply_effects(
    desires: dict[str, float],
    effects: dict[str, dict[str, float]],
) -> None:
    """effects を desires に適用する（in-place）。"""
    # "*" は全欲求に適用
    wildcard = effects.get("*")

    for desire_id in desires:
        eff = effects.get(desire_id, {})
        if wildcard and desire_id not in effects:
            eff = wildcard

        if "set" in eff:
            desires[desire_id] = float(eff["set"])
        if "add" in eff:
            desires[desire_id] += float(eff["add"])
        if "multiply" in eff:
            desires[desire_id] *= float(eff["multiply"])


# ---------------------------------------------------------------------------
# Main compute pipeline
# ---------------------------------------------------------------------------

def compute_desires(
    db_path: Path,
    config: DesireSystemConfig,
    sensor_data: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> DesireState:
    """全欲求レベルを3段階で計算してDesireStateを返す。"""
    if now is None:
        now = datetime.now()

    desires: dict[str, float] = {}
    labels: dict[str, str] = {}
    colors: dict[str, str] = {}

    # Step 1: 時間ベース計算
    for desire_id, dcfg in config.desires.items():
        labels[desire_id] = dcfg.name_ja
        colors[desire_id] = dcfg.color

        if not dcfg.time_driven:
            # time_driven: false の欲求は base_level に留まる
            desires[desire_id] = dcfg.base_level if dcfg.base_level is not None else 0.0
            continue

        last_ts = get_latest_memory_timestamp(db_path, dcfg.keywords)
        level = calculate_desire_level(last_ts, dcfg.satisfaction_hours, now)

        # base_level が設定されている場合、それ以下には下がらない
        if dcfg.base_level is not None:
            level = max(level, dcfg.base_level)

        desires[desire_id] = round(level, 3)

    # Step 2: センサー効果の適用
    sensor_snapshot: dict[str, Any] = {}
    if sensor_data:
        sensor_snapshot = dict(sensor_data)
        for effect in config.sensor_effects:
            sensor_val = sensor_data.get(effect.sensor)
            if sensor_val is not None and evaluate_sensor_condition(effect.condition, sensor_val):
                apply_effects(desires, effect.effects)

    # Step 3: 欲求間の相互作用
    for cross in config.cross_effects:
        when = cross.when
        desire_id = when.get("desire", "")
        if desire_id not in desires:
            continue
        threshold = float(when.get("value", 0))
        op = when.get("op", ">")

        current = desires[desire_id]
        triggered = False
        if op == ">" and current > threshold:
            triggered = True
        elif op == ">=" and current >= threshold:
            triggered = True
        elif op == "<" and current < threshold:
            triggered = True
        elif op == "<=" and current <= threshold:
            triggered = True

        if triggered:
            apply_effects(desires, cross.effects)

    # クランプ: 全値を 0.0-1.0
    for k in desires:
        desires[k] = round(max(0.0, min(1.0, desires[k])), 3)

    # dominant 決定（最も高い欲求、同値は priority 順）
    priority = config.priority

    def _sort_key(k: str) -> tuple:
        rank = priority.index(k) if k in priority else len(priority)
        return (-desires[k], rank)

    dominant = min(desires, key=_sort_key) if desires else ""

    return DesireState(
        updated_at=now.isoformat(),
        desires=desires,
        dominant=dominant,
        labels=labels,
        colors=colors,
        sensor_snapshot=sensor_snapshot,
    )


# ---------------------------------------------------------------------------
# Save / Load
# ---------------------------------------------------------------------------

def save_desires(state: DesireState, path: Path = DESIRES_PATH) -> None:
    """desires.json に保存する。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state.to_dict(), f, ensure_ascii=False, indent=2)


def load_desires(path: Path = DESIRES_PATH) -> DesireState | None:
    """desires.json を読み込む。存在しなければ None。"""
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return DesireState(
            updated_at=data.get("updated_at", ""),
            desires=data.get("desires", {}),
            dominant=data.get("dominant", ""),
            labels=data.get("labels", {}),
            colors=data.get("colors", {}),
            sensor_snapshot=data.get("sensor_snapshot", {}),
        )
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """メインエントリポイント（cronから呼ばれる）。

    2026-09 から、更新は petit_desire.cli（家の表 `STATE#DESIRES` にも desires.json にも書ける
    積み上げ式のエンジン）に任せる。上の compute_desires() は元の一括計算として残してある。
    """
    from petit_desire.cli import update_main

    update_main(sys.argv[1:])


if __name__ == "__main__":
    main()
