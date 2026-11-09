import io
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from PIL import Image
from rest_framework.test import APIClient

from assistant.models import Role, User
from inspections import services
from inspections.models import Inspection, Product

PACK = Path(settings.ROUND2_PACK_DIR)
SAMPLES = PACK / 'Samples'
VALIDATION = PACK / 'Validation'

FAKE_RESULT = {
    'predicted_class': 'Defective', 'confidence': 0.9, 'defect_probability': 0.9, 'model_version': 'TEST-MODEL-1.0',
}


def image_bytes(fmt='PNG', size=(256, 256), color=(120, 120, 120), mode='RGB'):
    image = Image.new(mode, size, color if mode in ('RGB', 'RGBA') else 128)
    buffer = io.BytesIO()
    image.save(buffer, format=fmt)
    return buffer.getvalue()


def upload(name='plate.png', data=None, content_type='image/png', **kwargs):
    return SimpleUploadedFile(name, data if data is not None else image_bytes(**kwargs), content_type=content_type)


def sample_upload(name='inspection_001.png'):
    path = SAMPLES / 'images' / name
    return SimpleUploadedFile(name, path.read_bytes(), content_type='image/png')


class InspectionTestCase(TestCase):
    """Isolated media directory, one user per role, and the P-PLATE01 product."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._media = tempfile.mkdtemp()
        cls._override = override_settings(
            MEDIA_ROOT=cls._media, PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'],
        )
        cls._override.enable()

    @classmethod
    def tearDownClass(cls):
        cls._override.disable()
        shutil.rmtree(cls._media, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        self.admin = User.objects.create_user('admin1', password='pw-Admin-1!', role=Role.ADMINISTRATOR)
        self.engineer = User.objects.create_user('eng1', password='pw-Eng-1!', role=Role.ENGINEER)
        self.engineer2 = User.objects.create_user('eng2', password='pw-Eng-2!', role=Role.ENGINEER)
        self.supervisor = User.objects.create_user('sup1', password='pw-Sup-1!', role=Role.SUPERVISOR)
        self.product = Product.objects.create(
            product_id='P-PLATE01', name='Demonstration metal plate', description='Synthetic centered plate',
        )

    # -- helpers ----------------------------------------------------------
    def client_for(self, user):
        client = APIClient()
        client.force_login(user)
        return client

    @contextmanager
    def worker(self, result=None, side_effect=None):
        """Mocked classifier plus an in-process stand-in for the Celery worker (isolated unit tests)."""
        with mock.patch('inspections.services.predict_image', return_value=result or FAKE_RESULT, side_effect=side_effect) as fake:
            with mock.patch('inspections.tasks.run_inspection.delay', side_effect=services.process_inspection):
                with self.captureOnCommitCallbacks(execute=True):
                    yield fake

    @contextmanager
    def real_worker(self):
        """Real supplied classifier, still run in-process instead of by a separate Celery worker."""
        with mock.patch('inspections.tasks.run_inspection.delay', side_effect=services.process_inspection):
            with self.captureOnCommitCallbacks(execute=True):
                yield

    @contextmanager
    def queue_only(self):
        """Capture enqueued task ids without running anything."""
        with mock.patch('inspections.tasks.run_inspection.delay') as delay:
            with self.captureOnCommitCallbacks(execute=True):
                yield delay

    def make_completed(self, user=None, predicted='Defective', name='plate.png'):
        """A completed inspection created through the real upload path with a mocked classifier."""
        user = user or self.engineer
        result = dict(FAKE_RESULT, predicted_class=predicted,
                      defect_probability=0.9 if predicted == 'Defective' else 0.1,
                      confidence=0.9)
        with self.worker(result=result):
            batch = services.create_batch(user, self.product, [upload(name)]).batch
        return batch.inspections.get()
