"""启动自检：以离屏后端把整个应用跑起来 9 秒，验证装配链路不崩。

用法： python scripts/boot_check.py [--seconds 9]
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(DEFAULT_ROOT))
    ap.add_argument("--seconds", type=float, default=9.0)
    ap.add_argument("--platform", default="offscreen")
    args = ap.parse_args()

    root = Path(args.root)
    if not (root / "src" / "app.py").exists():
        print(f"[FAIL] 找不到应用: {root}")
        return 1
    os.environ.setdefault("QT_QPA_PLATFORM", args.platform)
    os.environ["VOICEREAD_SELFTEST_SECONDS"] = str(args.seconds)
    os.environ["VOICEREAD_NO_WELCOME"] = "1"   # 离屏下模态欢迎框会卡住自检
    sys.path.insert(0, str(root))

    from src.app import main as app_main

    print(f"[INFO] 启动应用（{args.platform} 后端，{args.seconds}s 后自动退出）…")
    code = app_main()
    print(f"[INFO] 退出码 {code}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
