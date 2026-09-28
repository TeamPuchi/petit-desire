"""欲求の 1 行の置き場。

- DynamoRowStore … house 表（`PETIT_HOUSE_TABLE`）の `pk = P#<pid>`・`sk = STATE#DESIRES`。
  pk の形は petit-infra README §8「段0 の住所形式」（アカウント根・`P#<pid>`）と、
  petit-api の desires_store.py（`single`）・house_store.py（`put_desires`/`list_state`）に合わせる。
  EC2 ホストのロールは `dynamodb:LeadingKeys` が `P#<pid>` の完全一致なので、GetItem と UpdateItem しか使わない。
- FileRowStore  … 手元・dev 用の desires.json（元の petit-desire の置き場）。行の形は同じ。

どちらも「読んだときの `updated_at` がまだ同じなら書く」条件付き書き込みにする。
家 API（MQTT のタッチ）・MCP（satisfy_desire）・cron（5 分ごとの更新）が同じ行を書くため。
"""

from __future__ import annotations

import json
import os
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

DESIRES_SK = "STATE#DESIRES"
# 書く属性（これ以外の属性には触らない）
# shape: ぷちが決めた欲求の形（akatsuki-petit#106・shape.py）。step() の行には入らないので、
# 書くのは shape_desire／retire_desire だけ
ROW_ATTRS = ("desires", "updated_at", "dominant", "labels", "engine", "shape")


class RowStore(Protocol):
    def read(self) -> dict[str, Any] | None:
        """行を読む（無ければ None）。"""
        ...

    def write(self, row: dict[str, Any], expected: dict[str, Any] | None) -> bool:
        """expected（read() が返したもの）から誰も書いていなければ書いて True。書かれていたら False。"""
        ...


def to_dynamo(value: Any) -> Any:
    if isinstance(value, bool) or value is None or isinstance(value, (int, str, Decimal)):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {str(k): to_dynamo(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_dynamo(v) for v in value]
    return str(value)


def from_dynamo(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() and "." not in str(value) else float(value)
    if isinstance(value, dict):
        return {k: from_dynamo(v) for k, v in value.items()}
    if isinstance(value, list):
        return [from_dynamo(v) for v in value]
    return value


def region_from_env(env: dict[str, str] | None = None) -> str | None:
    """boto3 は AWS_REGION を読まない版がある（petit-env gen-mcp-config.sh の注）。両方見る。"""
    env = os.environ if env is None else env
    return env.get("AWS_DEFAULT_REGION") or env.get("AWS_REGION") or None


def boto3_resource(region: str | None = None):
    import boto3

    kwargs: dict[str, Any] = {}
    if region:
        kwargs["region_name"] = region
    if os.environ.get("AWS_ENDPOINT_URL_DYNAMODB"):
        kwargs["endpoint_url"] = os.environ["AWS_ENDPOINT_URL_DYNAMODB"]
    return boto3.resource("dynamodb", **kwargs)


class DynamoRowStore:
    def __init__(self, table_name: str, pid: str, *, region: str | None = None, table: Any = None) -> None:
        if not pid:
            raise ValueError("pid is required")
        self.table_name = table_name
        self.pid = pid
        self.region = region
        self._table = table

    @property
    def table(self) -> Any:
        if self._table is None:
            self._table = boto3_resource(self.region).Table(self.table_name)
        return self._table

    @property
    def key(self) -> dict[str, str]:
        return {"pk": f"P#{self.pid}", "sk": DESIRES_SK}

    def read(self) -> dict[str, Any] | None:
        item = self.table.get_item(Key=self.key, ConsistentRead=True).get("Item")
        if item is None:
            return None
        return from_dynamo({k: v for k, v in item.items() if k not in ("pk", "sk")})

    def write(self, row: dict[str, Any], expected: dict[str, Any] | None) -> bool:
        from botocore.exceptions import ClientError

        names = {f"#a{i}": a for i, a in enumerate(ROW_ATTRS) if a in row}
        values = {f":v{i}": to_dynamo(row[a]) for i, a in enumerate(ROW_ATTRS) if a in row}
        sets = ", ".join(f"#a{i} = :v{i}" for i, a in enumerate(ROW_ATTRS) if a in row)
        kwargs: dict[str, Any] = {
            "Key": self.key,
            "UpdateExpression": f"SET {sets}",
            "ExpressionAttributeNames": names,
            "ExpressionAttributeValues": values,
        }
        if expected is None:
            kwargs["ConditionExpression"] = "attribute_not_exists(pk)"
        elif expected.get("updated_at") is None:
            kwargs["ConditionExpression"] = "attribute_exists(pk) AND attribute_not_exists(#u)"
            kwargs["ExpressionAttributeNames"]["#u"] = "updated_at"
        else:
            # updated_at は秒単位。家 API（desires_store.py）は条件なしで `desires.<名前>` を書くので、
            # 同じ秒の中の書き込みも見分けられるよう、読んだ desires の Map との一致も条件にする
            kwargs["ExpressionAttributeNames"]["#u"] = "updated_at"
            kwargs["ExpressionAttributeNames"]["#d"] = "desires"
            kwargs["ExpressionAttributeValues"][":expected"] = expected["updated_at"]
            if expected.get("desires") is None:
                kwargs["ConditionExpression"] = "#u = :expected AND attribute_not_exists(#d)"
            else:
                kwargs["ConditionExpression"] = "#u = :expected AND #d = :expected_desires"
                kwargs["ExpressionAttributeValues"][":expected_desires"] = to_dynamo(expected["desires"])
        try:
            self.table.update_item(**kwargs)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return False
            raise
        return True


class FileRowStore:
    """desires.json。書き手が同じホストの中だけなので、ファイルロック＋読み直しで条件付きにする。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def read(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def write(self, row: dict[str, Any], expected: dict[str, Any] | None) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        with open(lock_path, "a+") as lock:
            _lock(lock)
            try:
                cur = self.read()
                if (cur is None) != (expected is None):
                    return False
                if cur is not None and (cur.get("updated_at"), cur.get("desires")) != (
                    (expected or {}).get("updated_at"), (expected or {}).get("desires")
                ):
                    return False
                merged = {**(cur or {}), **{k: row[k] for k in ROW_ATTRS if k in row}}
                tmp = self.path.with_suffix(self.path.suffix + ".tmp")
                tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
                os.replace(tmp, self.path)
                return True
            finally:
                _unlock(lock)


def _lock(f) -> None:
    try:
        import fcntl

        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
    except ImportError:  # Windows（手元のテストだけ）
        pass


def _unlock(f) -> None:
    try:
        import fcntl

        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except ImportError:
        pass
