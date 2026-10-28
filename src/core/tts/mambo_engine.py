"""曼波本地语音引擎。

对接 GPT-SoVITS 的 api_v2.py 服务（社区「曼波」音色模型基本都是这一套）：
  启动：python api_v2.py -a 127.0.0.1 -p 9880 -c GPT_SoVITS/configs/tts_infer.yaml
  请求：GET /tts?text=...&text_lang=zh&ref_audio_path=...&prompt_text=...&speed_factor=1.0

「音色」由参考音频（ref_audio_path + prompt_text）决定，所以设置里做成
多预设切换：曼波/其他角色只要换参考音频即可，无需换模型。
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urljoin

from .base import SpeechRequest, TTSEngine


class MamboEngine(TTSEngine):
    id = "mambo"
    label = "曼波本地语音（GPT-SoVITS）"
    supports_pitch = True     # 通过 ref 音高 + 轻微 pitch 抖动近似，实际以 speed 为主
    needs_network = False     # 本地回环，不依赖外网
    file_suffix = ".wav"
    description = "本地显卡推理的曼波音色，需先启动 GPT-SoVITS 服务；音质最像人。"

    def __init__(self, conf) -> None:
        # conf: config.MamboSettings
        self.conf = conf

    # ---------------- 预设 ----------------
    def presets(self) -> list[dict]:
        presets = getattr(self.conf, "presets", None) or []
        out = [p for p in presets if isinstance(p, dict)]
        return out or [{"name": "曼波", "ref_audio_path": "", "prompt_text": "", "prompt_lang": "zh"}]

    def active_preset(self, voice: str = "") -> dict:
        presets = self.presets()
        idx = getattr(self.conf, "active_preset", 0)
        if voice:
            for p in presets:
                if voice in (p.get("name"), str(presets.index(p))):
                    return p
            if voice.isdigit():
                idx = int(voice)
        idx = max(0, min(len(presets) - 1, int(idx)))
        return presets[idx]

    def voices(self) -> list[tuple[str, str]]:
        return [(str(i), p.get("name") or f"预设{i + 1}") for i, p in enumerate(self.presets())]

    def cache_tag(self, req: SpeechRequest) -> str:
        p = self.active_preset(req.voice)
        return f"{self.id}|{p.get('name')}|{p.get('ref_audio_path')}"

    # ---------------- 连接 ----------------
    @property
    def base_url(self) -> str:
        return (self.conf.base_url or "http://127.0.0.1:9880").rstrip("/")

    def _tts_url(self) -> str:
        base = self.base_url
        if base.endswith("/tts"):
            return base
        return urljoin(base + "/", "tts")

    def probe(self) -> tuple[bool, str]:
        import requests

        preset = self.active_preset()
        if not preset.get("ref_audio_path"):
            return False, "未配置参考音频（设置 → 语音引擎 → 曼波）"
        if not Path(preset["ref_audio_path"]).exists():
            return False, f"参考音频不存在：{preset['ref_audio_path']}"
        try:
            # api_v2 根路径返回 404 也算服务在线
            requests.get(self.base_url, timeout=3)
        except requests.exceptions.RequestException:
            return False, f"服务未启动：{self.base_url}（先跑 api_v2.py 或点「自动启动」）"
        return True, "服务在线"

    # ---------------- 合成 ----------------
    def synthesize(self, req: SpeechRequest, out_path: Path) -> None:
        import requests

        preset = self.active_preset(req.voice)
        ref = preset.get("ref_audio_path") or ""
        if not ref:
            raise RuntimeError("曼波引擎未配置参考音频，请到设置里填写")
        if not Path(ref).exists():
            raise RuntimeError(f"参考音频不存在：{ref}")

        params = {
            "text": req.text,
            "text_lang": self.conf.text_lang or "zh",
            "ref_audio_path": ref,
            "prompt_text": preset.get("prompt_text") or "",
            "prompt_lang": preset.get("prompt_lang") or self.conf.prompt_lang or "zh",
            "text_split_method": self.conf.text_split_method or "cut5",
            "batch_size": 1,
            "media_type": self.conf.media_type or "wav",
            "streaming_mode": "false",
            "speed_factor": str(round(max(0.5, min(2.0, req.rate)), 3)),
            "top_k": int(getattr(self.conf, "top_k", 10)),
            "top_p": 1,
            "temperature": float(getattr(self.conf, "temperature", 0.8)),
            "repetition_penalty": float(getattr(self.conf, "repetition_penalty", 1.15)),
        }
        try:
            resp = requests.get(self._tts_url(), params=params, timeout=int(self.conf.timeout or 90))
        except requests.exceptions.ConnectionError as exc:
            raise RuntimeError(
                f"连不上曼波服务 {self.base_url}；请确认 api_v2.py 已启动，"
                f"或在设置里点「自动启动曼波服务」"
            ) from exc
        except requests.exceptions.Timeout as exc:
            raise RuntimeError("曼波合成超时（句子太长或显存不足）") from exc

        if resp.status_code != 200:
            detail = resp.text[:300] if resp.text else ""
            raise RuntimeError(f"曼波服务返回 {resp.status_code}：{detail}")
        if len(resp.content) < 512:
            raise RuntimeError("曼波返回音频为空（检查参考音频与模型是否匹配）")
        out_path.write_bytes(resp.content)
