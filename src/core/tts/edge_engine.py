"""Edge 在线语音：免费、无需 Key、支持语速与音调。

走微软 speech.platform.bing.com 的公开接口（edge-tts 库），国内一般可直连。
缺点：非官方承诺的稳定接口；断网不可用。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from .base import SpeechRequest, TTSEngine

# 常用的中文音色（可在设置里下拉选择）
ZH_VOICES: list[tuple[str, str]] = [
    ("zh-CN-YunxiNeural", "云希 · 男声 · 年轻清朗"),
    ("zh-CN-YunjianNeural", "云健 · 男声 · 浑厚解说"),
    ("zh-CN-YunyangNeural", "云扬 · 男声 · 播音腔"),
    ("zh-CN-XiaoxiaoNeural", "晓晓 · 女声 · 自然亲和"),
    ("zh-CN-XiaoyiNeural", "晓伊 · 女声 · 甜美人设"),
    ("zh-CN-YunxiaNeural", "云夏 · 男童"),
    ("zh-CN-XiaochenNeural", "晓辰 · 女声 · 温柔"),
    ("zh-CN-liaoning-XiaobeiNeural", "晓北 · 东北女声"),
    ("zh-CN-shaanxi-XiaoniNeural", "晓妮 · 陕西女声"),
    ("zh-HK-HiuMaanNeural", "曉曼 · 粤语女声"),
    ("zh-TW-HsiaoChenNeural", "曉臻 · 台湾女声"),
]


class EdgeEngine(TTSEngine):
    id = "edge"
    label = "Edge 在线语音（免费）"
    supports_pitch = True
    needs_network = True
    file_suffix = ".mp3"
    description = "微软 Edge 朗读引擎，免费无需 Key，支持语速和音调；需联网。"

    def __init__(self, voice: str = "zh-CN-YunxiNeural") -> None:
        self.voice = voice or "zh-CN-YunxiNeural"

    def voices(self) -> list[tuple[str, str]]:
        return list(ZH_VOICES)

    def cache_tag(self, req: SpeechRequest) -> str:
        return f"{self.id}|{req.voice or self.voice}"

    def probe(self) -> tuple[bool, str]:
        try:
            import edge_tts  # noqa: F401
        except ImportError:
            return False, "未安装 edge-tts"
        return True, "已就绪（需联网）"

    def synthesize(self, req: SpeechRequest, out_path: Path) -> None:
        import edge_tts

        voice = req.voice or self.voice
        percent = int(round((max(0.5, min(2.0, req.rate)) - 1.0) * 100))
        rate_str = f"{percent:+d}%"
        pitch_str = f"{int(max(-60, min(60, req.pitch))):+d}Hz"

        last_err: Exception | None = None
        for _attempt in range(2):
            try:
                async def _run() -> None:
                    comm = edge_tts.Communicate(req.text, voice, rate=rate_str, pitch=pitch_str)
                    await comm.save(str(out_path))

                asyncio.run(_run())
                if out_path.exists() and out_path.stat().st_size > 512:
                    return
                last_err = RuntimeError("返回音频为空")
            except Exception as exc:  # noqa: BLE001
                last_err = exc
        raise RuntimeError(f"Edge 合成失败：{last_err}")
