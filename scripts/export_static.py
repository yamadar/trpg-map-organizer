"""DB と画像から GitHub Pages 用の静的サイトを生成する.

出力構成:
    docs/
    ├── index.html / style.css / app.js   (web/ からコピー)
    ├── data/maps.json                    (タグ + メタ情報)
    └── images/
        ├── thumb/  (グリッド用 ~400px JPEG)
        └── mid/    (プレビュー用 ~1280px JPEG)

Usage:
    python -m scripts.export_static                # 全量再生成
    python -m scripts.export_static --no-images    # JSON と HTML だけ更新
    python -m scripts.export_static --output dist  # 出力先を変更
    python -m scripts.export_static --workers 8
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import db  # noqa: E402
from src.config import load_config  # noqa: E402

logger = logging.getLogger("export_static")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = PROJECT_ROOT / "docs"
WEB_TEMPLATE = PROJECT_ROOT / "web"

THUMB_WIDTH = 400
JPEG_QUALITY_THUMB = 82


@dataclass
class ImageJob:
    src: Path
    thumb_dst: Path
    stem: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument(
        "--output", type=Path, default=DEFAULT_OUTPUT, help="出力ディレクトリ (既定: docs/)"
    )
    parser.add_argument(
        "--no-images",
        action="store_true",
        help="画像変換をスキップ (HTML/CSS/JS と JSON のみ更新)",
    )
    parser.add_argument(
        "--no-originals",
        action="store_true",
        help="元画像 (PNG/JPG) を docs/originals/ にコピーしない (リポジトリ容量節約)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help="画像変換の並列数 (省略時は config.api_workers または 4)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="既存の生成画像があっても上書き再生成する",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args()


def _convert_image(src: Path, dst: Path, max_width: int, quality: int) -> int:
    """画像をリサイズ + JPEG 保存。書き出したファイルサイズを返す."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(src) as img:
        if img.width > max_width:
            ratio = max_width / img.width
            new_size = (max_width, max(1, int(img.height * ratio)))
            img = img.resize(new_size, Image.LANCZOS)
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGB")
        img.save(dst, format="JPEG", quality=quality, optimize=True, progressive=True)
    return dst.stat().st_size


def _thumb_needs_generation(job: ImageJob, force: bool) -> bool:
    """thumb を生成する必要があるか判定 (差分検出用、I/O だけで高速)."""
    if force:
        return True
    if not job.thumb_dst.exists():
        return True
    try:
        src_mtime = job.src.stat().st_mtime
    except OSError:
        return True
    try:
        return job.thumb_dst.stat().st_mtime < src_mtime
    except OSError:
        return True


def _process_one(job: ImageJob) -> tuple[str, int]:
    """1 画像に対して thumb を生成。(stem, thumb_size) を返す.

    呼び出し側で _thumb_needs_generation により事前フィルタ済みの前提。
    元解像度の画像はプレビューにそのまま使うため、ここでは生成しない。
    """
    thumb_size = _convert_image(
        job.src, job.thumb_dst, THUMB_WIDTH, JPEG_QUALITY_THUMB
    )
    return (job.stem, thumb_size)


def _build_json_payload(
    records: list[db.MapRecord],
    has_originals: bool,
    target_folder: Path,
) -> dict:
    theme_set: set[str] = set()
    terrain_set: set[str] = set()
    mood_set: set[str] = set()
    location_set: set[str] = set()
    maps_out: list[dict] = []
    for r in records:
        stem = Path(r.file_name).stem
        entry: dict = {
            "id": r.id,
            "file": r.file_name,
            "thumb": f"{stem}.jpg",
            "desc": r.description or "",
            "theme": list(r.theme_tags),
            "terrain": list(r.terrain_tags),
            "mood": list(r.mood_tags),
            "location": list(r.location_tags),
        }
        # 元画像コピーが有効でも、ソースが消えていれば per-record で has_original=false
        if has_originals:
            src = db.resolve_path(r, target_folder)
            entry["has_original"] = src.exists()
        maps_out.append(entry)
        theme_set.update(r.theme_tags)
        terrain_set.update(r.terrain_tags)
        mood_set.update(r.mood_tags)
        location_set.update(r.location_tags)

    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total": len(maps_out),
        "has_originals": has_originals,
        "tags": {
            "theme": sorted(theme_set),
            "terrain": sorted(terrain_set),
            "mood": sorted(mood_set),
            "location": sorted(location_set),
        },
        "maps": maps_out,
    }


def _copy_i18n(output: Path) -> None:
    """web/i18n.json を docs/data/i18n.json にコピーする."""
    src = WEB_TEMPLATE / "i18n.json"
    if not src.exists():
        logger.warning("i18n.json が見つかりません: %s", src)
        return
    dst = output / "data" / "i18n.json"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    logger.info("i18n.json をコピー: %s", dst)


def _copy_originals(
    records: list[db.MapRecord],
    target_folder: Path,
    output: Path,
) -> tuple[int, int, int]:
    """元画像を docs/originals/ にコピーする。
    (新規コピー件数, スキップ件数, 合計バイト数) を返す.
    """
    originals_dir = output / "originals"
    originals_dir.mkdir(parents=True, exist_ok=True)
    copied = 0
    skipped = 0
    total = 0
    for r in records:
        src = db.resolve_path(r, target_folder)
        if not src.exists():
            continue
        dst = originals_dir / r.file_name
        try:
            src_stat = src.stat()
        except OSError:
            continue
        if (
            dst.exists()
            and dst.stat().st_size == src_stat.st_size
            and dst.stat().st_mtime >= src_stat.st_mtime - 1
        ):
            total += dst.stat().st_size
            skipped += 1
            continue
        shutil.copy2(src, dst)
        total += dst.stat().st_size
        copied += 1
    return copied, skipped, total


_TEMPLATE_FILES = {"index.html", "style.css", "app.js"}


def _copy_template(output: Path) -> None:
    """web/ から HTML/CSS/JS のみを docs/ ルートにコピーする.

    i18n.json は _copy_i18n が docs/data/ に配置するためここでは扱わない。
    """
    if not WEB_TEMPLATE.exists():
        raise RuntimeError(f"テンプレートが見つかりません: {WEB_TEMPLATE}")
    for src in WEB_TEMPLATE.iterdir():
        if not src.is_file():
            continue
        if src.name not in _TEMPLATE_FILES:
            continue
        shutil.copy2(src, output / src.name)


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

    output: Path = args.output
    output.mkdir(parents=True, exist_ok=True)

    # 1. テンプレートをコピー
    _copy_template(output)
    logger.info("HTML/CSS/JS テンプレートをコピー: %s", output)

    # 2. DB レコードを読み込み
    with db.connect(cfg.database_path) as conn:
        records = db.list_maps(conn)
    logger.info("DB レコード: %d 件", len(records))

    # 3. JSON 生成 (i18n.json も含む)
    data_dir = output / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    payload = _build_json_payload(
        records,
        has_originals=not args.no_originals,
        target_folder=cfg.target_folder,
    )
    json_path = data_dir / "maps.json"
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, separators=(",", ": ")),
        encoding="utf-8",
    )
    logger.info("maps.json 生成: %s (%d 件)", json_path, len(payload["maps"]))
    _copy_i18n(output)

    if args.no_images:
        logger.info("画像変換をスキップ (--no-images)")
        return 0

    # 4. 画像変換 (thumb のみ。プレビューは originals を直接参照する)
    thumb_dir = output / "images" / "thumb"
    thumb_dir.mkdir(parents=True, exist_ok=True)

    jobs: list[ImageJob] = []
    missing: list[str] = []
    for r in records:
        src = db.resolve_path(r, cfg.target_folder)
        if not src.exists():
            missing.append(r.file_name)
            continue
        stem = Path(r.file_name).stem
        jobs.append(
            ImageJob(
                src=src,
                thumb_dst=thumb_dir / f"{stem}.jpg",
                stem=stem,
            )
        )

    if missing:
        logger.warning("元ファイルが見つからずスキップ: %d 件", len(missing))

    workers = args.workers or max(1, cfg.api_workers or 4)

    # 事前に差分判定し、生成が必要なジョブだけを残す (既存サムネは触らない)
    jobs_to_run: list[ImageJob] = []
    skipped_size = 0
    for j in jobs:
        if _thumb_needs_generation(j, args.force):
            jobs_to_run.append(j)
        else:
            try:
                skipped_size += j.thumb_dst.stat().st_size
            except OSError:
                pass

    logger.info(
        "画像変換: 対象 %d 件 (生成 %d / 既存スキップ %d) / ワーカ %d",
        len(jobs),
        len(jobs_to_run),
        len(jobs) - len(jobs_to_run),
        workers,
    )

    total_thumb = skipped_size
    generated = 0
    failed = 0
    if jobs_to_run:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_process_one, j): j for j in jobs_to_run}
            for fut in as_completed(futures):
                generated += 1
                try:
                    stem, ts = fut.result()
                    total_thumb += ts
                    if generated % 25 == 0 or generated == len(jobs_to_run):
                        logger.info("  進捗: %d / %d", generated, len(jobs_to_run))
                except Exception as e:  # noqa: BLE001
                    failed += 1
                    logger.error("変換失敗 %s: %s", futures[fut].src.name, e)

    logger.info(
        "画像変換完了: 生成 %d / スキップ %d / 失敗 %d / thumb 合計 %.1f MB",
        generated - failed,
        len(jobs) - len(jobs_to_run),
        failed,
        total_thumb / 1024 / 1024,
    )

    # 5. 元画像のコピー (デフォルト ON、--no-originals でスキップ)
    if not args.no_originals:
        logger.info("元画像を docs/originals/ にコピー中...")
        copied, orig_skipped, total_orig = _copy_originals(
            records, cfg.target_folder, output
        )
        logger.info(
            "元画像コピー: 新規 %d 件 / 既存スキップ %d 件 / 合計 %.1f MB",
            copied,
            orig_skipped,
            total_orig / 1024 / 1024,
        )

    # サマリ
    docs_size = sum(p.stat().st_size for p in output.rglob("*") if p.is_file())
    logger.info("docs/ 総容量: %.1f MB", docs_size / 1024 / 1024)
    logger.info("ローカル確認: python -m http.server -d %s 8080", output)
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
