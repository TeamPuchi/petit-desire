"""欲求エンジンの窓口。環境変数から置き場と入力を組み立て、更新・満たす・強める・表示をする。

## 環境変数（どれも petit-env の petit-core コンテナに既にあるもの。足すのは CHARACTER_ID だけ）

| 変数 | 使い道 | 無いとき |
|---|---|---|
| `CHARACTER_ID`（または引数） | ぷちの id（pk `P#<id>`） | 必須 |
| `PETIT_DESIRE_TABLE` → `PETIT_HOUSE_TABLE` | 欲求の行の表 | desires.json（`DESIRES_PATH` か `$PETIT_DATA_DIR/characters/<id>/data/desires.json`） |
| `PETIT_MEMORY_STORE` | `dynamo`/`dual` なら house 表の記憶を読む。空なら表があれば dynamo | sqlite（`MEMORY_DB_PATH` か `$PETIT_DATA_DIR/characters/<id>/memory.db`） |
| `PETIT_MEMORY_DYNAMO_TABLE` | 記憶の表 | 欲求の表と同じ |
| `PETIT_MEMORY_HOUSE_ID` | 記憶の pk を従来形 `H#<hid>#P#<pid>` にする（記憶 MCP と同じ） | `P#<pid>` |
| `PETIT_MEMORY_KEYS_TABLE`・`PETIT_MEMORY_KMS_KEY_ID` | 記憶の本文を開く鍵（K20） | 本文は読まず、記憶の種類（category）だけで数える |
| `PETIT_SNS_URL`・`PETIT_SNS_INTERNAL_SECRET` | SNS の受け箱 | SNS は見ない |
| `AWS_DEFAULT_REGION` → `AWS_REGION` | boto3 のリージョン | boto3 の既定 |
| `COMPANION_NAME` → `PETIT_USER_NAME` | miss_companion のキーワードを作る名前 | 「あなた」 |
| `PETIT_DESIRE_CONFIG` | 設定ファイルの場所を直接指す | `$PETIT_DATA_DIR/characters/<id>/config/desire_config.json`、無ければ defaults.py |
| `PETIT_DESIRE_STALE_MIN` | get_desires がこれより古い行を見たら、その場で更新する（分） | 10 |
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from desire_updater import DesireSystemConfig, parse_desire_config

from .defaults import DEFAULT_DESIRE_CONFIG
from .engine import Inputs, nudge, parse_time, step, utcnow
from .store import DynamoRowStore, FileRowStore, RowStore, region_from_env

logger = logging.getLogger("petit-desire")

WRITE_RETRIES = 5
SATISFY_AMOUNT = 0.4  # 元の server.py と同じ
BOOST_MAX = 0.5  # 元の server.py と同じ
STRONG = 0.7  # SOUL.md の行動原則「level >= 0.7 の強い欲求に従って行動する」


def load_config(pid: str, data_dir: Path, env: dict[str, str] | None = None) -> tuple[DesireSystemConfig, str]:
    """(設定, 出どころ)。キャラの desire_config.json が無ければ既定（仮置き）。"""
    env = os.environ if env is None else env
    companion = env.get("COMPANION_NAME") or env.get("PETIT_USER_NAME") or None
    explicit = env.get("PETIT_DESIRE_CONFIG", "").strip()
    path = Path(explicit) if explicit else data_dir / "characters" / pid / "config" / "desire_config.json"
    if path.exists():
        raw = json.loads(path.read_text(encoding="utf-8"))
        return parse_desire_config(raw, companion), str(path)
    if explicit:
        raise FileNotFoundError(f"PETIT_DESIRE_CONFIG が無い: {explicit}")
    return parse_desire_config(DEFAULT_DESIRE_CONFIG, companion), "defaults"


@dataclass
class DesireService:
    pid: str
    config: DesireSystemConfig
    store: RowStore
    memory: Any = None  # .read(cursor) -> (events, cursor)
    device: Any = None  # .read(now) -> dict
    sns: Any = None  # .read(cursor) -> (events, cursor)
    stale_after: timedelta = timedelta(minutes=10)
    config_source: str = ""

    # ── 組み立て ──

    @classmethod
    def from_env(cls, pid: str, env: dict[str, str] | None = None) -> "DesireService":
        env = os.environ if env is None else env
        if not pid:
            raise ValueError("CHARACTER_ID（ぷちの id）が要る")
        data_dir = Path(env.get("PETIT_DATA_DIR", str(Path.home() / "petit_data")))
        region = region_from_env(env)
        config, source = load_config(pid, data_dir, env)

        table = (env.get("PETIT_DESIRE_TABLE") or env.get("PETIT_HOUSE_TABLE") or "").strip()
        store: RowStore
        if table:
            store = DynamoRowStore(table, pid, region=region)
        else:
            path = env.get("DESIRES_PATH") or str(data_dir / "characters" / pid / "data" / "desires.json")
            store = FileRowStore(Path(path))

        from .sources import (
            DynamoDeviceSource,
            DynamoMemorySource,
            HttpSensorSource,
            SnsInboxSource,
            SqliteMemorySource,
        )

        mem_store = (env.get("PETIT_MEMORY_STORE") or "").strip() or ("dynamo" if table else "sqlite")
        memory: Any
        if mem_store in ("dynamo", "dual"):
            mem_table = (env.get("PETIT_MEMORY_DYNAMO_TABLE") or table or "house").strip()
            memory = DynamoMemorySource(
                mem_table, pid,
                house_id=env.get("PETIT_MEMORY_HOUSE_ID", ""),
                shredder=_shredder_from_env(pid, env, region),
                region=region,
            )
        else:
            db = env.get("MEMORY_DB_PATH") or str(data_dir / "characters" / pid / "memory.db")
            memory = SqliteMemorySource(Path(db))

        device: Any = DynamoDeviceSource(table, pid, region=region) if table else HttpSensorSource(pid, data_dir)

        sns = None
        secret = env.get("PETIT_SNS_INTERNAL_SECRET", "")
        if secret:
            sns = SnsInboxSource(env.get("PETIT_SNS_URL") or "http://sns-api:8780", pid, secret)

        stale = float(env.get("PETIT_DESIRE_STALE_MIN", "10") or 10)
        return cls(pid, config, store, memory, device, sns, timedelta(minutes=stale), source)

    # ── 書き込み（条件付きで、取られたら読み直してやり直す） ──

    def _commit(self, make: Callable[[dict[str, Any] | None], dict[str, Any]]) -> dict[str, Any]:
        row = self.store.read()
        for _ in range(WRITE_RETRIES):
            new = make(row)
            if self.store.write(new, row):
                return new
            row = self.store.read()
        raise RuntimeError("欲求の行を書けなかった（他の書き手と競合し続けた）")

    # ── 更新 ──

    def gather(self, row: dict[str, Any] | None, now: datetime) -> Inputs:
        engine = (row or {}).get("engine") or {}
        initialized = isinstance(engine.get("raw"), dict) and bool(engine.get("at"))
        inputs = Inputs()
        if self.memory is not None:
            try:
                cursor = engine.get("mem_cursor") if initialized else None
                inputs.memories, inputs.memory_cursor = self.memory.read(cursor if cursor is not None else None)
            except Exception as e:
                logger.warning("memory source failed: %s", type(e).__name__)
        if self.device is not None:
            try:
                inputs.sensors = self.device.read(now) or {}
            except Exception as e:
                logger.warning("device source failed: %s", type(e).__name__)
        if self.sns is not None:
            try:
                inputs.sns, inputs.sns_cursor = self.sns.read(engine.get("sns_cursor"))
            except Exception as e:
                logger.warning("sns source failed: %s", type(e).__name__)
        return inputs

    def update(self, now: datetime | None = None) -> tuple[dict[str, Any], Inputs]:
        now = now or utcnow()
        inputs = self.gather(self.store.read(), now)
        row = self._commit(lambda r: step(r, self.config, inputs, now))
        return row, inputs

    def nudge(self, name: str, delta: float, *, satisfied: bool = False) -> dict[str, Any]:
        if name not in self.config.desires:
            raise KeyError(name)
        now = utcnow()
        return self._commit(lambda r: nudge(r, self.config, name, delta, now, satisfied=satisfied))

    def satisfy(self, name: str) -> dict[str, Any]:
        return self.nudge(name, -SATISFY_AMOUNT, satisfied=True)

    def boost(self, name: str, amount: float) -> tuple[dict[str, Any], float]:
        amount = max(0.0, min(BOOST_MAX, float(amount)))
        return self.nudge(name, amount), amount

    def current(self, refresh_if_stale: bool = True) -> dict[str, Any] | None:
        """今の行。古ければ（cron が止まっている等）その場で更新してから返す。"""
        row = self.store.read()
        if not refresh_if_stale:
            return row
        at = parse_time(((row or {}).get("engine") or {}).get("at"))
        if at is None or utcnow() - at > self.stale_after:
            try:
                row, _ = self.update()
            except Exception as e:
                logger.warning("refresh failed: %s", type(e).__name__)
        return row

    # ── 表示 ──

    def levels(self, row: dict[str, Any] | None) -> dict[str, float]:
        desires = (row or {}).get("desires") or {}
        out = {}
        for name in self.config.desires:
            if name in desires:
                try:
                    out[name] = float(desires[name])
                except (TypeError, ValueError):
                    continue
        return out

    def format(self, row: dict[str, Any] | None, *, compact: bool = False) -> str:
        levels = self.levels(row)
        if not levels:
            return "欲求はまだ計算されていない（desire-updater がまだ一度も回っていない）。"
        labels = {k: d.name_ja for k, d in self.config.desires.items()}
        order = sorted(levels, key=lambda k: (-levels[k], self.config.priority.index(k)
                                               if k in self.config.priority else 99))
        dominant = order[0]
        lines = []
        if not compact:
            top = labels.get(dominant, dominant)
            lines.append(f"【最も強い欲求】{top}（{dominant}） level: {levels[dominant]:.3f}")
            lines.append("")
            lines.append("【欲求レベル一覧】")
        for k in order:
            v = levels[k]
            bar = "█" * int(v * 10) + "░" * (10 - int(v * 10))
            mark = " ←強い" if v >= STRONG else ""
            desc = self.config.desires[k].description
            lines.append(f"  {labels.get(k, k)}（{k}）: [{bar}] {v:.3f}{mark}" + (f" … {desc}" if desc else ""))
        strong = [k for k in order if levels[k] >= STRONG]
        lines.append("")
        if strong:
            lines.append(
                "level 0.7 以上の強い欲求がある: "
                + "、".join(f"{labels.get(k, k)}（{k}）" for k in strong)
                + "。どう満たすかは自分で決めて、実際に行動する。行動したら satisfy_desire でその欲求を記録する。"
            )
        else:
            lines.append("強い欲求（0.7 以上）は無い。いつもの自分のペースで過ごしてよい。")
        sensors = ((row or {}).get("engine") or {}).get("sensors") or {}
        if sensors and not compact:
            lines.append("")
            lines.append("【体（機体）の様子】" + " ".join(f"{k}={v}" for k, v in sensors.items()))
        updated = (row or {}).get("updated_at")
        if updated and not compact:
            lines.append(f"\n更新: {updated}")
        return "\n".join(lines)


def _shredder_from_env(pid: str, env: dict[str, str], region: str | None) -> Any:
    table = env.get("PETIT_MEMORY_KEYS_TABLE", "").strip()
    key_id = env.get("PETIT_MEMORY_KMS_KEY_ID", "").strip()
    if not table or not key_id:
        return None
    try:
        import boto3

        from .crypto_shred import CryptoShredder, KmsDataKeyWrapper

        kwargs = {"region_name": region} if region else {}
        return CryptoShredder(
            pid, table, KmsDataKeyWrapper(key_id, client=boto3.client("kms", **kwargs)),
            dynamodb_client=boto3.client("dynamodb", **kwargs),
        )
    except Exception as e:
        logger.warning("memory shredder unavailable: %s", type(e).__name__)
        return None
