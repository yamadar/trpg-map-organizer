"""画像フォルダを走査し、未解析ファイルを Gemini に投げて DB を構築/更新する.

Usage:
    python -m scripts.build_db                # 増分解析
    python -m scripts.build_db --rebuild      # 全件再解析
    python -m scripts.build_db --limit 5      # 最大 N 件だけ処理 (テスト用)
    python -m scripts.build_db --dry-run      # API は呼ばず、対象一覧のみ表示
    python -m scripts.build_db --config path  # 設定ファイル指定
"""
from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from pathlib import Path

# プロジェクトルートを sys.path に通す（python script.py での直接実行に対応）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import db
from src.analyzer import GeminiAnalyzer  # noqa: E402
from src.config import load_config  # noqa: E402
from src.scanner import iter_images, needs_reanalyze, quick_hash  # noqa: E402

logger = logging.getLogger("build_db")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="config.yaml のパス (省略時は既定パスを探索)",
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="既に解析済みでも全て再解析する",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="処理するファイル数の上限 (テスト用)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="API を呼ばず処理対象だけ表示",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help="並列ワーカ数 (省略時は config.api_workers を使用)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="詳細ログを出力",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg = load_config(args.config)
    logger.info("対象フォルダ: %s", cfg.target_folder)
    logger.info("DB: %s", cfg.database_path)
    logger.info("モデル: %s", cfg.gemini_model)

    if not cfg.target_folder.exists():
        logger.error("ターゲットフォルダが存在しません: %s", cfg.target_folder)
        return 1

    db.init_db(cfg.database_path)

    # 解析対象を収集
    pending: list[tuple] = []
    with db.connect(cfg.database_path) as conn:
        for found in iter_images(cfg.target_folder, cfg.image_extensions):
            existing = db.get_map_by_path(conn, found.path_str)
            if not args.rebuild and existing is not None:
                if not needs_reanalyze(
                    found,
                    existing_size=existing.file_size,
                    existing_mtime=existing.file_mtime,
                ):
                    logger.debug("skip (unchanged): %s", found.path_str)
                    continue
            pending.append(found)
            if args.limit and len(pending) >= args.limit:
                break

    logger.info("解析対象: %d 件", len(pending))
    if not pending:
        logger.info("更新は不要でした。")
        return 0

    if args.dry_run:
        for f in pending:
            logger.info("[dry-run] %s", f.path_str)
        return 0

    analyzer = GeminiAnalyzer(
        api_key=cfg.gemini_api_key,
        model=cfg.gemini_model,
        max_attempts=cfg.api_retries,
    )

    workers = max(1, args.workers if args.workers is not None else cfg.api_workers)
    logger.info("ワーカ数: %d", workers)

    if workers == 1:
        return _run_sequential(cfg, analyzer, pending)
    return _run_parallel(cfg, analyzer, pending, workers)


def _persist_result(
    conn,
    db_lock: threading.Lock,
    found,
    result,
) -> None:
    try:
        hash_value = quick_hash(found.path)
    except OSError:
        hash_value = None

    with db_lock:
        db.upsert_map(
            conn,
            file_path=found.path_str,
            file_name=found.name,
            file_size=found.size,
            file_mtime=found.mtime,
            file_hash=hash_value,
            terrain_tags=result.terrain_tags,
            mood_tags=result.mood_tags,
            location_tags=result.location_tags,
            description=result.description,
        )


def _run_sequential(cfg, analyzer: GeminiAnalyzer, pending: list) -> int:
    success = 0
    failed = 0
    db_lock = threading.Lock()  # 単一スレッドだが API を統一する
    with db.connect(cfg.database_path) as conn:
        for i, found in enumerate(pending, start=1):
            logger.info("[%d/%d] %s", i, len(pending), found.path_str)
            try:
                result = analyzer.analyze(found.path)
            except Exception as e:  # noqa: BLE001
                failed += 1
                logger.error("  解析失敗: %s", e)
                continue

            _persist_result(conn, db_lock, found, result)
            success += 1
            logger.info(
                "  地形=%s / 雰囲気=%s / 場所=%s",
                result.terrain_tags,
                result.mood_tags,
                result.location_tags,
            )
            if cfg.api_min_interval_sec > 0 and i < len(pending):
                time.sleep(cfg.api_min_interval_sec)

    logger.info("完了: 成功 %d / 失敗 %d", success, failed)
    return 0 if failed == 0 else 2


def _run_parallel(cfg, analyzer: GeminiAnalyzer, pending: list, workers: int) -> int:
    success = 0
    failed = 0
    db_lock = threading.Lock()
    progress_lock = threading.Lock()
    done = 0
    total = len(pending)

    # check_same_thread=False を許容するため SQLite 接続をここで開く
    import sqlite3
    conn = sqlite3.connect(cfg.database_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            future_to_found: dict[Future, object] = {
                pool.submit(analyzer.analyze, f.path): f for f in pending
            }
            for fut in as_completed(future_to_found):
                found = future_to_found[fut]
                with progress_lock:
                    done += 1
                    idx = done
                try:
                    result = fut.result()
                except Exception as e:  # noqa: BLE001
                    failed += 1
                    logger.error("[%d/%d] %s -> 解析失敗: %s", idx, total, found.path_str, e)
                    continue

                _persist_result(conn, db_lock, found, result)
                success += 1
                logger.info(
                    "[%d/%d] %s -> 地形=%s / 雰囲気=%s / 場所=%s",
                    idx,
                    total,
                    found.name,
                    result.terrain_tags,
                    result.mood_tags,
                    result.location_tags,
                )
    finally:
        conn.close()

    logger.info("完了: 成功 %d / 失敗 %d", success, failed)
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
