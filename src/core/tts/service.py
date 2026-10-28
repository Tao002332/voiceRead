"""语音合成服务：缓存 + 后台线程池 + 信号回传。

UI/播放器只和这里打交道：
    svc.request(key, req)        提交（已在缓存则立即 ready）
    svc.cached(key) -> Path|None 查缓存
    svc.ready / svc.failed       合成完成信号（key 回传，调用方自己映射到句子）
"""
from __future__ import annotations

import hashlib
import queue
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from ...config import CACHE_DIR
from ..audio_utils import trim_wav_silence
from .base import SpeechRequest, TTSEngine
from .registry import build_engine


def _key(text: str, req: SpeechRequest, engine: TTSEngine) -> str:
    raw = f"{engine.cache_tag(req)}|{req.rate:.3f}|{req.pitch}|{req.style}|{text}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


class SynthesisService(QObject):
    ready = Signal(str)          # key
    failed = Signal(str, str)    # key, message
    engine_changed = Signal(str)  # engine id

    def __init__(self, settings, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.engine: TTSEngine = build_engine(settings.engine_id, settings)
        self._pending: set[str] = set()
        self._failed: set[str] = set()
        self._lock = threading.Lock()

        # 用 daemon 工作线程而不是 ThreadPoolExecutor：
        # 后者是非 daemon 线程，退出时 Python 会等合成跑完，
        # 一旦服务没起、某次合成卡在超时上，进程就退不掉。
        self._queue: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._workers: list[threading.Thread] = []
        for i in range(2):
            worker = threading.Thread(
                target=self._worker_loop, name=f"tts-worker-{i}", daemon=True
            )
            worker.start()
            self._workers.append(worker)

    # ---------------- 引擎 ----------------
    def switch_engine(self, engine_id: str) -> None:
        self.settings.engine_id = engine_id
        self.engine = build_engine(engine_id, self.settings)
        self._pending.clear()
        self._failed.clear()
        self.engine_changed.emit(engine_id)

    def reload_engine(self) -> None:
        self.engine = build_engine(self.settings.engine_id, self.settings)
        self._pending.clear()
        self._failed.clear()

    def probe(self) -> tuple[bool, str]:
        try:
            return self.engine.probe()
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    # ---------------- 缓存 ----------------
    def key_for(self, text: str, req: SpeechRequest) -> str:
        return _key(text, req, self.engine)

    def cached(self, key: str) -> Path | None:
        for p in CACHE_DIR.glob(f"{key}.*"):
            if p.is_file() and p.stat().st_size > 512:
                return p
        return None

    def request(self, key: str, req: SpeechRequest) -> Path | None:
        """命中缓存直接返回路径；否则提交后台合成并返回 None。"""
        hit = self.cached(key)
        if hit:
            return hit
        if key in self._failed:
            self.failed.emit(key, "上次合成失败，请检查引擎设置")
            return None
        with self._lock:
            if key in self._pending:
                return None
            self._pending.add(key)
        engine = self.engine
        suffix = engine.file_suffix
        target = CACHE_DIR / f"{key}{suffix}"
        self._queue.put((key, req, engine, target))
        return None

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.4)
            except queue.Empty:
                continue
            if item is None:
                break
            key, req, engine, target = item
            try:
                self._work(key, req, engine, target)
            except Exception:  # noqa: BLE001 - 单条任务异常不该弄死工作线程
                continue

    def _work(self, key: str, req: SpeechRequest, engine: TTSEngine, target: Path) -> None:
        try:
            tmp = target.with_suffix(target.suffix + ".part")
            engine.synthesize(req, tmp)
            if target.suffix.lower() == ".wav":
                # 裁掉句首静音/换气，避免「开口前吸一口气」
                trim_wav_silence(tmp)
            tmp.replace(target)
            ok, err = True, ""
        except Exception as exc:  # noqa: BLE001
            ok, err = False, str(exc)
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
        with self._lock:
            self._pending.discard(key)
            if not ok:
                self._failed.add(key)
        if ok:
            self.ready.emit(key)
        else:
            self.failed.emit(key, err)

    def clear_failure(self, key: str) -> None:
        self._failed.discard(key)

    # ---------------- 维护 ----------------
    def prune_cache(self, limit_mb: int | None = None) -> int:
        """按最近使用时间清理缓存，返回删除的文件数。"""
        limit = (limit_mb if limit_mb is not None else self.settings.cache_limit_mb) * 1024 * 1024
        files = [p for p in CACHE_DIR.glob("*") if p.is_file()]
        total = sum(p.stat().st_size for p in files)
        if total <= limit:
            return 0
        files.sort(key=lambda p: p.stat().st_mtime)
        removed = 0
        for p in files:
            if total <= limit:
                break
            try:
                size = p.stat().st_size
                p.unlink()
                total -= size
                removed += 1
            except OSError:
                continue
        return removed

    def cache_size_mb(self) -> float:
        total = sum(p.stat().st_size for p in CACHE_DIR.glob("*") if p.is_file())
        return round(total / 1024 / 1024, 1)

    def prune_by_age(self, minutes: int | None = None) -> int:
        """按最后修改时间清理合成缓存（默认保留 10 分钟），返回删除数量。"""
        keep = minutes if minutes is not None else int(getattr(self.settings, "cache_retention_minutes", 10))
        cutoff = time.time() - max(1, keep) * 60
        removed = 0
        for path in CACHE_DIR.glob("*"):
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                continue
        return removed

    def shutdown(self) -> None:
        """停止接收新任务，并唤醒工作线程退出（不阻塞调用方）。"""
        self._stop.set()
        for _ in self._workers:
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                break
