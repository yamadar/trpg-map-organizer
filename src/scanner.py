"""ターゲットフォルダから画像を列挙し、要解析ファイルを判定する."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator


@dataclass
class FoundImage:
    path: Path
    size: int
    mtime: float

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def path_str(self) -> str:
        return str(self.path)


def iter_images(root: Path, extensions: Iterable[str]) -> Iterator[FoundImage]:
    """root 配下の画像ファイルを再帰的に列挙する.

    extensions: ".png" のような形式 (小文字)。
    """
    ext_set = {e.lower() if e.startswith(".") else f".{e.lower()}" for e in extensions}
    if not root.exists():
        return
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in ext_set:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        yield FoundImage(path=path, size=stat.st_size, mtime=stat.st_mtime)


def quick_hash(path: Path, *, chunk: int = 65536) -> str:
    """ファイル先頭の最大 chunk バイトから簡易ハッシュを取る.

    完全なハッシュは大きい画像では遅いため、変更検出には mtime+size を主とし、
    本ハッシュは補助的に保存する。
    """
    h = hashlib.sha1()
    with path.open("rb") as f:
        data = f.read(chunk)
        if data:
            h.update(data)
    return h.hexdigest()


def needs_reanalyze(
    found: FoundImage,
    *,
    existing_size: int | None,
    existing_mtime: float | None,
) -> bool:
    """既存レコードと比較して再解析が必要か判定する."""
    if existing_size is None or existing_mtime is None:
        return True
    if found.size != existing_size:
        return True
    # mtime は浮動小数のため小さな誤差を許容
    if abs(found.mtime - existing_mtime) > 1.0:
        return True
    return False
