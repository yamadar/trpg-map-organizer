"""タグと説明文を基に Gemini で自然な英語ファイル名を生成し、
実ファイルと DB の両方をリネームする.

Usage:
    python -m scripts.rename_to_english --dry-run    # 提案のみ
    python -m scripts.rename_to_english              # 実適用
    python -m scripts.rename_to_english --only-hash  # ハッシュ名のみ対象
    python -m scripts.rename_to_english --workers 10
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import threading
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google import genai  # noqa: E402
from google.genai import types  # noqa: E402
from PIL import Image  # noqa: E402
from tenacity import (  # noqa: E402
    retry,
    stop_after_attempt,
    wait_exponential,
)

from src import db  # noqa: E402
from src.config import load_config  # noqa: E402

logger = logging.getLogger("rename_to_english")

# 出力は常に WebP に統一する (画像配信の標準フォーマット)
TARGET_EXT = ".webp"
WEBP_QUALITY = 85
WEBP_METHOD = 4  # libwebp effort: 0-6 (高いほど圧縮率良いが遅い)


_PROMPT_TEMPLATE = """\
You generate short, evocative English filenames for TRPG (tabletop RPG) map images.

Combine the image's terrain, mood, and location into a natural English filename
(stem only, no extension). Examples of the style we want:

    mystical_forest_ruins
    bustling_medieval_marketplace
    ancient_underwater_temple
    crumbling_castle_courtyard
    snowy_mountain_pass
    haunted_graveyard_at_dusk

Rules:
- Lowercase ASCII letters, digits, and underscores only
- 2 to 4 words separated by underscores
- Total length <= 40 characters
- Prefer adjective + noun combinations that convey atmosphere AND subject
- Do not translate Japanese tags literally; pick the most evocative English equivalent
- Do not include words like "trpg", "map", "image", "scene"

Image data:
- description: {description}
- terrain tags: {terrain}
- mood tags: {mood}
- location tags: {location}

Return JSON with a single field "name" containing the filename stem.
"""


_RESPONSE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "Filename stem, no extension"},
    },
    "required": ["name"],
}


_HASH_NAME_RE = re.compile(r"^[a-f0-9]{16,}\.[A-Za-z0-9]+$")
_SAFE_STEM_RE = re.compile(r"^[a-z0-9_]+$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument(
        "--dry-run", action="store_true", help="リネームせず計画だけ表示"
    )
    parser.add_argument(
        "--only-hash",
        action="store_true",
        help="ハッシュ風 (16文字以上の hex) のファイル名のみを対象にする",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help="並列ワーカ数 (省略時は config.api_workers を使用)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="処理件数の上限 (テスト用)",
    )
    parser.add_argument(
        "--keep-format",
        action="store_true",
        help="ファイル形式を WebP に変換せず元の拡張子のまま保持する",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args()


def _sanitize_stem(raw: str) -> str:
    s = raw.strip().lower()
    s = re.sub(r"\.[A-Za-z0-9]+$", "", s)  # extension を剥がす (安全のため)
    s = re.sub(r"[^a-z0-9_]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s[:40]


@retry(
    stop=stop_after_attempt(6),
    wait=wait_exponential(multiplier=2, min=2, max=60),
    reraise=True,
)
def _generate_stem(
    client: genai.Client,
    model: str,
    record: db.MapRecord,
) -> str:
    prompt = _PROMPT_TEMPLATE.format(
        description=(record.description or "")[:300] or "(none)",
        terrain=", ".join(record.terrain_tags) or "(none)",
        mood=", ".join(record.mood_tags) or "(none)",
        location=", ".join(record.location_tags) or "(none)",
    )
    resp = client.models.generate_content(
        model=model,
        contents=[prompt],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=_RESPONSE_SCHEMA,
        ),
    )
    parsed = getattr(resp, "parsed", None)
    if parsed is None:
        parsed = json.loads(resp.text or "{}")
    raw = str(parsed.get("name") or "").strip()
    stem = _sanitize_stem(raw)
    if not stem or not _SAFE_STEM_RE.fullmatch(stem):
        raise ValueError(f"invalid stem returned: {raw!r}")
    return stem


def _is_hash_named(file_name: str) -> bool:
    return bool(_HASH_NAME_RE.match(file_name))


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
    workers = max(1, args.workers if args.workers is not None else cfg.api_workers)

    with db.connect(cfg.database_path) as conn:
        all_maps = db.list_maps(conn)

    # 対象選別
    candidates: list[db.MapRecord] = []
    for m in all_maps:
        if args.only_hash and not _is_hash_named(m.file_name):
            continue
        candidates.append(m)
        if args.limit and len(candidates) >= args.limit:
            break

    logger.info(
        "DB 総数: %d / リネーム対象: %d / ワーカ: %d",
        len(all_maps),
        len(candidates),
        workers,
    )
    if not candidates:
        logger.info("対象なし。")
        return 0

    # 並列で英語名を生成
    client = genai.Client(api_key=cfg.gemini_api_key)
    proposals: dict[int, str] = {}
    failed: list[int] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures: dict[Future, db.MapRecord] = {
            pool.submit(_generate_stem, client, cfg.gemini_model, m): m
            for m in candidates
        }
        done = 0
        for fut in as_completed(futures):
            m = futures[fut]
            done += 1
            try:
                stem = fut.result()
                proposals[m.id] = stem
                logger.info("[%d/%d] %s -> %s", done, len(candidates), m.file_name, stem)
            except Exception as e:  # noqa: BLE001
                failed.append(m.id)
                logger.error("[%d/%d] %s -> 失敗: %s", done, len(candidates), m.file_name, e)

    # 重複解決 (新ファイル名が既存と被らないように)
    used_names: set[str] = set()
    # 既存のリネーム対象外ファイル名は予約済みとして登録
    for m in all_maps:
        if m.id not in proposals:
            used_names.add(m.file_name.lower())
    # 実フォルダにある (DB 未登録の) ファイル名も保護
    if target.exists():
        for p in target.iterdir():
            if p.is_file():
                used_names.add(p.name.lower())

    rename_plan: list[tuple[db.MapRecord, str]] = []  # (record, new_filename)
    for m in candidates:
        if m.id not in proposals:
            continue
        stem = proposals[m.id]
        src_ext = Path(m.file_name).suffix.lower() or ".png"
        # --keep-format でなければ常に WebP 出力 (画像配信の標準フォーマット)
        ext = src_ext if args.keep_format else TARGET_EXT
        base = f"{stem}{ext}"
        candidate = base
        n = 1
        while candidate.lower() in used_names:
            n += 1
            candidate = f"{stem}_{n}{ext}"
        used_names.add(candidate.lower())
        rename_plan.append((m, candidate))

    # ファイルとDBの実更新
    success = 0
    renamed_skipped = 0
    fs_failed = 0
    with db.connect(cfg.database_path) as conn:
        for m, new_name in rename_plan:
            if new_name == m.file_name:
                renamed_skipped += 1
                continue

            new_rel = new_name  # フラット構造前提
            old_abs = db.resolve_path(m, target)
            new_abs = target / new_rel

            if args.dry_run:
                logger.debug("[dry-run] %s -> %s", m.file_name, new_name)
                continue

            if not old_abs.exists():
                logger.warning("元ファイルなし、DB のみ更新: %s", old_abs)
            else:
                # 入力が WebP 以外なら WebP に変換しつつリネーム
                src_ext = old_abs.suffix.lower()
                if args.keep_format or src_ext == ".webp":
                    try:
                        old_abs.rename(new_abs)
                    except OSError as e:
                        fs_failed += 1
                        logger.error("FS rename 失敗 %s: %s", old_abs, e)
                        continue
                else:
                    try:
                        with Image.open(old_abs) as img:
                            img.save(
                                new_abs,
                                format="WEBP",
                                quality=WEBP_QUALITY,
                                method=WEBP_METHOD,
                            )
                        old_abs.unlink()
                    except Exception as e:  # noqa: BLE001
                        fs_failed += 1
                        logger.error("WebP 変換失敗 %s: %s", old_abs, e)
                        continue

            db.update_file_path(
                conn,
                map_id=m.id,
                file_path=new_rel,
                file_name=new_name,
            )
            success += 1

    action = "提案生成" if args.dry_run else "リネーム完了"
    logger.info(
        "%s: 成功 %d / API 失敗 %d / FS 失敗 %d / 変化なし %d",
        action,
        success if not args.dry_run else len(rename_plan),
        len(failed),
        fs_failed,
        renamed_skipped,
    )
    return 0 if not failed and not fs_failed else 2


if __name__ == "__main__":
    sys.exit(main())
