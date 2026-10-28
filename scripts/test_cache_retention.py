"""回归测试：缓存按时间清理（只保留最近 N 分钟）。

用法： python scripts/test_cache_retention.py
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from PySide6.QtWidgets import QApplication  # noqa: E402

from src.config import CACHE_DIR, Settings  # noqa: E402
from src.core.player import CHUNK_DIR, prune_chunk_cache  # noqa: E402
from src.core.tts.service import SynthesisService  # noqa: E402


def touch(path: Path, age_minutes: float, payload: bytes = b"RIFF....WAVEtest") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    ts = time.time() - age_minutes * 60
    os.utime(path, (ts, ts))
    return path


def main() -> int:
    app = QApplication.instance() or QApplication([])  # noqa: F841
    settings = Settings.load()
    settings.cache_retention_minutes = 10
    service = SynthesisService(settings)

    print(f"tts 缓存目录  : {CACHE_DIR}")
    print(f"拼块缓存目录  : {CHUNK_DIR}")

    fresh_tts = touch(CACHE_DIR / "retention_fresh.wav", 2)
    old_tts = touch(CACHE_DIR / "retention_old.wav", 11)
    fresh_chunk = touch(CHUNK_DIR / "retention_fresh.wav", 3)
    old_chunk = touch(CHUNK_DIR / "retention_old.wav", 30)
    print("已准备：新缓存 2/3 分钟，旧缓存 11/30 分钟")

    removed_tts = service.prune_by_age(10)
    removed_chunk = prune_chunk_cache(10)
    print(f"清理：tts 删除 {removed_tts} 个，拼块删除 {removed_chunk} 个")

    assert not old_tts.exists(), "11 分钟前的音频没被删掉"
    assert fresh_tts.exists(), "2 分钟内的音频被误删"
    assert not old_chunk.exists(), "30 分钟前的拼块没被删掉"
    assert fresh_chunk.exists(), "3 分钟内的拼块被误删"
    print("① 按时间清理：过期删、新鲜留  ✓")

    # ---- 保留时长可配
    settings.cache_retention_minutes = 1
    keep1 = touch(CACHE_DIR / "retention_keep1.wav", 0.5)
    gone5 = touch(CACHE_DIR / "retention_gone5.wav", 5)
    service.prune_by_age()          # 走默认（读取 settings）
    assert keep1.exists() and not gone5.exists(), "读取 settings 的保留时长未生效"
    print("② 保留时长跟随设置（1 分钟）  ✓")

    for p in (fresh_tts, fresh_chunk, keep1):
        p.unlink(missing_ok=True)
    print("\n全部通过：缓存只保留最近 10 分钟。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
