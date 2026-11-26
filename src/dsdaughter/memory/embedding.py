"""Embedding 适配：默认不启用；配置了 OpenAI 兼容的 embeddings 接口才开。

DeepSeek 官方没有 embeddings 接口（2026-11 文档），所以向量检索是可选项：
- 不配：纯词面 + 时间衰减 + 重要度（零依赖，中文短句效果够用）
- 配了：叠加向量相似度（SiliconFlow / 阿里百炼 / Ollama 等都兼容 /v1/embeddings）
"""

from __future__ import annotations

from typing import Protocol

import httpx
import structlog

log = structlog.get_logger(__name__)


class Embedder(Protocol):
    dimension: int

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class OpenAICompatEmbedder:
    def __init__(
        self, *, base_url: str, model: str, api_key: str = "", dimension: int = 1024
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key
        self.dimension = dimension

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{self._base_url}/embeddings",
                json={"model": self._model, "input": texts},
                headers=headers,
            )
            response.raise_for_status()
            payload = response.json()
        data = payload.get("data") or []
        vectors: list[list[float]] = []
        for item in sorted(data, key=lambda entry: entry.get("index", 0)):
            vector = [float(value) for value in item.get("embedding") or []]
            vectors.append(vector)
        if len(vectors) != len(texts):
            raise RuntimeError("embedding 返回数量不对")
        return vectors


def build_embedder(settings) -> Embedder | None:
    if not settings.embedding_base_url:
        return None
    log.info("启用向量检索", base_url=settings.embedding_base_url, model=settings.embedding_model)
    return OpenAICompatEmbedder(
        base_url=settings.embedding_base_url,
        model=settings.embedding_model,
        api_key=settings.embedding_api_key,
        dimension=settings.embedding_dim,
    )
