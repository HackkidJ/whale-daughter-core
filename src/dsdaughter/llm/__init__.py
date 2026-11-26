"""LLM provider 工厂。"""

from __future__ import annotations

import structlog

from ..config import Settings
from .base import LLM, LLMError
from .deepseek import DeepSeekLLM
from .fake import FakeLLM

__all__ = ["DeepSeekLLM", "FakeLLM", "LLM", "LLMError", "build_llm"]

log = structlog.get_logger(__name__)


def build_llm(settings: Settings) -> LLM:
    """按配置选择 provider；没有 Key 时自动降级到 fake，避免首次启动就崩。"""
    if settings.llm_mode == "fake":
        log.info("使用 fake 模型（DSD_LLM_MODE=fake）")
        return FakeLLM()
    if not settings.deepseek_api_key:
        log.warning("没有配置 DEEPSEEK_API_KEY，自动切到 fake 模式")
        return FakeLLM()
    return DeepSeekLLM(settings)
