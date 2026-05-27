"""Gemini API による画像解析.

入力: 画像ファイル
出力: 地形・雰囲気・場所のタグリストと簡単な説明文 (日本語)

Gemini の構造化出力 (response_schema) で堅牢に JSON を取得し、
ネットワーク/レート制限による失敗は tenacity でリトライする。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from google import genai
from google.genai import types
from PIL import Image
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
)

logger = logging.getLogger(__name__)

# Gemini 429 レスポンス本文にある retryDelay: "35s" を拾うための正規表現。
# 'retryDelay': '35s' / "retryDelay": "35.2s" のような表記に対応する。
_RETRY_DELAY_RE = re.compile(
    r"retry[Dd]elay['\"]?\s*[:\s]\s*['\"]?(\d+(?:\.\d+)?)\s*s",
)

_DEFAULT_MAX_WAIT = 60.0
_RETRY_DELAY_SAFETY_MARGIN = 1.0


ANALYSIS_PROMPT = """\
あなたは TRPG (テーブルトーク RPG) のゲームマスター向けに、マップ画像を分類する
アシスタントです。与えられた画像を分析し、以下 3 つのカテゴリーごとに日本語のタグを
抽出してください。

## 地形分類 (terrain_tags)
画像に映る地形や自然要素。例: 森、山岳、洞窟、海岸、平原、川、砂漠、雪原、湿地、火山

## 雰囲気分類 (mood_tags)
画像から受ける印象・空気感。例: 神秘的、暗い、明るい、廃墟、賑やか、静寂、不気味、
壮大、荒涼、温かい、危険、冒険的

## 場所分類 (location_tags)
画像が表す場所や空間の種類。例: ダンジョン、町、村、屋外、室内、城、神殿、酒場、
地下、塔、墓地、港、街道

## 出力ルール
- 各カテゴリ 1〜5 個。重要度が高い順に並べる。
- タグは短い単語または短い熟語 (例: 「鬱蒼とした森」ではなく「森」「鬱蒼」を分ける)。
- 不確実なタグは含めない。
- description: 画像の特徴を 80 文字程度の日本語で簡潔に説明する。

JSON フォーマットで返答してください。
"""


@dataclass
class AnalysisResult:
    terrain_tags: list[str] = field(default_factory=list)
    mood_tags: list[str] = field(default_factory=list)
    location_tags: list[str] = field(default_factory=list)
    description: str = ""


_RESPONSE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "terrain_tags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "地形・自然要素タグ (日本語)",
        },
        "mood_tags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "雰囲気・印象タグ (日本語)",
        },
        "location_tags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "場所・空間タグ (日本語)",
        },
        "description": {
            "type": "string",
            "description": "画像の簡潔な説明 (日本語、80 文字程度)",
        },
    },
    "required": ["terrain_tags", "mood_tags", "location_tags", "description"],
}


class GeminiAnalyzer:
    def __init__(
        self,
        api_key: str,
        model: str = "gemini-2.5-flash",
        *,
        max_attempts: int = 3,
    ) -> None:
        self._client = genai.Client(api_key=api_key)
        self._model = model
        self._max_attempts = max(1, int(max_attempts))

    def analyze(self, image_path: Path) -> AnalysisResult:
        """画像を解析しタグと説明を返す."""
        return self._analyze_with_retry(image_path)

    def _analyze_with_retry(self, image_path: Path) -> AnalysisResult:
        retrier = retry(
            stop=stop_after_attempt(self._max_attempts),
            wait=_wait_respecting_retry_delay,
            retry=retry_if_exception_type(Exception),
            reraise=True,
            before_sleep=_log_before_sleep,
        )
        return retrier(self._analyze_once)(image_path)

    def _analyze_once(self, image_path: Path) -> AnalysisResult:
        with Image.open(image_path) as img:
            img.load()
            response = self._client.models.generate_content(
                model=self._model,
                contents=[img, ANALYSIS_PROMPT],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=_RESPONSE_SCHEMA,
                ),
            )

        return _parse_response(response)


def _extract_retry_delay(exc: BaseException) -> float | None:
    """例外メッセージから Gemini が示す retryDelay (秒) を抽出する."""
    msg = str(exc)
    m = _RETRY_DELAY_RE.search(msg)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


def _wait_respecting_retry_delay(retry_state: RetryCallState) -> float:
    """Tenacity 用 wait 関数。429 の retryDelay を最優先で尊重する.

    優先順位:
        1. レスポンス本文の retryDelay (例: "35s") + 安全マージン
        2. 指数バックオフ (1, 2, 4, 8, ... を _DEFAULT_MAX_WAIT で打ち切り)
    """
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    if exc is not None:
        hinted = _extract_retry_delay(exc)
        if hinted is not None:
            return min(hinted + _RETRY_DELAY_SAFETY_MARGIN, _DEFAULT_MAX_WAIT)

    return min(_DEFAULT_MAX_WAIT, 2.0 ** max(0, retry_state.attempt_number - 1))


def _log_before_sleep(retry_state: RetryCallState) -> None:
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    code = ""
    if exc is not None:
        m = re.match(r"\s*(\d{3})\b", str(exc))
        if m:
            code = f"[{m.group(1)}] "
    next_wait = retry_state.next_action.sleep if retry_state.next_action else 0.0
    logger.warning(
        "  %sリトライ %d/%d (%.1fs 待機)",
        code,
        retry_state.attempt_number,
        retry_state.retry_object.stop.max_attempt_number
        if hasattr(retry_state.retry_object, "stop")
        else 0,
        next_wait,
    )


def _parse_response(response: types.GenerateContentResponse) -> AnalysisResult:
    # google-genai は parsed 属性に JSON 由来の dict を載せる。
    # 念のため text からのフォールバックも用意する。
    parsed = getattr(response, "parsed", None)
    if parsed is None:
        import json

        text = (response.text or "").strip()
        if not text:
            raise RuntimeError("Gemini response is empty")
        parsed = json.loads(text)

    if not isinstance(parsed, dict):
        raise RuntimeError(f"unexpected response shape: {type(parsed).__name__}")

    def _as_str_list(value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(v).strip() for v in value if str(v).strip()]

    return AnalysisResult(
        terrain_tags=_as_str_list(parsed.get("terrain_tags")),
        mood_tags=_as_str_list(parsed.get("mood_tags")),
        location_tags=_as_str_list(parsed.get("location_tags")),
        description=str(parsed.get("description") or "").strip(),
    )
