"""欲求エンジンの入力。どれも「無い・読めない」ときは空で返し、更新そのものは止めない。

| 入力 | クラウド（EC2 ホストのぷちコンテナ） | 手元・dev |
|---|---|---|
| 記憶 | house 表の `MEM#` / `PRIV#`（petit-memory の DynamoDB 版）。本文は暗号シュレッダーの鍵で開く | memory.db（SQLite） |
| 機体の状態 | house 表の `DEVICE#<Thing>`（家 API が MQTT の battery・status から書く） | 機体の `/sensors`（元の作り） |
| SNS の反応 | sns-api の `/internal/petits/<pid>/inbox`（SNS-MCP と同じ口・別の読み位置） | 同左（秘密が無ければ使わない） |

タッチ（touch・stroke・tickle）は家 API が `STATE#DESIRES` の `desires` に直接差分を足すので、
ここでは読まない（engine.step() が `desires - written` として取り込む）。

🔴 記憶の本文は、キーワードに当てるためにこのプロセスの中で開くだけ。ログにも行にも書かない。
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .engine import MemoryEvent, SnsEvent, parse_time

logger = logging.getLogger("petit-desire")

MEMORY_PREFIXES = ("MEM#", "PRIV#")
# 初回に遡って読む記憶の数（前置辞ごと）。元は memory.db 全体の MAX(timestamp) を見ていた
INITIAL_SCAN = 300
# 1 回の更新で読む新しい記憶の上限（前置辞ごと）
MAX_NEW = 500
_SK_MAX = "￿"


def _bytes(value: Any) -> bytes:
    value = getattr(value, "value", value)  # boto3.dynamodb.types.Binary
    return bytes(value)


# ── 記憶 ──────────────────────────────


class DynamoMemorySource:
    """petit-memory の DynamoDB 版を読むだけの口（書かない）。形は dynamo_backend.py の docstring。"""

    def __init__(
        self,
        table_name: str,
        pid: str,
        *,
        house_id: str = "",
        shredder: Any = None,
        region: str | None = None,
        table: Any = None,
    ) -> None:
        self.table_name = table_name
        self.pid = pid
        self.house_id = house_id
        self.shredder = shredder
        self.region = region
        self._table = table

    @property
    def table(self) -> Any:
        if self._table is None:
            from .store import boto3_resource

            self._table = boto3_resource(self.region).Table(self.table_name)
        return self._table

    @property
    def partition_key(self) -> str:
        # petit-memory の DynamoMemoryStore.partition_key と同じ組み立て
        return f"H#{self.house_id}#P#{self.pid}" if self.house_id else f"P#{self.pid}"

    def _query(self, **kwargs: Any) -> list[dict[str, Any]]:
        return self.table.query(
            ProjectionExpression="#sk, #id, #ts, #cat, #sealed, #encv, #content",
            ExpressionAttributeNames={
                "#sk": "sk", "#id": "id", "#ts": "timestamp", "#cat": "category",
                "#sealed": "sealed", "#encv": "enc_v", "#content": "content",
            },
            **kwargs,
        ).get("Items", [])

    def read(self, cursor: dict[str, str] | None) -> tuple[list[MemoryEvent], dict[str, str]]:
        from boto3.dynamodb.conditions import Key

        new_cursor: dict[str, str] = dict(cursor or {})
        items: list[dict[str, Any]] = []
        for prefix in MEMORY_PREFIXES:
            pk_cond = Key("pk").eq(self.partition_key)
            if cursor is None:
                # 初回: 新しい順に少しだけ遡る（最後に満たされた時刻を知るため）
                got = self._query(
                    KeyConditionExpression=pk_cond & Key("sk").begins_with(prefix),
                    ScanIndexForward=False,
                    Limit=INITIAL_SCAN,
                )
                new_cursor[prefix] = str(got[0]["sk"]) if got else ""
                items.extend(got)
                continue
            after = cursor.get(prefix) or ""
            cond = pk_cond & (Key("sk").between(after + "\x00", prefix + _SK_MAX) if after
                              else Key("sk").begins_with(prefix))
            got: list[dict[str, Any]] = []
            kwargs: dict[str, Any] = {"KeyConditionExpression": cond, "ScanIndexForward": True}
            while len(got) < MAX_NEW:
                resp = self.table.query(
                    ProjectionExpression="#sk, #id, #ts, #cat, #sealed, #encv, #content",
                    ExpressionAttributeNames={
                        "#sk": "sk", "#id": "id", "#ts": "timestamp", "#cat": "category",
                        "#sealed": "sealed", "#encv": "enc_v", "#content": "content",
                    },
                    Limit=MAX_NEW - len(got),
                    **kwargs,
                )
                got.extend(resp.get("Items", []))
                if "LastEvaluatedKey" not in resp:
                    break
                kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
            if got:
                new_cursor[prefix] = str(got[-1]["sk"])
            items.extend(got)
        return self._to_events(items), new_cursor

    def _to_events(self, items: list[dict[str, Any]]) -> list[MemoryEvent]:
        sealed_ids = [str(i["id"]) for i in items if "enc_v" in i and "id" in i]
        keys: dict[str, bytes] = {}
        if sealed_ids and self.shredder is not None:
            try:
                keys = self.shredder.keys_for(sealed_ids)
            except Exception as e:  # 鍵の表・KMS に届かない: 種類（category）だけで数える
                logger.warning("memory keys unavailable: %s", type(e).__name__)
        events: list[MemoryEvent] = []
        for item in items:
            at = parse_time(item.get("timestamp"))
            if at is None:
                parts = str(item.get("sk", "")).split("#")
                at = parse_time(parts[1]) if len(parts) > 2 else None
            if at is None:
                continue
            content: str | None = item.get("content") if isinstance(item.get("content"), str) else None
            if "enc_v" in item:
                content = None
                dek = keys.get(str(item.get("id")))
                if dek is not None and "sealed" in item and self.shredder is not None:
                    try:
                        from .crypto_shred import open_json

                        secret = open_json(dek, _bytes(item["sealed"]), self.shredder.aad(str(item["id"]), "mem"))
                        content = secret.get("content") if isinstance(secret, dict) else None
                    except Exception:
                        content = None
            events.append(MemoryEvent(at=at, content=content, category=item.get("category")))
        return events


class SqliteMemorySource:
    """元の作り（memory.db）。手元・dev 用。"""

    CURSOR = "sqlite"

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)

    def read(self, cursor: dict[str, str] | None) -> tuple[list[MemoryEvent], dict[str, str]]:
        new_cursor = dict(cursor or {})
        if not self.db_path.exists():
            return [], new_cursor
        try:
            conn = sqlite3.connect(str(self.db_path))
            try:
                cols = {r[1] for r in conn.execute("PRAGMA table_info(memories)")}
                cat = "category" if "category" in cols else "NULL"
                if cursor is None:
                    rows = conn.execute(
                        f"SELECT content, timestamp, {cat} FROM memories ORDER BY timestamp DESC LIMIT ?",
                        (INITIAL_SCAN,),
                    ).fetchall()
                else:
                    after = cursor.get(self.CURSOR) or ""
                    rows = conn.execute(
                        f"SELECT content, timestamp, {cat} FROM memories WHERE timestamp > ? "
                        "ORDER BY timestamp ASC LIMIT ?",
                        (after, MAX_NEW),
                    ).fetchall()
            finally:
                conn.close()
        except sqlite3.Error as e:
            logger.warning("memory.db unreadable: %s", e)
            return [], new_cursor
        events = []
        newest = new_cursor.get(self.CURSOR) or ""
        for content, ts, category in rows:
            at = parse_time(ts)
            if at is None:
                continue
            newest = max(newest, str(ts))
            events.append(MemoryEvent(at=at, content=content, category=category))
        new_cursor[self.CURSOR] = newest
        return events, new_cursor


# ── 機体の状態 ──────────────────────────


# battery の値をセンサーとして使う鮮度（これより古い電池残量は見ない）
BATTERY_FRESH = timedelta(hours=3)


class DynamoDeviceSource:
    """house 表の `DEVICE#<Thing>`（petit-api docs/iot-bridge.md §機体の状態の行）からセンサー値を作る。

    返す名前（desire_config.json の sensor_effects の `sensor` に書く）:
    - `battery`        … 電池残量（%）。`battery_at` が 3 時間以内のときだけ
    - `charging`       … 充電中なら 1
    - `sleeping`       … 機体が眠っていれば 1
    - `device_seen_min` … 機体の行が最後に更新されてからの分数
    機体が 1 台も無ければ空（センサーが無いぷち＝元の「全ホスト応答なし」と同じ扱い）。
    """

    def __init__(self, table_name: str, pid: str, *, region: str | None = None, table: Any = None) -> None:
        self.table_name = table_name
        self.pid = pid
        self.region = region
        self._table = table

    @property
    def table(self) -> Any:
        if self._table is None:
            from .store import boto3_resource

            self._table = boto3_resource(self.region).Table(self.table_name)
        return self._table

    def read(self, now: datetime | None = None) -> dict[str, Any]:
        from boto3.dynamodb.conditions import Key

        from .store import from_dynamo

        now = now or datetime.now(timezone.utc)
        rows = self.table.query(
            KeyConditionExpression=Key("pk").eq(f"P#{self.pid}") & Key("sk").begins_with("DEVICE#")
        ).get("Items", [])
        if not rows:
            return {}
        rows = [from_dynamo(r) for r in rows]
        rows.sort(key=lambda r: str(r.get("updated_at", "")), reverse=True)
        row = rows[0]
        out: dict[str, Any] = {}
        battery = row.get("battery")
        battery_at = row.get("battery_at")
        if isinstance(battery, dict) and isinstance(battery.get("level"), (int, float)):
            fresh = True
            if isinstance(battery_at, (int, float)):
                fresh = now - datetime.fromtimestamp(float(battery_at), timezone.utc) <= BATTERY_FRESH
            if fresh:
                out["battery"] = battery["level"]
                if "charging" in battery:
                    out["charging"] = 1 if battery.get("charging") else 0
        if "sleeping" in row:
            out["sleeping"] = 1 if row.get("sleeping") else 0
        seen = parse_time(row.get("updated_at"))
        if seen is not None:
            out["device_seen_min"] = round((now - seen).total_seconds() / 60, 1)
        return out


class HttpSensorSource:
    """元の作り: 機体の `/sensors` を HTTP で読む（config.json の m5_host）。手元・dev 用。"""

    def __init__(self, char_id: str, data_dir: Path) -> None:
        self.char_id = char_id
        self.data_dir = Path(data_dir)

    def read(self, now: datetime | None = None) -> dict[str, Any]:
        from desire_updater import fetch_sensor_data

        return fetch_sensor_data(self.char_id, self.data_dir)


# ── SNS ──────────────────────────────


class SnsInboxSource:
    """sns-api の受け箱。読み位置は欲求エンジン自身が持つ（SNS-MCP の既読位置は動かさない）。"""

    PAGE = 50
    MAX_PAGES = 20

    def __init__(self, base_url: str, pid: str, secret: str, *, transport: Any = None, timeout: float = 5.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.pid = pid
        self.secret = secret
        self.transport = transport
        self.timeout = timeout

    def read(self, cursor: str | None) -> tuple[list[SnsEvent], str | None]:
        import httpx

        events: list[SnsEvent] = []
        after = cursor
        try:
            with httpx.Client(base_url=self.base_url, transport=self.transport, timeout=self.timeout) as c:
                for _ in range(self.MAX_PAGES):
                    params: dict[str, Any] = {"limit": self.PAGE}
                    if after:
                        params["after"] = after
                    r = c.get(
                        f"/internal/petits/{self.pid}/inbox",
                        params=params,
                        headers={"x-petit-internal-secret": self.secret},
                    )
                    r.raise_for_status()
                    data = r.json()
                    got = data.get("events") or []
                    for ev in got:
                        at = parse_time(ev.get("created_at"))
                        if at is not None and ev.get("type"):
                            events.append(SnsEvent(type=str(ev["type"]), at=at))
                    after = data.get("next_after") or after
                    if len(got) < self.PAGE:
                        break
        except Exception as e:  # SNS が落ちていても欲求の更新は続ける
            logger.warning("sns inbox unavailable: %s", type(e).__name__)
            return [], cursor
        return events, after
