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

from .actions import ACTION_SHARE, CHAT, actions_of, count_actions, deltas_for, words_of
from .defaults import DEFAULT_DESIRE_CONFIG
from .engine import Inputs, is_body_driven, iso, nudge, parse_time, step, utcnow
from .history import KEEP_DAYS, DynamoHistory, changes_of, format_history
from .shape import (
    BODY_DRIVEN_WORDS,
    MAX_DESIRES,
    ShapeError,
    apply_shape,
    check_name,
    new_spec,
    satisfy_amount_of,
    shape_of,
)
from .store import DynamoRowStore, FileRowStore, RowStore, region_from_env

logger = logging.getLogger("petit-desire")

WRITE_RETRIES = 5
# ぷちたち。の受け箱の出来事の言い方（履歴用）
SNS_WORDS = {"comment": "コメント", "like": "いいね", "touch": "なでなで", "snack": "おやつ"}
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


def _is_nade(name: str, label: str) -> bool:
    """撫でられたい（触れ合いの欲求）か。ぷちが自分で足した欲求なので、キーか表示の名前で見る（撫/な は同じ）。"""
    return any(str(x).strip().replace("撫", "な") == "なでられたい" for x in (name, label))


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

    @property
    def history(self) -> DynamoHistory | None:
        """欲求の履歴（akatsuki-petit#261）。house 表のときだけ。"""
        table = getattr(self.store, "table", None) if isinstance(self.store, DynamoRowStore) else None
        return DynamoHistory(table, self.pid) if table is not None else None

    def _log(self, write: Callable[[DynamoHistory], None]) -> None:
        """履歴を書く。書けなくても欲求の更新は止めない。"""
        hist = self.history
        if hist is None:
            return
        try:
            write(hist)
        except Exception as e:
            logger.warning("history write failed: %s", type(e).__name__)

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
                # 読み位置が無い・空（{}）なら初回と同じ扱い（新しい順に少し遡る。最古から辿り直さない）
                cursor = (engine.get("mem_cursor") or None) if initialized else None
                inputs.memories, inputs.memory_cursor = self.memory.read(cursor)
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

    def config_for(self, row: dict[str, Any] | None) -> DesireSystemConfig:
        """生まれつきの設定に、ぷちが行に置いた形（shape）を重ねたもの（akatsuki-petit#106）。"""
        return apply_shape(self.config, shape_of(row))

    def update(self, now: datetime | None = None) -> tuple[dict[str, Any], Inputs]:
        now = now or utcnow()
        before = self.store.read()
        inputs = self.gather(before, now)
        row = self._commit(lambda r: step(r, self.config_for(r), inputs, now))
        self._log(lambda h: self._log_update(h, before, row, inputs, now))
        return row, inputs

    def _log_update(self, hist: DynamoHistory, before: dict[str, Any] | None, row: dict[str, Any],
                    inputs: Inputs, now: datetime) -> None:
        """記憶・ぷちたち。で満たされた出来事と、1 時間に 1 回の値の写しを残す。"""
        old_d, new_d = dict((before or {}).get("desires") or {}), dict(row.get("desires") or {})
        # step() の返す行には shape が無い（書くときも shape には触らない）ので、読んだ行の shape で見る。
        # これが無いと、ぷちが足した欲求が 1 時間ごとの写しに入らなかった（澪 10/21「撫でられたい・残したいの流れが いま だけ」）
        cfg = self.config_for({**(before or {}), **row})
        old_e = (before or {}).get("engine") or {}
        old_sat, new_sat = old_e.get("satisfied_at") or {}, (row.get("engine") or {}).get("satisfied_at") or {}
        if isinstance(old_e.get("raw"), dict):  # 初回（過去の記憶をまとめて読む）は出来事にしない
            by_mem = [n for n, at in new_sat.items() if at != old_sat.get(n)]
            hist.add_event("記憶に残したことで満たされた", changes_of(old_d, new_d, by_mem), now)
            last = parse_time(old_e.get("sns_last_at"))
            fresh = [e for e in inputs.sns if last is not None and e.at > last]
            if fresh:
                kinds: dict[str, dict[str, int]] = {}
                names: set[str] = set()
                for e in fresh:
                    who = "人" if e.actor.startswith("A#") else "ぷち" if e.actor.startswith("P#") else ""
                    by = kinds.setdefault(who, {})
                    by[e.type] = by.get(e.type, 0) + 1
                    names |= set(cfg.event_effects.get(e.type) or cfg.event_effects.get("*") or {})
                what = " / ".join(
                    (f"{who}から " if who else "") + "・".join(f"{SNS_WORDS.get(k, k)}×{n}" for k, n in by.items())
                    for who, by in sorted(kinds.items(), key=lambda kv: {"ぷち": 0, "人": 1}.get(kv[0], 2)))
                hist.add_event(f"ぷちたち。で反応があった（{what}）", changes_of(old_d, new_d, sorted(names)), now)
        hist.snapshot({k: v for k, v in new_d.items() if k in cfg.desires}, now)

    def nudge(self, name: str, delta: float, *, satisfied: bool = False) -> dict[str, Any]:
        before = self.store.read()
        if name not in self.config_for(before).desires:
            raise KeyError(name)
        now = utcnow()
        row = self._commit(lambda r: nudge(r, self.config_for(r), name, delta, now, satisfied=satisfied))
        cause = "自分で満たした（satisfy_desire）" if satisfied else (
            "驚き・新しさで強まった（boost_desire）" if delta > 0 else "自分で下げた")
        self._log(lambda h: h.add_event(cause, changes_of(dict((before or {}).get("desires") or {}),
                                                          dict(row.get("desires") or {}), [name]), now))
        return row

    def history_text(self, hours: float = 24, only: str = "") -> str:
        """desire_history の中身。"""
        hist = self.history
        if hist is None:
            return "欲求の履歴は、家（house 表）で暮らしているときだけ残る。"
        hours = max(1.0, min(float(hours or 24), 24.0 * KEEP_DAYS))
        row = self.store.read()
        snaps, events = hist.read(utcnow() - timedelta(hours=hours))
        cfg = self.config_for(row)
        labels = {k: d.name_ja for k, d in cfg.desires.items()}
        return format_history(snaps, events, labels, self.levels(row), hours, (only or "").strip())

    def satisfy_amount(self, name: str, row: dict[str, Any] | None = None) -> float:
        cfg = self.config_for(row if row is not None else self.store.read())
        return satisfy_amount_of(cfg.desires[name]) if name in cfg.desires else SATISFY_AMOUNT

    def check_not_body_driven(self, name: str, row: dict[str, Any] | None = None) -> None:
        """体が決める欲求（眠い）は、ぷち自身の道具では動かさない（2026-10-22 なぎさん）。"""
        cfg = self.config_for(row if row is not None else self.store.read())
        if name in cfg.desires and is_body_driven(name, cfg.desires[name]):
            raise ShapeError(f"「{cfg.desires[name].name_ja}」{BODY_DRIVEN_WORDS}")

    def satisfy(self, name: str, amount: float | None = None) -> dict[str, Any]:
        """満たす。amount を省けば、その欲求の満たし方（satisfy_amount、無ければ 0.4）だけ下げる。"""
        self.check_not_body_driven(name)
        if amount is None:
            amount = self.satisfy_amount(name)
        amount = max(0.0, min(1.0, float(amount)))
        return self.nudge(name, -amount, satisfied=True)

    def boost(self, name: str, amount: float) -> tuple[dict[str, Any], float]:
        self.check_not_body_driven(name)
        amount = max(0.0, min(BOOST_MAX, float(amount)))
        return self.nudge(name, amount), amount

    def act(self, names: list[str]) -> tuple[dict[str, Any] | None, dict[str, dict[str, int]]]:
        """行動（道具の名前・chat）で、その行動を持つ欲求を少し満たす（actions.py）。(行, 欲求ごとの効いた行動)。"""
        counts = count_actions(names)
        if not counts:
            return None, {}
        before = self.store.read()
        cfg0 = self.config_for(before)
        deltas, hits = deltas_for(cfg0, counts, satisfy_amount_of)
        deltas = {k: v for k, v in deltas.items() if k in cfg0.desires and not is_body_driven(k, cfg0.desires[k])}
        hits = {k: v for k, v in hits.items() if k in deltas}
        if not deltas:
            return None, {}
        now = utcnow()

        def make(r: dict[str, Any] | None) -> dict[str, Any]:
            row, cfg = r, self.config_for(r)
            for name, delta in deltas.items():
                if name in cfg.desires:
                    row = nudge(row, cfg, name, delta, now, satisfied=True)
            return row

        row = self._commit(make)
        old = dict((before or {}).get("desires") or {})
        new = dict(row.get("desires") or {})

        def log(h: DynamoHistory) -> None:
            for name, got in hits.items():
                h.add_event(f"行動で満たされた（{words_of(got)}）", changes_of(old, new, [name]), now)

        self._log(log)
        return row, hits

    # ── 形を変える（akatsuki-petit#106） ──

    def shape(self, name: str, *, name_ja: str | None = None, description: str | None = None,
              satisfaction_hours: float | None = None, satisfy_amount: float | None = None,
              keywords: list[str] | None = None, level: float | None = None,
              actions: list[str] | None = None) -> tuple[dict[str, Any], bool]:
        """欲求を足す（無い名前）か、形を変える（ある名前）。(行, 新しく足したか)。

        新しい欲求は level（省けば initial_level）から始まり、次の更新から時間で満ちていく。
        手放した（retire）名前を渡すと、戻ってくる。
        """
        name = check_name(name)
        spec = new_spec(name_ja=name_ja, description=description, satisfaction_hours=satisfaction_hours,
                        satisfy_amount=satisfy_amount, keywords=keywords, actions=actions)
        now = utcnow()
        added = {"v": False}

        def make(r: dict[str, Any] | None) -> dict[str, Any]:
            row = dict(r or {})
            shape = shape_of(row)
            cfg = self.config_for(row)
            self.check_not_body_driven(name, row)
            is_new = name not in cfg.desires
            if is_new and len(cfg.desires) >= MAX_DESIRES:
                raise ShapeError(f"欲求は {MAX_DESIRES} 個まで。足すなら、どれかを retire_desire で手放してから")
            if is_new and "satisfaction_hours" not in spec and name not in self.config.desires:
                raise ShapeError("新しい欲求には satisfaction_hours（0 から 1 まで満ちる時間）が要る")
            entry = {} if shape.get(name, {}).get("retired") else dict(shape.get(name, {}))
            entry.update(spec)
            entry.update({"by": "petit", "at": iso(now)})
            shape[name] = entry
            row["shape"] = shape
            desires = dict(row.get("desires") or {})
            if is_new:
                start = self.config.initial_level if level is None else max(0.0, min(1.0, float(level)))
                desires[name] = round(start, 4)
            row["desires"] = desires
            row["updated_at"] = iso(now)
            row["labels"] = {k: d.name_ja for k, d in apply_shape(self.config, shape).desires.items()}
            added["v"] = is_new
            return row

        return self._commit(make), added["v"]

    def retire(self, name: str) -> dict[str, Any]:
        """欲求を手放す（計算にも表示にも出さない）。shape_desire で同じ名前を渡せば戻る。"""
        name = check_name(name)
        now = utcnow()

        def make(r: dict[str, Any] | None) -> dict[str, Any]:
            row = dict(r or {})
            if name not in self.config_for(row).desires:
                raise ShapeError(f"「{name}」という欲求は無い")
            self.check_not_body_driven(name, row)
            shape = shape_of(row)
            shape[name] = {"retired": True, "by": "petit", "at": iso(now)}
            row["shape"] = shape
            row["desires"] = {k: v for k, v in (row.get("desires") or {}).items() if k != name}
            row["updated_at"] = iso(now)
            row["labels"] = {k: d.name_ja for k, d in apply_shape(self.config, shape).desires.items()}
            return row

        return self._commit(make)

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
        for name in self.config_for(row).desires:
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
        cfg = self.config_for(row)
        labels = {k: d.name_ja for k, d in cfg.desires.items()}
        order = sorted(levels, key=lambda k: (-levels[k], cfg.priority.index(k)
                                               if k in cfg.priority else 99))
        mine = {k for k, v in shape_of(row).items() if not v.get("retired")}
        # 家 API が数える、まだ読んでいない触れ合い（akatsuki-petit#262。petit-api desires_store.UNREAD_ATTR）
        try:
            touch_unread = int((row or {}).get("touch_unread") or 0)
        except (TypeError, ValueError):
            touch_unread = 0
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
            d = cfg.desires[k]
            desc = d.description
            how = ""
            if not compact:
                own = "・自分で決めた形" if k in mine else ""
                acts = actions_of(k, d)
                act = ("・" + "・".join("会話" if a == CHAT else a for a in acts)
                       + f" をすると自動で −{satisfy_amount_of(d) * ACTION_SHARE:g}") if acts else ""
                how = f"（満たすと −{satisfy_amount_of(d):g}・0→1 まで {d.satisfaction_hours:g} 時間{act}{own}）"
            unread = ""
            if touch_unread and _is_nade(k, labels.get(k, "")):
                unread = f"（まだ読んでいない、触られた・撫でられた {touch_unread} 回。house の body_since で読むと受け取れる）"
            lines.append(f"  {labels.get(k, k)}（{k}）: [{bar}] {v:.3f}{mark}{unread}{how}" + (f" … {desc}" if desc else ""))
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
        no_act = [k for k in order if not actions_of(k, cfg.desires[k])]
        if no_act and not compact:
            # 澪 10/21「shape_desire に actions が見えず、ぷちたちと話したい・進みたいを下げる行動を決められない」
            lines.append(
                "行動で自動で下がる形がまだ無い欲求: " + "、".join(labels.get(k, k) for k in no_act)
                + "。shape_desire の actions に道具の名前（sns_comment・board_post・WebSearch など）か"
                " chat（会話の 1 往復）を渡すと、その行動で少しずつ下がる。"
            )
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
