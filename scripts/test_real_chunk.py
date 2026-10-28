"""真实链路测试：用真引擎跑分块，验证「合成跟得上播放」。

指标：每块「合成耗时 / 音频时长」= RTF。
RTF < 1 表示合成比播放快，只要提前预取 1~2 块就绝不会出现长停顿。

用法： python scripts/test_real_chunk.py [--epub ...] [--chapter 3]
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import wave
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from PySide6.QtWidgets import QApplication  # noqa: E402

from src.config import Settings  # noqa: E402
from src.core.library import import_book  # noqa: E402
from src.core.player import PlaybackController  # noqa: E402
from src.core.tts.service import SynthesisService  # noqa: E402

DEFAULT_EPUB = ""      # 由 --epub 或环境变量 VOICEREAD_TEST_EPUB 提供


def chunk_duration(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as wf:
            return wf.getnframes() / float(wf.getframerate())
    except Exception:  # noqa: BLE001
        return 0.0


def wait_ready(app: QApplication, chunk, timeout: float = 600.0) -> bool:
    deadline = time.time() + timeout
    while not chunk.ready:
        app.processEvents()
        if time.time() > deadline:
            return False
        time.sleep(0.02)
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--epub", default=DEFAULT_EPUB)
    ap.add_argument("--chapter", type=int, default=3)
    ap.add_argument("--chunks", type=int, default=3)
    args = ap.parse_args()
    if not args.epub or not Path(args.epub).exists():
        print("未指定测试 EPUB：用法 --epub <路径>，或设置环境变量 VOICEREAD_TEST_EPUB")
        return 0

    app = QApplication.instance() or QApplication([])
    settings = Settings.load()
    print(f"引擎 {settings.engine_id} / 参考音频 {Path(settings.mambo.presets[0].get('ref_audio_path', '')).name}")
    print(f"参考文本 {(settings.mambo.presets[0].get('prompt_text') or '(空)')[:40]}")

    service = SynthesisService(settings)
    ok, msg = service.probe()
    print(f"引擎自检 {'✓' if ok else '✗'} {msg}")
    if not ok:
        return 1

    ctl = PlaybackController(settings, service)
    book = import_book(args.epub)
    t_load = time.time()
    ctl.load(book, args.chapter, 0)
    print(f"载入章节：{book.chapter_title(args.chapter)}  共 {len(ctl.sentences)} 句 / {len(ctl.chunks)} 块"
          f"（{time.time() - t_load:.2f}s）")

    total_synth = total_audio = 0.0
    for i in range(min(args.chunks, len(ctl.chunks))):
        chunk = ctl.chunks[i]
        chars = sum(len(ctl.sentences[j].text) for j in range(chunk.start, chunk.end))
        t0 = time.time()
        ctl._prepare(chunk)
        if not wait_ready(app, chunk):
            print(f"  块 {i}: 超时未就绪")
            return 1
        cost = time.time() - t0
        dur = chunk_duration(chunk.path) if chunk.path else 0.0
        total_synth += cost
        total_audio += dur
        rtf = cost / dur if dur else 0
        verdict = "✓ 跟得上" if rtf < 0.9 else ("△ 勉强" if rtf < 1.15 else "✗ 跟不上")
        print(f"  块 {i}: {chunk.end - chunk.start:>2} 句 / {chars:>3} 字 / 音频 {dur:6.1f}s / "
              f"合成 {cost:5.1f}s / RTF {rtf:.2f}  {verdict}")

    if total_audio:
        print(f"\n合计：音频 {total_audio:.1f}s，合成 {total_synth:.1f}s，整体 RTF {total_synth / total_audio:.2f}")
        print("结论：预取 1 块就能覆盖合成耗时，播放中不会出现长停顿。"
              if total_synth / total_audio < 0.9 else
              "警告：合成偏慢，播放可能追不上，需要提高预取深度或降低模型精度要求。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
