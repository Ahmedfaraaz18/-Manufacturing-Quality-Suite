import hashlib

import numpy as np
from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.core.validators import FileExtensionValidator
from django.db import models
from django.db.models import Q
from django.utils.functional import cached_property


class Role(models.TextChoices):
    ADMINISTRATOR = 'administrator', 'Administrator'
    ENGINEER = 'engineer', 'Engineer'
    SUPERVISOR = 'supervisor', 'Supervisor'


class User(AbstractUser):
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.ENGINEER, db_index=True)

    @property
    def is_administrator(self):
        return self.is_active and self.role == Role.ADMINISTRATOR

    @property
    def is_engineer(self):
        return self.is_active and self.role == Role.ENGINEER

    @property
    def is_supervisor(self):
        return self.is_active and self.role == Role.SUPERVISOR


class Machine(models.Model):
    machine_id = models.CharField(max_length=32, unique=True, help_text='e.g. M-HP200')
    model_name = models.CharField(max_length=64, help_text='e.g. HP-200')
    name = models.CharField(max_length=128, help_text='e.g. Hydraulic forming press')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['machine_id']

    def __str__(self):
        return f'{self.machine_id} ({self.model_name})'


def manual_upload_path(instance, filename):
    return f'manuals/{instance.machine.machine_id}/{instance.document_id}_rev{instance.revision}.pdf'


class Manual(models.Model):
    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        INDEXING = 'indexing', 'Indexing'
        INDEXED = 'indexed', 'Indexed'
        FAILED = 'failed', 'Failed'

    machine = models.ForeignKey(Machine, on_delete=models.PROTECT, related_name='manuals')
    document_id = models.CharField(max_length=64)
    document_name = models.CharField(max_length=255)
    revision = models.CharField(max_length=32)
    file = models.FileField(upload_to=manual_upload_path, validators=[FileExtensionValidator(['pdf'])])
    original_filename = models.CharField(max_length=255, blank=True)
    sha256 = models.CharField(max_length=64, unique=True)
    page_count = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING, db_index=True)
    error_message = models.TextField(blank=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='uploaded_manuals'
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)
    indexed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['machine__machine_id', 'document_id']
        constraints = [
            models.UniqueConstraint(fields=['document_id', 'revision'], name='uniq_manual_document_revision'),
        ]

    def __str__(self):
        return f'{self.document_id} rev {self.revision}'

    @staticmethod
    def compute_sha256(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()


class DocumentChunk(models.Model):
    """A searchable passage. Machine and document metadata are denormalised so retrieval can
    filter on `machine` in a single indexed query before any similarity ranking happens."""

    manual = models.ForeignKey(Manual, on_delete=models.CASCADE, related_name='chunks')
    machine = models.ForeignKey(Machine, on_delete=models.CASCADE, related_name='chunks')
    document_id = models.CharField(max_length=64)
    document_name = models.CharField(max_length=255)
    revision = models.CharField(max_length=32)
    original_page_number = models.PositiveIntegerField()
    chunk_index = models.PositiveIntegerField()
    chunk_text = models.TextField()
    embedding = models.BinaryField()
    embedding_model = models.CharField(max_length=128)

    class Meta:
        ordering = ['manual_id', 'original_page_number', 'chunk_index']
        indexes = [
            models.Index(fields=['machine', 'document_id'], name='chunk_machine_doc_idx'),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(original_page_number__gte=1), name='chunk_page_number_gte_1'),
            models.UniqueConstraint(
                fields=['manual', 'original_page_number', 'chunk_index'], name='uniq_chunk_position'
            ),
        ]

    def __str__(self):
        return f'{self.document_id} p{self.original_page_number} #{self.chunk_index}'

    @staticmethod
    def encode_embedding(vector) -> bytes:
        return np.asarray(vector, dtype=np.float32).tobytes()

    @property
    def vector(self) -> np.ndarray:
        return np.frombuffer(bytes(self.embedding), dtype=np.float32)


class QuestionHistory(models.Model):
    class Status(models.TextChoices):
        ANSWERED = 'answered', 'Answered'
        INSUFFICIENT = 'insufficient_evidence', 'Insufficient evidence'
        ERROR = 'error', 'Service error'

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='questions')
    machine = models.ForeignKey(Machine, on_delete=models.PROTECT, related_name='questions')
    question = models.TextField()
    answer = models.TextField(blank=True)
    status = models.CharField(max_length=24, choices=Status.choices, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at', '-id']
        verbose_name_plural = 'question history'
        indexes = [
            models.Index(fields=['user', '-created_at'], name='question_user_created_idx'),
            models.Index(fields=['machine', 'status', '-created_at'], name='question_machine_status_idx'),
        ]

    def __str__(self):
        return f'{self.machine.machine_id}: {self.question[:50]}'

    @cached_property
    def verified_sources(self):
        return [source for source in self.sources.all() if source.is_verified]

    @property
    def display_answer(self):
        if self.status != self.Status.ANSWERED:
            return self.answer
        if not self.verified_sources:
            return 'The original answer is unavailable because its supporting evidence cannot be verified.'
        return '\n\n'.join(
            f'{source.passage} [{source.document_id} rev {source.revision}, p.{source.page_number}]'
            for source in self.verified_sources
        )


class AnswerSource(models.Model):
    """Snapshot of one cited passage, so history stays verifiable even if the manual is replaced."""

    question = models.ForeignKey(QuestionHistory, on_delete=models.CASCADE, related_name='sources')
    chunk = models.ForeignKey(DocumentChunk, null=True, on_delete=models.SET_NULL, related_name='+')
    manual = models.ForeignKey(Manual, null=True, on_delete=models.SET_NULL, related_name='+')
    document_id = models.CharField(max_length=64)
    document_name = models.CharField(max_length=255)
    revision = models.CharField(max_length=32)
    page_number = models.PositiveIntegerField()
    passage = models.TextField()
    score = models.FloatField(default=0.0)
    position = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ['position']
        constraints = [
            models.CheckConstraint(condition=Q(page_number__gte=1), name='source_page_number_gte_1'),
        ]

    def __str__(self):
        return f'{self.document_id} rev {self.revision} p{self.page_number}'

    @property
    def is_verified(self):
        if self.chunk_id is not None:
            chunk = self.chunk
            return (
                chunk.machine_id == self.question.machine_id
                and chunk.manual_id == self.manual_id
                and chunk.document_id == self.document_id
                and chunk.document_name == self.document_name
                and chunk.revision == self.revision
                and chunk.original_page_number == self.page_number
                and bool(self.passage.strip())
                and self.passage in chunk.chunk_text
            )
        return self._snapshot_matches_original_pdf()

    def _snapshot_matches_original_pdf(self):
        """Verify a reindex-detached source against its unchanged source PDF."""
        if self.manual_id is None or not self.passage.strip():
            return False
        try:
            manual = self.manual
        except Manual.DoesNotExist:
            return False
        if (
            manual.machine_id != self.question.machine_id
            or manual.document_id != self.document_id
            or manual.document_name != self.document_name
            or manual.revision != self.revision
            or not 1 <= self.page_number <= manual.page_count
        ):
            return False

        # ``sources__manual`` is prefetched for history responses, so this cache
        # avoids extracting the same PDF page repeatedly within one response.
        cache = getattr(manual, '_answer_source_page_text_cache', None)
        if cache is None:
            cache = manual._answer_source_page_text_cache = {}
        if self.page_number not in cache:
            try:
                from .services.chunking import normalize_text
                from .services.ingestion import IngestionError, extract_page

                with manual.file.open('rb') as handle:
                    pdf_bytes = handle.read()
                if Manual.compute_sha256(pdf_bytes) != manual.sha256:
                    return False
                cache[self.page_number] = normalize_text(extract_page(pdf_bytes, self.page_number))
            except (FileNotFoundError, OSError, IngestionError):
                return False

        from .services.chunking import normalize_text

        return normalize_text(self.passage) in cache[self.page_number]
