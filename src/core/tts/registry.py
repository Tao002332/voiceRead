"""引擎注册表：按设置构造引擎实例。"""
from __future__ import annotations

from .base import TTSEngine
from .edge_engine import EdgeEngine
from .mambo_engine import MamboEngine
from .sapi_engine import SapiEngine


def build_engine(engine_id: str, settings) -> TTSEngine:
    if engine_id == "mambo":
        return MamboEngine(settings.mambo)
    if engine_id == "sapi":
        return SapiEngine(settings.sapi_voice)
    return EdgeEngine(settings.edge_voice)


def all_engine_meta() -> list[tuple[str, str, str]]:
    """[(id, 名称, 说明)] 供设置界面展示。"""
    return [
        (EdgeEngine.id, EdgeEngine.label, EdgeEngine.description),
        (MamboEngine.id, MamboEngine.label, MamboEngine.description),
        (SapiEngine.id, SapiEngine.label, SapiEngine.description),
    ]
