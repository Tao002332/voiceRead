"""回归测试：悬浮字幕长文本不被挤压 + 送 TTS 前剥掉首尾引号。

用法： python scripts/test_subtitle_fit.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from src.core.textproc import analyze, clean_for_tts  # noqa: E402
from src.ui.subtitle_window import SubtitleWindow  # noqa: E402

LONG = "“……额，那个，我们是有原则的，原则上来说，既然订单已经确定，那我们……”" + "又补了一句很长的话用来撑满两行以上。"
SHORT = "他推开门。"


def main() -> int:
    print("① 送 TTS 前剥掉首尾引号（避免开口/收尾怪调）")
    cases = [
        ("“……你好……”", "……你好……"),
        ("“你好。”", "你好。"),
        ("“额，中巴撅起一点，”", "额，中巴撅起一点，"),
        ("没有引号的句子。", "没有引号的句子。"),
    ]
    for raw, expect in cases:
        got = clean_for_tts(raw)
        ok = got == expect
        print(f"   {'✓' if ok else '✗'} {raw!r} → {got!r}（期望 {expect!r}）")
        assert ok, (raw, got, expect)

    print("\n② 对话不再人为加速/升调")
    for text in ("“额，那个，我们是有原则的。”", "“额，中巴撅起一点，”"):
        p = analyze(text)
        ok = p.kind == "dialogue" and abs(p.rate - 1.0) < 1e-6 and p.pitch == 0
        print(f"   {'✓' if ok else '✗'} {text[:16]}… → {p.kind_label} {p.rate:.2f}× {p.pitch:+d}Hz")
        assert ok, (text, p.kind, p.rate, p.pitch)

    print("\n③ 长文本先缩字号、放不下才省略")
    width, lines = 714, 2
    text, size = SubtitleWindow._fit_text(LONG, width, lines, 22)
    per_line = max(1, int(width / (size * 1.06)))
    fits = len(text) <= per_line * lines
    print(f"   原文 {len(LONG)} 字 → 显示 {len(text)} 字 / 字号 {size}px / 每行 {per_line} 字 / "
          f"{'放得下' if fits else '仍超出'}")
    assert fits, (len(text), per_line, lines)
    assert size <= 22, size

    short_text, short_size = SubtitleWindow._fit_text(SHORT, width, lines, 22)
    assert short_text == SHORT and short_size == 22, (short_text, short_size)
    print(f"   短句不受影响：{short_text!r} / {short_size}px  ✓")

    # 极端长文本必须省略且不超过预算
    huge = "长" * 400
    t2, s2 = SubtitleWindow._fit_text(huge, width, lines, 22)
    per2 = max(1, int(width / (s2 * 1.06)))
    assert len(t2) <= per2 * lines, (len(t2), per2)
    assert t2.endswith("…"), t2[-10:]
    print(f"   超长 400 字 → {len(t2)} 字（含省略号）/ {s2}px  ✓")

    print("\n全部通过：字幕长句不再挤压。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
