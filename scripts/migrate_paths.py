"""DB に保存された絶対パスを target_folder からの相対パスに書き換える.

GitHub Pages 等の静的配信先で再利用できるようパスを portable にする。
1 回だけ実行すれば良い (実行済みのレコードはスキップ)。

Usage:
    python -m scripts.migrate_paths --dry-run
    python -m scripts.migrate_paths
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import db  # noqa: E402
from src.config import load_config  # noqa: E402

logger = logging.getLogger("migrate_paths")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
    )

    cfg = load_config(args.config)
    if not cfg.database_path.exists():
        logger.error("DB が見つかりません: %s", cfg.database_path)
        return 1

    target = cfg.target_folder.resolve()
    logger.info("target_folder: %s", target)

    converted = 0
    skipped_relative = 0
    out_of_root = 0
    total = 0

    with db.connect(cfg.database_path) as conn:
        maps = db.list_maps(conn)
        total = len(maps)
        for m in maps:
            p = Path(m.file_path)
            if not p.is_absolute():
                skipped_relative += 1
                continue
            try:
                rel = p.resolve().relative_to(target).as_posix()
            except ValueError:
                out_of_root += 1
                logger.warning("ターゲット外のパス (スキップ): %s", m.file_path)
                continue

            logger.debug("%s -> %s", m.file_path, rel)
            if not args.dry_run:
                db.update_file_path(
                    conn,
                    map_id=m.id,
                    file_path=rel,
                    file_name=m.file_name,
                )
            converted += 1

    action = "変換予定" if args.dry_run else "変換完了"
    logger.info(
        "%s: %d 件 / 既に相対: %d / 範囲外: %d / 合計 %d",
        action,
        converted,
        skipped_relative,
        out_of_root,
        total,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
