"""集成测试：走应用自己的引擎适配层（MamboEngine + SynthesisService）合成一句。

验证的不是脚本直连，而是应用实际使用的代码路径：
    Settings → build_engine("mambo") → SynthesisService.request → ready 信号 → 缓存文件

用法： python scripts/test_app_engine.py [--text "..."] [--engine mambo]
"""
from __future__ import annotations

import argparse
import sys
import time
import wave
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from PySide6.QtCore import QCoreApplication  # noqa: E402

from src.config import Settings  # noqa: E402
from src.core.tts.base import SpeechRequest  # noqa: E402
from src.core.tts.service import SynthesisService  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", default="他看着她，什么也没说……天亮了。")
    ap.add_argument("--engine", default="")
    ap.add_argument("--rate", type=float, default=1.0)
    ap.add_argument("--timeout", type=float, default=180.0)
    args = ap.parse_args()

    app = QCoreApplication([])
    settings = Settings.load()
    if args.engine:
        settings.engine_id = args.engine
    print(f"引擎      : {settings.engine_id}")
    print(f"参考音频  : {settings.mambo.presets[0].get('ref_audio_path')}")

    service = SynthesisService(settings)
    ok, msg = service.probe()
    print(f"引擎自检  : {'✓' if ok else '✗'} {msg}")
    if not ok:
        return 1

    req = SpeechRequest(
        text=args.text,
        rate=args.rate,
        pitch=0,
        voice=str(settings.mambo.active_preset),
    )
    key = service.key_for(req.text, req)
    print(f"缓存 key  : {key}")

    state = {"done": False, "error": ""}
    service.ready.connect(lambda k: state.update(done=True) if k == key else None)
    service.failed.connect(lambda k, e: state.update(done=True, error=e) if k == key else None)

    t0 = time.time()
    path = service.request(key, req)
    if path is None:
        deadline = t0 + args.timeout
        while not state["done"] and time.time() < deadline:
            app.processEvents()
            time.sleep(0.05)
        path = service.cached(key)
    cost = time.time() - t0

    if state["error"]:
        print(f"✗ 合成失败：{state['error']}")
        return 1
    if path is None or not Path(path).exists():
        print("✗ 超时或没有产出音频")
        return 1

    size_kb = path.stat().st_size / 1024
    seconds = 0.0
    try:
        with wave.open(str(path), "rb") as wf:
            seconds = wf.getnframes() / float(wf.getframerate())
    except Exception:  # noqa: BLE001
        pass
    print(f"✓ 合成成功  {size_kb:.1f} KB / {seconds:.2f}s / 耗时 {cost:.1f}s")
    print(f"  文件：{path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
