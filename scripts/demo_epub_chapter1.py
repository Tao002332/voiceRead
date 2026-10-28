"""端到端演示：导入指定 EPUB 第一章 → 分章分句 → 语调分析 → 调曼波服务合成。

用法：
    python scripts/demo_epub_chapter1.py --epub "D:\\books\\sample.epub" --n 3
"""
from __future__ import annotations

import argparse
import sys
import time
import wave
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import requests  # noqa: E402

from src.core.library import import_book  # noqa: E402
from src.core.textproc import analyze, prepare_chapter  # noqa: E402

DEFAULT_EPUB = ""      # 用 --epub 指定，仓库里不放任何书
DEFAULT_REF = ""       # 留空则取设置里的参考音频
OUT_DIR = BASE / "demo_out"


def synth(base: str, text: str, ref: str, out: Path, speed: float = 1.0) -> tuple[float, float]:
    params = {
        "text": text,
        "text_lang": "zh",
        "ref_audio_path": ref,
        "prompt_text": "",
        "prompt_lang": "zh",
        "text_split_method": "cut5",
        "batch_size": 1,
        "media_type": "wav",
        "streaming_mode": "false",
        "speed_factor": str(speed),
    }
    t0 = time.time()
    resp = requests.get(f"{base.rstrip('/')}/tts", params=params, timeout=300)
    cost = time.time() - t0
    if resp.status_code != 200:
        raise RuntimeError(f"{resp.status_code}: {resp.text[:300]}")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(resp.content)
    try:
        with wave.open(str(out), "rb") as wf:
            seconds = wf.getnframes() / float(wf.getframerate())
    except Exception:  # noqa: BLE001
        seconds = 0.0
    return cost, seconds


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--epub", default=DEFAULT_EPUB)
    ap.add_argument("--ref", default=DEFAULT_REF)
    ap.add_argument("--url", default="http://127.0.0.1:9880")
    ap.add_argument("--n", type=int, default=3)
    ap.add_argument("--chapter", type=int, default=0)
    args = ap.parse_args()

    print("=" * 70)
    print("① 导入 EPUB")
    book = import_book(args.epub)
    print(f"   书名   : {book.title}")
    print(f"   章节数 : {book.chapter_count()}   总字数: {book.char_count}")
    print(f"   格式   : {book.fmt.upper()}   路径: {Path(book.path).name}")
    print(f"   前 5 章: {book.chapter_titles[:5]}")

    print("\n② 第一章解析")
    ch_title = book.chapter_title(args.chapter)
    ch_text = book.chapter_text(args.chapter)
    display, sentences = prepare_chapter(ch_text)
    print(f"   章节标题: {ch_title}")
    print(f"   正文字数: {len(ch_text)}   句数: {len(sentences)}")
    print(f"   首段预览: {display[:80].replace(chr(10), ' / ')}")

    print("\n③ 语调分析（按上下文自动判断）")
    pick = sentences[: args.n]
    for i, sent in enumerate(pick):
        p = analyze(sent.text)
        print(
            f"   [{i}] {p.kind_label:<3} 语速 {p.rate:.2f}× 音调 {p.pitch:+d}Hz "
            f"停顿 {p.pause_ms}ms | {sent.text[:26]}"
        )

    print("\n④ 调用曼波服务合成")
    if not Path(args.ref).exists():
        print(f"   ✗ 参考音频不存在: {args.ref}")
        return 1
    total_cost = total_sec = 0.0
    for i, sent in enumerate(pick):
        p = analyze(sent.text)
        out = OUT_DIR / f"ch{args.chapter + 1}_s{i + 1}.wav"
        try:
            cost, sec = synth(args.url, sent.text, args.ref, out, speed=p.rate)
        except Exception as exc:  # noqa: BLE001
            print(f"   [{i}] ✗ {exc}")
            return 1
        total_cost += cost
        total_sec += sec
        rtf = cost / sec if sec else 0
        print(f"   [{i}] ✓ {len(sent.text):>3}字  音频 {sec:5.2f}s  耗时 {cost:5.2f}s  RTF {rtf:.2f}  {out.name}")
        print(f"        → {sent.text[:40]}")

    if total_sec:
        print(f"\n   合计：音频 {total_sec:.1f}s / 合成 {total_cost:.1f}s / 平均 RTF {total_cost / total_sec:.2f}")
        print(f"   试听目录：{OUT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
