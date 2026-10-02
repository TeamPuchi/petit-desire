"""engine.step() / nudge()（クラウド版の積み上げ式）の単体テスト。I/O なし。"""

from datetime import datetime, timedelta, timezone

import pytest

from desire_updater import parse_desire_config
from petit_desire.defaults import DEFAULT_DESIRE_CONFIG
from petit_desire.engine import Inputs, MemoryEvent, SnsEvent, iso, nudge, step

T0 = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)


def cfg(**over):
    raw = {
        "desires": {
            "curiosity": {"name_ja": "知りたい", "satisfaction_hours": 2.0, "keywords": ["調べた"]},
            "miss_companion": {
                "name_ja": "会いたい", "satisfaction_hours": 4.0,
                "keywords": ["なぎと話した"], "categories": ["conversation"],
            },
        },
        "sensor_effects": [
            {"sensor": "battery", "condition": {"op": "range", "min": 1, "max": 20},
             "effects": {"*": {"multiply": 0.5}}},
        ],
        "event_effects": {"comment": {"miss_companion": {"add": -0.1}}, "*": {"curiosity": {"add": 0.05}}},
        "priority": ["miss_companion", "curiosity"],
    }
    raw.update(over)
    return parse_desire_config(raw)


def test_first_run_uses_initial_level_without_memories():
    row = step(None, cfg(), Inputs(), T0)
    assert row["desires"] == {"curiosity": 0.5, "miss_companion": 0.5}
    assert row["engine"]["raw"] == {"curiosity": 0.5, "miss_companion": 0.5}
    assert row["engine"]["written"] == row["desires"]
    assert row["updated_at"] == "2026-09-26T12:00:00Z"
    assert row["labels"]["curiosity"] == "知りたい"


def test_first_run_uses_memory_elapsed_like_original():
    mem = [MemoryEvent(at=T0 - timedelta(hours=1), content="今日はなぎと話した")]
    row = step(None, cfg(), Inputs(memories=mem), T0)
    assert row["desires"]["miss_companion"] == 0.25  # 1h / 4h（元の calculate_desire_level と同じ）
    assert row["desires"]["curiosity"] == 0.5
    assert row["engine"]["satisfied_at"]["miss_companion"] == iso(T0 - timedelta(hours=1))


def test_time_growth_matches_original_formula():
    """何も起きなければ、元の「最後に満たされてからの経過 ÷ h」と同じ列になる。"""
    mem = [MemoryEvent(at=T0, content="調べた")]
    row = step(None, cfg(), Inputs(memories=mem), T0)
    assert row["desires"]["curiosity"] == 0.0
    for k in range(1, 5):
        now = T0 + timedelta(minutes=30 * k)
        row = step(row, cfg(), Inputs(), now)
        assert row["desires"]["curiosity"] == pytest.approx(min(1.0, 0.5 * k / 2), abs=1e-3)


def test_growth_caps_at_one_after_long_gap():
    row = step(None, cfg(), Inputs(), T0)
    row = step(row, cfg(), Inputs(), T0 + timedelta(days=90))
    assert row["desires"] == {"curiosity": 1.0, "miss_companion": 1.0}


def test_new_memory_lowers_level():
    row = step(None, cfg(), Inputs(), T0)
    row = step(row, cfg(), Inputs(), T0 + timedelta(hours=2))
    assert row["desires"]["miss_companion"] == 1.0
    mem = [MemoryEvent(at=T0 + timedelta(hours=2), content=None, category="conversation")]
    row = step(row, cfg(), Inputs(memories=mem), T0 + timedelta(hours=3))
    assert row["desires"]["miss_companion"] == 0.25  # 会話の記憶（本文なし・種類だけ）から 1h


def test_external_delta_from_house_api_is_kept():
    """家 API（MQTT のタッチ）が desires に足した差分は、次の更新で消えない。"""
    row = step(None, cfg(), Inputs(), T0)
    row["desires"]["miss_companion"] = 0.35  # タッチで -0.15（desires_store.apply と同じ書き方）
    row["updated_at"] = "2026-09-26T12:01:00Z"
    row = step(row, cfg(), Inputs(), T0 + timedelta(minutes=60))
    assert row["desires"]["miss_companion"] == pytest.approx(0.35 + 0.25, abs=1e-3)


def test_satisfy_survives_next_update():
    """元は desires.json の再計算で satisfy_desire が消えていた。積み上げ式では残る。"""
    c = cfg()
    row = step(None, c, Inputs(), T0)
    row = step(row, c, Inputs(), T0 + timedelta(hours=1))  # curiosity 1.0
    row = nudge(row, c, "curiosity", -0.4, T0 + timedelta(hours=1, minutes=1), satisfied=True)
    assert row["desires"]["curiosity"] == 0.6
    assert "curiosity" in row["engine"]["satisfied_at"]
    row = step(row, c, Inputs(), T0 + timedelta(hours=1, minutes=5))
    assert row["desires"]["curiosity"] == pytest.approx(0.6 + (5 / 60) / 2, abs=1e-3)


def test_sensor_effect_is_transient():
    c = cfg()
    row = step(None, c, Inputs(sensors={"battery": 10}), T0)
    assert row["desires"]["curiosity"] == 0.25  # 0.5 × 0.5（元の Step 2）
    assert row["engine"]["raw"]["curiosity"] == 0.5  # raw には残さない
    row = step(row, c, Inputs(sensors={"battery": 80}), T0)
    assert row["desires"]["curiosity"] == 0.5  # 電池が戻れば元の値
    row = step(row, c, Inputs(sensors={}), T0)  # センサーが無くても壊れない
    assert row["desires"]["curiosity"] == 0.5


def test_cross_effects_like_original():
    c = cfg(cross_effects=[{"when": {"desire": "miss_companion", "op": ">=", "value": 0.9},
                            "effects": {"curiosity": {"multiply": 0.5}}}])
    row = step(None, c, Inputs(), T0)
    row = step(row, c, Inputs(), T0 + timedelta(hours=2))
    assert row["desires"]["miss_companion"] == 1.0
    assert row["desires"]["curiosity"] == 0.5  # 1.0 × 0.5
    assert row["engine"]["raw"]["curiosity"] == 1.0


def test_sns_events_ignored_on_first_run_then_applied():
    c = cfg()
    old = [SnsEvent(type="comment", at=T0 - timedelta(days=1))]
    row = step(None, c, Inputs(sns=old, sns_cursor="c1"), T0)
    assert row["desires"]["miss_companion"] == 0.5  # 過去分は効かせない
    assert row["engine"]["sns_cursor"] == "c1"
    new = [SnsEvent(type="comment", at=T0 + timedelta(minutes=1)),
           SnsEvent(type="like", at=T0 + timedelta(minutes=2))]
    row = step(row, c, Inputs(sns=new, sns_cursor="c2"), T0 + timedelta(minutes=5))
    # comment: -0.1、like は "*" で curiosity +0.05（両方に時間ぶんが乗る）
    assert row["desires"]["miss_companion"] == pytest.approx(0.5 - 0.1 + (5 / 60) / 4, abs=1e-3)
    assert row["desires"]["curiosity"] == pytest.approx(0.5 + 0.05 + (5 / 60) / 2, abs=1e-3)
    # 同じ出来事をもう一度渡しても二重に効かない（sns_last_at より古い）
    again = step(row, c, Inputs(sns=new), T0 + timedelta(minutes=5))
    assert again["desires"] == row["desires"]


def test_unknown_names_in_row_are_kept():
    row = {"desires": {"hunger": 0.2}, "updated_at": "2026-09-26T11:00:00Z"}
    row = step(row, cfg(), Inputs(), T0)
    assert row["desires"]["hunger"] == 0.2
    assert "hunger" not in row["engine"]["raw"]


def test_row_without_engine_starts_from_existing_values():
    """家 API がエンジンより先に行を作っていた（タッチで 0.5 起点に差分）。"""
    row = {"desires": {"miss_companion": 0.4}, "updated_at": "2026-09-26T11:00:00Z"}
    row = step(row, cfg(), Inputs(), T0)
    assert row["desires"]["miss_companion"] == 0.4
    assert row["desires"]["curiosity"] == 0.5


def test_nudge_only_row_counts_as_first_run():
    c = cfg()
    row = nudge(None, c, "curiosity", -0.4, T0, satisfied=True)
    assert row["desires"]["curiosity"] == 0.1
    row = step(row, c, Inputs(sns=[SnsEvent("comment", T0 - timedelta(hours=1))]), T0)
    assert row["desires"]["miss_companion"] == 0.5  # 初回扱いなので過去の SNS は効かない
    assert row["desires"]["curiosity"] == 0.1


def test_time_driven_false_and_base_level():
    c = cfg(desires={
        "sleepy": {"name_ja": "眠い", "satisfaction_hours": 1.0, "time_driven": False, "base_level": 0.3},
        "play": {"name_ja": "遊びたい", "satisfaction_hours": 1.0, "base_level": 0.2, "keywords": ["遊んだ"]},
    })
    row = step(None, c, Inputs(memories=[MemoryEvent(at=T0, content="遊んだ")]), T0)
    assert row["desires"]["sleepy"] == 0.3
    assert row["desires"]["play"] == 0.2  # 0.0 だが base_level で下げ止まり
    row = step(row, c, Inputs(), T0 + timedelta(hours=5))
    assert row["desires"]["sleepy"] == 0.3


def test_default_config_is_loadable():
    c = parse_desire_config(DEFAULT_DESIRE_CONFIG)
    assert set(c.desires) == {"curiosity", "miss_companion", "sleepy"}
    assert c.desires["miss_companion"].keywords  # COMPANION_NAME から自動生成
    row = step(None, c, Inputs(), T0)
    assert row["dominant"] == "miss_companion"  # 同値は priority 順


# ===================== 満ちる速さ（akatsuki-petit#157） =====================

def test_growth_is_the_same_however_often_the_updater_runs():
    """5 分ごとに回しても、2 時間まとめて 1 回でも、伸びは同じ（二重に数えていない）。"""
    c = cfg()
    row = step(None, c, Inputs(), T0)
    every5 = row
    for m in range(5, 121, 5):
        every5 = step(every5, c, Inputs(), T0 + timedelta(minutes=m))
    once = step(row, c, Inputs(), T0 + timedelta(hours=2))
    for k, v in once["desires"].items():
        assert every5["desires"][k] == pytest.approx(v, abs=0.002)  # 1 回ごとの丸め（小数 4 桁）ぶんだけ違う
    assert once["desires"]["miss_companion"] == pytest.approx(min(1.0, 0.5 + 2 / 3), abs=0.001)


def test_growth_is_the_same_day_and_night():
    """元の欲求システムと同じく、夜も昼も満ちる速さは同じ（akatsuki-petit#157 の 1/4 は外した）。"""
    from petit_desire.engine import growth_hours

    local = datetime.now().astimezone().tzinfo
    night = datetime(2026, 9, 28, 3, 0, tzinfo=local)
    day = night.replace(hour=12)
    assert growth_hours(night, night + timedelta(hours=2)) == pytest.approx(2.0)
    assert growth_hours(day, day + timedelta(hours=2)) == pytest.approx(2.0)
    c = cfg()
    grown = []
    for start in (night, day):
        row = step(None, c, Inputs(), start)
        row = step(row, c, Inputs(sensors={"sleeping": 1}), start + timedelta(hours=1))  # 機体が眠っていても同じ
        grown.append(row["desires"]["miss_companion"])
    hours = c.desires["miss_companion"].satisfaction_hours
    assert grown[0] == grown[1] == pytest.approx(0.5 + 1 / hours, abs=0.001)


def test_default_config_has_no_rest_window():
    assert "rest_hours" not in DEFAULT_DESIRE_CONFIG
    assert not hasattr(parse_desire_config(DEFAULT_DESIRE_CONFIG), "rest")


def test_default_hours_are_the_longer_ones():
    """なぎさん 9/29「夜の欲求は長くしておいて」: 既定の満タンまでの時間は 知りたい 4 時間・会いたい 6 時間。"""
    c = parse_desire_config(DEFAULT_DESIRE_CONFIG)
    assert c.desires["curiosity"].satisfaction_hours == 4.0
    assert c.desires["miss_companion"].satisfaction_hours == 6.0


def test_default_sleepy_is_satisfied_by_remembering_sleep():
    """眠い（akatsuki-petit#186）: 時間で満ち、「スリープした」と記憶に残すと満たされる。
    満タンまで 16 時間（なぎさん 2026-10-02「人と同じように 1 日 1 日で」）。起きたときに家 API が 0 に戻す。"""
    c = parse_desire_config(DEFAULT_DESIRE_CONFIG)
    sleepy = c.desires["sleepy"]
    assert sleepy.name_ja == "眠い" and sleepy.satisfaction_hours == 16.0
    assert sleepy.keywords == ["スリープ", "眠った", "寝た"]
    row = step(None, c, Inputs(), T0)
    assert row["desires"]["sleepy"] == 0.5  # 手がかりが無いうちは initial_level
    slept = T0 + timedelta(minutes=1)
    row = step(row, c, Inputs(memories=[MemoryEvent(at=slept, content="眠くなったのでスリープした")]),
               T0 + timedelta(minutes=5))
    assert row["desires"]["sleepy"] < 0.01  # 眠ったら満たされる
    # 8 時間眠って、朝に起きる。起きたとき家 API が眠いを 0 に戻す（他の書き手が動かした分として取り込む）
    woke = slept + timedelta(hours=8)
    row = step(row, c, Inputs(), woke)
    assert row["desires"]["sleepy"] == pytest.approx(0.5, abs=0.01)  # 眠っているあいだも時間ぶんは進んでいる
    row = {**row, "desires": {**row["desires"], "sleepy": 0.0}}        # 家 API: wake で -1（0 に丸め）
    row = step(row, c, Inputs(), woke + timedelta(hours=11, minutes=12))
    assert row["desires"]["sleepy"] == pytest.approx(0.7, abs=0.01)   # 起きて 11 時間ほどで眠くなる
    row = step(row, c, Inputs(), woke + timedelta(hours=16))
    assert row["desires"]["sleepy"] == 1.0                            # 起きて 16 時間で満タン


def test_shape_is_applied_on_top_of_config():
    from petit_desire.shape import apply_shape, satisfy_amount_of

    c = cfg()
    shaped = apply_shape(c, {"残したい": {"satisfaction_hours": 12, "satisfy_amount": 0.1, "name_ja": "残したい"},
                             "curiosity": {"retired": True}})
    assert set(shaped.desires) == {"miss_companion", "残したい"}
    assert set(c.desires) == {"curiosity", "miss_companion"}  # 元の設定は変えない
    assert satisfy_amount_of(shaped.desires["残したい"]) == 0.1
    assert satisfy_amount_of(shaped.desires["miss_companion"]) == 0.4
    row = step(None, shaped, Inputs(), T0)
    assert row["labels"]["残したい"] == "残したい"
