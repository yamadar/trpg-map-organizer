"""TRPG マップタグ検索・閲覧 WebUI (Streamlit).

Usage:
    streamlit run src/app.py
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

# `streamlit run src/app.py` 実行時にプロジェクトルートを sys.path に通す
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import streamlit as st  # noqa: E402
from PIL import Image  # noqa: E402

from src import db  # noqa: E402
from src.config import AppConfig, load_config  # noqa: E402


st.set_page_config(
    page_title="TRPG Map Organizer",
    page_icon="🗺️",
    layout="wide",
)


@st.cache_resource
def get_config() -> AppConfig:
    return load_config()


@st.cache_data(show_spinner=False)
def load_thumbnail(file_path: str, width: int, mtime: float) -> bytes | None:
    """サムネイル画像を JPEG バイト列で返す。mtime はキャッシュ無効化のためのキー."""
    try:
        with Image.open(file_path) as img:
            img.thumbnail((width, width * 4))
            buf = io.BytesIO()
            if img.mode in ("RGBA", "LA", "P"):
                img = img.convert("RGB")
            img.save(buf, format="JPEG", quality=85)
            return buf.getvalue()
    except (FileNotFoundError, OSError):
        return None


def _format_tags(tags: list[str], limit: int = 5) -> str:
    if not tags:
        return "—"
    shown = tags[:limit]
    text = " ".join(f"`{t}`" for t in shown)
    if len(tags) > limit:
        text += f" …+{len(tags) - limit}"
    return text


@st.dialog("マップ詳細", width="large")
def show_preview(record: db.MapRecord) -> None:
    path = Path(record.file_path)
    if path.exists():
        st.image(str(path), use_container_width=True)
    else:
        st.warning(f"ファイルが見つかりません: {record.file_path}")

    st.markdown(f"**ファイル名:** `{record.file_name}`")
    st.markdown(f"**パス:** `{record.file_path}`")
    if record.description:
        st.markdown(f"**説明:** {record.description}")

    col1, col2, col3 = st.columns(3)
    with col1:
        st.markdown("**地形タグ**")
        st.markdown(_format_tags(record.terrain_tags, limit=20))
    with col2:
        st.markdown("**雰囲気タグ**")
        st.markdown(_format_tags(record.mood_tags, limit=20))
    with col3:
        st.markdown("**場所タグ**")
        st.markdown(_format_tags(record.location_tags, limit=20))

    if record.analyzed_at:
        st.caption(f"解析日時: {record.analyzed_at}")


def render_setup_help() -> None:
    st.title("🗺️ TRPG Map Organizer")
    st.warning("まだマップが登録されていません。")
    st.markdown(
        """
### セットアップ手順
1. `.env.example` を `.env` にコピーし `GEMINI_API_KEY` を設定する
2. `config.example.yaml` を `config.yaml` にコピーし `target_folder` を設定する
3. 依存をインストール: `pip install -r requirements.txt`
4. 解析を実行: `python -m scripts.build_db`
5. このページを再読み込みする
        """
    )


def render_grid(
    records: list[db.MapRecord],
    *,
    columns: int,
    thumbnail_width: int,
) -> None:
    if not records:
        st.info("条件に一致するマップはありません。")
        return

    for row_start in range(0, len(records), columns):
        cols = st.columns(columns, gap="medium")
        for i, col in enumerate(cols):
            idx = row_start + i
            if idx >= len(records):
                break
            rec = records[idx]
            with col:
                _render_card(rec, thumbnail_width)


def _render_card(record: db.MapRecord, thumbnail_width: int) -> None:
    thumb = load_thumbnail(
        record.file_path,
        thumbnail_width,
        record.file_mtime or 0.0,
    )
    if thumb:
        st.image(thumb, use_container_width=True)
    else:
        st.markdown(":warning: 画像読込失敗")

    st.markdown(f"**{record.file_name}**")
    if record.terrain_tags:
        st.caption("地形: " + " / ".join(record.terrain_tags[:3]))
    if record.mood_tags:
        st.caption("雰囲気: " + " / ".join(record.mood_tags[:3]))
    if record.location_tags:
        st.caption("場所: " + " / ".join(record.location_tags[:3]))

    if st.button("詳細を見る", key=f"detail_{record.id}", use_container_width=True):
        show_preview(record)


def main() -> None:
    try:
        cfg = get_config()
    except Exception as e:  # noqa: BLE001
        st.title("🗺️ TRPG Map Organizer")
        st.error(f"設定の読み込みに失敗しました: {e}")
        st.info(
            "`.env` に `GEMINI_API_KEY` を、`config.yaml` に `target_folder` を"
            "設定してから再読み込みしてください。"
        )
        return

    if not cfg.database_path.exists():
        render_setup_help()
        return

    db.init_db(cfg.database_path)

    with db.connect(cfg.database_path) as conn:
        all_records = db.list_maps(conn)
        terrain_choices = db.distinct_tags(conn, "terrain_tags")
        mood_choices = db.distinct_tags(conn, "mood_tags")
        location_choices = db.distinct_tags(conn, "location_tags")

    st.title("🗺️ TRPG Map Organizer")

    if not all_records:
        render_setup_help()
        return

    # --- サイドバー: フィルタ ---
    with st.sidebar:
        st.header("検索フィルタ")
        match_mode_label = st.radio(
            "タグ一致条件",
            options=["any", "all"],
            format_func=lambda v: "いずれか含む (OR)" if v == "any" else "全て含む (AND)",
            horizontal=True,
            index=0,
        )
        selected_terrain = st.multiselect("地形タグ", terrain_choices)
        selected_mood = st.multiselect("雰囲気タグ", mood_choices)
        selected_location = st.multiselect("場所タグ", location_choices)
        name_query = st.text_input("ファイル名で検索", placeholder="部分一致")

        st.markdown("---")
        st.subheader("表示設定")
        columns = st.slider(
            "列数", min_value=1, max_value=6, value=cfg.ui_grid_columns
        )

        st.markdown("---")
        st.caption(f"登録件数: {len(all_records)}")
        st.caption(f"DB: `{cfg.database_path}`")

    # --- 検索実行 ---
    with db.connect(cfg.database_path) as conn:
        records = db.search_maps(
            conn,
            terrain_tags=selected_terrain,
            mood_tags=selected_mood,
            location_tags=selected_location,
            name_query=name_query,
            match_mode=match_mode_label,
        )

    st.markdown(f"**{len(records)} 件**")
    render_grid(records, columns=columns, thumbnail_width=cfg.ui_thumbnail_width)


main()
