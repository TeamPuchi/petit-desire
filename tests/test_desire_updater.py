"""Tests for desire_updater v2 (config-driven)."""

import json
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from desire_updater import (
    CrossEffect,
    DesireConfig,
    DesireState,
    DesireSystemConfig,
    SensorEffect,
    apply_effects,
    calculate_desire_level,
    compute_desires,
    evaluate_sensor_condition,
    get_latest_memory_timestamp,
    load_desire_config,
    load_desires,
    save_desires,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(
    desires: dict[str, DesireConfig] | None = None,
    sensor_effects: list[SensorEffect] | None = None,
    cross_effects: list[CrossEffect] | None = None,
    priority: list[str] | None = None,
) -> DesireSystemConfig:
    """テスト用のDesireSystemConfigを生成する。"""
    if desires is None:
        desires = {
            "curiosity": DesireConfig(
                name_ja="知りたい", description="", satisfaction_hours=6.0,
                keywords=["調べた"], color="#aaa",
            ),
            "rest": DesireConfig(
                name_ja="休みたい", description="", satisfaction_hours=4.0,
                keywords=["休んだ"], color="#bbb",
            ),
        }
    return DesireSystemConfig(
        desires=desires,
        sensor_effects=sensor_effects or [],
        cross_effects=cross_effects or [],
        priority=priority or list(desires.keys()),
    )


def _make_memory_db(entries: list[tuple[str, str]]) -> Path:
    """テスト用のSQLite memory DBを作成して返す。entries = [(content, timestamp), ...]"""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_path = Path(tmp.name)
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE memories (id TEXT, content TEXT, timestamp TEXT, "
        "category TEXT, emotion TEXT, importance INTEGER)"
    )
    for i, (content, ts) in enumerate(entries):
        conn.execute(
            "INSERT INTO memories (id, content, timestamp) VALUES (?, ?, ?)",
            (f"m{i}", content, ts),
        )
    conn.commit()
    conn.close()
    return db_path


# ---------------------------------------------------------------------------
# TestCalculateDesireLevel
# ---------------------------------------------------------------------------

class TestCalculateDesireLevel:
    def test_no_prior_satisfaction_returns_max(self):
        assert calculate_desire_level(None, 1.0) == 1.0

    def test_just_satisfied_returns_zero(self):
        now = datetime(2026, 2, 18, 12, 0, 0, tzinfo=timezone.utc)
        assert calculate_desire_level(now, 1.0, now) == 0.0

    def test_half_elapsed_returns_half(self):
        now = datetime(2026, 2, 18, 12, 0, 0, tzinfo=timezone.utc)
        last = now - timedelta(hours=0.5)
        assert calculate_desire_level(last, 1.0, now) == pytest.approx(0.5, abs=0.01)

    def test_full_elapsed_returns_one(self):
        now = datetime(2026, 2, 18, 12, 0, 0, tzinfo=timezone.utc)
        last = now - timedelta(hours=1.0)
        assert calculate_desire_level(last, 1.0, now) == 1.0

    def test_over_elapsed_capped_at_one(self):
        now = datetime(2026, 2, 18, 12, 0, 0, tzinfo=timezone.utc)
        last = now - timedelta(hours=5.0)
        assert calculate_desire_level(last, 1.0, now) == 1.0

    def test_custom_threshold(self):
        now = datetime(2026, 2, 18, 12, 0, 0, tzinfo=timezone.utc)
        last = now - timedelta(hours=1.0)
        assert calculate_desire_level(last, 2.0, now) == pytest.approx(0.5, abs=0.01)

    def test_naive_datetime_handled(self):
        now = datetime(2026, 2, 18, 12, 0, 0, tzinfo=timezone.utc)
        last = datetime(2026, 2, 18, 11, 0, 0)  # naive
        result = calculate_desire_level(last, 1.0, now)
        assert result == 1.0


# ---------------------------------------------------------------------------
# TestGetLatestMemoryTimestamp (SQLite版)
# ---------------------------------------------------------------------------

class TestGetLatestMemoryTimestamp:
    def test_returns_none_when_no_match(self):
        db = _make_memory_db([
            ("今日は晴れです", "2026-02-18T10:00:00"),
            ("部屋が暑い", "2026-02-18T11:00:00"),
        ])
        result = get_latest_memory_timestamp(db, ["外を見た", "空を見た"])
        assert result is None
        db.unlink()

    def test_returns_latest_matching_timestamp(self):
        db = _make_memory_db([
            ("外を見た、空が青い", "2026-02-18T08:00:00"),
            ("今日は雨", "2026-02-18T09:00:00"),
            ("空を見た、曇ってる", "2026-02-18T10:00:00"),
        ])
        result = get_latest_memory_timestamp(db, ["外を見た", "空を見た"])
        assert result is not None
        assert result.hour == 10
        db.unlink()

    def test_returns_none_for_missing_db(self):
        result = get_latest_memory_timestamp(Path("/nonexistent.db"), ["test"])
        assert result is None

    def test_returns_none_for_empty_keywords(self):
        db = _make_memory_db([("hello", "2026-02-18T10:00:00")])
        result = get_latest_memory_timestamp(db, [])
        assert result is None
        db.unlink()


# ---------------------------------------------------------------------------
# TestEvaluateSensorCondition
# ---------------------------------------------------------------------------

class TestEvaluateSensorCondition:
    def test_less_than(self):
        assert evaluate_sensor_condition({"op": "<", "value": 20}, 15) is True
        assert evaluate_sensor_condition({"op": "<", "value": 20}, 25) is False

    def test_greater_than(self):
        assert evaluate_sensor_condition({"op": ">", "value": 500}, 600) is True
        assert evaluate_sensor_condition({"op": ">", "value": 500}, 400) is False

    def test_range(self):
        cond = {"op": "range", "min": 1, "max": 20}
        assert evaluate_sensor_condition(cond, 10) is True
        assert evaluate_sensor_condition(cond, 0) is False  # 充電中
        assert evaluate_sensor_condition(cond, 25) is False
        assert evaluate_sensor_condition(cond, 1) is True  # 境界
        assert evaluate_sensor_condition(cond, 20) is True  # 境界

    def test_none_value_returns_false(self):
        assert evaluate_sensor_condition({"op": "<", "value": 20}, None) is False

    def test_recent_true(self):
        import time
        now_ms = time.time() * 1000  # 今のタイムスタンプ（ミリ秒）
        cond = {"op": "recent", "within_seconds": 60}
        assert evaluate_sensor_condition(cond, now_ms) is True

    def test_recent_false_old(self):
        import time
        old_ms = (time.time() - 120) * 1000  # 2分前
        cond = {"op": "recent", "within_seconds": 60}
        assert evaluate_sensor_condition(cond, old_ms) is False

    def test_recent_zero_returns_false(self):
        cond = {"op": "recent", "within_seconds": 60}
        assert evaluate_sensor_condition(cond, 0) is False


# ---------------------------------------------------------------------------
# TestApplyEffects
# ---------------------------------------------------------------------------

class TestApplyEffects:
    def test_add(self):
        desires = {"a": 0.5, "b": 0.3}
        apply_effects(desires, {"a": {"add": 0.2}})
        assert desires["a"] == pytest.approx(0.7)
        assert desires["b"] == pytest.approx(0.3)

    def test_multiply(self):
        desires = {"a": 0.8, "b": 0.6}
        apply_effects(desires, {"a": {"multiply": 0.5}})
        assert desires["a"] == pytest.approx(0.4)

    def test_set(self):
        desires = {"a": 0.5}
        apply_effects(desires, {"a": {"set": 1.0}})
        assert desires["a"] == pytest.approx(1.0)

    def test_wildcard(self):
        desires = {"a": 0.8, "b": 0.6, "c": 0.4}
        apply_effects(desires, {"*": {"multiply": 0.5}})
        assert desires["a"] == pytest.approx(0.4)
        assert desires["b"] == pytest.approx(0.3)
        assert desires["c"] == pytest.approx(0.2)

    def test_wildcard_with_specific_override(self):
        desires = {"a": 0.8, "b": 0.6}
        apply_effects(desires, {"a": {"add": 0.1}, "*": {"multiply": 0.5}})
        # a uses specific effect (add 0.1), b uses wildcard (multiply 0.5)
        assert desires["a"] == pytest.approx(0.9)
        assert desires["b"] == pytest.approx(0.3)


# ---------------------------------------------------------------------------
# TestComputeDesires
# ---------------------------------------------------------------------------

class TestComputeDesires:
    def test_all_desires_from_config(self):
        config = _make_config()
        db = _make_memory_db([])
        now = datetime(2026, 2, 18, 12, 0, 0)
        state = compute_desires(db, config, now=now)
        assert set(state.desires.keys()) == {"curiosity", "rest"}
        db.unlink()

    def test_all_max_when_no_memories(self):
        config = _make_config()
        db = _make_memory_db([])
        now = datetime(2026, 2, 18, 12, 0, 0)
        state = compute_desires(db, config, now=now)
        for level in state.desires.values():
            assert level == 1.0
        db.unlink()

    def test_time_driven_false_stays_at_base_level(self):
        desires = {
            "rare": DesireConfig(
                name_ja="レア", description="", satisfaction_hours=168.0,
                keywords=["散歩"], color="#ccc", base_level=0.0, time_driven=False,
            ),
        }
        config = _make_config(desires=desires)
        db = _make_memory_db([])
        now = datetime(2026, 2, 18, 12, 0, 0)
        state = compute_desires(db, config, now=now)
        assert state.desires["rare"] == 0.0
        db.unlink()

    def test_sensor_effects_applied(self):
        config = _make_config(
            sensor_effects=[
                SensorEffect(
                    sensor="battery",
                    condition={"op": "range", "min": 1, "max": 20},
                    effects={"rest": {"add": 0.4}},
                ),
            ],
        )
        db = _make_memory_db([])
        now = datetime(2026, 2, 18, 12, 0, 0)
        state = compute_desires(db, config, sensor_data={"battery": 15}, now=now)
        # rest は 1.0（no memory）+ 0.4 → clamped to 1.0
        assert state.desires["rest"] == 1.0
        db.unlink()

    def test_sensor_battery_zero_not_triggered(self):
        """バッテリー0（充電中）はrange条件に引っかからない。"""
        config = _make_config(
            sensor_effects=[
                SensorEffect(
                    sensor="battery",
                    condition={"op": "range", "min": 1, "max": 20},
                    effects={"*": {"multiply": 0.5}},
                ),
            ],
        )
        db = _make_memory_db([])
        now = datetime(2026, 2, 18, 12, 0, 0)
        state = compute_desires(db, config, sensor_data={"battery": 0}, now=now)
        # Should NOT be multiplied by 0.5 since battery=0 is charging
        assert state.desires["curiosity"] == 1.0
        assert state.desires["rest"] == 1.0
        db.unlink()

    def test_cross_effects_applied(self):
        config = _make_config(
            cross_effects=[
                CrossEffect(
                    when={"desire": "rest", "op": ">", "value": 0.8},
                    effects={"curiosity": {"multiply": 0.5}},
                ),
            ],
        )
        db = _make_memory_db([])
        now = datetime(2026, 2, 18, 12, 0, 0)
        state = compute_desires(db, config, now=now)
        # rest = 1.0 > 0.8 → curiosity *= 0.5
        assert state.desires["curiosity"] == 0.5
        assert state.desires["rest"] == 1.0
        db.unlink()

    def test_dominant_uses_priority(self):
        config = _make_config(priority=["rest", "curiosity"])
        db = _make_memory_db([])
        now = datetime(2026, 2, 18, 12, 0, 0)
        state = compute_desires(db, config, now=now)
        # Both 1.0, priority says "rest" first
        assert state.dominant == "rest"
        db.unlink()

    def test_labels_and_colors_populated(self):
        config = _make_config()
        db = _make_memory_db([])
        now = datetime(2026, 2, 18, 12, 0, 0)
        state = compute_desires(db, config, now=now)
        assert state.labels["curiosity"] == "知りたい"
        assert state.colors["curiosity"] == "#aaa"
        db.unlink()

    def test_sensor_snapshot_included(self):
        config = _make_config()
        db = _make_memory_db([])
        now = datetime(2026, 2, 18, 12, 0, 0)
        state = compute_desires(db, config, sensor_data={"battery": 80}, now=now)
        assert state.sensor_snapshot == {"battery": 80}
        db.unlink()


# ---------------------------------------------------------------------------
# TestLoadDesireConfig
# ---------------------------------------------------------------------------

class TestLoadDesireConfig:
    def test_loads_valid_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            char_dir = Path(tmpdir) / "characters" / "testchar" / "config"
            char_dir.mkdir(parents=True)
            config_data = {
                "desires": {
                    "test_desire": {
                        "name_ja": "テスト欲求",
                        "satisfaction_hours": 5.0,
                        "keywords": ["test"],
                        "color": "#fff",
                    },
                },
                "sensor_effects": [],
                "cross_effects": [],
                "priority": ["test_desire"],
            }
            (char_dir / "desire_config.json").write_text(
                json.dumps(config_data), encoding="utf-8"
            )
            result = load_desire_config("testchar", data_dir=Path(tmpdir))
            assert "test_desire" in result.desires
            assert result.desires["test_desire"].name_ja == "テスト欲求"
            assert result.desires["test_desire"].satisfaction_hours == 5.0

    def test_missing_config_raises(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with pytest.raises(FileNotFoundError):
                load_desire_config("nonexistent", data_dir=Path(tmpdir))

    def test_time_driven_default_true(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            char_dir = Path(tmpdir) / "characters" / "testchar" / "config"
            char_dir.mkdir(parents=True)
            config_data = {
                "desires": {
                    "d1": {
                        "name_ja": "test",
                        "satisfaction_hours": 1.0,
                        "keywords": [],
                    },
                },
            }
            (char_dir / "desire_config.json").write_text(
                json.dumps(config_data), encoding="utf-8"
            )
            result = load_desire_config("testchar", data_dir=Path(tmpdir))
            assert result.desires["d1"].time_driven is True

    def test_time_driven_false(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            char_dir = Path(tmpdir) / "characters" / "testchar" / "config"
            char_dir.mkdir(parents=True)
            config_data = {
                "desires": {
                    "rare": {
                        "name_ja": "レア",
                        "satisfaction_hours": 168.0,
                        "keywords": [],
                        "time_driven": False,
                        "base_level": 0.0,
                    },
                },
            }
            (char_dir / "desire_config.json").write_text(
                json.dumps(config_data), encoding="utf-8"
            )
            result = load_desire_config("testchar", data_dir=Path(tmpdir))
            assert result.desires["rare"].time_driven is False
            assert result.desires["rare"].base_level == 0.0


# ---------------------------------------------------------------------------
# TestSaveAndLoadDesires
# ---------------------------------------------------------------------------

class TestSaveAndLoadDesires:
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "desires.json"
            state = DesireState(
                updated_at="2026-02-18T12:00:00+00:00",
                desires={"curiosity": 0.8, "rest": 0.5},
                dominant="curiosity",
                labels={"curiosity": "知りたい", "rest": "休みたい"},
                colors={"curiosity": "#aaa", "rest": "#bbb"},
            )
            save_desires(state, path)
            loaded = load_desires(path)
            assert loaded is not None
            assert loaded.dominant == "curiosity"
            assert loaded.desires["curiosity"] == pytest.approx(0.8)
            assert loaded.labels["curiosity"] == "知りたい"
            assert loaded.colors["curiosity"] == "#aaa"

    def test_load_missing_file_returns_none(self):
        result = load_desires(Path("/nonexistent/path/desires.json"))
        assert result is None

    def test_save_creates_parent_dirs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "nested" / "dir" / "desires.json"
            state = DesireState(
                updated_at="2026-02-18T12:00:00",
                desires={"curiosity": 1.0},
                dominant="curiosity",
            )
            save_desires(state, path)
            assert path.exists()

    def test_saved_json_includes_labels_and_colors(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "desires.json"
            state = DesireState(
                updated_at="2026-02-18T12:00:00",
                desires={"curiosity": 0.6},
                dominant="curiosity",
                labels={"curiosity": "知りたい"},
                colors={"curiosity": "#aaa"},
            )
            save_desires(state, path)
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            assert data["labels"]["curiosity"] == "知りたい"
            assert data["colors"]["curiosity"] == "#aaa"

    def test_to_dict_includes_sensor_snapshot(self):
        state = DesireState(
            updated_at="2026-02-18T12:00:00",
            desires={"curiosity": 0.5},
            dominant="curiosity",
            sensor_snapshot={"battery": 80, "ambient": 120},
        )
        d = state.to_dict()
        assert d["sensor_snapshot"] == {"battery": 80, "ambient": 120}

    def test_to_dict_omits_empty_sensor_snapshot(self):
        state = DesireState(
            updated_at="2026-02-18T12:00:00",
            desires={"curiosity": 0.5},
            dominant="curiosity",
        )
        d = state.to_dict()
        assert "sensor_snapshot" not in d
