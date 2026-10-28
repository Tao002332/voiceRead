"""语音引擎抽象层。

新增一种语音只需继承 TTSEngine 并在 registry 里注册：
  - id / label / 能力标记
  - probe() 判断当前是否可用
  - synthesize(req, out_path) 把一句话合成到本地文件
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class SpeechRequest:
    text: str
    rate: float = 1.0        # 语速倍率 0.5 - 2.0
    pitch: int = 0           # 音调偏移（Hz），仅部分引擎支持
    voice: str = ""          # 引擎内音色标识
    style: str = ""          # 语境标签（narration/dialogue/question...），部分引擎可用于情感控制
    extra: dict = field(default_factory=dict)


class TTSEngine(ABC):
    id: str = "base"
    label: str = "基础引擎"
    supports_pitch: bool = False
    supports_speed: bool = True
    needs_network: bool = False
    file_suffix: str = ".wav"
    description: str = ""

    @abstractmethod
    def synthesize(self, req: SpeechRequest, out_path: Path) -> None:
        """把 req.text 合成音频并写入 out_path；失败时抛异常。"""

    def probe(self) -> tuple[bool, str]:
        """返回 (是否可用, 说明)。默认可用。"""
        return True, ""

    def voices(self) -> list[tuple[str, str]]:
        """返回 [(音色标识, 显示名)]。"""
        return []

    def cache_tag(self, req: SpeechRequest) -> str:
        """参与缓存 key 的身份信息（音色等）。"""
        return f"{self.id}|{req.voice}"

    def close(self) -> None:
        """释放资源。"""
