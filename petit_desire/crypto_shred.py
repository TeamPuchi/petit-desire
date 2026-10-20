# 写し: TeamPuchi/petit-memory src/memory_mcp/crypto_shred.py（commit 9b06827・akatsuki-petit#190 の KEK）。形を変えずに使う。
# 記憶の本文（sealed）を、このプロセスの中だけで開いて欲求のキーワードに当てるために読む。
"""暗号シュレッダー（crypto-shredding）の部品。

「忘れる」をバックアップや PITR からも戻せない形にするための仕組み。
petit-memory の DynamoDB 実装が使うほか、他のリポ（会話ログ `MSG#` の書き手など）
からもそのまま import して使えるよう、DynamoDB の house 表の形には依存させていない。

## 仕組み

1. 項目 1 件ごとにデータ鍵（DEK, 256 bit）を作り、本文を AES-256-GCM で暗号化する。
2. DEK はぷち 1 体に 1 本の鍵（KEK, 256 bit）で AES-256-GCM で包み、**別の「鍵の表」**に置く。
   KEK だけを KMS の鍵（env ごとに 1 本の CMK）で包んで同じ鍵の表に置く（sk `KEK`）。
   KMS で包むときの暗号化コンテキストは `{"pid": <pid>}`。KMS の鍵ポリシーはこの pid と
   呼び出し元ロールの組でしか Decrypt を通さない（petit-infra 側）。
   KMS を呼ぶのは、プロセスがそのぷちの KEK をはじめて使うときの `Decrypt` 1 回だけ
   （KEK がまだ無ければ `GenerateDataKey` 1 回）。記憶の読み書きでは KMS を呼ばない
   （akatsuki-petit#190。前は 1 件ごとに `GenerateDataKey`、未キャッシュの 1 件ごとに `Decrypt` で、
   無料枠 2 万回/月を月の 8 日で使い切っていた）。
3. 忘れる＝鍵の表からその項目の DEK を消す。鍵の表は PITR 無効・毎晩バックアップの対象外。
   本体の表を PITR や S3 から戻しても、DEK がもう無いので誰にも読めない（KEK が残っていても、
   包まれた DEK そのものが無いので戻らない）。

### 前の形（DEK を KMS で直接包んだ行）

#190 より前に書いた行の `wrapped` は KMS の CiphertextBlob のまま。読むときは KMS で解き、
その場で KEK で包み直して置き直す（行がまだあるときだけ。忘れた行を生き返らせない）。
一度読まれた行は以後 KMS を呼ばない。

## 鍵の表の形

- 表名は環境ごと（例 `petit-<env>-memory-keys`）。
- `pk` = `P#<pid>`、`sk` = `KEY#<item id>`、属性 `wrapped`（Binary）と `created_at`。
- `wrapped` = `b"PKEK1"` || nonce(12 byte) || AES-256-GCM(DEK || tag)、AAD は `"<pid>|kek|<item id>"`
  （別の行・別のぷちの包みを貼り替えても解けない）。先頭が `PKEK1` でなければ前の形（KMS の CiphertextBlob）。
- KEK の行: `sk` = `KEK`、属性 `wrapped`（KMS の CiphertextBlob）と `created_at`。

## 暗号文の形（他言語で読み書きするときの約束）

`blob = 0x01 || nonce(12 byte) || AES-256-GCM(ciphertext || tag(16 byte))`

- 先頭 1 byte は形式の版（今は `0x01`）。
- AAD（追加認証データ）は呼び出し側が決める UTF-8 文字列。petit-memory は
  `"<pid>|<item id>|<種類>"`（種類は `mem` か `vec`）。別の項目・別のぷちの暗号文を
  貼り替えても復号に失敗する。

## 平文の置き場所

復号した KEK と DEK はこのプロセスのメモリ（`_kek`・`_DekCache`）にだけ持つ。ディスクには書かない。
"""

from __future__ import annotations

import json
import os
import threading
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Protocol

FORMAT_VERSION = b"\x01"
NONCE_BYTES = 12
DEK_BYTES = 32

KEY_SK_PREFIX = "KEY#"
KEK_SK = "KEK"
KEK_WRAP_MAGIC = b"PKEK1"

_BATCH_GET_LIMIT = 100
_DEFAULT_CACHE_SIZE = 20000


def _error_code(exc: Exception) -> str:
    response = getattr(exc, "response", None) or {}
    return str(response.get("Error", {}).get("Code", ""))


class ShreddedError(LookupError):
    """DEK が鍵の表に無い（＝忘れた項目、または鍵の表を戻せない復元の後）。"""


# ── 暗号文 ────────────────────────────────


def seal(dek: bytes, plaintext: bytes, aad: str) -> bytes:
    """AES-256-GCM で暗号化する。戻り値は `0x01 || nonce || ciphertext+tag`。"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = os.urandom(NONCE_BYTES)
    return FORMAT_VERSION + nonce + AESGCM(dek).encrypt(nonce, plaintext, aad.encode("utf-8"))


def open_sealed(dek: bytes, blob: bytes, aad: str) -> bytes:
    """`seal()` の逆。鍵・AAD が合わなければ `cryptography.exceptions.InvalidTag`。"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if not blob or blob[:1] != FORMAT_VERSION:
        raise ValueError("unknown sealed blob format")
    nonce = blob[1 : 1 + NONCE_BYTES]
    return AESGCM(dek).decrypt(nonce, blob[1 + NONCE_BYTES :], aad.encode("utf-8"))


def seal_json(dek: bytes, value: Any, aad: str) -> bytes:
    return seal(dek, json.dumps(value, ensure_ascii=False).encode("utf-8"), aad)


def open_json(dek: bytes, blob: bytes, aad: str) -> Any:
    return json.loads(open_sealed(dek, blob, aad).decode("utf-8"))


# ── DEK を包む（KMS）──────────────────────


class DataKeyWrapper(Protocol):
    """KEK を作って包む／包みを解く口（前の形の行では DEK も）。本番は KMS、試験は moto の KMS。"""

    def generate(self, pid: str) -> tuple[bytes, bytes]:
        """(平文の鍵, 包んだ鍵) を返す。"""
        ...

    def unwrap(self, wrapped: bytes, pid: str) -> bytes:
        """包んだ鍵を解く。"""
        ...


class KmsDataKeyWrapper:
    """KMS の `GenerateDataKey` / `Decrypt`。暗号化コンテキストに `pid` を入れる。"""

    def __init__(self, key_id: str, client: Any | None = None):
        self._key_id = key_id
        if client is None:
            import boto3

            client = boto3.client("kms")
        self._client = client

    def generate(self, pid: str) -> tuple[bytes, bytes]:
        response = self._client.generate_data_key(
            KeyId=self._key_id,
            KeySpec="AES_256",
            EncryptionContext={"pid": pid},
        )
        return bytes(response["Plaintext"]), bytes(response["CiphertextBlob"])

    def unwrap(self, wrapped: bytes, pid: str) -> bytes:
        response = self._client.decrypt(
            CiphertextBlob=wrapped,
            KeyId=self._key_id,
            EncryptionContext={"pid": pid},
        )
        return bytes(response["Plaintext"])


# ── 平文 DEK のメモリ内キャッシュ ──────────


class _DekCache:
    """item id → 平文 DEK の LRU。プロセスのメモリだけに置く。"""

    def __init__(self, max_size: int):
        self._max_size = max_size
        self._items: OrderedDict[str, bytes] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, item_id: str) -> bytes | None:
        with self._lock:
            dek = self._items.get(item_id)
            if dek is not None:
                self._items.move_to_end(item_id)
            return dek

    def put(self, item_id: str, dek: bytes) -> None:
        with self._lock:
            self._items[item_id] = dek
            self._items.move_to_end(item_id)
            while len(self._items) > self._max_size:
                self._items.popitem(last=False)

    def evict(self, item_id: str) -> None:
        with self._lock:
            self._items.pop(item_id, None)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


# ── 本体 ──────────────────────────────────


class CryptoShredder:
    """1 体のぷち（pid）の DEK を鍵の表で管理する。すべて同期 API（boto3 と同じ）。

    使い方（他リポから）::

        shredder = CryptoShredder.from_env(pid="mio")    # or CryptoShredder(pid, table, KmsDataKeyWrapper(key_id))
        dek = shredder.new_key(item_id)                   # 書くとき: DEK を作って鍵の表に置く
        blob = seal(dek, body_bytes, shredder.aad(item_id, "msg"))
        ...
        dek = shredder.key_for(item_id)                   # 読むとき: 無ければ None（忘れた項目）
        body = open_sealed(dek, blob, shredder.aad(item_id, "msg"))
        ...
        shredder.shred(item_id)                           # 忘れる: DEK を消す（先に呼ぶ）
    """

    def __init__(
        self,
        pid: str,
        keys_table: str,
        wrapper: DataKeyWrapper,
        *,
        dynamodb_client: Any | None = None,
        cache_size: int = _DEFAULT_CACHE_SIZE,
    ):
        if not pid:
            raise ValueError("pid is required for CryptoShredder")
        self._pid = pid
        self._table_name = keys_table
        self._wrapper = wrapper
        if dynamodb_client is None:
            import boto3

            dynamodb_client = boto3.client("dynamodb")
        self._client = dynamodb_client
        self._cache = _DekCache(cache_size)
        self._kek: bytes | None = None
        self._kek_lock = threading.Lock()

    @classmethod
    def from_env(cls, pid: str) -> CryptoShredder | None:
        """`PETIT_MEMORY_KEYS_TABLE` と `PETIT_MEMORY_KMS_KEY_ID` から作る。どちらも無ければ None。"""
        table = os.getenv("PETIT_MEMORY_KEYS_TABLE", "")
        key_id = os.getenv("PETIT_MEMORY_KMS_KEY_ID", "")
        if not table and not key_id:
            return None
        if not table or not key_id:
            raise ValueError("PETIT_MEMORY_KEYS_TABLE と PETIT_MEMORY_KMS_KEY_ID は両方そろえて設定する")
        return cls(pid, table, KmsDataKeyWrapper(key_id))

    @property
    def pid(self) -> str:
        return self._pid

    @property
    def partition_key(self) -> str:
        return f"P#{self._pid}"

    @staticmethod
    def key_sk(item_id: str) -> str:
        return f"{KEY_SK_PREFIX}{item_id}"

    def aad(self, item_id: str, kind: str) -> str:
        """暗号文の AAD の既定の組み立て `"<pid>|<item id>|<種類>"`。"""
        return f"{self._pid}|{item_id}|{kind}"

    def _key(self, item_id: str) -> dict[str, Any]:
        return {"pk": {"S": self.partition_key}, "sk": {"S": self.key_sk(item_id)}}

    # ── KEK（ぷち 1 体に 1 本。KMS で包んで鍵の表に置く）──

    def _kek_key(self) -> dict[str, Any]:
        return {"pk": {"S": self.partition_key}, "sk": {"S": KEK_SK}}

    def kek(self) -> bytes:
        """平文の KEK。プロセスで最初の 1 回だけ KMS を呼ぶ（無ければ作って置く）。"""
        if self._kek is not None:
            return self._kek
        with self._kek_lock:
            if self._kek is not None:
                return self._kek
            item = self._client.get_item(TableName=self._table_name, Key=self._kek_key(), ConsistentRead=True).get(
                "Item"
            )
            if item is None:
                kek, wrapped = self._wrapper.generate(self._pid)
                try:
                    self._client.put_item(
                        TableName=self._table_name,
                        Item={
                            **self._kek_key(),
                            "wrapped": {"B": wrapped},
                            "created_at": {"S": datetime.now(timezone.utc).isoformat()},
                        },
                        ConditionExpression="attribute_not_exists(sk)",
                    )
                except Exception as exc:  # 別のプロセスが先に置いた → そちらを使う
                    if _error_code(exc) != "ConditionalCheckFailedException":
                        raise
                    item = self._client.get_item(TableName=self._table_name, Key=self._kek_key(), ConsistentRead=True)[
                        "Item"
                    ]
                    kek = self._wrapper.unwrap(bytes(item["wrapped"]["B"]), self._pid)
            else:
                kek = self._wrapper.unwrap(bytes(item["wrapped"]["B"]), self._pid)
            self._kek = kek
            return kek

    def _kek_aad(self, item_id: str) -> bytes:
        return f"{self._pid}|kek|{item_id}".encode("utf-8")

    def _wrap(self, item_id: str, dek: bytes) -> bytes:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        nonce = os.urandom(NONCE_BYTES)
        return KEK_WRAP_MAGIC + nonce + AESGCM(self.kek()).encrypt(nonce, dek, self._kek_aad(item_id))

    def _unwrap(self, item_id: str, wrapped: bytes) -> bytes:
        """包んだ DEK を解く。前の形（KMS の包み）なら KMS で解いて KEK で包み直す。"""
        if wrapped.startswith(KEK_WRAP_MAGIC):
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM

            body = wrapped[len(KEK_WRAP_MAGIC) :]
            return AESGCM(self.kek()).decrypt(body[:NONCE_BYTES], body[NONCE_BYTES:], self._kek_aad(item_id))
        dek = self._wrapper.unwrap(wrapped, self._pid)
        self._rewrap(item_id, wrapped, dek)
        return dek

    def _rewrap(self, item_id: str, old_wrapped: bytes, dek: bytes) -> None:
        """前の形の行を KEK の包みに置き直す。行が消えた・変わったなら何もしない（忘れた行を戻さない）。"""
        try:
            self._client.put_item(
                TableName=self._table_name,
                Item={
                    **self._key(item_id),
                    "wrapped": {"B": self._wrap(item_id, dek)},
                    "created_at": {"S": datetime.now(timezone.utc).isoformat()},
                },
                ConditionExpression="#w = :old",  # wrapped は予約語
                ExpressionAttributeNames={"#w": "wrapped"},
                ExpressionAttributeValues={":old": {"B": old_wrapped}},
            )
        except Exception:  # 置き直しはついで。失敗しても読みは止めない（次に読むときまた試す）
            pass

    # ── 作る・読む・消す ──

    def new_key(self, item_id: str) -> bytes:
        """DEK を作り、包んで鍵の表に置き、平文の DEK を返す。本体を書く**前に**呼ぶ。"""
        dek = os.urandom(DEK_BYTES)
        wrapped = self._wrap(item_id, dek)
        self._client.put_item(
            TableName=self._table_name,
            Item={
                **self._key(item_id),
                "wrapped": {"B": wrapped},
                "created_at": {"S": datetime.now(timezone.utc).isoformat()},
            },
        )
        self._cache.put(item_id, dek)
        return dek

    def key_for(self, item_id: str) -> bytes | None:
        """平文の DEK。鍵の表に無ければ None（忘れた項目）。"""
        cached = self._cache.get(item_id)
        if cached is not None:
            return cached
        response = self._client.get_item(TableName=self._table_name, Key=self._key(item_id), ConsistentRead=True)
        item = response.get("Item")
        if item is None:
            return None
        dek = self._unwrap(item_id, bytes(item["wrapped"]["B"]))
        self._cache.put(item_id, dek)
        return dek

    def has_key(self, item_id: str) -> bool:
        """鍵の表に鍵があるか（包まれた鍵を解かずに見る。K28）。"""
        response = self._client.get_item(
            TableName=self._table_name, Key=self._key(item_id), ConsistentRead=True, ProjectionExpression="sk"
        )
        return "Item" in response

    def require_key(self, item_id: str) -> bytes:
        dek = self.key_for(item_id)
        if dek is None:
            raise ShreddedError(item_id)
        return dek

    def keys_for(self, item_ids: list[str]) -> dict[str, bytes]:
        """まとめて引く。鍵の無い項目は結果に入らない。"""
        result: dict[str, bytes] = {}
        missing: list[str] = []
        for item_id in dict.fromkeys(item_ids):
            cached = self._cache.get(item_id)
            if cached is not None:
                result[item_id] = cached
            else:
                missing.append(item_id)

        for start in range(0, len(missing), _BATCH_GET_LIMIT):
            chunk = missing[start : start + _BATCH_GET_LIMIT]
            request: Any = {self._table_name: {"Keys": [self._key(i) for i in chunk], "ConsistentRead": True}}
            while request:
                response = self._client.batch_get_item(RequestItems=request)
                for raw in response.get("Responses", {}).get(self._table_name, []):
                    item_id = raw["sk"]["S"][len(KEY_SK_PREFIX) :]
                    dek = self._unwrap(item_id, bytes(raw["wrapped"]["B"]))
                    self._cache.put(item_id, dek)
                    result[item_id] = dek
                unprocessed = response.get("UnprocessedKeys") or {}
                request = unprocessed if unprocessed else None
        return result

    def shred(self, item_id: str) -> None:
        """DEK を鍵の表とキャッシュから消す。これで本体の暗号文はどこにあっても読めなくなる。

        本体の削除より**先に**呼ぶ（途中で落ちても「読めない」側に倒れる）。
        """
        self._cache.evict(item_id)
        self._client.delete_item(TableName=self._table_name, Key=self._key(item_id))

    def clear_cache(self) -> None:
        self._cache.clear()
