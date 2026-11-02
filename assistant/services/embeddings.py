"""Local sentence-transformers embeddings. The model is loaded lazily, once per process."""

import threading
import logging

import numpy as np
from django.conf import settings

logger = logging.getLogger(__name__)

_model = None
_lock = threading.Lock()


class EmbeddingError(RuntimeError):
    pass


def _get_model():
    global _model
    if _model is None:
        with _lock:
            if _model is None:
                try:
                    from sentence_transformers import SentenceTransformer

                    _model = SentenceTransformer(settings.EMBEDDING_MODEL)
                except Exception as exc:
                    raise EmbeddingError(f'Could not load embedding model: {exc}') from exc
    return _model


def warm_up() -> bool:
    """Load the configured model during web-worker startup without failing startup.

    The existing lock and process-local cache in ``_get_model`` make this
    idempotent within a process.  Returning ``False`` leaves lazy loading in
    place, so a temporary startup failure can still recover on a later request.
    """
    try:
        _get_model()
    except EmbeddingError as exc:
        logger.error(
            'Embedding model warm-up failed for %s (%s); requests will retry lazy loading.',
            settings.EMBEDDING_MODEL,
            type(exc.__cause__).__name__ if exc.__cause__ else type(exc).__name__,
        )
        return False
    return True


def embed_texts(texts: list[str]) -> np.ndarray:
    """Return an (n, d) float32 matrix of L2-normalised embeddings, so dot product == cosine."""
    if not texts:
        return np.zeros((0, 0), dtype=np.float32)
    vectors = _get_model().encode(texts, normalize_embeddings=True, show_progress_bar=False)
    return np.asarray(vectors, dtype=np.float32)


def embed_query(text: str) -> np.ndarray:
    return embed_texts([text])[0]


def model_name() -> str:
    return settings.EMBEDDING_MODEL
