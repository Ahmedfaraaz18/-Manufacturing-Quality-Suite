"""Adapter around the supplied reference helper (Model/inference.py).

The helper is authoritative: its ``predict`` function is imported and called unchanged, so the
preprocessing, weights and 0.5 threshold are exactly the ones supplied. This module only adds
integrity checking, a process-level cache of the imported helper, and result sanity checks.
"""

import hashlib
import importlib.util
import json
import logging
import threading
from pathlib import Path

from django.conf import settings

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_helper = None


class InferenceError(RuntimeError):
    """``user_message`` is safe to show to end users; the cause is only logged."""

    user_message = 'The classifier could not process this image.'


class ModelUnavailable(InferenceError):
    user_message = 'The inspection model is unavailable. Please contact an administrator.'


def model_dir() -> Path:
    return Path(settings.ROUND2_PACK_DIR) / 'Model'


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _expected_checksums() -> dict:
    sums = Path(settings.ROUND2_PACK_DIR) / 'SHA256SUMS.txt'
    expected = {}
    for line in sums.read_text(encoding='utf-8').splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) == 2:
            expected[parts[1].strip()] = parts[0].lower()
    return expected


def verify_model_integrity() -> dict:
    """Compare the model files with the checksums shipped in the pack."""
    expected = _expected_checksums()
    results = {}
    for name in ('Model/classifier.npz', 'Model/inference.py', 'Model/model_metadata.json'):
        path = Path(settings.ROUND2_PACK_DIR) / name
        results[name] = path.is_file() and expected.get(name) == _sha256(path)
    return results


def verify_organizer_integrity() -> dict:
    """Verify every file listed in the organizer manifest without modifying any asset."""
    try:
        expected = _expected_checksums()
    except OSError:
        return {}
    root = Path(settings.ROUND2_PACK_DIR)
    results = {}
    for name, expected_hash in expected.items():
        path = root / name
        try:
            results[name] = path.is_file() and _sha256(path) == expected_hash
        except OSError:
            results[name] = False
    return results


def _load_helper():
    """Import the supplied helper once per process (works independently in every Celery worker)."""
    global _helper
    if _helper is not None:
        return _helper
    with _lock:
        if _helper is not None:
            return _helper
        directory = model_dir()
        helper_path, weights = directory / 'inference.py', directory / 'classifier.npz'
        try:
            if not helper_path.is_file() or not weights.is_file():
                raise ModelUnavailable('model files are missing')
            if settings.INSPECTION_VERIFY_MODEL_CHECKSUMS and not all(verify_model_integrity().values()):
                raise ModelUnavailable('model files do not match the supplied checksums')
            spec = importlib.util.spec_from_file_location('round2_reference_inference', helper_path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            if not callable(getattr(module, 'predict', None)):
                raise ModelUnavailable('the helper does not define predict()')
        except ModelUnavailable:
            logger.exception('Inspection model unavailable')
            raise
        except Exception as exc:
            logger.exception('Inspection model could not be loaded')
            raise ModelUnavailable('model could not be loaded') from exc
        _helper = module
    return _helper


def reset_cache():
    """Test hook: forget the imported helper."""
    global _helper
    with _lock:
        _helper = None


def model_metadata() -> dict:
    try:
        return json.loads((model_dir() / 'model_metadata.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def predict_image(image_path) -> dict:
    """Run the supplied classifier on a validated, saved image file."""
    helper = _load_helper()
    try:
        result = helper.predict(str(image_path), str(model_dir() / 'classifier.npz'))
        probability = float(result['defect_probability'])
        predicted = result['predicted_class']
        confidence = float(result['confidence'])
        version = str(result['model_version'])
    except Exception as exc:
        logger.exception('Classifier failed for %s', image_path)
        raise InferenceError('classifier raised an error') from exc
    # Sanity checks only; values are never adjusted.
    if not (0.0 <= probability <= 1.0 and 0.0 <= confidence <= 1.0) or not version:
        raise InferenceError('classifier returned out-of-range values')
    if predicted != ('Defective' if probability >= 0.5 else 'Normal'):
        raise InferenceError('classifier output is inconsistent with the 0.5 threshold')
    return {
        'predicted_class': predicted, 'confidence': confidence,
        'defect_probability': probability, 'model_version': version,
    }
