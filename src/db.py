"""SQLite データベース管理.

仕様書の `maps` テーブルを基本とし、増分解析のためのファイルメタ情報
(サイズ・更新時刻・ハッシュ) と AI 生成の説明文を追加で保持する。
タグは JSON 配列として保存し、検索時は SQLite の JSON1 拡張で展開する。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Iterator


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS maps (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path     TEXT    NOT NULL UNIQUE,
    file_name     TEXT    NOT NULL,
    file_size     INTEGER,
    file_mtime    REAL,
    file_hash     TEXT,
    terrain_tags  TEXT    NOT NULL DEFAULT '[]',
    mood_tags     TEXT    NOT NULL DEFAULT '[]',
    location_tags TEXT    NOT NULL DEFAULT '[]',
    description   TEXT,
    analyzed_at   TIMESTAMP,
    created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_maps_file_name ON maps(file_name);
CREATE INDEX IF NOT EXISTS idx_maps_analyzed_at ON maps(analyzed_at);
"""


@dataclass
class MapRecord:
    id: int
    file_path: str
    file_name: str
    file_size: int | None
    file_mtime: float | None
    file_hash: str | None
    terrain_tags: list[str]
    mood_tags: list[str]
    location_tags: list[str]
    description: str | None
    analyzed_at: str | None
    created_at: str
    updated_at: str


def _row_to_record(row: sqlite3.Row) -> MapRecord:
    return MapRecord(
        id=row["id"],
        file_path=row["file_path"],
        file_name=row["file_name"],
        file_size=row["file_size"],
        file_mtime=row["file_mtime"],
        file_hash=row["file_hash"],
        terrain_tags=json.loads(row["terrain_tags"] or "[]"),
        mood_tags=json.loads(row["mood_tags"] or "[]"),
        location_tags=json.loads(row["location_tags"] or "[]"),
        description=row["description"],
        analyzed_at=row["analyzed_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def init_db(db_path: Path) -> None:
    """DB ファイルとテーブルを用意する."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA_SQL)
        conn.commit()


@contextmanager
def connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    """sqlite3.Connection を yield する。row_factory は Row 固定."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def get_map_by_path(conn: sqlite3.Connection, file_path: str) -> MapRecord | None:
    row = conn.execute(
        "SELECT * FROM maps WHERE file_path = ?", (file_path,)
    ).fetchone()
    return _row_to_record(row) if row else None


def upsert_map(
    conn: sqlite3.Connection,
    *,
    file_path: str,
    file_name: str,
    file_size: int | None,
    file_mtime: float | None,
    file_hash: str | None,
    terrain_tags: list[str],
    mood_tags: list[str],
    location_tags: list[str],
    description: str | None,
) -> int:
    """新規登録 or 既存レコードを更新し、id を返す."""
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        """
        INSERT INTO maps (
            file_path, file_name, file_size, file_mtime, file_hash,
            terrain_tags, mood_tags, location_tags, description,
            analyzed_at, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(file_path) DO UPDATE SET
            file_name     = excluded.file_name,
            file_size     = excluded.file_size,
            file_mtime    = excluded.file_mtime,
            file_hash     = excluded.file_hash,
            terrain_tags  = excluded.terrain_tags,
            mood_tags     = excluded.mood_tags,
            location_tags = excluded.location_tags,
            description   = excluded.description,
            analyzed_at   = excluded.analyzed_at,
            updated_at    = excluded.updated_at
        """,
        (
            file_path,
            file_name,
            file_size,
            file_mtime,
            file_hash,
            json.dumps(terrain_tags, ensure_ascii=False),
            json.dumps(mood_tags, ensure_ascii=False),
            json.dumps(location_tags, ensure_ascii=False),
            description,
            now,
            now,
            now,
        ),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM maps WHERE file_path = ?", (file_path,)
    ).fetchone()
    return int(row["id"])


def delete_map_by_path(conn: sqlite3.Connection, file_path: str) -> bool:
    cur = conn.execute("DELETE FROM maps WHERE file_path = ?", (file_path,))
    conn.commit()
    return cur.rowcount > 0


def resolve_path(record: "MapRecord", target_folder: Path) -> Path:
    """DB の file_path を絶対パスに解決する.

    - 既に絶対パスなら (旧データ後方互換) そのまま返す
    - 相対パスなら target_folder と結合する
    """
    p = Path(record.file_path)
    return p if p.is_absolute() else (target_folder / p)


def update_file_path(
    conn: sqlite3.Connection,
    *,
    map_id: int,
    file_path: str,
    file_name: str,
) -> None:
    """ファイルパスとファイル名だけを更新する (リネーム/パス移行用)."""
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "UPDATE maps SET file_path = ?, file_name = ?, updated_at = ? WHERE id = ?",
        (file_path, file_name, now, map_id),
    )
    conn.commit()


def update_tags(
    conn: sqlite3.Connection,
    *,
    map_id: int,
    terrain_tags: list[str],
    mood_tags: list[str],
    location_tags: list[str],
) -> None:
    """タグ列だけを更新する (再解析せずに正規化を反映する用途)."""
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        """
        UPDATE maps SET
            terrain_tags = ?,
            mood_tags = ?,
            location_tags = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            json.dumps(terrain_tags, ensure_ascii=False),
            json.dumps(mood_tags, ensure_ascii=False),
            json.dumps(location_tags, ensure_ascii=False),
            now,
            map_id,
        ),
    )
    conn.commit()


def list_maps(conn: sqlite3.Connection) -> list[MapRecord]:
    rows = conn.execute("SELECT * FROM maps ORDER BY file_name").fetchall()
    return [_row_to_record(r) for r in rows]


def search_maps(
    conn: sqlite3.Connection,
    *,
    terrain_tags: Iterable[str] = (),
    mood_tags: Iterable[str] = (),
    location_tags: Iterable[str] = (),
    name_query: str = "",
    match_mode: str = "any",
) -> list[MapRecord]:
    """タグとファイル名でマップを検索する.

    match_mode:
        - "any": 同一カテゴリ内の選択タグのうち1つでも含めばヒット (OR)
        - "all": 全ての選択タグを含むものだけがヒット (AND)
    異なるカテゴリ間は常に AND。
    """
    if match_mode not in ("any", "all"):
        raise ValueError("match_mode must be 'any' or 'all'")

    where: list[str] = []
    params: list[object] = []

    def _add_tag_filter(column: str, tags: Iterable[str]) -> None:
        tag_list = [t for t in tags if t]
        if not tag_list:
            return
        # JSON1 拡張で配列展開し、選択タグとの一致を集計する
        # 各タグごとに EXISTS をつなぐ
        sub_clauses = []
        for tag in tag_list:
            sub_clauses.append(
                f"EXISTS (SELECT 1 FROM json_each(maps.{column}) "
                f"WHERE json_each.value = ?)"
            )
            params.append(tag)
        joiner = " AND " if match_mode == "all" else " OR "
        where.append("(" + joiner.join(sub_clauses) + ")")

    _add_tag_filter("terrain_tags", terrain_tags)
    _add_tag_filter("mood_tags", mood_tags)
    _add_tag_filter("location_tags", location_tags)

    if name_query.strip():
        where.append("LOWER(file_name) LIKE ?")
        params.append(f"%{name_query.strip().lower()}%")

    sql = "SELECT * FROM maps"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY file_name"

    rows = conn.execute(sql, params).fetchall()
    return [_row_to_record(r) for r in rows]


def distinct_tags(conn: sqlite3.Connection, column: str) -> list[str]:
    """指定カラム (terrain_tags/mood_tags/location_tags) のユニークタグ一覧."""
    if column not in {"terrain_tags", "mood_tags", "location_tags"}:
        raise ValueError(f"invalid column: {column}")
    rows = conn.execute(
        f"SELECT DISTINCT json_each.value AS tag "
        f"FROM maps, json_each(maps.{column}) "
        f"ORDER BY tag"
    ).fetchall()
    return [r["tag"] for r in rows if r["tag"]]
