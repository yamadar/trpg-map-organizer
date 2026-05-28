"""maps/ と docs/originals/ 配下の PNG/JPG を WebP に一括変換し、DB も同期する.

特徴:
- 既定: Quality=85, Effort=4 (libwebp の method パラメータ)
- 並列変換 (config.api_workers と同じ既定値)
- maps/ 内のファイルを変換した場合は DB の file_path / file_name / file_size
  / file_mtime / file_hash を更新する
- docs/originals/ も同様に変換 (delete original PNG/JPG)
- before_process/ は触らない (まだ解析されていない可能性があるため)
- 既に .webp のものはスキップ

Usage:
    python -m scripts.convert_to_webp                       # 既定設定で実行
    python -m scripts.convert_to_webp --quality 90 --effort 6
    python -m scripts.convert_to_webp --workers 8
    python -m scripts.convert_to_webp --dry-run             # 変換せず計画のみ表示
    python -m scripts.convert_to_webp --only maps           # maps/ のみ
    python -m scripts.convert_to_webp --only docs           # docs/originals/ のみ
"""
from __future__ import annotations

import argparse
import logging
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import db  # noqa: E402
from src.config import load_config  # noqa: E402
from src.scanner import quick_hash  # noqa: E402

logger = logging.getLogger("convert_to_webp")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DOCS_ORIGINALS = PROJECT_ROOT / "docs" / "originals"

# WebP 変換対象とする拡張子 (.webp 自体は除外)
CONVERTIBLE_EXTS = {".png", ".jpg", ".jpeg"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--quality", type=int, default=85, help="WebP quality (0-100)")
    parser.add_argument(
        "--effort",
        type=int,
        default=4,
        help="WebP method/effort (0-6, 高いほど圧縮率良いが遅い)",
    )
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--only",
        choices=["maps", "docs", "both"],
        default="both",
        help="対象ディレクトリ",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args()


def _convert_one(
    src: Path, quality: int, effort: int, dry_run: bool
) -> tuple[Path, int, int] | None:
    """1 ファイルを WebP に変換し、元ファイルを削除する.

    Returns:
        (new_path, src_bytes, dst_bytes) または None (変換不要/失敗)
    """
    if src.suffix.lower() == ".webp" or src.suffix.lower() not in CONVERTIBLE_EXTS:
        return None
    dst = src.with_suffix(".webp")
    if dst.exists() and not dry_run:
        # 既に webp 版があるなら元 PNG を削除して終了 (再変換しない)
        try:
            src_size = src.stat().st_size
        except OSError:
            src_size = 0
        dst_size = dst.stat().st_size
        src.unlink()
        return (dst, src_size, dst_size)

    try:
        src_size = src.stat().st_size
    except OSError:
        src_size = 0

    if dry_run:
        return (dst, src_size, 0)

    try:
        with Image.open(src) as img:
            img.save(dst, format="WEBP", quality=quality, method=effort)
    except Exception as e:  # noqa: BLE001
        logger.error("変換失敗 %s: %s", src, e)
        # 部分的に書かれた dst を削除
        if dst.exists():
            try:
                dst.unlink()
            except OSError:
                pass
        return None

    dst_size = dst.stat().st_size
    # 元ファイルを削除
    try:
        src.unlink()
    except OSError as e:
        logger.warning("元ファイル削除失敗 %s: %s", src, e)

    return (dst, src_size, dst_size)


def _convert_directory(
    root: Path,
    quality: int,
    effort: int,
    workers: int,
    dry_run: bool,
    recursive: bool = False,
) -> tuple[list[tuple[Path, Path, int, int]], int]:
    """ディレクトリ内の対象ファイルを並列変換.

    Returns:
        (変換結果リスト [(old_path, new_path, src_size, dst_size)], 失敗数)
    """
    if not root.exists():
        logger.warning("ディレクトリが存在しません: %s", root)
        return ([], 0)

    if recursive:
        candidates = [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in CONVERTIBLE_EXTS]
    else:
        candidates = [p for p in root.iterdir() if p.is_file() and p.suffix.lower() in CONVERTIBLE_EXTS]

    logger.info("[%s] 変換対象: %d 件", root.name, len(candidates))
    if not candidates:
        return ([], 0)

    results: list[tuple[Path, Path, int, int]] = []
    failed = 0
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_convert_one, p, quality, effort, dry_run): p for p in candidates}
        for fut in as_completed(futures):
            done += 1
            src = futures[fut]
            r = fut.result()
            if r is None:
                failed += 1
                continue
            new_path, src_size, dst_size = r
            results.append((src, new_path, src_size, dst_size))
            if done % 25 == 0 or done == len(candidates):
                logger.info("  進捗: %d / %d", done, len(candidates))
    return (results, failed)


def _update_db(
    db_path: Path,
    target_folder: Path,
    renames: list[tuple[Path, Path]],
) -> int:
    """maps/ 配下の変換に伴う DB 更新.

    file_path / file_name / file_size / file_mtime / file_hash を更新する。
    Returns:
        更新件数
    """
    updated = 0
    with db.connect(db_path) as conn:
        for old_abs, new_abs in renames:
            try:
                old_rel = old_abs.resolve().relative_to(target_folder.resolve()).as_posix()
                new_rel = new_abs.resolve().relative_to(target_folder.resolve()).as_posix()
            except ValueError:
                # target_folder 外のファイル (docs/originals 等)
                continue
            record = db.get_map_by_path(conn, old_rel)
            if not record:
                # ファイル名そのものでも検索 (相対パスが異なる場合)
                # まずは old_rel ベースだけ。後で再走査する想定
                continue

            try:
                stat = new_abs.stat()
            except OSError:
                continue
            now = datetime.now().isoformat(timespec="seconds")
            conn.execute(
                """
                UPDATE maps SET
                    file_path = ?,
                    file_name = ?,
                    file_size = ?,
                    file_mtime = ?,
                    file_hash = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    new_rel,
                    new_abs.name,
                    stat.st_size,
                    stat.st_mtime,
                    quick_hash(new_abs),
                    now,
                    record.id,
                ),
            )
            updated += 1
        conn.commit()
    return updated


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg = load_config(args.config)
    workers = max(1, args.workers if args.workers is not None else cfg.api_workers)
    target = cfg.target_folder.resolve()

    logger.info(
        "WebP 変換 (Quality=%d, Effort=%d, Workers=%d, dry-run=%s)",
        args.quality,
        args.effort,
        workers,
        args.dry_run,
    )

    total_src = 0
    total_dst = 0
    total_failed = 0

    # --- maps/ (target_folder 直下のみ。before_process はサブフォルダなのでスキップ) ---
    if args.only in ("maps", "both"):
        results, failed = _convert_directory(
            target,
            args.quality,
            args.effort,
            workers,
            args.dry_run,
            recursive=False,
        )
        total_failed += failed
        for src, dst, ss, ds in results:
            total_src += ss
            total_dst += ds

        # DB 更新
        if not args.dry_run and results:
            renames = [(src, dst) for src, dst, _, _ in results]
            updated = _update_db(cfg.database_path, target, renames)
            logger.info("DB 更新: %d 件", updated)

    # --- docs/originals/ ---
    if args.only in ("docs", "both"):
        results, failed = _convert_directory(
            DOCS_ORIGINALS,
            args.quality,
            args.effort,
            workers,
            args.dry_run,
            recursive=False,
        )
        total_failed += failed
        for src, dst, ss, ds in results:
            total_src += ss
            total_dst += ds

    saved_mb = (total_src - total_dst) / 1024 / 1024
    logger.info(
        "完了: 元 %.1f MB → WebP %.1f MB (節約 %.1f MB / 失敗 %d)",
        total_src / 1024 / 1024,
        total_dst / 1024 / 1024,
        saved_mb,
        total_failed,
    )
    return 0 if total_failed == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
