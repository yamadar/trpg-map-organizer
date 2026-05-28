"""既存 DB レコードの theme_tags を Gemini で再解析して埋める.

新しく theme_tags カテゴリを追加した直後、既存レコードの theme_tags が空のものに
対して画像を再解析しテーマを付与する。terrain/mood/location/description は触らない。

Usage:
    python -m scripts.analyze_themes              # theme_tags が空のものだけ
    python -m scripts.analyze_themes --rebuild    # 全件再解析
    python -m scripts.analyze_themes --workers 10
    python -m scripts.analyze_themes --limit 5    # テスト用
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import threading
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import db  # noqa: E402
from src.analyzer import GeminiAnalyzer  # noqa: E402
from src.config import load_config  # noqa: E402
from src.normalize import TagNormalizer  # noqa: E402

logger = logging.getLogger("analyze_themes")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="既に theme_tags が入っているレコードも再解析する",
    )
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument("--limit", type=int, default=None, help="処理件数の上限 (テスト用)")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg = load_config(args.config)
    if not cfg.database_path.exists():
        logger.error("DB が見つかりません: %s", cfg.database_path)
        return 1

    db.init_db(cfg.database_path)
    target = cfg.target_folder.resolve()
    workers = max(1, args.workers if args.workers is not None else cfg.api_workers)

    with db.connect(cfg.database_path) as conn:
        all_maps = db.list_maps(conn)

    candidates: list[db.MapRecord] = []
    for m in all_maps:
        if not args.rebuild and m.theme_tags:
            continue
        candidates.append(m)
        # --limit 0 を「処理 0 件」として正しく解釈するため is not None で判定
        if args.limit is not None and len(candidates) >= args.limit:
            break

    logger.info(
        "対象: %d 件 (全 %d 件中) / ワーカ: %d", len(candidates), len(all_maps), workers
    )
    if not candidates:
        logger.info("対象なし。")
        return 0

    analyzer = GeminiAnalyzer(
        api_key=cfg.gemini_api_key,
        model=cfg.gemini_model,
        max_attempts=cfg.api_retries,
    )
    normalizer = TagNormalizer.load()
    logger.info("alias 件数: %d", len(normalizer.aliases))

    success = 0
    failed = 0
    db_lock = threading.Lock()
    conn = sqlite3.connect(cfg.database_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures: dict[Future, db.MapRecord] = {}
            for m in candidates:
                abs_path = db.resolve_path(m, target)
                if not abs_path.exists():
                    failed += 1
                    logger.error("元ファイルなし、スキップ: %s", abs_path)
                    continue
                futures[pool.submit(analyzer.analyze_themes, abs_path)] = m

            done = 0
            total = len(futures)
            for fut in as_completed(futures):
                m = futures[fut]
                done += 1
                try:
                    themes = fut.result()
                except Exception as e:  # noqa: BLE001
                    failed += 1
                    logger.error("[%d/%d] %s -> 解析失敗: %s", done, total, m.file_name, e)
                    continue

                normalized = normalizer.normalize_tags(themes)
                with db_lock:
                    db.update_theme_tags(conn, map_id=m.id, theme_tags=normalized)
                success += 1
                logger.info("[%d/%d] %s -> テーマ=%s", done, total, m.file_name, normalized)
    finally:
        conn.close()

    logger.info("完了: 成功 %d / 失敗 %d", success, failed)
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
