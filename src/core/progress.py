"""每本书的阅读进度持久化。"""
from __future__ import annotations

import time
from dataclasses import dataclass, asdict
from typing import Any

from ..config import DATA_DIR, load_json, _atomic_write_json


@dataclass
class Progress:
    chapter: int = 0
    sentence: int = 0
    percent: float = 0.0
    updated_at: float = 0.0


class ProgressStore:
    def __init__(self) -> None:
        self.path = DATA_DIR / "progress.json"
        self._data: dict[str, dict[str, Any]] = {}
        raw = load_json(self.path, {})
        if isinstance(raw, dict):
            self._data = {k: v for k, v in raw.items() if isinstance(v, dict)}

    def save(self) -> None:
        _atomic_write_json(self.path, self._data)

    def get(self, book_id: str) -> Progress:
        d = self._data.get(book_id) or {}
        return Progress(
            chapter=int(d.get("chapter", 0)),
            sentence=int(d.get("sentence", 0)),
            percent=float(d.get("percent", 0.0)),
            updated_at=float(d.get("updated_at", 0.0)),
        )

    def set(self, book_id: str, chapter: int, sentence: int, percent: float) -> None:
        prev = self._data.get(book_id) or {}
        if (
            prev.get("chapter") == chapter
            and prev.get("sentence") == sentence
            and abs(float(prev.get("percent", 0.0)) - percent) < 1e-6
        ):
            return
        self._data[book_id] = {
            "chapter": int(chapter),
            "sentence": int(sentence),
            "percent": round(float(percent), 4),
            "updated_at": time.time(),
        }
        self.save()

    def forget(self, book_id: str) -> None:
        if self._data.pop(book_id, None) is not None:
            self.save()
