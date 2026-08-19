"""bge-m3 embeddings behind a swappable interface (spec §8).

Two implementations of the same contract:

* ``LocalEmbedder`` — sentence-transformers on CPU. The model costs ~2 GB of
  RAM, so exactly one process (the API container) loads it.
* ``RemoteEmbedder`` — forwards to the API's ``POST /v1/embed`` over the
  compose network. The bot and worker use this so they stay lightweight.

``get_embedder()`` picks by ``EMBED_SERVICE_URL``: set → remote, empty → local.
Vectors are L2-normalised, so pgvector's cosine distance is the right metric.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from abc import ABC, abstractmethod

import httpx

from miya.config import settings

log = logging.getLogger(__name__)


class EmbeddingError(RuntimeError):
    pass


class Embedder(ABC):
    name: str = "base"

    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """One vector of ``settings.embed_dim`` floats per input text."""


class LocalEmbedder(Embedder):
    """sentence-transformers BAAI/bge-m3 on CPU, loaded lazily and once."""

    name = "local"

    def __init__(self, model_name: str | None = None) -> None:
        self._model_name = model_name or settings.embed_model
        self._model = None
        # The first two concurrent embed() calls must not both load the model.
        self._load_lock = threading.Lock()

    def _get_model(self):
        if self._model is None:
            with self._load_lock:
                if self._model is None:
                    # Deferred import: torch is a ~1 GB dependency the bot and
                    # worker containers never need to import.
                    from sentence_transformers import SentenceTransformer

                    log.info("loading embedding model %s ...", self._model_name)
                    self._model = SentenceTransformer(self._model_name, device="cpu")
                    log.info("embedding model %s ready", self._model_name)
        return self._model

    def _encode(self, texts: list[str]) -> list[list[float]]:
        model = self._get_model()
        vectors = model.encode(texts, normalize_embeddings=True)
        return [v.tolist() for v in vectors]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            # Model inference is CPU-bound; keep it off the event loop.
            return await asyncio.to_thread(self._encode, texts)
        except Exception as exc:  # torch/model errors are opaque — wrap them all
            raise EmbeddingError(f"local embedding failed: {exc}") from exc


class RemoteEmbedder(Embedder):
    """Calls the API process's /v1/embed so only it holds the model in RAM."""

    name = "remote"

    def __init__(self, base_url: str | None = None, timeout: float = 120.0) -> None:
        self._base_url = (base_url or settings.embed_service_url).rstrip("/")
        self._timeout = timeout

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    f"{self._base_url}/v1/embed",
                    headers={"Authorization": f"Bearer {settings.api_bearer_token}"},
                    json={"texts": texts},
                )
        except httpx.HTTPError as exc:
            raise EmbeddingError(f"embed service unreachable: {exc}") from exc
        if response.status_code >= 400:
            raise EmbeddingError(
                f"embed service returned {response.status_code}: {response.text[:200]}"
            )
        try:
            vectors = response.json()["vectors"]
        except (ValueError, KeyError) as exc:
            raise EmbeddingError("embed service returned an unparseable body") from exc
        if len(vectors) != len(texts):
            raise EmbeddingError(
                f"embed service returned {len(vectors)} vectors for {len(texts)} texts"
            )
        return vectors


_local: LocalEmbedder | None = None


def get_local_embedder() -> LocalEmbedder:
    """The API process's singleton — also serves /v1/embed."""
    global _local
    if _local is None:
        _local = LocalEmbedder()
    return _local


def get_embedder() -> Embedder:
    if settings.embed_service_url:
        return RemoteEmbedder()
    return get_local_embedder()
