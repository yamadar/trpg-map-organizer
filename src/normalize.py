"""タグ表記揺れの正規化.

辞書 (`tag_aliases.yaml`) に基づき variant -> canonical の置換を行う。
チェーン (A -> B -> C) は最大深度まで再帰的に解決する。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

DEFAULT_ALIASES_PATH = Path(__file__).resolve().parent.parent / "tag_aliases.yaml"
_MAX_CHAIN_DEPTH = 16


@dataclass
class TagNormalizer:
    aliases: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | None = None) -> "TagNormalizer":
        """辞書 YAML を読み込む。ファイルが無ければ空の辞書で返す."""
        target = path or DEFAULT_ALIASES_PATH
        if not target.exists():
            logger.info("alias file not found: %s (empty normalizer)", target)
            return cls()
        with target.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        raw = data.get("aliases") or {}
        if not isinstance(raw, dict):
            raise ValueError(f"`aliases` must be a mapping in {target}")
        # 値を文字列化し、空キー・空値は除外
        cleaned = {
            str(k).strip(): str(v).strip()
            for k, v in raw.items()
            if str(k).strip() and str(v).strip()
        }
        return cls(aliases=cleaned)

    def normalize_tag(self, tag: str) -> str:
        """単一タグを正規化する。チェーン・循環安全."""
        tag = (tag or "").strip()
        if not tag:
            return tag
        seen: set[str] = set()
        for _ in range(_MAX_CHAIN_DEPTH):
            if tag in seen:
                logger.warning("alias chain cycle detected at %r", tag)
                break
            if tag not in self.aliases:
                break
            seen.add(tag)
            tag = self.aliases[tag]
        return tag

    def normalize_tags(self, tags: list[str]) -> list[str]:
        """タグリストを正規化し、順序を保ちつつ重複を除去する."""
        out: list[str] = []
        seen: set[str] = set()
        for t in tags:
            n = self.normalize_tag(t)
            if not n or n in seen:
                continue
            seen.add(n)
            out.append(n)
        return out
