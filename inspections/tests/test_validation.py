"""Upload validation: decoded content, dimensions, size and format - never name or MIME type."""

import io
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, override_settings
from PIL import Image

from inspections.validators import ImageValidationError, clean_filename, validate_image_upload

from .base import VALIDATION, image_bytes, upload


def png_noise(size):
    import os

    image = Image.frombytes('RGB', size, os.urandom(size[0] * size[1] * 3))
    buffer = io.BytesIO()
    image.save(buffer, format='PNG')
    return buffer.getvalue()


class ImageValidationTests(SimpleTestCase):
    def reject(self, file, fragment=None):
        with self.assertRaises(ImageValidationError) as ctx:
            validate_image_upload(file)
        if fragment:
            self.assertIn(fragment, str(ctx.exception))
        return str(ctx.exception)

    def test_valid_jpeg(self):
        result = validate_image_upload(upload('a.jpg', image_bytes('JPEG'), 'image/jpeg'))
        self.assertEqual((result.format, result.extension, result.width, result.height), ('JPEG', 'jpg', 256, 256))

    def test_valid_png(self):
        result = validate_image_upload(upload('a.png', image_bytes('PNG')))
        self.assertEqual((result.format, result.extension, result.content_type), ('PNG', 'png', 'image/png'))

    def test_boundary_dimensions_are_accepted(self):
        for size in ((64, 64), (4096, 64), (64, 4096)):
            result = validate_image_upload(upload('edge.png', image_bytes('PNG', size=size)))
            self.assertEqual((result.width, result.height), size)

    def test_extension_and_mime_type_are_ignored(self):
        # A real PNG named .txt with a wrong MIME type is still accepted, and a stored extension comes from the format.
        result = validate_image_upload(upload('plate.txt', image_bytes('PNG'), 'text/plain'))
        self.assertEqual(result.extension, 'png')

    def test_text_file_renamed_to_jpg(self):
        self.reject(upload('not_an_image.jpg', b'this is just text, not an image', 'image/jpeg'), 'not a valid JPEG or PNG')

    def test_supplied_text_file_with_jpg_extension(self):
        data = (VALIDATION / 'not_an_image.jpg').read_bytes()
        self.reject(upload('not_an_image.jpg', data, 'image/jpeg'))

    def test_corrupt_image(self):
        data = image_bytes('PNG')
        corrupted = data[:60] + bytes(b ^ 0xFF for b in data[60:200]) + data[200:]
        self.reject(upload('corrupt.png', corrupted))

    def test_supplied_truncated_png(self):
        self.reject(upload('corrupt_image.png', (VALIDATION / 'corrupt_image.png').read_bytes()), 'corrupt or truncated')

    def test_truncated_png_and_jpeg(self):
        png = image_bytes('PNG', size=(300, 300))
        self.reject(upload('t.png', png[: len(png) // 2]), 'corrupt or truncated')
        jpeg = image_bytes('JPEG', size=(300, 300))
        self.reject(upload('t.jpg', jpeg[: len(jpeg) // 2], 'image/jpeg'), 'corrupt or truncated')

    def test_too_small(self):
        for size in ((63, 256), (256, 63), (32, 32)):
            self.reject(upload('s.png', image_bytes('PNG', size=size)), 'at least 64')
        self.reject(upload('too_small.png', (VALIDATION / 'too_small.png').read_bytes()), '32x32')

    def test_oversized_dimensions(self):
        self.reject(upload('big.png', image_bytes('PNG', size=(4097, 100))), 'at most 4096')
        self.reject(upload('big.png', image_bytes('PNG', size=(100, 4097))), 'at most 4096')

    def test_file_over_five_mib(self):
        data = png_noise((1500, 1500))   # incompressible noise: ~6.7 MB, well over 5 MiB
        self.assertGreater(len(data), 5 * 1024 * 1024)
        self.reject(upload('huge.png', data), 'exceeds the 5 MiB limit')

    def test_empty_file(self):
        self.reject(upload('empty.png', b''), 'empty')

    def test_unsupported_formats(self):
        for fmt in ('GIF', 'BMP', 'WEBP', 'TIFF'):
            self.reject(upload(f'x.{fmt.lower()}', image_bytes(fmt)), 'Unsupported image format')

    def test_decompression_bomb_is_rejected_without_decoding(self):
        data = image_bytes('PNG', size=(256, 256))        # 65,536 pixels
        for limit in (40000, 10000):                      # warning band (1x-2x) and hard-error band (>2x)
            with mock.patch.object(Image, 'MAX_IMAGE_PIXELS', limit):
                self.reject(upload('bomb.png', data), 'unsafe')

    def test_animated_images_are_rejected(self):
        frames = [Image.new('RGB', (100, 100), c) for c in ((255, 0, 0), (0, 255, 0))]
        buffer = io.BytesIO()
        frames[0].save(buffer, format='PNG', save_all=True, append_images=frames[1:])
        self.reject(upload('anim.png', buffer.getvalue()), 'Animated')

    def test_file_size_limit_is_configurable(self):
        with override_settings(INSPECTION_MAX_FILE_BYTES=50):
            self.reject(upload('p.png', image_bytes('PNG', size=(256, 256))), 'exceeds')

    def test_clean_filename_strips_paths_and_control_characters(self):
        self.assertEqual(clean_filename('..\\..\\evil/../name\x00.png'), 'name.png')
        self.assertEqual(clean_filename(''), 'upload')
        self.assertEqual(len(clean_filename('a' * 400 + '.png')), 255)
