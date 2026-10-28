"""回归测试：纯标点句不得进入 TTS（省略号卡死的根因）。

分块架构下，纯标点句会在拼块阶段被替换为 0.6s 静音，
这里只守住最底层的不变量。

用法： python scripts/test_pause_skip.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from src.core.textproc import analyze, clean_for_tts, is_speakable, split_sentences  # noqa: E402

CASES = {
    "……": False,
    "——": False,
    "“……”": False,
    "……。": False,
    "!!!": False,
    "* * *": False,
    "他看着她……": True,
    "走了。": True,
    "你好": True,
    "“我吗？”": True,
    "2024年3月": True,
    "OK": True,
}


def main() -> int:
    fails = 0
    print("① 可读性判定")
    for text, expect in CASES.items():
        got = is_speakable(text)
        flag = "✓" if got == expect else "✗"
        if got != expect:
            fails += 1
        print(f"   {flag} {text!r:<14} → 可读={got}（期望 {expect}）")

    print("\n② 纯标点句的语调结果")
    for text in ("……", "——", "“……”"):
        p = analyze(text)
        ok = p.kind == "pause"
        fails += 0 if ok else 1
        print(f"   {'✓' if ok else '✗'} {text!r:<8} kind={p.kind} 停顿={p.pause_ms}ms")

    print("\n③ 分句不把标点句吃掉")
    sents = split_sentences("他看着她……\n\n……\n\n天亮了。")
    print(f"   分句结果：{sents}")
    assert any(not is_speakable(s) for s in sents), "纯标点句应单独成句"
    assert any("天亮了" in s for s in sents)

    print("\n④ clean_for_tts 不该产出空串给引擎")
    empty = [t for t in ("……", "——", "  ") if is_speakable(clean_for_tts(t))]
    assert not empty, f"这些被误判为可读：{empty}"
    print("   ✓ 纯标点清理后仍判定为不可读")

    if fails:
        print(f"\n失败 {fails} 项")
        return 1
    print("\n全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
