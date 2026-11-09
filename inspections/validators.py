"""Upload validation. Everything is judged on the decoded content, never on name or MIME type."""

import io
import os
import re
import warnings
from dataclasses import dataclass

from django.conf import settings
from PIL import Image, UnidentifiedImageError

ALLOWED_FORMATS = {'JPEG': ('jpg', 'image/jpeg'), 'PNG': ('png', 'image/png')}
MIB = 1024 * 1024


class ImageValidationError(ValueError):
    """The message is readable and safe to show to the submitting user."""


@dataclass(frozen=True)
class ValidatedImage:
    data: bytes
    format: str
    width: int
    height: int
    size: int

    @property
    def extension(self):
        return ALLOWED_FORMATS[self.format][0]

    @property
    def content_type(self):
        return ALLOWED_FORMATS[self.format][1]


def clean_filename(name) -> str:
    """Display-only copy of the submitted name: no path components or control characters."""
    name = os.path.basename(str(name or '').replace('\\', '/'))
    name = re.sub(r'[\x00-\x1f\x7f]', '', name).strip()
    return (name or 'upload')[:255]


def validate_image_upload(uploaded_file) -> ValidatedImage:
    max_bytes = settings.INSPECTION_MAX_FILE_BYTES
    lo, hi = settings.INSPECTION_MIN_DIMENSION, settings.INSPECTION_MAX_DIMENSION

    size = getattr(uploaded_file, 'size', None) or 0
    if size == 0:
        raise ImageValidationError('The file is empty.')
    if size > max_bytes:
        raise ImageValidationError(
            f'The file is {size / MIB:.1f} MiB, which exceeds the {max_bytes // MIB} MiB limit.'
        )
    uploaded_file.seek(0)
    data = uploaded_file.read(max_bytes + 1)  # bounded read: never more than one file in memory
    if not data:
        raise ImageValidationError('The file is empty.')
    if len(data) > max_bytes:
        raise ImageValidationError(f'The file exceeds the {max_bytes // MIB} MiB limit.')

    corrupt = ImageValidationError('The image is corrupt or truncated and cannot be decoded.')
    try:
        with warnings.catch_warnings():
            # A decompression-bomb warning is treated as a rejection, not a log line.
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:  # reads the header only
                fmt, (width, height) = image.format, image.size
                if fmt not in ALLOWED_FORMATS:
                    raise ImageValidationError(
                        f'Unsupported image format ({fmt or "unknown"}). Only JPEG and PNG are accepted.'
                    )
                # Dimension limits are applied before any pixel data is decoded.
                if width < lo or height < lo:
                    raise ImageValidationError(
                        f'The image is {width}x{height} pixels; each side must be at least {lo} pixels.'
                    )
                if width > hi or height > hi:
                    raise ImageValidationError(
                        f'The image is {width}x{height} pixels; each side must be at most {hi} pixels.'
                    )
                if getattr(image, 'n_frames', 1) > 1:
                    raise ImageValidationError('Animated or multi-frame images are not supported.')
                image.verify()
            with Image.open(io.BytesIO(data)) as image:
                image.load()  # a full decode exposes truncated data that verify() can miss
    except ImageValidationError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ImageValidationError('The image dimensions are unsafe or exceed the allowed maximum.') from None
    except UnidentifiedImageError:
        if data.startswith((b'\x89PNG', b'\xff\xd8')):
            raise corrupt from None  # recognisable PNG/JPEG signature, but the contents are broken
        raise ImageValidationError('The file is not a valid JPEG or PNG image.') from None
    except (OSError, SyntaxError, ValueError, EOFError, MemoryError):
        raise corrupt from None
    return ValidatedImage(data=data, format=fmt, width=width, height=height, size=len(data))
