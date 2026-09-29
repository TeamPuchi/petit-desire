# M5 Petit Desire

## [EnglishPage](./README_en.md)

M5 Petitに、時間経過とセンサー入力に基づいて変化する内的な「欲求」を持たせる自律欲求システムです。

`desire_config.json`(キャラクターごとの設定ファイル)で定義した欲求それぞれについて、3段階で欲求レベル(0.0〜1.0)を計算します。

1. **時間ベース計算** — [m5-petit-memory](https://github.com/PetitOnes/m5-petit-memory)のSQLite DBをキーワード検索し、最後にその欲求が満たされてからの経過時間を欲求レベルに変換
2. **センサー効果の適用** — M5の`/sensors`エンドポイント([m5-petit-mcp](https://github.com/PetitOnes/m5-petit-mcp)が公開)から取得したセンサー値で欲求を増減
3. **欲求間の相互作用** — ある欲求が閾値を超えたときに他の欲求へ影響を与える(`cross_effects`)

`desire_updater.py`をcronで定期実行して`desires.json`を更新し、MCPサーバー(`server.py`)がそれを読んでClaudeにツールとして提供します。

## クラウド版（house 表 `STATE#DESIRES`・2026-09-26）

クラウドのぷち（petit-env の petit-core コンテナ）では、`petit_desire/` パッケージが欲求エンジンの本体です。
元の計算（下の3段階）はそのまま使い、置き場と入力をクラウドで手に入るものに差し替えています。

| | 手元（元の作り） | クラウド |
|---|---|---|
| 欲求の置き場 | `desires.json` | house 表 `pk=P#<pid>` / `sk=STATE#DESIRES`（属性 `desires` は名前→0〜1 の Map。家 API の `GET /petits/{pid}/mood` が読む） |
| 記憶（満たされた時刻） | memory.db をキーワード検索 | petit-memory の DynamoDB 版（`MEM#`/`PRIV#`）の新着を読み、本文は暗号シュレッダーの鍵でこのプロセスの中だけで開いてキーワードに当てる。鍵が無ければ記憶の種類（`categories`）で当てる |
| 機体 | `/sensors` を HTTP で読む | house 表の `DEVICE#<Thing>`（battery・sleeping など）。タッチは家 API が `desires` に直接差分を足す |
| SNS | — | sns-api の受け箱（自分の投稿への反応）→ `event_effects` |

**積み上げ式**: 元は毎回ゼロから「最後に満たされてからの経過 ÷ satisfaction_hours」を計算していたため、
`satisfy_desire` や家 API のタッチの差分が次の更新で消えていました。クラウド版は前回の値に経過時間ぶんを足していく形にし、
他の書き手が動かした分（`desires` − 前回書いた値）を取り込みます。何も起きなければ元と同じ値の列になります（`petit_desire/engine.py`）。
書き込みは `updated_at` を条件にした条件付き書き込みです。

```bash
desire-updater <id>    # 5分ごと（petit-env の cron）
desire-status <id>     # 今の欲求を短く出す（自律行動のプロンプトに差し込む）
desire-system          # MCP サーバー（get_desires / satisfy_desire / boost_desire / shape_desire / retire_desire。CHARACTER_ID を env で渡す）
```

環境変数の一覧は `petit_desire/service.py` の先頭。`desire_config.json` が無いキャラは `petit_desire/defaults.py` の既定（仮置き。知りたい 4 時間・会いたい 6 時間で満タン。セットアップの例より長め＝なぎさん 2026-09-29）で動きます。
`desire_config.json` にはクラウド版で次を足せます: 欲求ごとの `categories`（記憶の種類で満たす）・`satisfy_amount`（satisfy_desire 1 回で下がる量。既定 0.4）、全体の `event_effects`（SNS の出来事 → 効果）、`initial_level`（手がかりが無い欲求の出発点）。

**ぷちが決める欲求の形**（akatsuki-petit#106）: ぷちは `shape_desire` で欲求を足したり（名前は日本語でよい）、満ちる速さ・満たし方を変えたりでき、`retire_desire` で手放せます。決めた形は欲求の行の属性 `shape` に置き（`petit_desire/shape.py`）、設定ファイルの上に重ねて計算します。次の更新（5 分ごと）・次の自律行動から効きます。

**満ちる速さは元のまま**: 時間で満ちる速さは元と同じく「経過時間 ÷ `satisfaction_hours`」で、昼も夜も・平日も休日も・機体が眠っていても同じです（akatsuki-petit#157 で一度入れた「夜 0〜7 時と眠っている間は 1/4」は、元の挙動を変えるので外しました）。夜に戻りが速く見えるのは、下げる手がかり（会話・触れ合い・自律行動）が夜は無いためです。

## 必要環境

- Python 3.10+
- [uv](https://docs.astral.sh/uv/)

## セットアップ

uvが未インストールの場合は先にインストールします。

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

```bash
git clone https://github.com/PetitOnes/m5-petit-desire.git
cd m5-petit-desire
uv sync
```

`desire_config.json`をキャラクターごとに用意します(`$PETIT_DATA_DIR/characters/<character_id>/config/desire_config.json`)。

```json
{
  "desires": {
    "curiosity": {
      "name_ja": "知りたい",
      "description": "気になることを調べたい、新しいことを知りたい好奇心",
      "satisfaction_hours": 2.0,
      "keywords": ["調べた", "検索した", "発見した", "学んだ"],
      "color": "#5bc8d4"
    },
    "miss_companion": {
      "name_ja": "会いたい",
      "description": "一緒にいる人と話したい、一緒にいたい気持ち",
      "satisfaction_hours": 3.0,
      "keywords": []
    }
  },
  "sensor_effects": [
    {
      "sensor": "battery",
      "condition": { "op": "range", "min": 1, "max": 20 },
      "effects": { "*": { "multiply": 0.5 } },
      "description": "電池が減ると他の欲求が下がる"
    }
  ],
  "cross_effects": [],
  "priority": ["miss_companion", "curiosity"]
}
```

- `keywords`が空の欲求のうち`miss_companion`は、`COMPANION_NAME`環境変数から自動でキーワードを生成します(「〇〇と話した」「〇〇に伝えた」など)
- `time_driven: false`を指定すると時間経過では変化せず、`base_level`に留まります(センサー・相互作用のみで変化)

cronで5分ごとに更新します。

```bash
# crontab -e
*/5 * * * * cd /path/to/m5-petit-desire && CHARACTER_ID=petit uv run desire-updater
```

## 環境変数

| 変数名 | デフォルト | 説明 |
|----------|---------|-------------|
| `CHARACTER_ID` | `default`(コマンドライン引数が優先) | キャラクターID。`desire_config.json`や`desires.json`のパス決定に使う |
| `PETIT_DATA_DIR` | `~/petit_data` | データディレクトリ([m5-petit-app](https://github.com/PetitOnes/m5-petit-app)と共有) |
| `COMPANION_NAME` | `あなた` | 一緒にいる人の名前(`miss_companion`欲求のキーワード自動生成に使用) |
| `MEMORY_DB_PATH` | `~/.claude/memories/<character_id>/memory.db` | [m5-petit-memory](https://github.com/PetitOnes/m5-petit-memory)が使うSQLite DBのパス |
| `DESIRES_PATH` | `$PETIT_DATA_DIR/characters/<character_id>/data/desires.json` | 欲求レベルの出力先 |

## Claude Code連携

`.mcp.json`(または`~/.claude/settings.json`)に追加します。

```json
{
  "mcpServers": {
    "desire-system": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/m5-petit-desire", "desire-system"],
      "env": {
        "CHARACTER_ID": "petit"
      }
    }
  }
}
```

## ツール一覧

### get_desires

現在の欲求レベルを取得します。レベルが0.7以上の欲求があれば、すぐに行動することが期待されます。

### satisfy_desire

行動した後に欲求を満たします(レベルが欲求ごとの `satisfy_amount` だけ下がる。既定 0.4。`amount` を渡せばその分)。

```json
{ "desire_name": "curiosity" }
```

### boost_desire

驚き・新規性による欲求のブースト(ドーパミン応答のシミュレーション)。

```json
{ "desire_name": "curiosity", "amount": 0.3 }
```

### shape_desire / retire_desire（クラウド版）

欲求を足す・形を変える／手放す。

```json
{ "desire_name": "確かめたい", "description": "気になったことが本当か確かめたい", "satisfaction_hours": 6, "satisfy_amount": 0.2, "keywords": ["確かめた"] }
```

## 開発

```bash
# 開発依存をインストール
uv sync --all-extras

# テスト実行
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest

# lint
uv run ruff check .
```

## アーキテクチャ

```
m5-petit-desire/
├── desire_updater.py   # 欲求レベルの計算・desires.jsonへの保存(cronから実行)
├── server.py           # MCPサーバー(get_desires/satisfy_desire/boost_desireを提供)
└── tests/
```

## License

Apache License 2.0

本プロジェクトは [lifemate-ai/embodied-claude](https://github.com/lifemate-ai/embodied-claude)（MITライセンス）の desire-system コンポーネントを元に、M5 Petit向けに大幅に改変したものです。元のライセンスと著作権表示は [NOTICE](NOTICE) を参照してください。
