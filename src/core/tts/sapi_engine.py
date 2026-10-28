"""Windows 离线语音（SAPI）：无需联网的兜底方案。

不支持音调变化，语速通过 words-per-minute 映射。
"""
from __future__ import annotations

from pathlib import Path

from .base import SpeechRequest, TTSEngine


class SapiEngine(TTSEngine):
    id = "sapi"
    label = "Windows 离线语音（SAPI）"
    supports_pitch = False
    needs_network = False
    file_suffix = ".wav"
    description = "系统自带语音，完全离线；音色机械，适合断网时兜底。"

    def __init__(self, voice: str = "") -> None:
        self.voice = voice

    def _init_engine(self):
        import pyttsx3

        engine = pyttsx3.init()
        return engine

    def voices(self) -> list[tuple[str, str]]:
        try:
            engine = self._init_engine()
            out: list[tuple[str, str]] = []
            for v in engine.getProperty("voices") or []:
                out.append((v.id, f"{v.name}"))
            engine.stop()
            return out
        except Exception:  # noqa: BLE001
            return []

    def probe(self) -> tuple[bool, str]:
        try:
            import pyttsx3  # noqa: F401
        except ImportError:
            return False, "未安装 pyttsx3"
        try:
            engine = self._init_engine()
            engine.stop()
            return True, "已就绪（完全离线）"
        except Exception as exc:  # noqa: BLE001
            return False, f"初始化失败：{exc}"

    def synthesize(self, req: SpeechRequest, out_path: Path) -> None:
        engine = self._init_engine()
        try:
            wpm = int(round(175 * max(0.5, min(2.0, req.rate))))
            engine.setProperty("rate", max(60, min(380, wpm)))
            if req.voice:
                engine.setProperty("voice", req.voice)
            engine.save_to_file(req.text, str(out_path))
            engine.runAndWait()
        finally:
            try:
                engine.stop()
            except Exception:  # noqa: BLE001
                pass
        if not out_path.exists() or out_path.stat().st_size < 512:
            raise RuntimeError("SAPI 未生成音频")
