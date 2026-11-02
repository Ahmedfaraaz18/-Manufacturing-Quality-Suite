import hashlib
import re
import shutil
import tempfile
from unittest import mock

import numpy as np
import pymupdf
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from assistant.models import DocumentChunk, Machine, Manual, Role, User

DIM = 512


def fake_vector(text: str) -> np.ndarray:
    """Deterministic bag-of-words embedding: texts sharing words are similar. No model download."""
    vec = np.zeros(DIM, dtype=np.float32)
    for token in re.findall(r'[a-z0-9]+', text.lower()):
        vec[int(hashlib.md5(token.encode()).hexdigest(), 16) % DIM] += 1.0
    norm = np.linalg.norm(vec)
    return vec / norm if norm else vec


def fake_embed_texts(texts):
    return np.vstack([fake_vector(t) for t in texts]) if texts else np.zeros((0, DIM), dtype=np.float32)


def make_pdf(page_texts: list[str]) -> bytes:
    doc = pymupdf.open()
    for text in page_texts:
        page = doc.new_page()
        y = 72
        for line in text.split('\n'):
            page.insert_text((72, y), line, fontsize=11)
            y += 16
    data = doc.tobytes()
    doc.close()
    return data


def pdf_upload(page_texts, name='manual.pdf') -> SimpleUploadedFile:
    return SimpleUploadedFile(name, make_pdf(page_texts), content_type='application/pdf')


class BaseTestCase(TestCase):
    """Isolated media dir, fake embeddings, and one user per role."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._media = tempfile.mkdtemp()
        cls._override = override_settings(
            MEDIA_ROOT=cls._media,
            INGEST_ASYNC=False,
            PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'],
        )
        cls._override.enable()

    @classmethod
    def tearDownClass(cls):
        cls._override.disable()
        shutil.rmtree(cls._media, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        patcher = mock.patch('assistant.services.embeddings.embed_texts', side_effect=fake_embed_texts)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.admin = User.objects.create_user('admin1', password='pw-Admin-1!', role=Role.ADMINISTRATOR)
        self.engineer = User.objects.create_user('eng1', password='pw-Eng-1!', role=Role.ENGINEER)
        self.engineer2 = User.objects.create_user('eng2', password='pw-Eng-2!', role=Role.ENGINEER)
        self.supervisor = User.objects.create_user('sup1', password='pw-Sup-1!', role=Role.SUPERVISOR)
        self.hp = Machine.objects.create(machine_id='M-HP200', model_name='HP-200', name='Hydraulic forming press')
        self.ac = Machine.objects.create(machine_id='M-AC90', model_name='AC-90', name='Air compressor')

    def client_for(self, user) -> APIClient:
        client = APIClient()
        client.force_login(user)
        return client

    def make_manual(self, machine, document_id, revision='1.0', chunks=()):
        """Create an indexed manual with hand-written chunks: [(page, text)]."""
        manual = Manual.objects.create(
            machine=machine, document_id=document_id, document_name=f'{document_id} manual', revision=revision,
            file=f'manuals/{document_id}.pdf', sha256=hashlib.sha256(document_id.encode()).hexdigest(),
            page_count=max([p for p, _ in chunks], default=1), status=Manual.Status.INDEXED,
        )
        for index, (page, text) in enumerate(chunks):
            DocumentChunk.objects.create(
                manual=manual, machine=machine, document_id=document_id, document_name=manual.document_name,
                revision=revision, original_page_number=page, chunk_index=index, chunk_text=text,
                embedding=DocumentChunk.encode_embedding(fake_vector(text)), embedding_model='fake',
            )
        return manual
