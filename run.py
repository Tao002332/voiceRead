"""启动入口：python run.py 或 pythonw run.py"""
from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))


def main() -> int:
    from src.crashlog import install

    log = install()
    try:
        from src.app import main as app_main

        return app_main()
    except BaseException:  # noqa: BLE001
        import traceback

        with open(log, "a", encoding="utf-8") as fh:
            fh.write("\n--- 启动失败 ---\n" + traceback.format_exc())
        raise


if __name__ == "__main__":
    sys.exit(main())
