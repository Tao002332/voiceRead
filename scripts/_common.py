"""脚本公用小工具：默认路径一律从「设置 / 环境变量 / 仓库根目录」推导，不写死机器路径。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))


def ref_from_settings() -> str:
    """取设置里配置的曼波参考音频路径；取不到返回空串。"""
    try:
        from src.config import Settings

        settings = Settings.load()
        presets = settings.mambo.presets or []
        if presets and isinstance(presets[0], dict):
            return str(presets[0].get("ref_audio_path") or "")
    except Exception:  # noqa: BLE001
        pass
    return ""


def epub_from_env() -> str:
    """测试用 EPUB 路径：环境变量 VOICEREAD_TEST_EPUB，其次命令行 --epub。"""
    for i, arg in enumerate(sys.argv):
        if arg == "--epub" and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
        if arg.startswith("--epub="):
            return arg.split("=", 1)[1]
    return os.getenv("VOICEREAD_TEST_EPUB", "")


def require_epub() -> str:
    """拿不到测试书就打印用法并返回空串（调用方据此退出）。"""
    path = epub_from_env()
    if path and Path(path).exists():
        return path
    print("未指定测试用 EPUB。用法：")
    print("   python scripts/xxx.py --epub \"D:\\books\\某本书.epub\"")
    print("   或先设置环境变量  set VOICEREAD_TEST_EPUB=D:\\books\\某本书.epub")
    return ""
