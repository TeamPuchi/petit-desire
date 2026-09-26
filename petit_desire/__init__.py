"""petit-desire のクラウド版（house 表 `STATE#DESIRES` を読み書きする欲求エンジン）。

- engine.py  … 1 回ぶんの更新（純粋関数。入力をそろえて渡すと、次の状態を返す）
- store.py   … 状態の置き場（house 表の `STATE#DESIRES` の 1 行 / 手元の desires.json）
- sources.py … 入力（記憶・機体の状態・SNS の受け箱）。どれも無ければ空で返す
- service.py … 上をつないだ窓口（更新・満たす・強める・表示）
- server.py  … MCP サーバー（get_desires / satisfy_desire / boost_desire）
- cli.py     … cron 用（desire-updater）と自律行動のプロンプト用（desire-status）
"""
