"""tag_aliases.yaml を既存 DB レコードに適用する.

API は呼ばない。タグの再正規化のみを行う。

Usage:
    python -m scripts.normalize_db --dry-run    # 影響範囲を確認
    python -m scripts.normalize_db              # 実際に更新
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import db  # noqa: E402
from src.config import load_config  # noqa: E402
from src.normalize import TagNormalizer  # noqa: E402

logger = logging.getLogger("normalize_db")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--aliases", type=Path, default=None, help="tag_aliases.yaml のパス")
    parser.add_argument("--dry-run", action="store_true", help="DB は更新せず差分のみ表示")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args()


def _diff_summary(name: str, before: list[str], after: list[str]) -> str | None:
    if before == after:
        return None
    return f"    {name}: {before} -> {after}"


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
    )

    cfg = load_config(args.config)
    normalizer = TagNormalizer.load(args.aliases)
    logger.info("alias 件数: %d", len(normalizer.aliases))
    logger.info("DB: %s", cfg.database_path)

    if not cfg.database_path.exists():
        logger.error("DB が見つかりません: %s", cfg.database_path)
        return 1

    changed = 0
    total = 0
    with db.connect(cfg.database_path) as conn:
        maps = db.list_maps(conn)
        total = len(maps)
        for m in maps:
            new_terrain = normalizer.normalize_tags(m.terrain_tags)
            new_mood = normalizer.normalize_tags(m.mood_tags)
            new_location = normalizer.normalize_tags(m.location_tags)
            new_theme = normalizer.normalize_tags(m.theme_tags)

            diffs = [
                _diff_summary("地形", m.terrain_tags, new_terrain),
                _diff_summary("雰囲気", m.mood_tags, new_mood),
                _diff_summary("場所", m.location_tags, new_location),
                _diff_summary("テーマ", m.theme_tags, new_theme),
            ]
            diffs = [d for d in diffs if d]
            if not diffs:
                continue

            changed += 1
            prefix = "[dry-run] " if args.dry_run else ""
            logger.info("%s%s", prefix, m.file_name)
            for d in diffs:
                logger.info("%s", d)

            if not args.dry_run:
                db.update_tags(
                    conn,
                    map_id=m.id,
                    terrain_tags=new_terrain,
                    mood_tags=new_mood,
                    location_tags=new_location,
                    theme_tags=new_theme,
                )

    action = "差分検出" if args.dry_run else "更新"
    logger.info("完了: %s %d / %d 件", action, changed, total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
