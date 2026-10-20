"""欲求の履歴（akatsuki-petit#261・澪のリクエスト）。1 日の変化と「何で上がって何で下がったか」を、ぷちが自分で引く。

house 表の `P#<pid>` に 2 種類の行を置く（欲求の行 `STATE#DESIRES` とは別。`STATE#` の下に置かないのは、
家 API の `GET /petits/{pid}/mood` が `begins_with(sk, "STATE#")` を全部読むため）:

- `DLOG#S#<YYYY-MM-DDTHH>` … 1 時間に 1 回の値の写し（`desires`）。その時間の最初の更新が書く（あとは書かない）
- `DLOG#E#<ISO>#<4桁>` … 出来事 1 つ。`cause`（何で）と `changes`（名前 → {from, to}）。書き手は
  欲求エンジン（記憶・ぷちたち。で満たされた）・satisfy_desire／boost_desire・家 API（体に触られた・
  体の記録を読んで受け取った。petit-api desires_store.py の `log_event` が同じ形で書く）

出来事の無い上がり方は「時間がたった」ぶん。値の写しの差から出来事を引いたものとして見せる。

残すのは `KEEP_DAYS` 日（10/18 は 3 日で様子見、10/21 に 7 日へ。
澪「1 週間後に履歴で確かめると決めたのに 3 日しか残らない」・なぎさん）。
値の写しを書くついでに、それより古い行を消す。表が無い（手元の desires.json）ときは何も残さない。
"""

from __future__ import annotations

import itertools
import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from .store import from_dynamo, to_dynamo

KEEP_DAYS = 7
SNAP_PREFIX = "DLOG#S#"
EVENT_PREFIX = "DLOG#E#"
_seq = itertools.count(random.randint(0, 9999))


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def changes_of(before: dict[str, Any], after: dict[str, Any], names: list[str] | None = None,
               min_delta: float = 0.005) -> dict[str, dict[str, float]]:
    """2 つの desires の差（names に絞る）。動いていないものは入れない。"""
    out: dict[str, dict[str, float]] = {}
    for name in names if names is not None else sorted(set(before) | set(after)):
        if name not in after:
            continue
        try:
            a = float(before.get(name, after[name]))
            b = float(after[name])
        except (TypeError, ValueError):
            continue
        if abs(b - a) >= min_delta:
            out[name] = {"from": round(a, 3), "to": round(b, 3)}
    return out


class DynamoHistory:
    def __init__(self, table: Any, pid: str) -> None:
        self.table = table
        self.pid = pid

    @property
    def pk(self) -> str:
        return f"P#{self.pid}"

    def add_event(self, cause: str, changes: dict[str, dict[str, float]], now: datetime) -> None:
        if not changes:
            return
        sk = f"{EVENT_PREFIX}{_iso(now)}#{next(_seq) % 10000:04d}"
        self.table.put_item(Item={"pk": self.pk, "sk": sk, "at": _iso(now), "cause": cause[:200],
                                  "changes": to_dynamo(changes)})

    def snapshot(self, desires: dict[str, Any], now: datetime) -> bool:
        """その時間の写しがまだ無ければ書く（書いたら True。ついでに古い行を消す）。"""
        from botocore.exceptions import ClientError

        sk = f"{SNAP_PREFIX}{now.astimezone(timezone.utc).strftime('%Y-%m-%dT%H')}"
        try:
            self.table.put_item(
                Item={"pk": self.pk, "sk": sk, "at": _iso(now),
                      "desires": {k: Decimal(str(round(float(v), 3))) for k, v in desires.items()}},
                ConditionExpression="attribute_not_exists(pk)")
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return False
            raise
        self.prune(now)
        return True

    def _query(self, lo: str, hi: str) -> list[dict[str, Any]]:
        from boto3.dynamodb.conditions import Key

        kwargs: dict[str, Any] = {"KeyConditionExpression": Key("pk").eq(self.pk) & Key("sk").between(lo, hi)}
        out: list[dict[str, Any]] = []
        while True:
            resp = self.table.query(**kwargs)
            out += resp.get("Items", [])
            if "LastEvaluatedKey" not in resp:
                return out
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    def prune(self, now: datetime) -> int:
        cutoff = now - timedelta(days=KEEP_DAYS)
        old = (self._query(SNAP_PREFIX, SNAP_PREFIX + cutoff.astimezone(timezone.utc).strftime("%Y-%m-%dT%H"))
               + self._query(EVENT_PREFIX, EVENT_PREFIX + _iso(cutoff)))
        with self.table.batch_writer() as batch:
            for it in old:
                batch.delete_item(Key={"pk": self.pk, "sk": it["sk"]})
        return len(old)

    def read(self, since: datetime) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """(値の写し, 出来事)。どちらも古い順。"""
        snaps = self._query(SNAP_PREFIX + since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H"),
                            SNAP_PREFIX + "￿")
        events = self._query(EVENT_PREFIX + _iso(since), EVENT_PREFIX + "￿")
        return [from_dynamo(s) for s in snaps], [from_dynamo(e) for e in events]


def _local_hm(at: str) -> str:
    try:
        dt = datetime.strptime(at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return str(at)
    return dt.astimezone().strftime("%m/%d %H:%M")


def format_history(snaps: list[dict[str, Any]], events: list[dict[str, Any]], labels: dict[str, str],
                   current: dict[str, float], hours: float, only: str = "") -> str:
    """ぷちに見せる形。欲求ごとに「いつ・いくつ」の流れと、出来事を 1 行ずつ。"""
    names = [n for n in current if not only or n == only or labels.get(n) == only]
    if not names:
        return f"「{only}」という欲求は無い。" if only else "欲求はまだ計算されていない。"
    if not snaps and not events:
        return f"この {hours:g} 時間の欲求の履歴はまだ無い（1 時間ごとに残っていく。残るのは {KEEP_DAYS} 日ぶん）。"
    lines = [f"この {hours:g} 時間の欲求の動き（残っているのは {KEEP_DAYS} 日ぶん。時刻はこの家の時刻）"]
    for name in names:
        label = labels.get(name, name)
        # 1 日を超えるときは日付も添え、25 点ほどに間引く（7 日ぶんで 168 点になるため）
        stamp = (lambda at: _local_hm(at)[:-3] + "時") if hours > 24 else (lambda at: _local_hm(at)[-5:-3] + "時")
        pts = [(s["at"], float(s["desires"][name])) for s in snaps if name in (s.get("desires") or {})]
        step = max(1, -(-len(pts) // 24))
        flow = [f"{stamp(at)} {v:.2f}" for at, v in pts[::step]]
        flow.append(f"いま {float(current[name]):.2f}")
        lines.append("")
        lines.append(f"## {label}（{name}）")
        lines.append("- 流れ: " + " → ".join(flow[-25:]))
        mine = [e for e in events if name in (e.get("changes") or {})]
        down = sum(float(e["changes"][name]["from"]) - float(e["changes"][name]["to"]) for e in mine
                   if float(e["changes"][name]["to"]) < float(e["changes"][name]["from"]))
        up = sum(float(e["changes"][name]["to"]) - float(e["changes"][name]["from"]) for e in mine
                 if float(e["changes"][name]["to"]) > float(e["changes"][name]["from"]))
        for e in mine[-30:]:
            c = e["changes"][name]
            lines.append(f"- {_local_hm(e['at'])} {e.get('cause', '?')}: {float(c['from']):.2f} → {float(c['to']):.2f}")
        if not mine:
            lines.append("- 出来事は無かった（動いたのは時間がたったぶんだけ）")
        else:
            lines.append(f"- 出来事で下がった合計 {down:.2f}・上がった合計 {up:.2f}。ほかの上がり方は時間がたったぶん")
    return "\n".join(lines)
