"""冒烟测试：不显示界面，尽量把主要链路跑一遍，用来定位启动期错误。

用法：python scripts/smoke_test.py
输出每步 PASS / FAIL，最后一节汇总；任何 FAIL 都不中断后续步骤（一次性暴露所有问题）。
"""
from __future__ import annotations

import os
import sys
import tempfile
import traceback
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

RESULTS: list[tuple[str, bool, str]] = []


def step(name: str):
    def deco(fn):
        try:
            detail = fn() or ""
            RESULTS.append((name, True, str(detail)))
            print(f"[PASS] {name}  {detail}")
        except Exception as exc:  # noqa: BLE001
            tb = traceback.format_exc().strip().splitlines()[-6:]
            RESULTS.append((name, False, str(exc)))
            print(f"[FAIL] {name}  {exc}")
            for line in tb:
                print("       " + line)
        return fn

    return deco


@step("1 文本处理：分章/分句/语调")
def _textproc():
    from src.core.textproc import analyze, prepare_chapter, split_chapters, split_sentences

    sample = "第一章 初遇\n\n他走进酒馆，点了一杯酒。\n“你来了吗？”她抬头问。\n\n第二章 离别\n\n“再见了！”他喊道……\n她什么也没说——转身就走。"
    chapters = split_chapters(sample)
    assert len(chapters) >= 2, f"章节数 {len(chapters)}"
    sents = split_sentences("他问：“你去哪儿？”她笑了。")
    assert len(sents) >= 2, f"句子 {sents}"
    p = analyze("你来了吗？")
    assert p.kind == "question", p.kind
    text, sent_objs = prepare_chapter("第一段。第二段！\n\n第三段？")
    assert len(sent_objs) == 3, len(sent_objs)
    assert text[sent_objs[0].start : sent_objs[0].end] == "第一段。", "偏移不一致"
    return f"{len(chapters)}章 / {len(sents)}句 / {p.kind}"


@step("2 导入 GBK 编码 TXT")
def _import_gbk():
    from src.core.library import import_book

    tmp = Path(tempfile.gettempdir()) / "voiceread_smoke_gbk.txt"
    body = "第一章 起点\n\n这是一段用来测试编码识别的中文正文。\n\n第二章 终点\n\n结束了。\n"
    tmp.write_bytes(body.encode("gb18030"))
    book = import_book(tmp)
    assert book.chapter_count() >= 2, book.chapter_count()
    assert "编码识别" in book.chapter_text(0), "正文解码失败"
    return f"{book.title} / {book.chapter_count()}章 / {book.char_count}字"


@step("3 阅读进度持久化")
def _progress():
    from src.core.progress import ProgressStore

    store = ProgressStore()
    store.set("smoke-book", 2, 17, 0.3456)
    got = ProgressStore().get("smoke-book")
    assert (got.chapter, got.sentence) == (2, 17), got
    store.forget("smoke-book")
    return f"chapter={got.chapter} sentence={got.sentence} pct={got.percent}"


@step("4 设置读写")
def _settings():
    from src.config import Settings

    s = Settings.load()
    s.prefetch = 3
    s.save()
    s2 = Settings.load()
    assert s2.prefetch == 3
    assert hasattr(s2.mambo, "presets"), "曼波预设字段缺失"
    return f"engine={s2.engine_id} speed={s2.speed} presets={len(s2.mambo.presets)}"


@step("5 TTS 引擎构造与缓存 key")
def _engines():
    from src.config import Settings
    from src.core.tts.base import SpeechRequest
    from src.core.tts.registry import all_engine_meta, build_engine

    s = Settings.load()
    out = []
    for eid, label, _ in all_engine_meta():
        engine = build_engine(eid, s)
        req = SpeechRequest(text="测试一句话。", rate=1.1, pitch=3, voice="")
        k1 = engine.cache_tag(req)
        k2 = build_engine(eid, s).cache_tag(SpeechRequest(text="不同的句子。", rate=1.1, pitch=3, voice=""))
        out.append(f"{eid}:{type(engine).__name__}")
    return " / ".join(out)


@step("6 Qt 界面对象构造（offscreen）")
def _qt():
    from PySide6.QtWidgets import QApplication

    from src.config import Settings
    from src.core.library import Library
    from src.core.player import PlaybackController
    from src.core.progress import ProgressStore
    from src.core.tts.service import SynthesisService

    app = QApplication.instance() or QApplication([])
    s = Settings.load()
    lib = Library()
    prog = ProgressStore()
    svc = SynthesisService(s)
    ctl = PlaybackController(s, svc)

    from src.ui.main_window import MainWindow
    from src.ui.subtitle_window import SubtitleWindow

    sub = SubtitleWindow(s)
    win = MainWindow(s, lib, prog, svc, ctl, sub)
    sub.apply_settings()
    sub.set_sentence("当前句。", "上一句。", "下一句。")
    win.refresh_engine_combo()
    win.set_speed(1.2)
    return f"windows={len(lib.books) if lib.books else 0} 窗口构造OK"


@step("7 章节载入与句子队列")
def _load_chapter():
    from PySide6.QtWidgets import QApplication

    from src.config import Settings
    from src.core.library import import_book
    from src.core.player import PlaybackController
    from src.core.tts.service import SynthesisService
    import tempfile

    QApplication.instance() or QApplication([])
    tmp = Path(tempfile.gettempdir()) / "voiceread_smoke_load.txt"
    tmp.write_text(
        "第一章 甲\n\n他坐下。她开口：“你来晚了。”\n\n窗外下雨了，很大。\n\n第二章 乙\n\n天亮了。\n",
        encoding="utf-8",
    )
    book = import_book(tmp)
    s = Settings.load()
    svc = SynthesisService(s)
    ctl = PlaybackController(s, svc)
    ctl.load(book, 0, 0)
    assert ctl.sentences, "句子队列为空"
    assert ctl.display_text, "正文为空"
    first = ctl.sentences[0]
    assert ctl.display_text[first.start : first.end] == first.text, "高亮偏移错位"
    return f"{len(ctl.sentences)} 句 / 首句={first.text[:16]}"


@step("8 播放器对象（QMediaPlayer）")
def _media():
    from PySide6.QtWidgets import QApplication

    from src.core.player import PlaybackController
    from src.core.tts.service import SynthesisService
    from src.config import Settings

    QApplication.instance() or QApplication([])
    s = Settings.load()
    ctl = PlaybackController(s, SynthesisService(s))
    state = ctl.player.playbackState()
    assert ctl.player.mediaStatus() is not None
    return f"player OK, state={state}"


def main() -> int:
    print("=" * 62)
    print("语音书架 冒烟测试")
    print("=" * 62)
    for fn in (_textproc, _import_gbk, _progress, _settings, _engines, _qt, _load_chapter, _media):
        fn()
    print("-" * 62)
    failed = [r for r in RESULTS if not r[1]]
    print(f"合计 {len(RESULTS)} 项，通过 {len(RESULTS) - len(failed)} 项，失败 {len(failed)} 项")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  ✗ {name}: {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
