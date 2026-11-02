"""PDF validation, text extraction (PyMuPDF), chunking, embedding and storage."""

import logging
import re

import pymupdf
from django.conf import settings
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.utils import timezone

from ..models import DocumentChunk, Manual
from . import embeddings
from .chunking import chunk_page

logger = logging.getLogger(__name__)

DOCUMENT_ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')
REVISION_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$')


class IngestionError(ValueError):
    """A user-correctable problem with an upload; the message is safe to show to the admin."""


def extract_pages(pdf_bytes: bytes) -> list[tuple[int, str]]:
    """Return [(page_number, text)] with one-based page numbers matching the original PDF."""
    try:
        doc = pymupdf.open(stream=pdf_bytes, filetype='pdf')
    except Exception as exc:
        raise IngestionError('The file is not a readable PDF.') from exc
    try:
        if doc.needs_pass:
            raise IngestionError('Password-protected PDFs are not supported.')
        return [(index + 1, page.get_text('text') or '') for index, page in enumerate(doc)]
    except IngestionError:
        raise
    except Exception as exc:
        raise IngestionError('The PDF could not be read; it may be corrupted.') from exc
    finally:
        doc.close()


def extract_page(pdf_bytes: bytes, page_number: int) -> str:
    """Extract one one-based PDF page for historical-source verification."""
    if page_number < 1:
        raise IngestionError('PDF page numbers start at 1.')
    try:
        doc = pymupdf.open(stream=pdf_bytes, filetype='pdf')
    except Exception as exc:
        raise IngestionError('The file is not a readable PDF.') from exc
    try:
        if doc.needs_pass:
            raise IngestionError('Password-protected PDFs are not supported.')
        if page_number > len(doc):
            raise IngestionError('The cited PDF page is unavailable.')
        return doc[page_number - 1].get_text('text') or ''
    except IngestionError:
        raise
    except Exception as exc:
        raise IngestionError('The PDF could not be read; it may be corrupted.') from exc
    finally:
        doc.close()


def validate_pdf_upload(uploaded_file, document_id: str) -> tuple[bytes, list[tuple[int, str]]]:
    """Validate type, size, structure and document-ID consistency. Returns (bytes, pages)."""
    name = (getattr(uploaded_file, 'name', '') or '').lower()
    if not name.endswith('.pdf'):
        raise IngestionError('Only .pdf files are accepted.')
    max_bytes = settings.MAX_UPLOAD_SIZE_MB * 1024 * 1024
    if uploaded_file.size > max_bytes:
        raise IngestionError(f'File is larger than the {settings.MAX_UPLOAD_SIZE_MB} MB limit.')
    uploaded_file.seek(0)
    data = uploaded_file.read()
    if not data.lstrip()[:5] == b'%PDF-':
        raise IngestionError('The file content is not a PDF.')
    pages = extract_pages(data)
    if not pages:
        raise IngestionError('The PDF has no pages.')
    if not any(text.strip() for _, text in pages):
        raise IngestionError('The PDF contains no extractable text (scanned images are not supported).')
    haystack = re.sub(r'\s+', ' ', ' '.join(text for _, text in pages)).upper()
    if document_id.upper() not in haystack:
        raise IngestionError(
            f'Document ID "{document_id}" does not appear in the PDF text; '
            'check that the file and its metadata match.'
        )
    return data, pages


def validate_metadata(document_id: str, revision: str) -> None:
    if not DOCUMENT_ID_RE.match(document_id):
        raise IngestionError('Document ID may only contain letters, digits, dot, dash and underscore.')
    if not REVISION_RE.match(revision):
        raise IngestionError('Revision may only contain letters, digits, dot, dash and underscore.')


def register_manual(*, machine, document_id, document_name, revision, uploaded_file, user) -> Manual:
    """Create a Manual (never touching existing ones) and index it. Raises IngestionError."""
    document_id, revision = document_id.strip(), revision.strip()
    validate_metadata(document_id, revision)
    data, pages = validate_pdf_upload(uploaded_file, document_id)
    sha256 = Manual.compute_sha256(data)

    existing = Manual.objects.filter(sha256=sha256).select_related('machine').first()
    if existing:
        raise IngestionError(
            f'This exact file is already registered as {existing.document_id} rev {existing.revision} '
            f'for machine {existing.machine.machine_id}.'
        )
    if Manual.objects.filter(document_id=document_id, revision=revision).exists():
        raise IngestionError(f'Document {document_id} revision {revision} is already registered.')

    manual = Manual(
        machine=machine,
        document_id=document_id,
        document_name=document_name.strip(),
        revision=revision,
        original_filename=(uploaded_file.name or '')[:255],
        sha256=sha256,
        page_count=len(pages),
        uploaded_by=user,
    )
    try:
        with transaction.atomic():
            manual.file.save(uploaded_file.name, ContentFile(data), save=False)
            manual.save()
    except IntegrityError as exc:
        raise IngestionError('This document or file is already registered.') from exc

    dispatch_indexing(manual)
    manual.refresh_from_db()
    return manual


def dispatch_indexing(manual: Manual) -> None:
    if settings.INGEST_ASYNC:
        from ..tasks import index_manual_task

        index_manual_task.delay(manual.pk)
    else:
        index_manual(manual.pk)


def index_manual(manual_id: int) -> Manual:
    """(Re)build all chunks for a manual. Safe to re-run; replaces previous chunks atomically."""
    manual = Manual.objects.select_related('machine').get(pk=manual_id)
    manual.status = Manual.Status.INDEXING
    manual.error_message = ''
    manual.save(update_fields=['status', 'error_message'])
    try:
        with manual.file.open('rb') as handle:
            pages = extract_pages(handle.read())

        pieces = []
        for page_number, text in pages:
            pieces.extend(
                chunk_page(page_number, text, settings.CHUNK_SIZE_CHARS, settings.CHUNK_OVERLAP_CHARS)
            )
        if not pieces:
            raise IngestionError('No extractable text was found in the PDF.')

        vectors = embeddings.embed_texts([piece.text for piece in pieces])
        model = embeddings.model_name()
        rows = [
            DocumentChunk(
                manual=manual,
                machine=manual.machine,
                document_id=manual.document_id,
                document_name=manual.document_name,
                revision=manual.revision,
                original_page_number=piece.page_number,
                chunk_index=piece.chunk_index,
                chunk_text=piece.text,
                embedding=DocumentChunk.encode_embedding(vector),
                embedding_model=model,
            )
            for piece, vector in zip(pieces, vectors)
        ]
        with transaction.atomic():
            manual.chunks.all().delete()
            DocumentChunk.objects.bulk_create(rows)
            manual.page_count = len(pages)
            manual.status = Manual.Status.INDEXED
            manual.indexed_at = timezone.now()
            manual.save(update_fields=['page_count', 'status', 'indexed_at'])
    except Exception as exc:
        logger.error('Indexing failed for manual %s (%s)', manual_id, type(exc).__name__)
        manual.status = Manual.Status.FAILED
        # Validation failures have deliberately safe, user-correctable messages.
        # Unexpected library/storage errors may contain implementation details, so
        # never return their raw text through the administrator API.
        manual.error_message = (
            str(exc)[:1000]
            if isinstance(exc, IngestionError)
            else 'The manual could not be indexed. Please review the PDF and try again.'
        )
        manual.save(update_fields=['status', 'error_message'])
    return manual
