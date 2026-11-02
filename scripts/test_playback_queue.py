"""回归测试：预加载队列流水线（就绪即播、消费即出队、不足即补、明细耗时）。

覆盖之前踩过的坑：
  * 不再"整块就绪才播"——只要有 1 条就绪就能开播
  * 播完把消费掉的条目移出队列，并继续补到设定深度
  * 未就绪时 is_loading=True（UI 据此禁用播放按钮）
  * 暂停不丢进度；改语速会重建队列
  * recent_items() 给出最近 10 条与各自生成耗时

用法： python scripts/test_playback_queue.py
"""
from __future__ import annotations

import math
import os
import struct
import sys
import tempfile
import time
import wave
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from PySide6.QtCore import QObject  # noqa: E402
from PySide6.QtMultimedia import QMediaPlayer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from src.config import CACHE_DIR, Settings  # noqa: E402
from src.core.library import import_book  # noqa: E402
from src.core.player import PlaybackController  # noqa: E402
from src.core.tts.service import SynthesisService  # noqa: E402


class StubPlayer(QObject):
    def __init__(self) -> None:
        super().__init__()
        self.pos = 0
        self.status = QMediaPlayer.MediaStatus.NoMedia
        self.state = QMediaPlayer.PlaybackState.StoppedState
        self.calls: list[tuple] = []

    def setSource(self, url) -> None:  # noqa: N802
        self.calls.append(("setSource", str(url)))
        self.pos = 0
        self.status = QMediaPlayer.MediaStatus.LoadedMedia

    def play(self) -> None:
        self.calls.append(("play",))
        self.state = QMediaPlayer.PlaybackState.PlayingState

    def pause(self) -> None:
        self.calls.append(("pause",))
        self.state = QMediaPlayer.PlaybackState.PausedState

    def stop(self) -> None:
        self.calls.append(("stop",))
        self.state = QMediaPlayer.PlaybackState.StoppedState

    def playbackState(self):  # noqa: N802
        return self.state

    def mediaStatus(self):  # noqa: N802
        return self.status

    def position(self) -> int:
        return self.pos

    def setPosition(self, ms: int) -> None:  # noqa: N802
        self.calls.append(("setPosition", ms))
        self.pos = ms

    def kinds(self) -> list[str]:
        return [c[0] for c in self.calls]


def write_wav(path: Path, seconds: float = 0.2) -> None:
    sr, n = 32000, int(32000 * seconds)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(b"".join(
            struct.pack("<h", int(1200 * math.sin(2 * math.pi * 220 * i / sr))) for i in range(n)
        ))


def pump(app: QApplication, seconds: float) -> None:
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.02)


def main() -> int:
    app = QApplication.instance() or QApplication([])
    settings = Settings.load()
    settings.prefetch = 6
    # 每次随机一档语速，避免命中历史缓存导致"秒就绪"，测不到生成分支
    import random

    settings.speed = round(1.0 + random.uniform(0.05, 0.45), 3)
    settings.prosody_strength = round(random.uniform(0.6, 0.95), 2)
    service = SynthesisService(settings)
    # 每次运行给缓存 key 加随机后缀：否则会命中历史缓存，
    # _pump 直接判定"已就绪"，就测不到"生成中"的分支了
    salt = format(random.getrandbits(24), "06x")
    _orig_key_for = service.key_for
    service.key_for = lambda text, req: _orig_key_for(text, req) + salt
    ctl = PlaybackController(settings, service)

    states: list[tuple[int, int, int, int]] = []
    ctl.synthProgress.connect(lambda a, b, c, d: states.append((a, b, c, d)))

    defer = {"on": True}
    pending: list[str] = []

    def fake_request(key: str, req) -> Path | None:
        """defer 时只落 .tmp 占位，正式 wav 由 complete() 写入。

        真实服务是「写 tmp → 原子改名」，如果这里直接写正式文件，
        _pump 的 cached() 会误判成已就绪，就测不到等待分支了。
        """
        path = CACHE_DIR / f"{key}.wav"
        tmp = CACHE_DIR / f"{key}.tmp"
        if defer["on"]:
            if not tmp.exists() and not path.exists():
                tmp.write_bytes(b"pending")
            if key not in pending:
                pending.append(key)
            return None          # 表示"还没生成完"，等 _on_ready 回调
        write_wav(path)
        return path

    def complete(index: int) -> None:
        """模拟某一条生成完成：写入正式缓存并回调 ready。"""
        key = ctl._items[index].key
        write_wav(CACHE_DIR / f"{key}.wav")
        ctl._on_ready(key)

    service.request = fake_request  # type: ignore[assignment]

    p = Path(tempfile.gettempdir()) / "vr_queue_test.txt"
    p.write_text(
        "第一章 测试\n\n"
        + "\n\n".join(["他推开门，风雪灌了进来。", "“你来了。”老人抬起头。"] * 20),
        encoding="utf-8",
    )
    book = import_book(p)
    ctl.load(book, 0, 0)

    stub = StubPlayer()
    ctl.player = stub  # type: ignore[assignment]

    # ---- 1) 载入后自动把队列补到设定深度
    depth = settings.prefetch
    assert len(ctl._items) == depth, (len(ctl._items), depth)
    assert ctl.is_loading is True, "还没生成时 is_loading 应为 True"
    assert ctl._ready_ahead() == 0
    print(f"① 载入后预加载队列 {len(ctl._items)}/{depth} 条（全部生成中）  ✓")

    # ---- 2) 只完成 1 条，就应该能开播（不等队列填满）
    first_index = ctl.index
    first_key = ctl._items[first_index].key
    stub.calls.clear()
    ctl.play()                      # 用户点了播放，此时还没有可播条目
    assert ctl._want_playing is True
    assert "play" not in stub.kinds(), "没有可播条目时不该直接播"
    complete(first_index)            # 第 1 条生成完成（写盘 + ready 回调）
    _it = ctl._items.get(first_index)
    assert _it is not None, f"first_index={first_index} 不在队列里：{list(ctl._items)}"
    assert ctl._ready_ahead() >= 1, (
        f"state={_it.state} key={_it.key[:10]} map={list(ctl._key_to_index)[:3]} "
        f"cached={ctl.service.cached(_it.key)}")
    assert "play" in stub.kinds(), f"已就绪 1 条却没有开播：{stub.calls}"
    assert ctl._segment and ctl._segment[0] == first_index, ctl._segment
    print(f"② 仅 1 条就绪即开播（段内 {len(ctl._segment)} 句）  ✓")

    # ---- 3) 播完消费：条目出队、index 前进、队列自动补齐
    consumed = list(ctl._segment)
    stub.status = QMediaPlayer.MediaStatus.EndOfMedia
    stub.state = QMediaPlayer.PlaybackState.StoppedState
    ctl._on_media_status(QMediaPlayer.MediaStatus.EndOfMedia)
    pump(app, 0.3)
    assert ctl.index == consumed[-1] + 1, (ctl.index, consumed)
    for idx in consumed:
        assert idx not in ctl._items, f"播完的条目没出队：{idx}"
    assert len(ctl._items) <= depth, len(ctl._items)
    print(f"③ 消费 {len(consumed)} 条出队，index→{ctl.index}，队列 {len(ctl._items)}/{depth}  ✓")

    # ---- 4) 补队列：把队头开始的 3 条完成，重新有可播内容
    defer["on"] = True
    ctl._pump()
    fill = [i for i in range(ctl._queue_head, ctl._queue_head + 3) if i in ctl._items]
    for i in fill:
        if ctl._items[i].state != "ready":
            complete(i)
    pump(app, 0.1)
    assert ctl._ready_ahead() >= 1, (
        f"补进来的条目没变成就绪：{[(i, ctl._items[i].state) for i in fill]}")
    print(f"④ 补齐 3 条并转就绪，当前可播 {ctl._ready_ahead()} 条  ✓")

    # ---- 5) 状态信号：ready / depth / running / played 都在动
    assert states, "没有发出 synthProgress"
    ready, dep, running, played = states[-1]
    assert dep == depth, (dep, depth)
    assert played >= len(consumed), played
    print(f"⑤ synthProgress 末次 {states[-1]}  ✓")

    # ---- 6) 明细：最近 10 条 + 各自耗时
    rows = ctl.recent_items(10)
    assert rows, "recent_items 为空"
    assert len(rows) <= 10, len(rows)
    assert all("cost" in r and "code" not in r for r in rows)
    assert any(r["cost"] > 0 for r in rows), f"耗时全为 0：{rows[:2]}"
    print(f"⑥ recent_items 返回 {len(rows)} 条，首条耗时 {rows[0]['cost']}s  ✓")

    # ---- 7) 暂停后继续：不重新 setSource，直接续播
    ctl.pause()
    stub.calls.clear()
    ctl.play()
    assert "play" in stub.kinds(), stub.calls
    assert "setSource" not in stub.kinds(), f"续播不该重新组装：{stub.calls}"
    print("⑦ 暂停→继续：直接 play，不重新拼段  ✓")

    # ---- 8) 跳句：重置播放段并从目标句重新排队
    target = min(len(ctl.sentences) - 1, ctl.index + 20)
    defer["on"] = True
    ctl.seek_sentence(target, autoplay=True)
    assert ctl.index == target, (ctl.index, target)
    assert ctl._items and min(ctl._items) == target, (min(ctl._items), target)
    print(f"⑧ 跳句到第 {target + 1} 句，队列从该句重建  ✓")

    # ---- 9) 改语速：队列重建、key 变化
    old_keys = {it.key for it in ctl._items.values() if it.key}
    settings.speed = round(settings.speed + 0.07, 3)
    ctl.apply_settings_changed()
    new_keys = {it.key for it in ctl._items.values() if it.key}
    assert new_keys and new_keys != old_keys, "改语速后缓存 key 没变"
    print(f"⑨ 改语速后队列重建（{len(old_keys)} → {len(new_keys)} 条 key 全换）  ✓")

    print("\n全部通过：预加载队列就绪即播、消费出队、自动补齐。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
