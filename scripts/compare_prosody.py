"""对照生成：两句「读得怪」的样本，用 3 种文本/语调方案合成，供人耳比对。

A 原始文本 + 旧对话语调（1.05× / +3Hz）   ← 问题版本
B 去引号文本 + 旧对话语调
C 去引号文本 + 中性语调（1.0× / 0Hz）     ← 新逻辑

用法： python scripts/compare_prosody.py
"""
from __future__ import annotations

import sys
import time
import wave
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import requests  # noqa: E402

from src.config import Settings  # noqa: E402
from src.core.textproc import analyze, clean_for_tts  # noqa: E402

SENTENCES = [
    "“……额，那个，我们是有原则的，原则上来说，既然订单已经确定，那我们……”",
    "“额，中巴撅起一点，”",
]

VARIANTS = [
    ("A_原始文本_旧语调", False, 1.05, 3),
    ("B_去引号_旧语调", True, 1.05, 3),
    ("C_去引号_中性", True, 1.00, 0),
]

OUT = BASE / "demo_out" / "prosody"


def synth(conf, text: str, ref: str, prompt: str, out: Path, rate: float, pitch: int) -> tuple[float, float]:
    params = {
        "text": text,
        "text_lang": "zh",
        "ref_audio_path": ref,
        "prompt_text": prompt,
        "prompt_lang": "zh",
        "text_split_method": "cut5",
        "batch_size": 1,
        "media_type": "wav",
        "streaming_mode": "false",
        "speed_factor": str(rate),
        "top_k": 10,
        "top_p": 1,
        "temperature": 0.8,
        "repetition_penalty": 1.15,
    }
    t0 = time.time()
    r = requests.get(f"{conf.base_url.rstrip('/')}/tts", params=params, timeout=300)
    cost = time.time() - t0
    if r.status_code != 200:
        raise RuntimeError(f"{r.status_code}: {r.text[:200]}")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(r.content)
    try:
        with wave.open(str(out), "rb") as wf:
            dur = wf.getnframes() / float(wf.getframerate())
    except Exception:  # noqa: BLE001
        dur = 0.0
    return cost, dur


def main() -> int:
    s = Settings.load()
    conf = s.mambo
    preset = conf.presets[0] if conf.presets else {}
    ref = preset.get("ref_audio_path", "")
    prompt = preset.get("prompt_text", "")
    if not ref or not Path(ref).exists():
        print(f"✗ 参考音频无效：{ref}")
        return 1
    print(f"参考音频 {Path(ref).name} / 参考文本 {prompt[:30]}")
    print()

    try:
        requests.get(conf.base_url, timeout=3)
    except requests.exceptions.RequestException:
        print("✗ 曼波服务未启动")
        return 1

    for si, sentence in enumerate(SENTENCES, 1):
        p = analyze(sentence)
        print(f"样本 {si}：{sentence}")
        print(f"  当前判定：{p.kind_label} 语速 {p.rate:.2f}× 音调 {p.pitch:+d}Hz")
        print(f"  clean_for_tts → {clean_for_tts(sentence)}")
        for tag, use_clean, rate, pitch in VARIANTS:
            text = clean_for_tts(sentence) if use_clean else sentence
            out = OUT / f"s{si}_{tag}.wav"
            try:
                cost, dur = synth(conf, text, ref, prompt, out, rate, pitch)
            except Exception as exc:  # noqa: BLE001
                print(f"    {tag:<18} ✗ {exc}")
                continue
            cps = len(text) / dur if dur else 0
            print(f"    {tag:<18} {dur:5.2f}s  {cps:4.2f} 字/秒  合成 {cost:5.1f}s  {out.name}")
        print()

    print(f"输出目录：{OUT}")
    print("建议对比 A / B / C，确认哪个最正常，我按结果定稿。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
