"""朗读控制器：句子级预加载队列 + 就绪即播。

核心模型（和之前"整块就绪才播"不同）：
  * 预加载队列：从当前句往后，按 settings.prefetch 的深度把句子排进队列并合成。
  * 播放队列：只消费"已就绪"的条目，**不等队列填满**——
    哪怕只有 1 条生成好了，也可以立刻开播。
  * 每播完一段，把消费掉的条目从队列移除，并继续把后面的句子补进队列。
  * 一次播放会把「连续已就绪」的若干句拼成一个音频段（减少句间切换缝隙），
    段内每句的起止时间由实际采样数算出，字幕因此严格对齐。

UI 需要的数据：
  queueStats(ready, depth, running, played)  —— 底栏实时数量
  recent_items(10)                           —— 最近 10 条各自的生成耗时
"""
from __future__ import annotations

import hashlib
import os
import time
import wave
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer

from ..config import DATA_DIR, Settings
from .library import Book
from .textproc import Prosody, analyze, clean_for_tts, is_speakable, prepare_chapter
from .tts.base import SpeechRequest
from .tts.service import SynthesisService

PAUSE_ONLY_MS = 600           # 纯标点句：块内插静音
SENTENCE_GAP_MS = 90          # 同段内句间呼吸
PARAGRAPH_GAP_MS = 260        # 段间停顿
SEGMENT_TAIL_GAP_MS = 160     # 播放段尾部留白
MAX_SEGMENT_SENTENCES = 8     # 一段最多拼几句（太多会拖慢首播）
SEGMENT_DIR = DATA_DIR / "cache" / "chunks"
SEGMENT_DIR.mkdir(parents=True, exist_ok=True)


def prune_chunk_cache(minutes: int = 10) -> int:
    """按最后修改时间清理拼段缓存。"""
    cutoff = time.time() - max(1, minutes) * 60
    removed = 0
    for path in SEGMENT_DIR.glob("*"):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


@dataclass
class _Item:
    """预加载队列里的一条（= 一句话）。"""
    index: int
    key: str = ""
    chars: int = 0
    state: str = "pending"      # pending / running / ready / failed
    duration: float = 0.0
    cost: float = 0.0           # 生成耗时（秒）
    started_at: float = 0.0
    finished_at: float = 0.0
    error: str = ""

    @property
    def state_label(self) -> str:
        return {
            "pending": "排队中",
            "running": "生成中",
            "ready": "就绪",
            "failed": "失败（按静音处理）",
        }.get(self.state, self.state)


class PlaybackController(QObject):
    sentenceChanged = Signal(int, str, object)   # index, text, Prosody
    sentenceSelected = Signal(int, str)          # 仅左键选中（不改变字幕/朗读）
    loadingChanged = Signal(bool)                # True = 当前句还没有可播音频
    chapterChanged = Signal(int)
    playingChanged = Signal(bool)
    progressChanged = Signal(int, int)
    statusChanged = Signal(str)
    error = Signal(str)
    loaded = Signal()
    # 底栏统计：(已就绪待播, 预加载队列深度, 生成中, 已播条数)
    synthProgress = Signal(int, int, int, int)

    def __init__(self, settings: Settings, service: SynthesisService, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.service = service
        self.book: Book | None = None
        self.chapter_index = 0
        self.sentences: list = []
        self.prosodies: list[Prosody] = []
        self.display_text = ""
        self.index = 0                       # 当前显示（播放头）的句子
        self._queue_head = 0                 # 预加载队列的消费起点（与显示位置解耦）

        self._items: dict[int, _Item] = {}
        # 一个缓存 key 可能对应多个句子（重复台词/重复的"……"），
        # 所以 key → 下标集合；否则重复句只会有一句被标记就绪，另一句永远卡在生成中
        self._key_to_index: dict[str, set[int]] = {}
        self._played_total = 0

        self._want_playing = False
        self._manual_stop = False
        self._reloading = False
        self._last_loading: bool | None = None
        self._last_emitted = -1

        # 当前正在播放的这一段
        self._segment: list[int] = []
        self._segment_starts: list[float] = []
        self._segment_durs: list[float] = []
        self._segment_path: Path | None = None
        self._segment_playing = False

        self._pos_timer = QTimer(self)
        self._pos_timer.setInterval(120)
        self._pos_timer.timeout.connect(self._on_tick)

        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.audio.setVolume(1.0)
        self.player.setAudioOutput(self.audio)
        self.player.mediaStatusChanged.connect(self._on_media_status)
        self.player.errorOccurred.connect(self._on_player_error)

        self.service.ready.connect(self._on_ready)
        self.service.failed.connect(self._on_failed)

    # ================================================================ 载入
    def load(self, book: Book, chapter: int = 0, sentence: int = 0, autoplay: bool = False) -> None:
        self.stop()
        self.book = book
        self.load_chapter(chapter, sentence, autoplay=autoplay)

    def load_chapter(self, chapter: int, sentence: int = 0, autoplay: bool = False) -> None:
        if self.book is None:
            return
        total = self.book.chapter_count()
        if total <= 0:
            self.error.emit("这本书没有解析出章节")
            return
        self._reset_playback()
        self.chapter_index = max(0, min(total - 1, chapter))
        text = self.book.chapter_text(self.chapter_index)
        self.display_text, self.sentences = prepare_chapter(text)
        auto = self.settings.auto_prosody
        strength = self.settings.prosody_strength
        self.prosodies = [analyze(s.text, strength) if auto else Prosody() for s in self.sentences]
        self.index = max(0, min(max(0, len(self.sentences) - 1), sentence))
        self._last_emitted = -1
        self.chapterChanged.emit(self.chapter_index)
        self.loaded.emit()
        if not self.sentences:
            self.statusChanged.emit("本章没有可朗读内容")
            return
        self._emit_sentence(self.index)
        self._pump()
        if autoplay:
            self.play()

    def set_book_position(self, chapter: int, sentence: int) -> None:
        self.load_chapter(chapter, sentence, autoplay=False)

    # ================================================================ 队列
    def _depth(self) -> int:
        try:
            depth = int(self.settings.prefetch or 10)
        except (TypeError, ValueError):
            depth = 10
        return max(1, min(40, depth))

    def _queue_size(self) -> int:
        return len(self._items)

    def _ready_ahead(self) -> int:
        """从当前句开始，连续已就绪的条数（= 播放队列里能立刻消费的量）。"""
        count = 0
        i = self.index
        while i < len(self.sentences):
            item = self._items.get(i)
            if item is None or item.state != "ready":
                break
            count += 1
            i += 1
        return count

    def _running_count(self) -> int:
        return sum(1 for it in self._items.values() if it.state == "running")

    def _make_item(self, index: int) -> _Item:
        text = self.sentences[index].text
        item = _Item(index=index, chars=len(text))
        if not is_speakable(clean_for_tts(text)):
            item.state = "ready"
            item.duration = PAUSE_ONLY_MS / 1000.0
            return item
        prosody = self.prosodies[index] if index < len(self.prosodies) else Prosody()
        req = self._build_request(text, prosody)
        item.key = self.service.key_for(req.text, req)
        return item

    def _pump(self) -> None:
        """把 [队列头, 队列头+depth) 补进预加载队列；已就绪的直接标记。"""
        if not self.sentences:
            return
        depth = self._depth()
        head = self._queue_head
        end = min(len(self.sentences), head + depth)
        for i in range(head, end):
            item = self._items.get(i)
            if item is None:
                item = self._make_item(i)
                self._items[i] = item
            if item.state in ("ready", "failed"):
                continue
            if item.key == "":
                item.state = "ready"
                item.duration = PAUSE_ONLY_MS / 1000.0
                continue
            cached = self.service.cached(item.key)
            if cached is not None:
                item.state = "ready"
                item.duration = self._wav_duration(cached)
                continue
            if item.state == "running":
                continue
            prosody = self.prosodies[i] if i < len(self.prosodies) else Prosody()
            req = self._build_request(self.sentences[i].text, prosody)
            item.state = "running"
            item.started_at = time.time()
            self._key_to_index.setdefault(item.key, set()).add(i)
            self.service.clear_failure(item.key)
            path = self.service.request(item.key, req)
            if path is not None:                      # 同步命中
                item.state = "ready"
                item.duration = self._wav_duration(path)
                item.finished_at = time.time()
                item.cost = max(0.0, item.finished_at - item.started_at)
        # 清掉窗口之外的条目（已经在前面播过的会在 _advance 里移除）
        for key in [k for k in self._items if k < head or k >= end]:
            item = self._items.pop(key, None)
            if item is not None and item.key:
                bucket = self._key_to_index.get(item.key)
                if bucket is not None:
                    bucket.discard(key)
                    if not bucket:
                        self._key_to_index.pop(item.key, None)
        self._emit_stats()

    def _emit_stats(self) -> None:
        ready = self._ready_ahead()
        depth = self._depth()
        running = self._running_count()
        self.synthProgress.emit(ready, depth, running, self._played_total)
        loading = ready == 0
        if loading != self._last_loading:
            self._last_loading = loading
            self.loadingChanged.emit(loading)

    @property
    def is_loading(self) -> bool:
        return self._ready_ahead() == 0

    @property
    def is_playing(self) -> bool:
        return self._want_playing

    def recent_items(self, limit: int = 10) -> list[dict]:
        """最近生成过的条目（按开始时间倒序），供 UI 展示耗时。"""
        items = [it for it in self._items.values() if it.started_at > 0]
        items.sort(key=lambda it: it.started_at, reverse=True)
        out: list[dict] = []
        for it in items[:limit]:
            text = self.sentences[it.index].text if it.index < len(self.sentences) else ""
            out.append({
                "index": it.index + 1,
                "state": it.state_label,
                "chars": it.chars,
                "cost": round(it.cost, 2),
                "duration": round(it.duration, 2),
                "text": text[:24],
            })
        return out

    # ================================================================ 播放
    def play(self) -> None:
        if not self.sentences:
            self.statusChanged.emit("没有可朗读的内容")
            return
        self._want_playing = True
        self.playingChanged.emit(True)
        if self._segment_playing:
            if self.player.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
                self.player.play()
                self._pos_timer.start()
            return
        # 已经处于暂停：只有「显示位置仍在本段里」才续播，否则必须重新拼段，
        # 不然会出现续播旧音频、字幕却是新句子的错位
        if (
            self._segment_path is not None
            and self.index in self._segment
            and self.player.playbackState() == QMediaPlayer.PlaybackState.PausedState
        ):
            self.player.play()
            self._pos_timer.start()
            self._pump()
            return
        if self._segment_path is not None and self.index not in self._segment:
            self._stop_segment()
        self._pump()
        self._try_play()

    def pause(self) -> None:
        self._want_playing = False
        self._pos_timer.stop()
        self.playingChanged.emit(False)
        self.player.pause()
        if self.is_loading:
            self.statusChanged.emit("已暂停（语音仍在生成，就绪后不会自动播放）")

    def stop(self) -> None:
        self._want_playing = False
        self._segment_playing = False
        self._pos_timer.stop()
        self._manual_stop = True
        self.player.stop()
        self._manual_stop = False
        self.playingChanged.emit(False)

    def toggle(self) -> None:
        if self._want_playing:
            self.pause()
        elif self.is_loading:
            self.statusChanged.emit("当前句语音还没生成好，稍等一下再点播放")
        else:
            self.play()

    # ---------------------------------------------------------------- 跳转
    def next_sentence(self, autoplay: bool | None = None) -> None:
        self.seek_sentence(self.index + 1, autoplay)

    def prev_sentence(self, autoplay: bool | None = None) -> None:
        self.seek_sentence(self.index - 1, autoplay)

    def select_sentence(self, index: int) -> None:
        """左键选中：播放中只做标记；暂停时移动阅读位置。"""
        if not self.sentences:
            return
        index = max(0, min(len(self.sentences) - 1, index))
        self.sentenceSelected.emit(index, self.sentences[index].text)
        if self._want_playing:
            return
        # 暂停时换位置：必须丢弃已拼好的段，否则再点播放会续播旧段的音频，
        # 而字幕/高亮已经跳到新句子——这就是"音字不符"的来源之一
        self._stop_segment()
        self.index = index
        self._queue_head = index
        self._pump()

    def seek_sentence(self, index: int, autoplay: bool | None = None) -> None:
        if not self.sentences:
            return
        want = self._want_playing if autoplay is None else autoplay
        index = max(0, min(len(self.sentences) - 1, index))
        self._stop_segment()
        self.index = index
        self._queue_head = index
        self._emit_sentence(index)
        self._pump()
        if want:
            self._want_playing = True
            self.playingChanged.emit(True)
            self._try_play()
        else:
            self._want_playing = False
            self.playingChanged.emit(False)

    def seek_chapter(self, chapter: int, autoplay: bool | None = None) -> None:
        want = self._want_playing if autoplay is None else autoplay
        self.load_chapter(chapter, 0, autoplay=want)

    # ================================================================ 内部
    def _reset_playback(self) -> None:
        self._stop_segment()
        self._items.clear()
        self._key_to_index.clear()
        self._played_total = 0

    def _stop_segment(self) -> None:
        self._pos_timer.stop()
        self._segment_playing = False
        self._segment = []
        self._segment_starts = []
        self._segment_durs = []
        self._segment_path = None
        self._manual_stop = True
        self.player.stop()
        self._manual_stop = False

    def _build_request(self, text: str, prosody: Prosody) -> SpeechRequest:
        rate = max(0.5, min(2.0, self.settings.speed * prosody.rate))
        pitch = (self.settings.pitch + prosody.pitch) if self.settings.auto_prosody else self.settings.pitch
        return SpeechRequest(
            text=clean_for_tts(text) or text,
            rate=round(rate, 3),
            pitch=int(max(-60, min(60, pitch))),
            voice=self._current_voice(),
            style=prosody.kind,
        )

    def _current_voice(self) -> str:
        engine_id = self.settings.engine_id
        if engine_id == "edge":
            return self.settings.edge_voice
        if engine_id == "sapi":
            return self.settings.sapi_voice
        if engine_id == "mambo":
            return str(self.settings.mambo.active_preset)
        return ""

    def _wav_duration(self, path: Path) -> float:
        try:
            with wave.open(str(path), "rb") as wf:
                return wf.getnframes() / float(wf.getframerate() or 1)
        except (wave.Error, OSError):
            return 0.0

    def _emit_sentence(self, index: int) -> None:
        if not self.sentences:
            return
        index = min(index, len(self.sentences) - 1)
        self.index = index
        prosody = self.prosodies[index] if index < len(self.prosodies) else Prosody()
        self.sentenceChanged.emit(index, self.sentences[index].text, prosody)
        self.progressChanged.emit(self.chapter_index, index)
        self._last_emitted = index

    def _gap_after(self, index: int) -> int:
        item = self._items.get(index)
        if item is not None and item.key == "":
            return 0
        nxt = index + 1
        if nxt >= len(self.sentences):
            return SEGMENT_TAIL_GAP_MS
        if self.sentences[nxt].start != self.sentences[index].end:
            return PARAGRAPH_GAP_MS
        return SENTENCE_GAP_MS

    def _build_segment(self, indexes: list[int]) -> tuple[Path, list[float], list[float]] | None:
        """把连续若干句拼成一个可播放的 wav，返回 (路径, 每句起点, 每句时长)。"""
        pieces: list[tuple[bytes | None, float, int]] = []
        params: tuple[int, int, int] | None = None
        for pos, idx in enumerate(indexes):
            item = self._items.get(idx)
            if item is None or item.state not in ("ready", "failed"):
                return None
            gap = self._gap_after(idx) if pos < len(indexes) - 1 else SEGMENT_TAIL_GAP_MS
            if item.state == "failed" or item.key == "":
                pieces.append((None, PAUSE_ONLY_MS / 1000.0, gap))
                continue
            path = self.service.cached(item.key)
            if path is None:
                return None
            try:
                with wave.open(str(path), "rb") as wf:
                    nch, sw, sr = wf.getnchannels(), wf.getsampwidth(), wf.getframerate()
                    frames = wf.readframes(wf.getnframes())
                    dur = wf.getnframes() / float(sr)
            except (wave.Error, OSError):
                pieces.append((None, PAUSE_ONLY_MS / 1000.0, gap))
                continue
            if params is None:
                params = (nch, sw, sr)
            elif params != (nch, sw, sr):
                pieces.append((None, PAUSE_ONLY_MS / 1000.0, gap))
                continue
            pieces.append((frames, dur, gap))

        if params is None:
            params = (1, 2, 32000)
        nch, sw, sr = params

        def silence(ms: int) -> bytes:
            return b"\x00" * (int(sr * ms / 1000) * nch * sw)

        sig = "|".join(
            f"{idx}:{self._items[idx].key}" for idx in indexes if idx in self._items
        )
        name = hashlib.sha1(sig.encode("utf-8")).hexdigest()[:20]
        out_path = SEGMENT_DIR / f"{name}.wav"
        if not out_path.exists():
            tmp = out_path.with_suffix(".wav.part")
            try:
                with wave.open(str(tmp), "wb") as out:
                    out.setnchannels(nch)
                    out.setsampwidth(sw)
                    out.setframerate(sr)
                    for frames, _dur, gap in pieces:
                        out.writeframes(frames if frames is not None else silence(PAUSE_ONLY_MS))
                        if gap > 0:
                            out.writeframes(silence(gap))
                tmp.replace(out_path)
            except (OSError, wave.Error):
                try:
                    if tmp.exists():
                        tmp.unlink()
                except OSError:
                    pass
                return None

        starts: list[float] = []
        durs: list[float] = []
        cursor = 0.0
        for _frames, dur, gap in pieces:
            starts.append(cursor)
            durs.append(dur)
            cursor += dur + gap / 1000.0
        return out_path, starts, durs

    def _try_play(self) -> None:
        """消费预加载队列里已就绪的条目开始播放（不等队列满）。"""
        if not self._want_playing or self._segment_playing or self._reloading:
            return
        indexes: list[int] = []
        i = self.index
        while i < len(self.sentences) and len(indexes) < MAX_SEGMENT_SENTENCES:
            item = self._items.get(i)
            if item is None or item.state not in ("ready", "failed"):
                break
            indexes.append(i)
            i += 1
        if not indexes:
            self.statusChanged.emit("等待语音生成…")
            return
        built = self._build_segment(indexes)
        if built is None:
            self.statusChanged.emit("等待语音生成…")
            return
        path, starts, durs = built
        self._segment = indexes
        self._segment_starts = starts
        self._segment_durs = durs
        self._segment_path = path
        self._segment_playing = True
        self.player.stop()
        self.player.setSource(QUrl.fromLocalFile(str(path)))
        self.player.play()
        self._pos_timer.start()
        self.statusChanged.emit(f"朗读中（{len(indexes)} 句 / {durs[-1] + starts[-1]:.0f}s）")
        self._emit_sentence(indexes[0])

    def _on_tick(self) -> None:
        if not self._segment_playing or not self._segment_starts:
            return
        if self.player.mediaStatus() in (
            QMediaPlayer.MediaStatus.NoMedia,
            QMediaPlayer.MediaStatus.LoadingMedia,
        ):
            return
        segment = self._segment
        base = segment[0] if segment else 0
        pos = self.player.position() / 1000.0
        idx = base
        for k, start in enumerate(self._segment_starts):
            if pos >= start - 0.02:
                idx = segment[k]
            else:
                break
        if idx != self._last_emitted:
            self._emit_sentence(idx)

    def _on_ready(self, key: str) -> None:
        indexes = self._key_to_index.get(key)
        if not indexes:
            return
        path = self.service.cached(key)
        if path is None:
            return
        duration = self._wav_duration(path)
        now = time.time()
        for i in list(indexes):
            item = self._items.get(i)
            if item is None or item.state == "ready":
                continue
            item.state = "ready"
            item.duration = duration
            item.finished_at = now
            item.cost = max(0.0, now - item.started_at) if item.started_at else 0.0
        self._emit_stats()
        if self._want_playing and not self._segment_playing:
            self._try_play()

    def _on_failed(self, key: str, message: str) -> None:
        for i in list(self._key_to_index.get(key) or ()):
            item = self._items.get(i)
            if item is not None and item.state != "ready":
                item.state = "failed"
                item.error = message
                item.finished_at = time.time()
                item.cost = max(0.0, item.finished_at - item.started_at) if item.started_at else 0.0
        self.statusChanged.emit(f"有句子合成失败，按停顿处理：{message[:36]}")
        self._emit_stats()
        if self._want_playing and not self._segment_playing:
            self._try_play()

    def _on_media_status(self, status) -> None:
        if self._reloading:
            return
        if status != QMediaPlayer.MediaStatus.EndOfMedia:
            return
        if self._manual_stop or not self._want_playing:
            return
        QTimer.singleShot(0, self._advance)

    def _advance(self) -> None:
        """一段播完：把消费掉的条目移出队列，补队列，再播下一段。"""
        if self._reloading or not self._want_playing:
            return
        consumed = list(self._segment)
        self._segment = []
        self._segment_playing = False
        for idx in consumed:
            item = self._items.pop(idx, None)
            if item is not None and item.key:
                bucket = self._key_to_index.get(item.key)
                if bucket is not None:
                    bucket.discard(idx)
                    if not bucket:
                        self._key_to_index.pop(item.key, None)
        self._played_total += len(consumed)
        if consumed:
            self.index = consumed[-1] + 1
            self._queue_head = self.index
        if self.index >= len(self.sentences):
            if self.book and self.chapter_index + 1 < self.book.chapter_count():
                self.load_chapter(self.chapter_index + 1, 0, autoplay=True)
            else:
                self._want_playing = False
                self._pos_timer.stop()
                self.playingChanged.emit(False)
                self.statusChanged.emit("已是全书末尾")
            return
        self._emit_sentence(self.index)
        self._pump()
        self._try_play()

    def _on_player_error(self, error, error_string: str) -> None:
        if error == QMediaPlayer.Error.NoError or self._reloading:
            return
        self.error.emit(f"播放失败：{error_string}")
        self._want_playing = False
        self._segment_playing = False
        self.playingChanged.emit(False)
        self._pos_timer.stop()

    # ================================================================ 设置联动
    def apply_settings_changed(self) -> None:
        """语速/音调/引擎变化：从当前句起用新参数重新排队。"""
        if not self.sentences:
            return
        auto = self.settings.auto_prosody
        strength = self.settings.prosody_strength
        self.prosodies = [analyze(s.text, strength) if auto else Prosody() for s in self.sentences]
        keep = self.index
        was_playing = self._want_playing
        self._reloading = True
        try:
            self._stop_segment()
            self._items.clear()
            self._key_to_index.clear()
            self._last_emitted = -1
            self.index = keep
            self._queue_head = keep
            self._emit_sentence(keep)
            self._pump()
            if was_playing:
                self._want_playing = True
                self.playingChanged.emit(True)
                self._try_play()
        finally:
            self._reloading = False
            self._emit_stats()

    def progress_percent(self) -> float:
        total_ch = self.book.chapter_count() if self.book else 0
        if total_ch <= 0:
            return 0.0
        inner = (self.index / max(1, len(self.sentences))) if self.sentences else 0.0
        return min(1.0, (self.chapter_index + inner) / total_ch)
