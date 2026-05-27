"""現在 DB にあるタグ群を Gemini に渡し、表記揺れ統合候補を提案させる.

出力: tag_aliases_suggested.yaml  (リポジトリルート)
       既存 tag_aliases.yaml にマージする差分形式で、コメントに根拠を添える。

Usage:
    python -m scripts.suggest_aliases             # 全カテゴリ
    python -m scripts.suggest_aliases --category terrain  # 地形だけ
    python -m scripts.suggest_aliases --output /tmp/foo.yaml
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google import genai  # noqa: E402
from google.genai import types  # noqa: E402

from src import db  # noqa: E402
from src.config import load_config  # noqa: E402
from src.normalize import TagNormalizer  # noqa: E402

logger = logging.getLogger("suggest_aliases")

CATEGORIES = ("terrain_tags", "mood_tags", "location_tags")
CATEGORY_LABELS = {
    "terrain_tags": "地形",
    "mood_tags": "雰囲気",
    "location_tags": "場所",
}

PROMPT_TEMPLATE = """\
あなたは日本語タグの正規化に長けたアシスタントです。
以下は TRPG マップ画像に付与された{label}タグの一覧です。
表記揺れ・類義語・接尾辞違いを統合するための辞書を YAML で出力してください。

## 統合ルール
- variant: canonical の形式 (variant が現れたら canonical に置換する)
- canonical は最も基本的・短い形式を選ぶ
- 統合の例:
    大木: 木
    樹木: 木
    河川: 川
    のどかな: のどか
    中世風: 中世
    魔法的: 魔法
    石造り: 石造
- 既に基本形のタグ (置換不要なもの) は出力に含めない
- 意味が明らかに同じものだけを統合する。意味が変わる恐れがあるものは含めない
- 同じ意味グループには必ず同じ canonical を使う
- 既存の辞書 (既知の正規化) も参考にする

## 既存の辞書 (既に正規化されているもの。重複登録不要)
{existing_aliases}

## タグ一覧 ({label})
{tag_list}

## 出力形式
JSON で `aliases` キーに variant→canonical のマップを返す。
"""

_RESPONSE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "aliases": {
            "type": "object",
            "description": "variant -> canonical の対応",
            "additionalProperties": {"type": "string"},
        },
    },
    "required": ["aliases"],
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument(
        "--category",
        choices=["terrain", "mood", "location", "all"],
        default="all",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="出力 YAML パス (省略時はリポジトリルートの tag_aliases_suggested.yaml)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args()


def _ask_gemini(
    client: genai.Client,
    model: str,
    label: str,
    tags: list[str],
    existing_aliases: dict[str, str],
) -> dict[str, str]:
    existing_dump = (
        "\n".join(f"  {k}: {v}" for k, v in sorted(existing_aliases.items()))
        or "  (なし)"
    )
    prompt = PROMPT_TEMPLATE.format(
        label=label,
        existing_aliases=existing_dump,
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
    aliases = parsed.get("aliases") or {}
    return {str(k): str(v) for k, v in aliases.items() if str(k) != str(v)}


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
    )

    cfg = load_config(args.config)
    if not cfg.database_path.exists():
        logger.error("DB が見つかりません。先に build_db を実行してください: %s", cfg.database_path)
        return 1

    existing = TagNormalizer.load().aliases

    categories: list[str] = (
        list(CATEGORIES)
        if args.category == "all"
        else [f"{args.category}_tags"]
    )

    client = genai.Client(api_key=cfg.gemini_api_key)

    all_suggestions: dict[str, str] = {}
    per_category_blocks: list[str] = []
    with db.connect(cfg.database_path) as conn:
        for col in categories:
            tags = db.distinct_tags(conn, col)
            label = CATEGORY_LABELS[col]
            logger.info("[%s] タグ数: %d", label, len(tags))
            if not tags:
                continue
            suggested = _ask_gemini(client, cfg.gemini_model, label, tags, existing)
            logger.info("[%s] 提案: %d 件", label, len(suggested))
            # 重複・既存と矛盾するものはスキップ
            block_lines = [f"  # --- {label} ---"]
            for variant, canonical in sorted(suggested.items()):
                if variant in existing or variant in all_suggestions:
                    continue
                if canonical not in tags and canonical not in existing.values():
                    # canonical が既存タグでも既存正規化先でもない場合は注意のためコメント
                    block_lines.append(
                        f"  # NOTE: '{canonical}' は既存タグに無いので慎重に確認"
                    )
                all_suggestions[variant] = canonical
                block_lines.append(f"  {_yaml_safe(variant)}: {_yaml_safe(canonical)}")
            per_category_blocks.append("\n".join(block_lines))

    output_path = args.output or (
        Path(__file__).resolve().parent.parent / "tag_aliases_suggested.yaml"
    )
    header = (
        "# Gemini による表記揺れ統合候補 (要レビュー)\n"
        "# 内容を確認のうえ tag_aliases.yaml の aliases: 配下にマージしてください。\n"
        f"# 提案件数: {len(all_suggestions)}\n\n"
        "aliases:\n"
    )
    body = "\n".join(per_category_blocks) if per_category_blocks else "  # (提案なし)"
    output_path.write_text(header + body + "\n", encoding="utf-8")
    logger.info("出力: %s (%d 件)", output_path, len(all_suggestions))
    return 0


_YAML_UNSAFE = re.compile(r"^[\s\-\?:,\[\]\{\}#&\*!\|>\'\"%@`]|[:#]\s|\s$")


def _yaml_safe(value: str) -> str:
    """日本語タグはほぼ問題ないが、念のため特殊文字を含む場合はクォートする."""
    if _YAML_UNSAFE.search(value) or value in {"yes", "no", "true", "false", "null"}:
        return '"' + value.replace('"', '\\"') + '"'
    return value


if __name__ == "__main__":
    sys.exit(main())
