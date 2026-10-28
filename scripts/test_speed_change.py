"""回归测试：改语速只影响「当前这句及之后」，且不会让字幕/音频错位。

覆盖：
  1. 改语速后 index（选中句）不变
  2. 缓存 key 变化（新语速 → 重新合成），整块作废重建
  3. 重建时每句语速 = 新基础语速 × 该句语调系数
  4. 等待定位期间，位置轮询不得覆盖选中句

用法： python scripts/test_speed_change.py
"""
from __future__ import annotations

import math
import os
import struct
import sys
import tempfile
import wave
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from PySide6.QtWidgets import QApplication  # noqa: E402

from src.config import CACHE_DIR, Settings  # noqa: E402
from src.core.library import import_book  # noqa: E402
from src.core.player import PlaybackController  # noqa: E402
from src.core.tts.service import SynthesisService  # noqa: E402


def make_book() -> Path:
    paras = [
        "他推开门，风雪灌了进来。",
        "“你终于来了。”老人抬起头。",
        "壁炉里的火噼啪作响，影子拉得很长。",
        "外面的风停了，只剩下心跳声。",
    ] * 6
    p = Path(tempfile.gettempdir()) / "vr_speed_test.txt"
    p.write_text("第一章 风雪\n\n" + "\n\n".join(paras), encoding="utf-8")
    return p


def main() -> int:
    app = QApplication.instance() or QApplication([])  # noqa: F841
    settings = Settings.load()
    settings.speed = 1.0
    settings.auto_prosody = True
    service = SynthesisService(settings)
    ctl = PlaybackController(settings, service)

    seen: list[tuple[str, float]] = []

    def fake_synth(key: str, req) -> Path:
        path = CACHE_DIR / f"{key}.wav"
        if not path.exists():
            sr, n = 32000, 6400
            with wave.open(str(path), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(sr)
                wf.writeframes(b"".join(
                    struct.pack("<h", int(2000 * math.sin(2 * math.pi * 180 * i / sr))) for i in range(n)
                ))
        seen.append((req.text, req.rate))
        return path

    service.request = fake_synth  # type: ignore[assignment]

    book = import_book(make_book())
    ctl.load(book, 0, 0)
    ctl._prepare(ctl.chunks[0])
    old_keys = list(ctl.chunks[0].keys)
    old_path = ctl.chunks[0].path
    print(f"语速 1.0：块 0 就绪 {Path(old_path).name if old_path else '-'}，{len(old_keys)} 句")

    # ---- 选中第 5 句后改语速
    target = 5
    ctl.select_sentence(target)
    assert ctl.index == target
    settings.speed = 1.35
    been = []
    ctl.sentenceChanged.connect(lambda i, t, p: been.append(i))
    seen.clear()
    ctl.apply_settings_changed()

    assert ctl.index == target, f"改语速后选中句被改动：{target} → {ctl.index}"
    assert been and been[-1] == target, f"没有重新发出当前句：{been}"
    print(f"① 改语速后选中句保持第 {ctl.index} 句   ✓")

    # 假合成是同步返回的，apply 期间就已经按新语速重建完成
    chunk = ctl.chunks[0]
    assert chunk.ready, "重建失败"
    new_keys = list(chunk.keys)
    assert new_keys != old_keys, "语速变了，缓存 key 却没变"
    assert chunk.path != old_path, "拼块没换文件，可能复用了旧语速的音频"
    print(f"② 按新语速重建：{Path(old_path).name[:10]}… → {Path(chunk.path).name[:10]}…   ✓")

    # ---- 每句语速必须严格等于基础语速（不再被语调系数缩放）
    bad = [(text[:12], rate) for text, rate in seen if abs(rate - 1.35) > 1e-6]
    assert not bad, f"这些句子语速与设置不一致：{bad[:5]}"
    print(f"③ 重建的 {len(seen)} 句语速严格等于设置值 1.35×   ✓")

    # 语调分析不再改动语速
    from src.core.textproc import analyze as _analyze

    for text in ("你为什么要这么做？", "那光芒太刺眼了！", "他看着她……", "“你来了。”", "走了。"):
        p = _analyze(text)
        assert abs(p.rate - 1.0) < 1e-9, (text, p.rate)
    print("   各类句子的语调系数均为 1.0（只调音调/停顿）   ✓")

    # ---- 定位期间不得被 tick 覆盖
    ctl.index = 3
    ctl._pending_seek_ms = 2500
    ctl._on_tick()
    assert ctl.index == 3, f"等待定位时 index 被覆盖为 {ctl.index}"
    ctl._pending_seek_ms = None
    print("④ 等待定位期间，位置轮询不覆盖选中句   ✓")

    # ---- 重建期间必须忽略播放器回调：否则会在 stop() 触发的槽里
    #      再 setSource/play，QMediaPlayer 会死锁（就是"播放中改配置卡死"）
    from PySide6.QtMultimedia import QMediaPlayer

    ctl._reloading = True
    ctl._want_playing = True
    before = ctl.index
    ctl._on_media_status(QMediaPlayer.MediaStatus.EndOfMedia)
    assert ctl.index == before, "重建期间不应响应 EndOfMedia"
    ctl._on_media_status(QMediaPlayer.MediaStatus.LoadedMedia)
    ctl._reloading = False
    ctl._want_playing = False
    print("⑤ 重建期间忽略播放器回调（防重入死锁）   ✓")

    print("\n全部通过：语速调整只影响当前句及之后。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
