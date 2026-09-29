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
    assert row["desires"]["miss_companion"] == pytest.approx(1 / 6, abs=1e-2)  # 1h ÷ 6h
    assert row["desires"]["curiosity"] == pytest.approx(0.125, abs=1e-2)  # 0.5h ÷ 4h
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
    assert row["desires"]["miss_companion"] == pytest.approx(1 / 6, abs=1e-2)


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
    assert row["desires"]["curiosity"] == pytest.approx((4 / 60) / 4, abs=1e-2)


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
    assert row["desires"]["miss_companion"] == pytest.approx(0.5 - 0.15 + 0.5 / 6, abs=1e-3)


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
    assert row["desires"]["miss_companion"] == pytest.approx(0.5 - 0.1 + (2 / 60) / 6, abs=1e-3)
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
    assert [t.name for t in tools] == ["get_desires", "satisfy_desire", "boost_desire", "shape_desire",
                                       "retire_desire"]
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


def test_conditional_write_detects_same_second_house_api_write(aws):
    """家 API は条件なしで書き、updated_at は秒単位。同じ秒の中の書き込みも desires の一致で見分ける。"""
    store = DynamoRowStore(TABLE, PID, region=REGION)
    same_second = "2026-09-26T23:59:59Z"  # _house_api_apply が書く updated_at と同じ値
    assert store.write({"desires": {"miss_companion": 0.5}, "updated_at": same_second}, None)
    seen = store.read()
    _house_api_apply(aws["table"], {"miss_companion": -0.1})  # タッチ（updated_at は同じ値のまま）
    assert store.read()["updated_at"] == seen["updated_at"]
    assert not store.write({"desires": {"miss_companion": 0.9}, "updated_at": same_second}, seen)
    assert store.read()["desires"]["miss_companion"] == pytest.approx(0.4)
    # 読み直せば書ける（service._commit のやり直しと同じ）
    assert store.write({"desires": {"miss_companion": 0.45}, "updated_at": same_second}, store.read())


def test_touch_in_same_second_survives_engine_update(aws, monkeypatch):
    """engine の読み→書きの間に家 API が同じ秒で書いても、タッチの差分が消えない。"""
    svc = DesireService.from_env(PID, _env(aws))
    t0 = datetime(2026, 9, 26, 23, 59, 59, tzinfo=timezone.utc)
    svc.update(t0)
    real_read = svc.store.read
    state = {"n": 0}

    def racing_read():
        row = real_read()
        state["n"] += 1
        if state["n"] == 2:  # _commit が読んだ直後に家 API が書く
            _house_api_apply(aws["table"], {"miss_companion": -0.15})
        return row

    monkeypatch.setattr(svc.store, "read", racing_read)
    row, _ = svc.update(t0)  # 同じ秒・経過 0
    assert row["desires"]["miss_companion"] == pytest.approx(0.35, abs=1e-3)


def test_empty_memory_cursor_is_treated_as_first_run(aws):
    now = datetime.now(timezone.utc)
    for i in range(3):
        _put_memory(aws, f"m{i}", now - timedelta(hours=3 - i), "調べた")
    src = DynamoMemorySource(TABLE, PID, region=REGION)
    events, cursor = src.read({})
    assert len(events) == 3
    assert cursor["MEM#"].endswith("#m2")  # 新しい順に遡った先頭（最新）が読み位置
    events, _ = src.read(cursor)
    assert events == []


# ===================== ぷちが欲求の形を変える（akatsuki-petit#106） =====================

def _svc(aws, monkeypatch):
    for k, v in _env(aws).items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("CHARACTER_ID", PID)
    monkeypatch.setattr(mcp_server, "_service", None)
    return mcp_server.service()


def test_petit_can_add_its_own_desire_and_it_is_kept_in_the_row(aws, monkeypatch):
    svc = _svc(aws, monkeypatch)
    svc.update()
    out = mcp_server.call_tool_sync("shape_desire", {
        "desire_name": "確かめたい", "description": "気になったことが本当か確かめたい",
        "satisfaction_hours": 6, "satisfy_amount": 0.2, "keywords": ["確かめた"], "level": 0.3})
    assert out.startswith("[形] 新しい欲求を足した: 確かめたい（確かめたい）… 0→1 まで 6 時間・満たすと −0.2")
    row = aws["table"].get_item(Key={"pk": f"P#{PID}", "sk": "STATE#DESIRES"})["Item"]
    assert row["shape"]["確かめたい"]["satisfy_amount"] == Decimal("0.2")
    assert row["desires"]["確かめたい"] == Decimal("0.3")
    assert row["labels"]["確かめたい"] == "確かめたい"

    # 別のプロセス（cron の desire-updater）からも同じ形で計算される
    fresh = DesireService.from_env(PID, _env(aws))
    t = datetime.now(timezone.utc)
    fresh.update(now=t)  # 足したあと最初の更新で計算に入り、そこから時間で満ちる
    row2, _ = fresh.update(now=t + timedelta(hours=3))
    assert row2["desires"]["確かめたい"] == pytest.approx(0.3 + 3 / 6, abs=0.01)

    # 満たし方は欲求ごと（−0.2）。boost も受け取る
    out = mcp_server.call_tool_sync("satisfy_desire", {"desire_name": "確かめたい"})
    assert "[満足] 確かめたい -0.2 →" in out
    assert mcp_server.call_tool_sync("boost_desire", {"desire_name": "確かめたい", "amount": 0.1}).startswith(
        "[ドーパミン] 確かめたい")
    # amount を渡せば今回だけその量
    assert "-0.05 →" in mcp_server.call_tool_sync("satisfy_desire", {"desire_name": "確かめたい", "amount": 0.05})
    # get_desires に満たし方と、自分で決めた形だと出る
    assert "満たすと −0.2・0→1 まで 6 時間・自分で決めた形" in mcp_server.call_tool_sync("get_desires", {})


def test_petit_can_reshape_builtin_and_retire(aws, monkeypatch):
    svc = _svc(aws, monkeypatch)
    svc.update()
    out = mcp_server.call_tool_sync("shape_desire", {"desire_name": "miss_companion", "satisfy_amount": 0.15,
                                                     "satisfaction_hours": 8})
    assert out.startswith("[形] 欲求の形を変えた: 会いたい（miss_companion）… 0→1 まで 8 時間・満たすと −0.15")
    before = svc.levels(svc.store.read())["miss_companion"]
    after = svc.levels(svc.satisfy("miss_companion"))["miss_companion"]
    assert after == pytest.approx(max(0.0, before - 0.15), abs=0.001)

    assert "手放した" in mcp_server.call_tool_sync("retire_desire", {"desire_name": "curiosity"})
    row = svc.store.read()
    assert "curiosity" not in row["desires"] and "curiosity" not in svc.levels(row)
    assert "欲求名が不正" in mcp_server.call_tool_sync("satisfy_desire", {"desire_name": "curiosity"})
    row, _ = svc.update()
    assert "curiosity" not in row["desires"]  # 更新しても戻らない
    # 同じ名前で shape_desire すれば戻る
    assert "新しい欲求を足した" in mcp_server.call_tool_sync("shape_desire", {"desire_name": "curiosity"})
    assert "curiosity" in svc.levels(svc.store.read())


def test_shape_desire_refuses_bad_input(aws, monkeypatch):
    _svc(aws, monkeypatch)
    call = mcp_server.call_tool_sync
    assert "satisfaction_hours" in call("shape_desire", {"desire_name": "残したい"})  # 新しいのに速さが無い
    assert "記号" in call("shape_desire", {"desire_name": "a.b", "satisfaction_hours": 3})
    assert "0.05〜1.0" in call("shape_desire", {"desire_name": "残したい", "satisfaction_hours": 3,
                                              "satisfy_amount": 3})
    for i in range(10):
        call("shape_desire", {"desire_name": f"d{i}", "satisfaction_hours": 3})
    assert "12 個まで" in call("shape_desire", {"desire_name": "もうひとつ", "satisfaction_hours": 3})
    assert "無い" in call("retire_desire", {"desire_name": "ない欲求"})


def test_night_growth_is_the_same_as_day(aws, monkeypatch):
    """#157: 元の欲求システムどおり、夜も昼と同じ速さ。既定の会いたい（6 時間で満タン）なら 1 時間で +1/6。"""
    svc = DesireService.from_env(PID, _env(aws))
    jst = timezone(timedelta(hours=9))
    for t0 in (datetime(2026, 9, 28, 3, 0, tzinfo=jst), datetime(2026, 9, 28, 13, 0, tzinfo=jst)):
        svc.update(now=t0)
        svc.nudge("miss_companion", 0.6 - svc.levels(svc.store.read())["miss_companion"])
        row, _ = svc.update(now=t0 + timedelta(minutes=1))
        base = svc.levels(row)["miss_companion"]
        assert base == pytest.approx(0.6, abs=0.01)
        for m in range(5, 61, 5):  # cron と同じく 5 分ごとに 1 時間
            row, _ = svc.update(now=t0 + timedelta(minutes=1 + m))
        assert svc.levels(row)["miss_companion"] - base == pytest.approx(1 / 6, abs=0.01)
