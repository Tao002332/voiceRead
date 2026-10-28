"""用 ASR 反推参考音频的文本，生成 prompt_text。

GPT-SoVITS 拿到与参考音频严格对齐的文本后，才不会把换气声当内容念出来，
韵律也会稳定很多。这里用 funasr（paraformer-zh + 标点）本地转写。

用法： python scripts/asr_prompt_text.py [--ref ...] [--out ...]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", default="", help="参考音频，默认取设置里配置的那个")
    ap.add_argument("--out", default="", help="输出 txt，默认写到参考音频同目录")
    args = ap.parse_args()

    ref = Path(args.ref)
    if not ref.exists():
        print(f"✗ 参考音频不存在：{ref}")
        return 1

    print(f"参考音频：{ref}")
    print("加载 funasr 模型（首次会从 ModelScope 下载，约 1GB）…")
    from funasr import AutoModel

    model = AutoModel(
        model="paraformer-zh",
        vad_model="fsmn-vad",
        punc_model="ct-punc",
        disable_update=True,
        device="cuda:0",
    )
    print("识别中…")
    res = model.generate(input=str(ref), batch_size_s=60, hotword="")
    text = ""
    if res:
        if isinstance(res, list):
            text = "".join(str(item.get("text", "")) for item in res if isinstance(item, dict))
        elif isinstance(res, dict):
            text = str(res.get("text", ""))
    text = text.strip()
    print("=" * 60)
    print(f"识别结果：{text!r}")
    print("=" * 60)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"已写入：{out}")
    print("\n把这段文本填到「设置 → 语音引擎 → 曼波 → 参考文本」，或直接重启应用（会自动读取该文件）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
