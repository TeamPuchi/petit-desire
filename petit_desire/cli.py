"""コマンド。

- `desire-updater <id>` … 5 分ごとの更新（petit-env の cron/petit.cron → run-for-each-character.sh desire）
- `desire-status <id>`  … 今の欲求を短く出す（autonomous-action.sh がプロンプトに差し込む）。古ければ更新してから出す
- `desire-act <id> <行動>...` … 行動（使った道具の名前・会話の 1 往復 `chat`）で欲求を少し満たす（actions.py）。
  autonomous-action.sh が自律行動のあとに、家 API が会話の返事のあとに呼ぶ

どちらもログに記憶の本文を出さない（出すのは欲求の値と、入力の件数だけ）。
"""

from __future__ import annotations

import logging
import os
import sys

from .service import DesireService


def _utf8_stdout() -> None:
    # コンテナは LANG=C.UTF-8 だが、手元（Windows の cp932 等）でも欲求のバー（█）を出せるように
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass


def _pid(argv: list[str]) -> str:
    args = [a for a in argv if not a.startswith("-")]
    return args[0] if args else os.environ.get("CHARACTER_ID", "")


def update_main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="[desire-updater] %(levelname)s %(message)s")
    argv = sys.argv[1:] if argv is None else argv
    _utf8_stdout()
    svc = DesireService.from_env(_pid(argv))
    row, inputs = svc.update()
    levels = svc.levels(row)
    print(
        f"[desire-updater] {row.get('updated_at')} pid={svc.pid} dominant={row.get('dominant')} "
        f"desires={levels} config={svc.config_source} "
        f"inputs(memories={len(inputs.memories)} sns={len(inputs.sns)} sensors={sorted(inputs.sensors)})"
    )
    return 0


def status_main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.ERROR, format="[desire-status] %(levelname)s %(message)s")
    argv = sys.argv[1:] if argv is None else argv
    _utf8_stdout()
    svc = DesireService.from_env(_pid(argv))
    row = svc.current(refresh_if_stale="--no-refresh" not in argv)
    print(svc.format(row, compact="--compact" in argv))
    return 0


def act_main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="[desire-act] %(levelname)s %(message)s")
    argv = sys.argv[1:] if argv is None else argv
    _utf8_stdout()
    if not argv:
        print("[desire-act] 使い方: desire-act <id> <行動>...", file=sys.stderr)
        return 2
    svc = DesireService.from_env(argv[0])
    row, hits = svc.act(argv[1:])
    if hits:
        levels = svc.levels(row)
        print("[desire-act] " + " ".join(f"{n}={levels.get(n, 0):.3f}({','.join(g)})" for n, g in hits.items()))
    return 0


def _run(fn) -> None:
    sys.exit(fn())


def update() -> None:
    _run(update_main)


def status() -> None:
    _run(status_main)


def act() -> None:
    _run(act_main)
