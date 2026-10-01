"""desire_config.json が無いときの既定の設定（仮置き）。

欲求の名前・係数は **決定ではない**。出どころ:
- 欲求 2 つ（curiosity・miss_companion）と電池の効果: このリポの README「セットアップ」のサンプル
  （家 API の api-contract §5-1・付録 B-5 も同じ 2 つを叩きとして使っている）
- categories・event_effects・initial_level: クラウド版で足した口。値は 2026-09-26 の叩き
- satisfaction_hours（満タンまでの時間）: README の例は 知りたい 2 時間・会いたい 3 時間だったが、なぎさん 2026-09-29
  「夜の欲求は長くしておいて」で 知りたい 4 時間・会いたい 6 時間に（akatsuki-petit#157）。計算の仕組みは元のまま。
  embodied-claude の create_character.py の雛形は 何か調べたい 6 時間・会いたい 4 時間
- sleepy（眠い）: akatsuki-petit#186。家（PetitOnes）のぷちは、眠いときに自分で体をスリープにして、記憶に
  「スリープした」と残す。説明とキーワード（スリープ・眠った・寝た）は家の desire_config に合わせた（#186 の記述）。
  **satisfaction_hours の 30 は仮置き**（家の値は公開リポジトリに無い。ありさんに確認中）。「最後に眠ってから
  30 時間で満タン」＝眠ってから 21 時間ほどで 0.7 を超えるので、夜に眠ると次の日の夜にまた眠くなる。
  クラウドでは house の body_sleep（petit-api）で体を眠らせる

キャラごとに `$PETIT_DATA_DIR/characters/<id>/config/desire_config.json` を置けば、そちらが優先される。
"""

from __future__ import annotations

from typing import Any

DEFAULT_DESIRE_CONFIG: dict[str, Any] = {
    "desires": {
        "curiosity": {
            "name_ja": "知りたい",
            "description": "気になることを調べたい、新しいことを知りたい好奇心",
            "satisfaction_hours": 4.0,
            "keywords": ["調べた", "検索した", "発見した", "学んだ"],
            "color": "#5bc8d4",
        },
        "miss_companion": {
            "name_ja": "会いたい",
            "description": "一緒にいる人と話したい、一緒にいたい気持ち",
            "satisfaction_hours": 6.0,
            # 空なら COMPANION_NAME（無ければ PETIT_USER_NAME）から「〇〇と話した」等を作る
            "keywords": [],
            # 本文を読めない記憶でも、会話の記憶なら満たされたと数える（仮置き）
            "categories": ["conversation"],
        },
        "sleepy": {
            "name_ja": "眠い",
            "description": "体（机の上の機体）をスリープさせて眠りたい",
            "satisfaction_hours": 30.0,
            "keywords": ["スリープ", "眠った", "寝た"],
            "color": "#8e7cc3",
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
    "priority": ["miss_companion", "curiosity", "sleepy"],
}
