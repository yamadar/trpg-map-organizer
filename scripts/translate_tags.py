"""DB に存在する日本語タグを Gemini で英訳し web/i18n.json に追記する.

増分実行: 既に翻訳済みのタグはスキップする。
タグの種類が増えた後に実行することで、英語表示用の翻訳辞書を最新化する。

Usage:
    python -m scripts.translate_tags
    python -m scripts.translate_tags --rebuild      # 既存翻訳を破棄して再生成
    python -m scripts.translate_tags --batch-size 40
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google import genai  # noqa: E402
from google.genai import types  # noqa: E402
from tenacity import retry, stop_after_attempt, wait_exponential  # noqa: E402

from src import db  # noqa: E402
from src.config import load_config  # noqa: E402

logger = logging.getLogger("translate_tags")

I18N_PATH = Path(__file__).resolve().parent.parent / "web" / "i18n.json"

CATEGORY_LABELS = {
    "terrain_tags": "terrain / landscape feature",
    "mood_tags": "mood / atmosphere",
    "location_tags": "place / location type",
    "theme_tags": "theme / genre / world setting (e.g. medieval, eastern, cthulhu)",
}

PROMPT_TEMPLATE = """\
You translate Japanese tags used for TRPG (tabletop RPG) map images into concise
English equivalents.

Category: {category}

Rules:
- 1 to 3 English words, lowercase, hyphen or space separated as natural
- Use the most evocative / visually descriptive English (not literal translation)
- Be consistent: if two Japanese tags map to similar concepts, use the same English word
- Avoid words like "trpg", "map", "scene", "image"
- For ambiguous tags, prefer common fantasy/RPG vocabulary

Japanese tags to translate:
{tag_list}

Return JSON with an "items" array. Each item has "ja" (the original tag) and
"en" (the translation).
"""


_RESPONSE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ja": {"type": "string"},
                    "en": {"type": "string"},
                },
                "required": ["ja", "en"],
            },
        },
    },
    "required": ["items"],
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="既存翻訳を破棄して全タグを再翻訳",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=50,
        help="1 リクエストあたりのタグ数 (既定 50)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args()


@retry(
    stop=stop_after_attempt(6),
    wait=wait_exponential(multiplier=2, min=2, max=60),
    reraise=True,
)
def _translate_batch(
    client: genai.Client,
    model: str,
    category: str,
    tags: list[str],
) -> dict[str, str]:
    prompt = PROMPT_TEMPLATE.format(
        category=CATEGORY_LABELS.get(category, category),
        tag_list="\n".join(f"- {t}" for t in tags),
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
    items = parsed.get("items") or []
    out: dict[str, str] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        ja = str(it.get("ja") or "").strip()
        en = str(it.get("en") or "").strip().lower()
        if ja and en and en != ja:
            out[ja] = en
    return out


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

    # 既存 i18n.json を読み込み
    if not I18N_PATH.exists():
        logger.error("i18n テンプレートが見つかりません: %s", I18N_PATH)
        return 1
    with I18N_PATH.open("r", encoding="utf-8") as f:
        i18n = json.load(f)
    existing: dict[str, str] = {} if args.rebuild else dict(i18n.get("tags") or {})

    # カテゴリごとに DB から distinct タグを取得
    needed: dict[str, list[str]] = {}
    with db.connect(cfg.database_path) as conn:
        for col in ("terrain_tags", "mood_tags", "location_tags", "theme_tags"):
            tags = db.distinct_tags(conn, col)
            missing = [t for t in tags if t not in existing]
            if missing:
                needed[col] = missing
            logger.info(
                "[%s] 既存 %d / 不足 %d", col, len(tags) - len(missing), len(missing)
            )

    if not needed:
        logger.info("追加翻訳は不要でした。")
        # それでも整形して保存しておく (キー順整列)
        i18n["tags"] = dict(sorted(existing.items()))
        I18N_PATH.write_text(
            json.dumps(i18n, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return 0

    # Gemini で翻訳
    client = genai.Client(api_key=cfg.gemini_api_key)
    new_translations: dict[str, str] = {}
    for category, tags in needed.items():
        logger.info("翻訳開始 [%s]: %d 件", category, len(tags))
        for i in range(0, len(tags), args.batch_size):
            batch = tags[i : i + args.batch_size]
            translated = _translate_batch(client, cfg.gemini_model, category, batch)
            new_translations.update(translated)
            logger.info(
                "  バッチ %d-%d: 翻訳 %d 件",
                i + 1,
                min(i + args.batch_size, len(tags)),
                len(translated),
            )

    merged = {**existing, **new_translations}
    merged = dict(sorted(merged.items()))
    i18n["tags"] = merged

    I18N_PATH.write_text(
        json.dumps(i18n, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    logger.info(
        "完了: 新規 %d 件追加 / 合計 %d 件 / %s",
        len(new_translations),
        len(merged),
        I18N_PATH,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
