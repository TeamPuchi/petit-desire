"""house 表（moto）を相手に、更新・MCP・家 API との同居を確かめる。

- 行の形: pk `P#<pid>` / sk `STATE#DESIRES`、属性 `desires`（Map）・`updated_at`（petit-api と同じ）
- 記憶: petit-memory の DynamoDB 版の形（`MEM#<ts>#<id>`・暗号化された `sealed`＋`enc_v`）
- 機体: `DEVICE#<Thing>`（petit-api docs/iot-bridge.md）
- SNS: sns-api の `/internal/petits/<pid>/inbox`（httpx の MockTransport）
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import boto3
import httpx
import pytest
from moto import mock_aws

from petit_desire import server as mcp_server
from petit_desire.crypto_shred import CryptoShredder, KmsDataKeyWrapper, seal_json
from petit_desire.service import DesireService
from petit_desire.sources import DynamoMemorySource, SnsInboxSource
from petit_desire.store import DynamoRowStore

REGION = "ap-northeast-1"
TABLE = "petit-test-house"
KEYS = "petit-test-memory-keys"
PID = "mio"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


@pytest.fixture
def aws(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    monkeypatch.setenv("AWS_REGION", REGION)  # petit-mio.env と同じく AWS_REGION だけ
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name=REGION)
        for name in (TABLE, KEYS):
            ddb.create_table(
                TableName=name,
                KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
                AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"},
                                      {"AttributeName": "sk", "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )
        key_id = boto3.client("kms", region_name=REGION).create_key()["KeyMetadata"]["KeyId"]
        yield {"table": ddb.Table(TABLE), "key_id": key_id}


def _env(aws, **extra):
    env = {
        "AWS_REGION": REGION,
        "PETIT_HOUSE_TABLE": TABLE,
        "PETIT_MEMORY_STORE": "dynamo",
        "PETIT_MEMORY_DYNAMO_TABLE": TABLE,
        "PETIT_MEMORY_KEYS_TABLE": KEYS,
        "PETIT_MEMORY_KMS_KEY_ID": aws["key_id"],
        "PETIT_DATA_DIR": "/nonexistent-data",
        "COMPANION_NAME": "なぎ",
    }
    env.update(extra)
    return env


def _put_memory(aws, mem_id: str, at: datetime, content: str, category: str = "daily", private=False):
    """petit-memory の dynamo_backend が書く形（K20 の暗号化あり）で 1 件置く。"""
    shredder = CryptoShredder(
        PID, KEYS, KmsDataKeyWrapper(aws["key_id"], client=boto3.client("kms", region_name=REGION)),
        dynamodb_client=boto3.client("dynamodb", region_name=REGION),
    )
    dek = shredder.new_key(mem_id)
    sealed = seal_json(dek, {"content": content, "tags": ""}, shredder.aad(mem_id, "mem"))
    ts = _iso(at)
    aws["table"].put_item(Item={
        "pk": f"P#{PID}", "sk": f"{'PRIV#' if private else 'MEM#'}{ts}#{mem_id}",
        "id": mem_id, "timestamp": ts, "category": category, "emotion": "neutral",
        "importance": 3, "sealed": sealed, "enc_v": 1,
    })


def _house_api_apply(table, deltas: dict[str, float]):
    """petit-api desires_store.DynamoDesireStore（single）と同じ書き方で差分を足す。"""
    pk = f"P#{PID}"
    item = table.get_item(Key={"pk": pk, "sk": "STATE#DESIRES"}, ConsistentRead=True).get("Item") or {}
    cur = {k: float(v) for k, v in (item.get("desires") or {}).items()}
    changed = {n: round(max(0.0, min(1.0, cur.get(n, 0.5) + d)), 4) for n, d in deltas.items()}
    names = {f"#n{i}": n for i, n in enumerate(changed)}
    values = {f":v{i}": Decimal(str(v)) for i, v in enumerate(changed.values())}
    sets = ", ".join(f"desires.{n} = {v}" for n, v in zip(names, values))
    table.update_item(
        Key={"pk": pk, "sk": "STATE#DESIRES"},
        UpdateExpression=f"SET {sets}, updated_at = :t",
        ExpressionAttributeNames=names,
        ExpressionAttributeValues={**values, ":t": "2026-09-26T23:59:59Z"},
    )


def test_update_writes_contract_row(aws):
    svc = DesireService.from_env(PID, _env(aws))
    row, _ = svc.update()
    item = aws["table"].get_item(Key={"pk": "P#mio", "sk": "STATE#DESIRES"})["Item"]
    assert set(item["desires"]) == {"curiosity", "miss_companion"}
    assert all(isinstance(v, Decimal) for v in item["desires"].values())
    assert item["updated_at"].endswith("Z")
    assert item["engine"]["v"] == 1
    assert svc.config_source == "defaults"


def test_sealed_memory_keyword_satisfies_and_is_not_logged(aws, capsys):
    now = datetime.now(timezone.utc)
    _put_memory(aws, "m1", now - timedelta(hours=1), "今日はなぎと話した。楽しかった")
    _put_memory(aws, "m2", now - timedelta(minutes=30), "空を見て調べた", private=True)
    svc = DesireService.from_env(PID, _env(aws))
    row, inputs = svc.update(now)
    assert len(inputs.memories) == 2
    assert row["desires"]["miss_companion"] == pytest.approx(1 / 3, abs=1e-2)  # 1h ÷ 3h
    assert row["desires"]["curiosity"] == pytest.approx(0.25, abs=1e-2)  # 0.5h ÷ 2h
    item = aws["table"].get_item(Key={"pk": "P#mio", "sk": "STATE#DESIRES"})["Item"]
    assert "なぎと話した" not in json.dumps(item, default=str, ensure_ascii=False)
    assert "なぎと話した" not in capsys.readouterr().out


def test_memory_without_keys_falls_back_to_category(aws):
    now = datetime.now(timezone.utc)
    _put_memory(aws, "m1", now - timedelta(hours=1), "…", category="conversation")
    env = _env(aws)
    env.pop("PETIT_MEMORY_KEYS_TABLE")  # 鍵が無い＝本文は読めない
    svc = DesireService.from_env(PID, env)
    row, _ = svc.update(now)
    assert row["desires"]["miss_companion"] == pytest.approx(1 / 3, abs=1e-2)


def test_memory_cursor_reads_only_new(aws):
    now = datetime.now(timezone.utc)
    _put_memory(aws, "m1", now - timedelta(hours=2), "調べた")
    svc = DesireService.from_env(PID, _env(aws))
    _, first = svc.update(now)
    assert len(first.memories) == 1
    _, second = svc.update(now + timedelta(minutes=5))
    assert second.memories == []
    _put_memory(aws, "m2", now + timedelta(minutes=6), "また調べた")
    row, third = svc.update(now + timedelta(minutes=10))
    assert len(third.memories) == 1
    assert row["desires"]["curiosity"] == pytest.approx((4 / 60) / 2, abs=1e-2)


def test_device_row_battery_is_a_sensor(aws):
    now = datetime.now(timezone.utc)
    aws["table"].put_item(Item={
        "pk": "P#mio", "sk": "DEVICE#petit-nagi-mio", "thing": "petit-nagi-mio",
        "battery": {"level": 15, "charging": False}, "battery_at": int(now.timestamp()),
        "sleeping": False, "updated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    svc = DesireService.from_env(PID, _env(aws))
    row, inputs = svc.update(now)
    assert inputs.sensors["battery"] == 15
    assert row["desires"]["curiosity"] == 0.25  # 0.5 × 0.5（電池 1〜20）
    assert row["engine"]["raw"]["curiosity"] == 0.5


def test_no_device_no_memory_no_sns_still_updates(aws):
    svc = DesireService.from_env(PID, _env(aws, PETIT_MEMORY_KEYS_TABLE="", PETIT_MEMORY_KMS_KEY_ID=""))
    row, inputs = svc.update()
    assert inputs.sensors == {} and inputs.memories == [] and inputs.sns == []
    assert row["desires"] == {"curiosity": 0.5, "miss_companion": 0.5}


def test_house_api_touch_and_engine_coexist(aws):
    svc = DesireService.from_env(PID, _env(aws))
    t0 = datetime.now(timezone.utc)
    svc.update(t0)
    _house_api_apply(aws["table"], {"miss_companion": -0.15})  # stroke
    row, _ = svc.update(t0 + timedelta(minutes=30))
    assert row["desires"]["miss_companion"] == pytest.approx(0.5 - 0.15 + 0.5 / 3, abs=1e-3)


def test_conditional_write_detects_concurrent_writer(aws):
    store = DynamoRowStore(TABLE, PID, region=REGION)
    assert store.write({"desires": {"curiosity": 0.5}, "updated_at": "A"}, None)
    seen = store.read()
    _house_api_apply(aws["table"], {"curiosity": 0.1})  # 読んだ後に家 API が書いた
    assert not store.write({"desires": {"curiosity": 0.9}, "updated_at": "B"}, seen)
    assert store.read()["desires"]["curiosity"] == 0.6
    assert not store.write({"desires": {}, "updated_at": "C"}, None)  # 無いはずの行がある


def test_sns_inbox_events(aws, monkeypatch):
    calls = []
    now = datetime.now(timezone.utc)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(dict(request.url.params))
        assert request.headers["x-petit-internal-secret"] == "s3cret"
        after = request.url.params.get("after")
        if after is None:
            return httpx.Response(200, json={"events": [
                {"post_id": "p1", "type": "comment", "actor": "u", "actor_name": "x", "body": "hi",
                 "created_at": _iso(now - timedelta(days=1))}], "next_after": "c1"})
        if after == "c1":
            return httpx.Response(200, json={"events": [
                {"post_id": "p1", "type": "comment", "actor": "u", "actor_name": "x", "body": "hi",
                 "created_at": _iso(now + timedelta(minutes=1))}], "next_after": "c2"})
        return httpx.Response(200, json={"events": [], "next_after": after})

    svc = DesireService.from_env(PID, _env(aws))
    svc.sns = SnsInboxSource("http://sns-api:8780", PID, "s3cret", transport=httpx.MockTransport(handler))
    row, _ = svc.update(now)
    assert row["desires"]["miss_companion"] == 0.5  # 初回: 過去分は効かせない
    assert row["engine"]["sns_cursor"] == "c1"
    row, _ = svc.update(now + timedelta(minutes=2))
    assert row["desires"]["miss_companion"] == pytest.approx(0.5 - 0.1 + (2 / 60) / 3, abs=1e-3)
    assert row["engine"]["sns_cursor"] == "c2"
    assert calls[1] == {"limit": "50", "after": "c1"}


def test_sns_down_does_not_break_update(aws):
    def handler(request):
        return httpx.Response(503)

    svc = DesireService.from_env(PID, _env(aws))
    svc.sns = SnsInboxSource("http://sns-api:8780", PID, "x", transport=httpx.MockTransport(handler))
    row, inputs = svc.update()
    assert inputs.sns == [] and row["desires"]


def test_mcp_tools_end_to_end(aws, monkeypatch):
    for k, v in _env(aws).items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("CHARACTER_ID", PID)
    monkeypatch.setattr(mcp_server, "_service", None)

    text = mcp_server.call_tool_sync("get_desires", {})  # 行が無い → その場で更新してから返す
    assert "【最も強い欲求】会いたい（miss_companion）" in text
    assert "強い欲求（0.7 以上）は無い" in text

    svc = mcp_server.service()
    svc.boost("curiosity", 0.5)  # 0.5 + 0.5 → 1.0
    text = mcp_server.call_tool_sync("get_desires", {})
    assert "level 0.7 以上の強い欲求がある: 知りたい（curiosity）" in text

    text = mcp_server.call_tool_sync("satisfy_desire", {"desire_name": "curiosity"})
    assert text.startswith("[満足] 知りたい -0.4 → 0.600")
    # 次の更新（cron）でも満たした分は消えない
    row, _ = svc.update()
    assert row["desires"]["curiosity"] == pytest.approx(0.6, abs=1e-2)
    assert "curiosity" in row["engine"]["satisfied_at"]

    assert "欲求名が不正" in mcp_server.call_tool_sync("satisfy_desire", {"desire_name": "nope"})


async def test_mcp_list_tools_names(aws, monkeypatch):
    for k, v in _env(aws).items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("CHARACTER_ID", PID)
    monkeypatch.setattr(mcp_server, "_service", None)
    tools = await mcp_server.list_tools()
    assert [t.name for t in tools] == ["get_desires", "satisfy_desire", "boost_desire"]
    assert "curiosity(知りたい)" in tools[0].description


def test_file_store_local_mode(tmp_path, monkeypatch):
    """表が無い手元・dev では desires.json（元の置き場）に同じ形で書く。"""
    env = {"PETIT_DATA_DIR": str(tmp_path)}
    svc = DesireService.from_env("alice", env)
    row, _ = svc.update()
    path = tmp_path / "characters" / "alice" / "data" / "desires.json"
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["desires"] == row["desires"]
    assert svc.satisfy("curiosity")["desires"]["curiosity"] == pytest.approx(0.1)
    assert json.loads(path.read_text(encoding="utf-8"))["desires"]["curiosity"] == pytest.approx(0.1)


def test_character_config_file_wins(tmp_path):
    cfg_dir = tmp_path / "characters" / "alice" / "config"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "desire_config.json").write_text(json.dumps({
        "desires": {"look_outside": {"name_ja": "外を見たい", "satisfaction_hours": 1.0}},
    }), encoding="utf-8")
    svc = DesireService.from_env("alice", {"PETIT_DATA_DIR": str(tmp_path)})
    row, _ = svc.update()
    assert list(svc.levels(row)) == ["look_outside"]


def test_memory_source_partition_key_matches_petit_memory():
    assert DynamoMemorySource("t", "mio").partition_key == "P#mio"
    assert DynamoMemorySource("t", "mio", house_id="h1").partition_key == "H#h1#P#mio"
