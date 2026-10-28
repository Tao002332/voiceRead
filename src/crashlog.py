"""崩溃日志：把未捕获异常和硬崩溃（访问违例等）都写到文件里。

- faulthandler 负责「进程直接挂掉」这类硬崩，会记录当时的调用栈
- excepthook 负责 Python 层未捕获异常
日志位置：%APPDATA%\\VoiceRead\\logs\\crash.log
"""
from __future__ import annotations

import faulthandler
import os
import sys
import threading
import traceback
from datetime import datetime
from pathlib import Path


def _log_dir() -> Path:
    base = os.getenv("APPDATA") or os.getenv("LOCALAPPDATA")
    root = Path(base) / "VoiceRead" if base else Path.home() / ".voiceread"
    out = root / "logs"
    out.mkdir(parents=True, exist_ok=True)
    return out


def log_path() -> Path:
    return _log_dir() / "crash.log"


def install() -> Path:
    """开启崩溃日志，返回日志路径。"""
    path = log_path()

    # 硬崩溃（段错误 / 访问违例）——必须自己持有文件句柄，进程退出前不会丢
    try:
        fh = open(path, "a", encoding="utf-8", buffering=1)
        fh.write(f"\n===== 启动 {datetime.now():%Y-%m-%d %H:%M:%S} pid={os.getpid()} =====\n")
        fh.flush()
        faulthandler.enable(file=fh, all_threads=True)
        globals()["_FAULT_FILE"] = fh  # 保持引用，避免被 GC 关闭
    except Exception:  # noqa: BLE001
        pass

    def _hook(exc_type, exc, tb) -> None:
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"\n--- 未捕获异常 {datetime.now():%H:%M:%S} ---\n")
                f.write(text)
                f.write(f"线程：{threading.current_thread().name}\n")
        except Exception:  # noqa: BLE001
            pass
        sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = _hook

    def _thread_hook(args) -> None:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"\n--- 线程异常 {datetime.now():%H:%M:%S} {args.thread.name} ---\n")
                f.write("".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)))
        except Exception:  # noqa: BLE001
            pass

    threading.excepthook = _thread_hook
    return path
