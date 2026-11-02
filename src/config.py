"""全局配置与数据目录管理。

数据统一放在 %APPDATA%/VoiceRead 下，避免打包后写入程序目录失败：
  settings.json   用户设置
  library.json    书架（书籍元数据 + 章节文本缓存）
  progress.json   每本书的阅读进度
  cache/tts/      语音合成缓存
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any


def app_data_dir() -> Path:
    base = os.getenv("APPDATA") or os.getenv("LOCALAPPDATA")
    if base:
        d = Path(base) / "VoiceRead"
    else:  # 非 Windows 兜底
        d = Path.home() / ".voiceread"
    d.mkdir(parents=True, exist_ok=True)
    return d


DATA_DIR = app_data_dir()
CACHE_DIR = DATA_DIR / "cache" / "tts"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# 曼波参考音频候选位置（首个存在的会被自动填进设置）
# 全部按「程序所在目录 / 仓库根目录 / 环境变量」推导，不写死任何机器路径
# 优先用修剪版：首尾静音/换气被裁掉，句首「吸气声」明显减少
_APP_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent


def _mambo_candidates() -> list[Path]:
    roots: list[Path] = []
    env_root = os.getenv("VOICEREAD_MAMBO_DIR")
    if env_root:
        roots.append(Path(env_root))
    roots.append(_APP_DIR / "mambo")
    roots.append(Path(__file__).resolve().parent.parent / "mambo")
    out: list[Path] = []
    for root in roots:
        out.append(root / "refs" / "mambo_ref_clean.wav")
        out.append(root / "refs" / "mambo_ref.wav")
    return out


DEFAULT_MAMBO_REFS = _mambo_candidates()


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def load_json(path: Path, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


@dataclass
class SubtitleSettings:
    font_size: int = 22
    font_family: str = "Microsoft YaHei UI"
    opacity: int = 88            # 背景不透明度 0-100
    bg_color: str = "#101418"
    fg_color: str = "#F2F5F7"
    highlight_color: str = "#8FD3FF"
    max_lines: int = 2
    show_context: bool = True    # 是否显示上下句
    click_through: bool = False  # 鼠标穿透
    visible: bool = True
    x: int = -1
    y: int = -1
    width: int = 760
    text_align: str = "center"


@dataclass
class MamboPreset:
    """一个「音色预设」= 一段参考音频 + 对应文本，GPT-SoVITS 用它来决定音色。"""
    name: str = "曼波"
    ref_audio_path: str = ""
    prompt_text: str = ""
    prompt_lang: str = "zh"


@dataclass
class MamboSettings:
    """本地曼波模型（GPT-SoVITS api_v2 兼容服务）参数。"""
    base_url: str = "http://127.0.0.1:9880"
    prompt_lang: str = "zh"
    text_lang: str = "zh"
    text_split_method: str = "cut5"
    media_type: str = "wav"
    timeout: int = 90
    auto_start: bool = True
    # 采样参数：temperature 偏高会让语速/韵律飘忽，1.0 → 0.8 明显更稳
    temperature: float = 0.8
    top_k: int = 10
    repetition_penalty: float = 1.15
    # 启动命令留空：程序会在找到 mambo\venv 后自动推导（见 _seed_mambo_defaults）
    # 想手动指定就填绝对路径，例如 "D:\mambo\venv\Scripts\python.exe" api_v2.py -a 127.0.0.1 -p 9880
    start_command: str = ""
    workdir: str = ""
    active_preset: int = 0
    presets: list = field(default_factory=lambda: [asdict(MamboPreset())])


@dataclass
class Settings:
    engine_id: str = "mambo"
    edge_voice: str = "zh-CN-YunxiNeural"
    sapi_voice: str = ""
    speed: float = 1.0
    pitch: int = 0                # 音调偏移（Hz），Edge 支持
    auto_prosody: bool = True     # 根据上下文自动调节
    prosody_strength: float = 1.0
    prefetch: int = 10            # 预加载队列深度（条 = 句）
    queue_model_v2: bool = False  # 迁移标记：老配置升到句子级队列
    cache_limit_mb: int = 800
    cache_retention_minutes: int = 10   # 音频缓存只保留最近 N 分钟
    last_book_id: str = ""
    first_run_done: bool = False
    subtitle: SubtitleSettings = field(default_factory=SubtitleSettings)
    mambo: MamboSettings = field(default_factory=MamboSettings)

    # ---------- 读写 ----------
    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        path = path or (DATA_DIR / "settings.json")
        raw = load_json(path, {})
        if not isinstance(raw, dict):
            raw = {}
        s = cls()
        for key, value in raw.items():
            if key in ("subtitle", "mambo") and isinstance(value, dict):
                sub = getattr(s, key)
                for k, v in value.items():
                    if hasattr(sub, k):
                        setattr(sub, k, v)
            elif hasattr(s, key):
                setattr(s, key, value)
        s.path = path
        s._seed_mambo_defaults()
        # v1.1 起预加载改成"句子级队列"，默认深度 10；
        # 老配置里的 3 是旧"预合成句数"语义，迁移一次即可
        if not s.queue_model_v2:
            s.prefetch = 10
            s.queue_model_v2 = True
        return s

    def _seed_mambo_defaults(self) -> None:
        """首次运行时自动认到随包部署的曼波参考音频，省得用户手填。"""
        presets = self.mambo.presets
        if not isinstance(presets, list) or not presets:
            presets = [{"name": "曼波", "ref_audio_path": "", "prompt_text": "", "prompt_lang": "zh"}]
            self.mambo.presets = presets
        first = presets[0]
        if not isinstance(first, dict):
            return
        if not first.get("ref_audio_path"):
            for candidate in DEFAULT_MAMBO_REFS:
                if candidate.exists():
                    first["ref_audio_path"] = str(candidate)
                    break
        # 参考文本：与参考音频同目录的 prompt_text.txt（ASR 生成）
        if not first.get("prompt_text") and first.get("ref_audio_path"):
            side = Path(first["ref_audio_path"]).with_name("prompt_text.txt")
            if side.exists():
                try:
                    text = side.read_text(encoding="utf-8").strip()
                except OSError:
                    text = ""
                if text:
                    first["prompt_text"] = text
                    first["prompt_lang"] = "zh"

        # 换了台电脑、找不到曼波环境时自动退回 Edge，避免一启动就报错
        if self.engine_id == "mambo" and not first.get("ref_audio_path"):
            self.engine_id = "edge"
            self.mambo.auto_start = False
            return

        # 参考音频找到了 → 顺手把服务启动命令/工作目录推导成相对本机的路径，
        # 这样整套目录拷到别的盘/别的电脑也不用改配置
        if first.get("ref_audio_path"):
            ref = Path(first["ref_audio_path"])
            mambo_dir = ref.parent.parent                      # refs → mambo
            venv_py = mambo_dir / "venv" / "Scripts" / "python.exe"
            gsv = mambo_dir / "GPT-SoVITS"
            if venv_py.exists() and gsv.is_dir():
                self.mambo.start_command = (
                    f'"{venv_py}" api_v2.py -a 127.0.0.1 -p 9880'
                )
                self.mambo.workdir = str(gsv)
                if self.engine_id == "mambo":
                    self.mambo.auto_start = True
            else:
                # 有参考音频但没有 venv（还没装环境）：不要自动拉起
                self.mambo.auto_start = False

    def save(self) -> None:
        path = getattr(self, "path", None) or (DATA_DIR / "settings.json")
        payload = asdict(self)
        payload.pop("path", None)
        _atomic_write_json(Path(path), payload)

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)
