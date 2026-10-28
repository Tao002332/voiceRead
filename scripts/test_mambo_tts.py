"""曼波服务联调测试：不打开界面，直接走一遍 HTTP 合成。

用法： python scripts/test_mambo_tts.py [--url http://127.0.0.1:9880] [--ref <wav>]
前置： scripts/start_mambo_server.ps1 已在另一个窗口跑起来
"""
from __future__ import annotations

import argparse
import sys
import time
import wave
from pathlib import Path

import requests


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:9880")
    ap.add_argument("--ref", default="", help="参考音频路径，默认取设置里已配置的那个")
    ap.add_argument("--text", default="这是曼波语音的测试，一二三四五。")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    base = args.url.rstrip("/")
    ref = Path(args.ref)
    print(f"服务地址 : {base}")
    print(f"参考音频 : {ref}  {'存在' if ref.exists() else '不存在!'}")
    if not ref.exists():
        return 1

    # 1. 服务是否在线（api_v2 根路径返回 404 也算在线）
    try:
        requests.get(base, timeout=5)
        print("[1/3] 服务在线 ✓")
    except requests.exceptions.RequestException as exc:
        print(f"[1/3] 服务不可达 ✗  {exc}")
        print("      先跑 scripts/start_mambo_server.ps1")
        return 1

    # 2. 合成
    params = {
        "text": args.text,
        "text_lang": "zh",
        "ref_audio_path": str(ref),
        "prompt_text": "",
        "prompt_lang": "zh",
        "text_split_method": "cut5",
        "batch_size": 1,
        "media_type": "wav",
        "streaming_mode": "false",
        "speed_factor": "1.0",
    }
    out = Path(args.out) if args.out else (Path(__file__).resolve().parent.parent / "mambo_test.wav")
    t0 = time.time()
    try:
        resp = requests.get(f"{base}/tts", params=params, timeout=180)
    except requests.exceptions.RequestException as exc:
        print(f"[2/3] 请求失败 ✗  {exc}")
        return 1

    if resp.status_code != 200:
        print(f"[2/3] 返回 {resp.status_code} ✗")
        print("      " + (resp.text[:500] if resp.text else "(空响应)"))
        return 1

    out.write_bytes(resp.content)
    cost = time.time() - t0
    size_kb = len(resp.content) / 1024
    print(f"[2/3] 合成成功 ✓  {size_kb:.1f} KB / 耗时 {cost:.1f}s")

    # 3. 音频有效性
    try:
        with wave.open(str(out), "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()
            seconds = frames / float(rate) if rate else 0
        print(f"[3/3] 音频有效 ✓  {seconds:.2f}s  {rate}Hz  {wf.getnchannels()}ch")
        print(f"      试听文件：{out}")
    except Exception as exc:  # noqa: BLE001
        print(f"[3/3] 音频解析失败（可能是 mp3 或损坏）：{exc}")
        print(f"      原始文件：{out}")

    rtf = (cost / seconds) if seconds else 0
    print(f"\n实时率 RTF={rtf:.2f}（<1 表示比真实语速快，能边读边合成）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
