"""小说导入：TXT（多编码探测）与 EPUB（stdlib 解析）。"""
from __future__ import annotations

import hashlib
import html
import os
import posixpath
import re
import time
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from ..config import DATA_DIR, load_json, _atomic_write_json
from .textproc import split_chapters

BOOKS_DIR = DATA_DIR / "books"
BOOKS_DIR.mkdir(parents=True, exist_ok=True)

_ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "big5", "utf-16-le", "utf-16-be")


def detect_text(data: bytes) -> tuple[str, str]:
    """返回 (解码后的文本, 使用的编码)。gb18030 是 gbk/gb2312 的超集。"""
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig", "replace"), "utf-8-sig"
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        try:
            return data.decode("utf-16"), "utf-16"
        except UnicodeDecodeError:
            pass
    for enc in ("utf-8", "gb18030", "big5"):
        try:
            text = data.decode(enc)
        except UnicodeDecodeError:
            continue
        # 用常见汉字出现率做二次校验，避免 utf-8 侥幸解码出乱码
        if enc != "utf-8":
            sample = text[:4000]
            common = sum(sample.count(w) for w in ("的", "了", "是", "我", "他", "她", "。", "，"))
            if common < 5:
                continue
        return text, enc
    return data.decode("utf-8", "replace"), "utf-8(replace)"


class _HTMLExtract(HTMLParser):
    """从 XHTML 提取纯文本，块级标签转换行。"""

    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "section"}
    SKIP = {"script", "style", "head", "title"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0
        self.heading: str = ""
        self._in_heading = ""
        self._heading_buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self.SKIP:
            self._skip += 1
        if tag in self.BLOCK:
            self.parts.append("\n")
        if tag in ("h1", "h2", "h3") and not self.heading:
            self._in_heading = tag
            self._heading_buf = []

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        if tag in self.BLOCK:
            self.parts.append("\n")
        if tag == self._in_heading:
            text = re.sub(r"\s+", " ", "".join(self._heading_buf)).strip()
            if text:
                self.heading = text
            self._in_heading = ""

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        self.parts.append(data)
        if self._in_heading:
            self._heading_buf.append(data)

    def text(self) -> str:
        raw = "".join(self.parts)
        raw = html.unescape(raw)
        raw = re.sub(r"[ \t\r\f\v]+", " ", raw)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        return raw.strip()


def _epub_chapters(path: Path) -> tuple[str, list[tuple[str, str]]]:
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        container_name = "META-INF/container.xml"
        if container_name not in names:
            raise ValueError("不是有效的 EPUB：缺少 META-INF/container.xml")
        container = zf.read(container_name).decode("utf-8", "ignore")
        m = re.search(r'full-path="([^"]+)"', container)
        if not m:
            raise ValueError("EPUB 缺少 OPF 路径")
        opf_path = m.group(1)

        import xml.etree.ElementTree as ET

        opf_root = ET.fromstring(zf.read(opf_path))
        OPF = "{http://www.idpf.org/2007/opf}"
        DC = "{http://purl.org/dc/elements/1.1/}"

        title = ""
        for el in opf_root.iter(f"{DC}title"):
            if el.text and el.text.strip():
                title = el.text.strip()
                break

        manifest: dict[str, str] = {}
        for item in opf_root.iter(f"{OPF}item"):
            iid, href = item.get("id"), item.get("href")
            if iid and href:
                manifest[iid] = href
        spine = [ref.get("idref") for ref in opf_root.iter(f"{OPF}itemref") if ref.get("idref")]
        if not spine:
            spine = list(manifest.keys())

        base = posixpath.dirname(opf_path)
        chapters: list[tuple[str, str]] = []
        for idx, iid in enumerate(spine):
            href = manifest.get(iid or "")
            if not href:
                continue
            target = posixpath.normpath(posixpath.join(base, href.split("#")[0]))
            if target not in names:
                continue
            try:
                raw = zf.read(target).decode("utf-8", "ignore")
            except (OSError, KeyError):
                continue
            parser = _HTMLExtract()
            try:
                parser.feed(raw)
            except Exception:  # noqa: BLE001 - 坏 HTML 不应毁掉整本导入
                continue
            body = parser.text()
            if len(body.strip()) < 20:
                continue
            ch_title = parser.heading or f"第{idx + 1}节"
            chapters.append((ch_title, body))

    if not title:
        title = path.stem
    return title, chapters


@dataclass
class Book:
    id: str
    title: str
    path: str
    fmt: str
    char_count: int = 0
    added_at: float = field(default_factory=time.time)
    chapter_titles: list[str] = field(default_factory=list)
    _chapters: list[dict[str, str]] | None = field(default=None, repr=False, compare=False)

    # ---------- 序列化 ----------
    def meta(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "path": self.path,
            "fmt": self.fmt,
            "char_count": self.char_count,
            "added_at": self.added_at,
            "chapter_titles": self.chapter_titles,
        }

    @classmethod
    def from_meta(cls, data: dict[str, Any]) -> "Book":
        return cls(
            id=data["id"],
            title=data.get("title", "未命名"),
            path=data.get("path", ""),
            fmt=data.get("fmt", "txt"),
            char_count=data.get("char_count", 0),
            added_at=data.get("added_at", time.time()),
            chapter_titles=data.get("chapter_titles", []),
        )

    # ---------- 章节 ----------
    def _payload_path(self) -> Path:
        return BOOKS_DIR / f"{self.id}.json"

    def chapters(self) -> list[dict[str, str]]:
        if self._chapters is None:
            data = load_json(self._payload_path(), [])
            self._chapters = data if isinstance(data, list) else []
        return self._chapters

    def chapter_count(self) -> int:
        if self.chapter_titles:
            return len(self.chapter_titles)
        return len(self.chapters())

    def chapter_text(self, index: int) -> str:
        ch = self.chapters()
        if 0 <= index < len(ch):
            return ch[index].get("text", "")
        return ""

    def chapter_title(self, index: int) -> str:
        ch = self.chapters()
        if 0 <= index < len(ch):
            return ch[index].get("title", f"第{index + 1}章")
        return ""

    def save_payload(self, chapters: list[tuple[str, str]]) -> None:
        payload = [{"title": t, "text": b} for t, b in chapters]
        _atomic_write_json(self._payload_path(), payload)
        self._chapters = payload
        self.chapter_titles = [t for t, _ in chapters]
        self.char_count = sum(len(b) for _, b in chapters)


def _book_id(path: Path, size: int) -> str:
    h = hashlib.sha1(f"{path.resolve()}|{size}|{path.stat().st_mtime_ns}".encode("utf-8"))
    return h.hexdigest()[:16]


def import_book(path: str | os.PathLike[str]) -> Book:
    """导入 txt/epub，返回已落盘的 Book（章节正文单独存储）。"""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"文件不存在：{p}")
    suffix = p.suffix.lower()
    size = p.stat().st_size
    book = Book(id=_book_id(p, size), title=p.stem, path=str(p), fmt=suffix.lstrip("."))

    if suffix == ".epub":
        title, chapters = _epub_chapters(p)
        book.title = title or p.stem
    elif suffix in (".txt", ".text"):
        data = p.read_bytes()
        text, _enc = detect_text(data)
        chapters = split_chapters(text)
        if not chapters:
            raise ValueError("文件内容为空或无法解析")
    else:
        raise ValueError(f"暂不支持的格式：{suffix}（目前支持 .txt / .epub）")

    if not chapters:
        raise ValueError("未能解析出任何章节")
    book.save_payload(chapters)
    return book


class Library:
    """书架：元数据存 library.json，正文分文件存 books/。"""

    def __init__(self) -> None:
        self.path = DATA_DIR / "library.json"
        self.books: list[Book] = []
        self.load()

    def load(self) -> None:
        raw = load_json(self.path, {"books": []})
        items = raw.get("books", []) if isinstance(raw, dict) else []
        self.books = [Book.from_meta(m) for m in items if isinstance(m, dict) and m.get("id")]

    def save(self) -> None:
        _atomic_write_json(self.path, {"books": [b.meta() for b in self.books]})

    def get(self, book_id: str) -> Book | None:
        for b in self.books:
            if b.id == book_id:
                return b
        return None

    def add(self, book: Book) -> Book:
        existed = self.get(book.id)
        if existed:
            existed.chapter_titles = book.chapter_titles
            existed.char_count = book.char_count
            existed.title = book.title
            self.save()
            return existed
        self.books.insert(0, book)
        self.save()
        return book

    def remove(self, book_id: str) -> None:
        self.books = [b for b in self.books if b.id != book_id]
        self.save()
        payload = BOOKS_DIR / f"{book_id}.json"
        if payload.exists():
            try:
                payload.unlink()
            except OSError:
                pass
