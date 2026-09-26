"""互換のための入口。本体は petit_desire/server.py（クラウド版・house 表 STATE#DESIRES）。

`uv run python server.py` で起動していた既存の MCP 設定（sample-character の autonomous-mcp.json 等）が
そのまま動くように残してある。新しく書くなら `desire-system`（console script）を使う。
"""

from petit_desire.server import main

if __name__ == "__main__":
    main()
