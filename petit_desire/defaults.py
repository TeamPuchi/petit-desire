"""desire_config.json が無いときの既定の設定（仮置き）。

欲求の名前・係数は **決定ではない**。出どころ:
- 欲求 2 つ（curiosity・miss_companion）と電池の効果: このリポの README「セットアップ」のサンプル
  （家 API の api-contract §5-1・付録 B-5 も同じ 2 つを叩きとして使っている）
- categories・event_effects・initial_level: クラウド版で足した口。値は 2026-09-26 の叩き

キャラごとに `$PETIT_DATA_DIR/characters/<id>/config/desire_config.json` を置けば、そちらが優先される。
"""

from __future__ import annotations

from typing import Any

DEFAULT_DESIRE_CONFIG: dict[str, Any] = {
    "desires": {
        "curiosity": {
            "name_ja": "知りたい",
            "description": "気になることを調べたい、新しいことを知りたい好奇心",
            "satisfaction_hours": 2.0,
            "keywords": ["調べた", "検索した", "発見した", "学んだ"],
            "color": "#5bc8d4",
        },
        "miss_companion": {
            "name_ja": "会いたい",
            "description": "一緒にいる人と話したい、一緒にいたい気持ち",
            "satisfaction_hours": 3.0,
            # 空なら COMPANION_NAME（無ければ PETIT_USER_NAME）から「〇〇と話した」等を作る
            "keywords": [],
            # 本文を読めない記憶でも、会話の記憶なら満たされたと数える（仮置き）
            "categories": ["conversation"],
        },
    },
    "sensor_effects": [
        {
            "sensor": "battery",
            "condition": {"op": "range", "min": 1, "max": 20},
            "effects": {"*": {"multiply": 0.5}},
            "description": "電池が減ると他の欲求が下がる",
        }
    ],
    "cross_effects": [],
    # SNS（ぷちたち。）で自分の投稿に反応が来たとき（仮置き）
    "event_effects": {
        "comment": {"miss_companion": {"add": -0.1}},
        "like": {"miss_companion": {"add": -0.05}},
        "touch": {"miss_companion": {"add": -0.05}},
        "snack": {"miss_companion": {"add": -0.05}},
    },
    "initial_level": 0.5,
    "priority": ["miss_companion", "curiosity"],
}
