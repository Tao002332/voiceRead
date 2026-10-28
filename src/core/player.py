"""朗读控制器：分块合成 + 连续播放 + 句子级字幕。

为什么要分块：
  单句一合成 → 每句都要等一次合成，句间还有文件切换间隙，听着就是一卡一卡的。
  改成「块」：把 10~16 句（约 260 字，40~70 秒音频）拼成一段连续 wav，
  块内零间隙，句与句之间的停顿完全由 TTS 决定（更自然）。
  播放当前块的同时，后面两块已经在合成，所以块与块之间也几乎无感。

字幕精度：块内每句的时长由「实际 wav 采样数」精确算出，
播放位置反查句子 → 字幕/高亮始终对得上。

纯标点句（…… / —— / “……”）不合成，在块里插入 0.6 秒静音，
既保留停顿感，又不会让 TTS 报错。
"""
from __future__ import annotations

import hashlib
import os
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer

from ..config import DATA_DIR, Settings
from .library import Book
from .textproc import Prosody, analyze, clean_for_tts, is_speakable, prepare_chapter
from .tts.base import SpeechRequest
from .tts.service import SynthesisService

CHUNK_TARGET_CHARS = 260      # 每块目标字数
CHUNK_MAX_SENTENCES = 16      # 每块最多句数
PAUSE_ONLY_MS = 600           # 纯标点句插入的静音时长
SENTENCE_GAP_MS = 90          # 同一段内，句与句之间的呼吸感
PARAGRAPH_GAP_MS = 260        # 段落与段落之间
CHUNK_TAIL_GAP_MS = 320       # 块结束时留的尾巴，让下一块接得自然
CHUNK_DIR = DATA_DIR / "cache" / "chunks"
CHUNK_DIR.mkdir(parents=True, exist_ok=True)


def prune_chunk_cache(minutes: int = 10) -> int:
    """按最后修改时间清理拼块缓存，返回删除数量。"""
    cutoff = time.time() - max(1, minutes) * 60
    removed = 0
    for path in CHUNK_DIR.glob("*"):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


@dataclass
class _Chunk:
    index: int
    start: int                                  # 起始句（含）
    end: int                                    # 结束句（不含）
    keys: list[str] = field(default_factory=list)
    durations: list[float] = field(default_factory=list)
    starts: list[float] = field(default_factory=list)
    path: Path | None = None
    ready: bool = False
    building: bool = False
    pending: int = 0
    failed: int = 0

    @property
    def char_count(self) -> int:
        return self.end - self.start


class PlaybackController(QObject):
    sentenceChanged = Signal(int, str, object)   # index, text, Prosody
    sentenceSelected = Signal(int, str)          # 仅左键选中（不改变字幕/朗读）
    chapterChanged = Signal(int)
    playingChanged = Signal(bool)
    progressChanged = Signal(int, int)
    statusChanged = Signal(str)
    error = Signal(str)
    loaded = Signal()
    # 语音生成进度：(本段已完成句数, 本段总句数, 下一段已完成, 下一段总数)
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
        self.index = 0

        self.chunks: list[_Chunk] = []
        self._chunk_cursor = 0
        self._pending_play: tuple[int, int] | None = None   # (chunk_index, sentence_idx)
        self._key_waiters: dict[str, set[int]] = {}

        self._want_playing = False
        self._manual_stop = False
        self._fail_streak = 0
        self._pending_seek_ms: int | None = None
        self._awaiting_seek: int | None = None
        self._awaiting_since = 0.0
        self._seek_retries = 0
        self._last_emitted = -1
        self._reloading = False
        self._last_touch = 0.0

        # 纯标点句的停顿定时器（块还没准备好时也会用到）
        self._pause_timer = QTimer(self)
        self._pause_timer.setSingleShot(True)
        self._pause_timer.timeout.connect(self._on_pause_done)

        # 播放位置轮询：驱动字幕/高亮
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
        chapter = max(0, min(total - 1, chapter))
        self.chapter_index = chapter
        text = self.book.chapter_text(chapter)
        self.display_text, self.sentences = prepare_chapter(text)
        strength = self.settings.prosody_strength
        auto = self.settings.auto_prosody
        self.prosodies = [analyze(s.text, strength) if auto else Prosody() for s in self.sentences]
        self.index = max(0, min(max(0, len(self.sentences) - 1), sentence))
        self._last_emitted = -1
        self._build_chunks()
        self.chapterChanged.emit(chapter)
        self.loaded.emit()
        if not self.sentences:
            self.statusChanged.emit("本章没有可朗读内容")
            return
        self._emit_sentence()
        if autoplay:
            self.play()
        else:
            self._prepare(self._chunk_of(self.index))

    # ================================================================ 分块
    def _build_chunks(self) -> None:
        self.chunks = []
        i, n = 0, len(self.sentences)
        while i < n:
            chars, j = 0, i
            while j < n and (j - i) < CHUNK_MAX_SENTENCES:
                chars += len(self.sentences[j].text)
                j += 1
                if chars >= CHUNK_TARGET_CHARS:
                    break
            self.chunks.append(_Chunk(index=len(self.chunks), start=i, end=j))
            i = j

    def _chunk_of(self, sentence_idx: int) -> _Chunk | None:
        for chunk in self.chunks:
            if chunk.start <= sentence_idx < chunk.end:
                return chunk
        return None

    def _prepare(self, chunk: _Chunk | None) -> None:
        """为一块准备音频：命中缓存直接拼，否则逐句提交合成。"""
        if chunk is None or chunk.ready or chunk.building:
            return
        chunk.building = True
        chunk.keys = []
        chunk.pending = 0
        for si in range(chunk.start, chunk.end):
            text = self.sentences[si].text
            if not is_speakable(clean_for_tts(text)):
                chunk.keys.append("")          # 纯标点：块内插静音
                continue
            prosody = self.prosodies[si] if si < len(self.prosodies) else Prosody()
            req = self._build_request(text, prosody)
            key = self.service.key_for(req.text, req)
            chunk.keys.append(key)
            self.service.clear_failure(key)
            path = self.service.request(key, req)
            if path is None:
                chunk.pending += 1
                self._key_waiters.setdefault(key, set()).add(chunk.index)
        if chunk.pending == 0:
            self._finalize(chunk)
        self._emit_synth_progress()

    def _gap_after(self, chunk: _Chunk, offset: int) -> int:
        """这一句后面该留多长间隔。纯标点句本身已是静音，不再叠加。"""
        if chunk.keys[offset] == "":
            return 0
        si = chunk.start + offset
        nxt = si + 1
        if nxt >= chunk.end:
            return CHUNK_TAIL_GAP_MS
        if nxt < len(self.sentences) and self.sentences[nxt].start != self.sentences[si].end:
            return PARAGRAPH_GAP_MS          # 下一句是新段落
        return SENTENCE_GAP_MS

    def _finalize(self, chunk: _Chunk) -> None:
        """把各句缓存拼成一块连续 wav，并算出每句的精确起止时间。

        句与句之间插入呼吸间隔（段内 90ms / 段间 260ms / 块尾 320ms），
        时间轴同步计入，保证字幕与音频严格对齐。
        """
        pieces: list[tuple[bytes | None, float, int]] = []   # (音频, 时长s, 后置间隔ms)
        params: tuple[int, int, int] | None = None

        for offset, key in enumerate(chunk.keys):
            gap = self._gap_after(chunk, offset)
            if key == "":
                pieces.append((None, PAUSE_ONLY_MS / 1000.0, gap))   # 静音占位
                continue
            path = self.service.cached(key)
            if path is None:
                chunk.building = False
                chunk.pending = 1
                return
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

        # 文件名必须覆盖「间隔策略」：否则改了间隔参数还会复用旧块，时间轴就对不上
        gap_seq = ",".join(str(gap) for _f, _d, gap in pieces)
        sig = "|".join(chunk.keys) + f"|g{gap_seq}|p{PAUSE_ONLY_MS}"
        name = hashlib.sha1(sig.encode("utf-8")).hexdigest()[:20]
        out_path = CHUNK_DIR / f"{name}.wav"

        if not out_path.exists():
            tmp = out_path.with_suffix(".wav.part")
            try:
                with wave.open(str(tmp), "wb") as out:
                    out.setnchannels(nch)
                    out.setsampwidth(sw)
                    out.setframerate(sr)
                    for frames, dur, gap in pieces:
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
                chunk.building = False
                chunk.pending = 1
                return

        starts: list[float] = []
        durations: list[float] = []
        cursor = 0.0
        for _frames, dur, gap in pieces:
            starts.append(cursor)
            durations.append(dur)
            cursor += dur + gap / 1000.0

        chunk.path = out_path
        chunk.starts = starts
        chunk.durations = durations
        chunk.ready = True
        chunk.building = False
        chunk.pending = 0
        self._on_chunk_ready(chunk)
        self._emit_synth_progress()

    def _emit_synth_progress(self) -> None:
        """把「本段/下一段」的生成进度发给界面，避免用户以为卡住。"""
        def stat(chunk: _Chunk | None) -> tuple[int, int]:
            if chunk is None:
                return 0, 0
            total = len(chunk.keys) or (chunk.end - chunk.start)
            if chunk.ready:
                return total, total
            if not chunk.keys:
                return 0, total
            return max(0, total - chunk.pending), total

        cur = self._chunk_of(self.index)
        nxt = None
        if cur is not None and cur.index + 1 < len(self.chunks):
            nxt = self.chunks[cur.index + 1]
        cur_ready, cur_total = stat(cur)
        nxt_ready, nxt_total = stat(nxt)
        self.synthProgress.emit(cur_ready, cur_total, nxt_ready, nxt_total)

    def _on_chunk_ready(self, chunk: _Chunk) -> None:
        if self._pending_play and self._pending_play[0] == chunk.index:
            _, target = self._pending_play
            self._pending_play = None
            self.index = max(chunk.start, min(chunk.end - 1, target))
            self._start_chunk(chunk, self.index)
        elif self._want_playing and chunk.index == self._chunk_cursor:
            self._start_chunk(chunk, self.index)

    # ================================================================ 播放
    def _start_chunk(self, chunk: _Chunk, sentence_idx: int) -> None:
        if chunk.path is None or not chunk.path.exists():
            # 缓存可能已被「保留 10 分钟」策略清掉，重新生成这一段
            chunk.ready = False
            chunk.path = None
            self._prepare(chunk)
            if not chunk.ready:
                self._pending_play = (chunk.index, max(chunk.start, min(chunk.end - 1, sentence_idx)))
                self.statusChanged.emit("该段缓存已过期，正在重新生成…")
                return
        self._chunk_cursor = chunk.index
        offset = 0.0
        pos = max(chunk.start, min(chunk.end - 1, sentence_idx))
        k = pos - chunk.start
        if 0 <= k < len(chunk.starts):
            offset = chunk.starts[k]
        self._pending_seek_ms = int(offset * 1000) if offset > 0 else None
        self._awaiting_seek = None
        self._manual_stop = True
        self.player.stop()
        self._manual_stop = False
        self.player.setSource(QUrl.fromLocalFile(str(chunk.path)))
        if self._want_playing:
            # 关键顺序：有偏移时**不能**先 play——Windows/FFmpeg 后端会丢掉
            # 「播放刚开始时下发的 seek」，结果从块首播（看起来就是跳回本段前面）。
            # 统一在 _on_media_status(LoadedMedia) 里「先 setPosition 再 play」。
            if self._pending_seek_ms is None:
                self.player.play()
            self._pos_timer.start()
            self.statusChanged.emit("朗读中")
        self._prefetch_next(chunk.index)
        self._emit_sentence_at(pos)

    def _prefetch_next(self, chunk_index: int) -> None:
        for offset in (1, 2):
            idx = chunk_index + offset
            if idx < len(self.chunks):
                self._prepare(self.chunks[idx])

    def play(self) -> None:
        if not self.sentences:
            self.statusChanged.emit("没有可朗读的内容")
            return
        self._want_playing = True
        self.playingChanged.emit(True)
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PausedState and self._chunk_cursor >= 0:
            self.player.play()
            self._pos_timer.start()
            self._prefetch_next(self._chunk_cursor)
            return
        chunk = self._chunk_of(self.index)
        if chunk is None:
            return
        self._prepare(chunk)
        if chunk.ready:
            self._start_chunk(chunk, self.index)
        else:
            self._pending_play = (chunk.index, self.index)
            self.statusChanged.emit("合成中…（准备本段）")

    def pause(self) -> None:
        self._want_playing = False
        self._pause_timer.stop()
        self._pos_timer.stop()
        self.playingChanged.emit(False)
        self.player.pause()

    def stop(self) -> None:
        self._want_playing = False
        self._pause_timer.stop()
        self._pos_timer.stop()
        self._manual_stop = True
        self.player.stop()
        self._manual_stop = False
        self.playingChanged.emit(False)

    def toggle(self) -> None:
        if self._want_playing:
            self.pause()
        else:
            self.play()

    # ---------------------------------------------------------------- 跳转
    def next_sentence(self, autoplay: bool | None = None) -> None:
        self.seek_sentence(self.index + 1, autoplay)

    def prev_sentence(self, autoplay: bool | None = None) -> None:
        self.seek_sentence(self.index - 1, autoplay)

    @property
    def is_playing(self) -> bool:
        return self._want_playing

    def select_sentence(self, index: int) -> None:
        """左键选中某句。

        - 播放中：只发选中标记（正文里加下划线），**不暂停、不动播放位置**，
          朗读与悬浮字幕完全不受影响；
        - 暂停时：移动阅读位置并高亮，方便随后右键「从这一句开始朗读」。
        """
        if not self.sentences:
            return
        index = max(0, min(len(self.sentences) - 1, index))
        self.sentenceSelected.emit(index, self.sentences[index].text)
        if self._want_playing:
            return
        self.index = index
        chunk = self._chunk_of(index)
        if chunk is not None:
            self._prepare(chunk)

    def seek_sentence(self, index: int, autoplay: bool | None = None) -> None:
        if not self.sentences:
            return
        want = self._want_playing if autoplay is None else autoplay
        index = max(0, min(len(self.sentences) - 1, index))
        self.index = index
        self._emit_sentence()
        chunk = self._chunk_of(index)
        if chunk is None:
            return
        self._prepare(chunk)
        if want:
            self._want_playing = True
            self.playingChanged.emit(True)
            if chunk.ready:
                self._start_chunk(chunk, index)
            else:
                self._pending_play = (chunk.index, index)
                self.statusChanged.emit("合成中…")
        else:
            self._want_playing = False
            self._pos_timer.stop()
            if chunk.ready:
                self._chunk_cursor = chunk.index
                self._pending_seek_ms = int(chunk.starts[index - chunk.start] * 1000)
                self.player.setSource(QUrl.fromLocalFile(str(chunk.path)))
            else:
                self._prepare(chunk)

    def seek_chapter(self, chapter: int, autoplay: bool | None = None) -> None:
        want = self._want_playing if autoplay is None else autoplay
        self.load_chapter(chapter, 0, autoplay=want)

    # ================================================================ 内部
    def _reset_playback(self) -> None:
        self._pause_timer.stop()
        self._pos_timer.stop()
        self._pending_play = None
        self._pending_seek_ms = None
        self._awaiting_seek = None
        self._key_waiters.clear()
        self._chunk_cursor = 0
        self._fail_streak = 0
        self.player.stop()

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

    def _emit_sentence(self) -> None:
        if not self.sentences:
            return
        idx = min(self.index, len(self.sentences) - 1)
        prosody = self.prosodies[idx] if idx < len(self.prosodies) else Prosody()
        self.sentenceChanged.emit(idx, self.sentences[idx].text, prosody)
        self.progressChanged.emit(self.chapter_index, idx)
        self._last_emitted = idx

    def _emit_sentence_at(self, idx: int) -> None:
        self.index = max(0, min(len(self.sentences) - 1, idx))
        self._emit_sentence()

    def _verify_seek(self) -> None:
        """校验 seek 是否真的生效；被后端丢掉就重试，最多 2 次。"""
        target = self._awaiting_seek
        if target is None or not self._want_playing:
            return
        if self.player.position() + 400 >= target:
            self._awaiting_seek = None
            return
        if self._seek_retries < 2:
            self._seek_retries += 1
            self._awaiting_since = time.time()
            self.player.setPosition(target)
            QTimer.singleShot(400, self._verify_seek)
            return
        # 两次都跳不过去：退回块首播放，并让字幕跟随真实位置
        self._awaiting_seek = None
        self.statusChanged.emit("定位未生效，已从本段开头继续")

    def _on_tick(self) -> None:
        if not (0 <= self._chunk_cursor < len(self.chunks)):
            return
        chunk = self.chunks[self._chunk_cursor]
        if not chunk.ready:
            return
        # 媒体还没加载/正在加载时 position() 是 0 或旧值，不能拿来推句子
        if self.player.mediaStatus() in (
            QMediaPlayer.MediaStatus.NoMedia,
            QMediaPlayer.MediaStatus.LoadingMedia,
        ):
            return
        # 正在播放的块定期刷新时间戳，避免被「保留 N 分钟」的清理策略误删
        now = time.time()
        if chunk.path is not None and now - getattr(self, "_last_touch", 0.0) > 20:
            self._last_touch = now
            try:
                os.utime(chunk.path, None)
            except OSError:
                pass
        # 等待定位生效期间不要用播放位置回推句子，否则会把刚选中的句子覆盖掉
        if self._pending_seek_ms is not None:
            return
        # setPosition 是异步的：位置真正跳到目标附近之前，一律不采信 position()
        if self._awaiting_seek is not None:
            if self.player.position() + 250 >= self._awaiting_seek:
                self._awaiting_seek = None
            elif time.time() - self._awaiting_since > 3:
                # 3 秒还没跳过去，放弃等待，按实际位置走，避免字幕冻结
                self._awaiting_seek = None
            else:
                return
        pos = self.player.position() / 1000.0
        idx = chunk.start
        for k, start in enumerate(chunk.starts):
            # 到达句首才算切换（留 20ms 容差），避免字幕比声音早跳
            if pos >= start - 0.02:
                idx = chunk.start + k
            else:
                break
        if idx != self._last_emitted:
            self._emit_sentence_at(idx)

    def _start_pause(self, ms: int) -> None:
        self._pause_timer.stop()
        self._pause_timer.start(int(max(120, ms)))

    def _on_pause_done(self) -> None:
        if self._want_playing:
            self.next_sentence(autoplay=True)

    def _on_ready(self, key: str) -> None:
        waiters = self._key_waiters.get(key)
        if not waiters:
            return
        for ci in list(waiters):
            if not (0 <= ci < len(self.chunks)):
                continue
            chunk = self.chunks[ci]
            if chunk.ready:
                waiters.discard(ci)
                continue
            chunk.pending = max(0, chunk.pending - 1)
            waiters.discard(ci)
            if chunk.pending == 0 and not chunk.ready:
                self._finalize(chunk)
        self._emit_synth_progress()

    def _on_failed(self, key: str, message: str) -> None:
        waiters = self._key_waiters.get(key)
        if waiters:
            for ci in list(waiters):
                if 0 <= ci < len(self.chunks):
                    chunk = self.chunks[ci]
                    chunk.failed += 1
                    chunk.pending = max(0, chunk.pending - 1)
                    if chunk.pending == 0 and not chunk.ready:
                        self._finalize(chunk)
                waiters.discard(ci)
        self._fail_streak += 1
        if self._fail_streak >= 3:
            self._want_playing = False
            self.playingChanged.emit(False)
            self._pos_timer.stop()
            self.error.emit(message)
            self.statusChanged.emit("合成失败")
        else:
            self.statusChanged.emit(f"有句子合成失败，已跳过：{message[:36]}")
        self._emit_synth_progress()

    def _on_media_status(self, status) -> None:
        # 重建参数期间（apply_settings_changed 内部）忽略播放器回调，
        # 否则会在 stop() 触发的槽里再 setSource/play，Qt 多媒体可能死锁
        if self._reloading:
            return
        if status in (
            QMediaPlayer.MediaStatus.LoadedMedia,
            QMediaPlayer.MediaStatus.BufferedMedia,
        ):
            # 媒体就绪后再定位（setPosition 必须在加载后调用才生效），然后确保在播
            if self._pending_seek_ms is not None:
                target = self._pending_seek_ms
                self._pending_seek_ms = None
                if target > 0:
                    self.player.setPosition(target)
                    # 记下目标，位置真正跳过去之前不采信 position()
                    self._awaiting_seek = target
                    self._awaiting_since = time.time()
                    self._seek_retries = 0
                    QTimer.singleShot(400, self._verify_seek)
            if self._want_playing and self.player.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
                self.player.play()
                self._pos_timer.start()
            return
        if status != QMediaPlayer.MediaStatus.EndOfMedia:
            return
        if self._manual_stop or not self._want_playing:
            return
        # 关键：不在 QMediaPlayer 的槽里直接操作播放器，推迟到下一轮事件循环
        QTimer.singleShot(0, self._advance_chunk)

    def _advance_chunk(self) -> None:
        """当前块播完 → 接下一块 / 下一章。"""
        if self._reloading or not self._want_playing:
            return
        self._chunk_cursor = self._chunk_cursor if 0 <= self._chunk_cursor < len(self.chunks) else 0
        nxt = self._chunk_cursor + 1
        if nxt < len(self.chunks):
            chunk = self.chunks[nxt]
            self._prepare(chunk)
            if chunk.ready:
                self._start_chunk(chunk, chunk.start)
            else:
                self._pending_play = (nxt, chunk.start)
                self.statusChanged.emit("接续下一段…")
        elif self.book and self.chapter_index + 1 < self.book.chapter_count():
            self.load_chapter(self.chapter_index + 1, 0, autoplay=True)
        else:
            self._want_playing = False
            self._pos_timer.stop()
            self.playingChanged.emit(False)
            self.statusChanged.emit("已是全书末尾")

    def _on_player_error(self, error, error_string: str) -> None:
        if error == QMediaPlayer.Error.NoError:
            return
        if self._reloading:
            return
        self.error.emit(f"播放失败：{error_string}")
        self._want_playing = False
        self.playingChanged.emit(False)
        self._pos_timer.stop()

    # ================================================================ 设置联动
    def apply_settings_changed(self) -> None:
        """语速/音调/引擎变化后：从「当前这一句」开始用新参数播放。

        整块缓存会被作废并重建（缓存 key 里含语速），
        所以当前句之前的内容也会按新语速重新合成，回看时不会跳回旧语速。
        """
        if not self.sentences:
            return
        auto = self.settings.auto_prosody
        strength = self.settings.prosody_strength
        self.prosodies = [
            analyze(s.text, strength) if auto else Prosody() for s in self.sentences
        ]
        keep = self.index
        was_playing = self._want_playing
        still_audible = self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        self._reloading = True
        try:
            self._pending_play = None
            self._pending_seek_ms = None
            for chunk in self.chunks:
                chunk.ready = False
                chunk.building = False
                chunk.keys = []
            self._last_emitted = -1
            self.index = keep
            self._emit_sentence()

            target = self._chunk_of(keep)
            if was_playing and still_audible and target is not None:
                # 不打断正在播的声音：旧音频继续放，新参数的新块一就绪就从这一句接上
                self._prepare(target)
                if target.ready:
                    self._start_chunk(target, keep)
                else:
                    self._pending_play = (target.index, keep)
                    self.statusChanged.emit("按新参数重新生成中…（不打断当前播放）")
            else:
                self._reset_playback()
                self.seek_sentence(keep, autoplay=was_playing)
        finally:
            self._reloading = False

    def progress_percent(self) -> float:
        total_ch = self.book.chapter_count() if self.book else 0
        if total_ch <= 0:
            return 0.0
        inner = (self.index / max(1, len(self.sentences))) if self.sentences else 0.0
        return min(1.0, (self.chapter_index + inner) / total_ch)
