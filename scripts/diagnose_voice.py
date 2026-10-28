"""诊断语音质量：参考音频健康度 + 合成语速一致性 + 句首呼吸段。

输出两类客观指标：
  1. 参考音频：时长、峰值、噪声底、首尾静音、呼吸段检测 → 顺手生成修剪版
  2. 合成结果：每句「字/秒」、音频时长、句首能量爬升时间（判断有没有吸气声）

用法：
    python scripts/diagnose_voice.py --ref "E:\\...\\mambo_ref.wav"
"""
from __future__ import annotations

import argparse
import statistics
import sys
import wave
from pathlib import Path

import numpy as np
import requests

BASE = Path(__file__).resolve().parent.parent
OUT = BASE / "diag"


# ---------------------------------------------------------------- 音频工具
def load_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as wf:
        sr, ch, sw, n = wf.getframerate(), wf.getnchannels(), wf.getsampwidth(), wf.getnframes()
        raw = wf.readframes(n)
    if sw != 2:
        raise SystemExit(f"只支持 16bit wav，当前 {sw * 8}bit：{path}")
    x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    return x, sr


def save_wav(path: Path, x: np.ndarray, sr: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())


def envelope(x: np.ndarray, sr: int, frame_ms: int = 20) -> np.ndarray:
    n = max(1, int(sr * frame_ms / 1000))
    frames = len(x) // n
    if frames == 0:
        return np.zeros(1)
    return np.sqrt(np.mean(x[: frames * n].reshape(frames, n) ** 2, axis=1))


def speech_onset_ms(x: np.ndarray, sr: int, rel: float = 0.18) -> float:
    """句首到「稳定发音」的时间。数值大 = 开头有静音/呼吸/杂音。"""
    env = envelope(x, sr)
    if env.size == 0:
        return 0.0
    peak = float(np.percentile(env, 95)) or 1e-6
    thresh = peak * rel
    run = 0
    for i, v in enumerate(env):
        run = run + 1 if v >= thresh else 0
        if run >= 3:
            return (i - 2) * 20.0
    return len(env) * 20.0


def describe(name: str, x: np.ndarray, sr: int) -> dict:
    env = envelope(x, sr)
    noise = float(np.percentile(env, 10)) if env.size else 0.0
    peak = float(np.max(np.abs(x))) if x.size else 0.0
    rms = float(np.sqrt(np.mean(x**2))) if x.size else 0.0
    snr = 20 * np.log10((rms + 1e-9) / (noise + 1e-9))
    print(f"  {name}")
    print(f"    时长 {len(x) / sr:6.2f}s   峰值 {peak:.3f}   RMS {rms:.4f}   噪声底 {noise:.4f}   SNR≈{snr:.1f}dB")
    return {"peak": peak, "rms": rms, "noise": noise, "snr": snr}


def trim_ref(x: np.ndarray, sr: int, thresh: float = 0.02, pad_ms: int = 40) -> tuple[np.ndarray, float, float]:
    idx = np.where(np.abs(x) > thresh)[0]
    if idx.size == 0:
        return x, 0.0, 0.0
    pad = int(sr * pad_ms / 1000)
    a, b = max(0, idx[0] - pad), min(len(x), idx[-1] + pad)
    return x[a:b], a / sr * 1000, (len(x) - b) / sr * 1000


# ---------------------------------------------------------------- 合成
def synth(base: str, text: str, ref: Path, out: Path, temperature: float = 1.0,
          top_k: int = 15, rep: float = 1.35, speed: float = 1.0, prompt_text: str = "") -> Path:
    params = {
        "text": text,
        "text_lang": "zh",
        "ref_audio_path": str(ref),
        "prompt_text": prompt_text,
        "prompt_lang": "zh",
        "text_split_method": "cut5",
        "batch_size": 1,
        "media_type": "wav",
        "streaming_mode": "false",
        "speed_factor": str(speed),
        "top_k": top_k,
        "top_p": 1,
        "temperature": temperature,
        "repetition_penalty": rep,
    }
    r = requests.get(f"{base.rstrip('/')}/tts", params=params, timeout=300)
    if r.status_code != 200:
        raise RuntimeError(f"{r.status_code}: {r.text[:200]}")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(r.content)
    return out


SENTENCES = [
    ("超短(3字)", "费舍尔"),
    ("短句(7字)", "他知道，结束了。"),
    ("标点收尾", "他看着她……"),
    ("中句(23字)", "下方的土地是一片凹凸不平，看似毫无人烟的旷野。"),
    ("长句(63字)", "整个天空被阴云笼罩着，明明是正午时分，那阳光却难以穿透层层叠叠看不清尽头的云层，直把整个不干净的云朵衬托成为接近于死寂的苍凉。"),
    ("对话句", "“你来晚了。”她说，“我等了很久。”"),
    ("感叹句", "那光芒太刺眼了！"),
    ("疑问句", "你为什么要这么做？"),
]


def benchmark(base: str, ref: Path, tag: str, **kw) -> list[float]:
    rates: list[float] = []
    onsets: list[float] = []
    print(f"\n  —— 参考音频：{tag} ——")
    print(f"  {'句子':<12}{'字数':>4}{'时长s':>8}{'字/秒':>8}{'句首爬升ms':>11}")
    for label, text in SENTENCES:
        out = OUT / f"{tag}_{label}.wav"
        try:
            synth(base, text, ref, out, **kw)
        except Exception as exc:  # noqa: BLE001
            print(f"  {label:<12} ✗ {exc}")
            continue
        x, sr = load_wav(out)
        dur = len(x) / sr
        cps = len(text) / dur if dur else 0
        onset = speech_onset_ms(x, sr)
        rates.append(cps)
        onsets.append(onset)
        print(f"  {label:<12}{len(text):>4}{dur:>8.2f}{cps:>8.2f}{onset:>11.0f}")
    if rates:
        print(f"  字/秒  中位 {statistics.median(rates):.2f}  最小 {min(rates):.2f}  最大 {max(rates):.2f}"
              f"  极差 {max(rates) - min(rates):.2f}")
        print(f"  句首爬升 中位 {statistics.median(onsets):.0f}ms  最大 {max(onsets):.0f}ms")
    return rates


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", default="", help="参考音频路径，默认取设置里已配置的那个")
    ap.add_argument("--url", default="http://127.0.0.1:9880")
    ap.add_argument("--skip-bench", action="store_true")
    ap.add_argument("--prompt", default="", help="参考音频对应的文本（prompt_text）")
    args = ap.parse_args()

    ref = Path(args.ref)
    print("=" * 74)
    print("① 参考音频体检")
    if not ref.exists():
        print(f"  ✗ 不存在：{ref}")
        return 1
    x, sr = load_wav(ref)
    info = describe(f"{ref.name}  {sr}Hz", x, sr)
    lead_ms = speech_onset_ms(x, sr)
    print(f"    句首爬升 {lead_ms:.0f}ms（>150ms 说明开头有静音或换气）")

    # 呼吸段检测：能量低于语音 25% 但高于噪声底的片段
    env = envelope(x, sr)
    speech = float(np.percentile(env, 85)) or 1e-6
    breath = np.where((env > info["noise"] * 1.8) & (env < speech * 0.30))[0]
    if breath.size:
        print(f"    疑似换气/杂音帧：{breath.size} / {env.size}（{breath.size / env.size * 100:.0f}%）")
    else:
        print("    未检出明显换气段")

    print("\n② 生成修剪版参考音频")
    trimmed, lms, rms_ms = trim_ref(x, sr)
    clean = ref.with_name("mambo_ref_clean.wav")
    save_wav(clean, trimmed, sr)
    print(f"    裁掉 首 {lms:.0f}ms / 尾 {rms_ms:.0f}ms → {len(trimmed) / sr:.2f}s  {clean}")
    x2 = trimmed
    describe(f"{clean.name}", x2, sr)
    print(f"    句首爬升 {speech_onset_ms(x2, sr):.0f}ms")

    if args.skip_bench:
        return 0

    print("\n③ 合成对照（参数固定，只换参考音频）")
    print("=" * 74)
    served = False
    try:
        requests.get(args.url, timeout=3)
        served = True
    except requests.exceptions.RequestException:
        print("  ✗ 服务未启动，跳过对照测试")
        return 0
    if served:
        benchmark(args.url, ref, "原始", temperature=1.0)
        benchmark(args.url, clean, "修剪后", temperature=1.0)
        benchmark(args.url, clean, "修剪+稳参", temperature=0.7, top_k=10, rep=1.15)
        if args.prompt:
            print(f"\n  参考文本：{args.prompt}")
            benchmark(args.url, clean, "修剪+文本", temperature=1.0, prompt_text=args.prompt)
            benchmark(args.url, clean, "修剪+文本+稳参", temperature=0.7, top_k=10, rep=1.15,
                      prompt_text=args.prompt)
    return 0


if __name__ == "__main__":
    sys.exit(main())
