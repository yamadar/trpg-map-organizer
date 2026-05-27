"""アプリケーション設定のロード."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"
DEFAULT_EXAMPLE_PATH = Path(__file__).resolve().parent.parent / "config.example.yaml"


@dataclass
class AppConfig:
    target_folder: Path
    image_extensions: list[str]
    database_path: Path
    gemini_model: str
    api_retries: int
    api_min_interval_sec: float
    api_workers: int
    ui_grid_columns: int
    ui_thumbnail_width: int
    gemini_api_key: str = field(repr=False)


def _expand_path(value: str) -> Path:
    return Path(os.path.expanduser(os.path.expandvars(value))).resolve()


def load_config(path: Path | None = None) -> AppConfig:
    """設定ファイルと環境変数を読み込む.

    Args:
        path: 明示的に指定する config.yaml のパス。None の場合は既定パスを使う。
              既定パスが存在しなければ config.example.yaml にフォールバックする。
    """
    load_dotenv()

    config_path = path or DEFAULT_CONFIG_PATH
    if not config_path.exists():
        if DEFAULT_EXAMPLE_PATH.exists():
            config_path = DEFAULT_EXAMPLE_PATH
        else:
            raise FileNotFoundError(
                f"設定ファイルが見つかりません: {config_path} / {DEFAULT_EXAMPLE_PATH}"
            )

    with config_path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "環境変数 GEMINI_API_KEY が未設定です。.env を作成して API キーを設定してください。"
        )

    extensions = [str(e).lower() for e in raw.get("image_extensions", [])]
    if not extensions:
        extensions = [".jpg", ".jpeg", ".png", ".webp"]

    return AppConfig(
        target_folder=_expand_path(raw["target_folder"]),
        image_extensions=extensions,
        database_path=_expand_path(raw.get("database_path", "./data/maps.db")),
        gemini_model=str(raw.get("gemini_model", "gemini-2.5-flash")),
        api_retries=int(raw.get("api_retries", 6)),
        api_min_interval_sec=float(raw.get("api_min_interval_sec", 0.5)),
        api_workers=max(1, int(raw.get("api_workers", 1))),
        ui_grid_columns=int(raw.get("ui_grid_columns", 4)),
        ui_thumbnail_width=int(raw.get("ui_thumbnail_width", 300)),
        gemini_api_key=api_key,
    )
