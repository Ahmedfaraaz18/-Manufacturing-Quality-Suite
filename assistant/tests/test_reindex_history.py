"""Reindex failures and historical evidence must remain safe and visible."""

from unittest import mock

from django.core.files.base import ContentFile
from django.urls import reverse

from assistant.models import AnswerSource, DocumentChunk, Manual, QuestionHistory

from .base import BaseTestCase, make_pdf, pdf_upload


class ReindexHistoryTests(BaseTestCase):
    pages = [
        'HP-OPS-01 Rev 1.0\nThe normal forming pressure range is 90 to 110 bar.',
        'HP-OPS-01 Rev 1.0\nInspect the hydraulic reservoir before operation.',
    ]

    def setUp(self):
        super().setUp()
        response = self.client_for(self.admin).post(reverse('api-manuals'), {
            'machine': 'M-HP200',
            'document_id': 'HP-OPS-01',
            'document_name': 'HP 200 Operations Manual',
            'revision': '1.0',
            'file': pdf_upload(self.pages),
        }, format='multipart')
        self.assertEqual(response.status_code, 201, response.content)
        self.manual = Manual.objects.get(document_id='HP-OPS-01')
        self.chunk = self.manual.chunks.filter(original_page_number=1).first()
        self.record = QuestionHistory.objects.create(
            user=self.engineer, machine=self.hp, question='What is the normal forming pressure?',
            answer='Legacy answer', status=QuestionHistory.Status.ANSWERED,
        )
        self.source = AnswerSource.objects.create(
            question=self.record, chunk=self.chunk, manual=self.manual,
            document_id=self.chunk.document_id, document_name=self.chunk.document_name,
            revision=self.chunk.revision, page_number=self.chunk.original_page_number,
            passage=self.chunk.chunk_text,
        )

    def reindex(self):
        return self.client_for(self.admin).post(reverse('api-manual-reindex', args=[self.manual.pk]))

    def detach_chunk(self):
        self.chunk.delete()
        self.source.refresh_from_db()
        self.assertIsNone(self.source.chunk_id)

    def test_successful_reindex_preserves_historical_citation(self):
        old_chunk_id = self.chunk.pk
        response = self.reindex()
        self.assertEqual(response.status_code, 200, response.content)
        self.source.refresh_from_db()
        self.assertIsNone(self.source.chunk_id)
        self.assertFalse(DocumentChunk.objects.filter(pk=old_chunk_id).exists())

        body = self.client_for(self.engineer).get(reverse('api-history-detail', args=[self.record.pk])).json()
        self.assertEqual([source['id'] for source in body['sources']], [self.source.pk])
        self.assertIn(self.source.passage, body['answer'])
        page = self.client_for(self.engineer).get(
            reverse('manual-page', args=[self.manual.pk, self.source.page_number]), {'source': self.source.pk}
        )
        self.assertContains(page, 'Cited passage')
        self.assertContains(page, self.source.passage)

    def test_historical_quote_verifies_against_original_pdf_after_chunk_deletion(self):
        self.detach_chunk()
        self.assertTrue(self.source.is_verified)

    def test_modified_historical_quote_is_rejected_after_chunk_deletion(self):
        self.detach_chunk()
        self.source.passage = 'Invented forming pressure range.'
        self.source.save(update_fields=['passage'])
        self.source.refresh_from_db()
        self.assertFalse(self.source.is_verified)

    def test_modified_document_metadata_is_rejected_after_chunk_deletion(self):
        self.detach_chunk()
        self.source.document_id = 'FAKE-OPS-01'
        self.source.save(update_fields=['document_id'])
        self.source.refresh_from_db()
        self.assertFalse(self.source.is_verified)

    def test_altered_pdf_is_rejected_after_chunk_deletion(self):
        self.detach_chunk()
        self.manual.file.save('altered.pdf', ContentFile(make_pdf([
            'HP-OPS-01 Rev 1.0\nAltered document content.',
        ])), save=True)
        self.source.refresh_from_db()
        self.assertFalse(self.source.is_verified)

    def test_cross_machine_historical_source_is_rejected_after_chunk_deletion(self):
        self.detach_chunk()
        self.record.machine = self.ac
        self.record.save(update_fields=['machine'])
        self.source.refresh_from_db()
        self.assertFalse(self.source.is_verified)

    def test_deleted_manual_makes_historical_source_unavailable(self):
        self.manual.delete()
        self.source.refresh_from_db()
        self.assertIsNone(self.source.manual_id)
        self.assertFalse(self.source.is_verified)

    def test_failed_reindex_returns_422_with_safe_error_message(self):
        with mock.patch('assistant.services.ingestion.embeddings.embed_texts', side_effect=RuntimeError('internal secret')):
            response = self.reindex()

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()['status'], Manual.Status.FAILED)
        self.assertEqual(response.json()['error_message'], 'The manual could not be indexed. Please review the PDF and try again.')
        self.assertNotIn('internal secret', response.content.decode())

    def test_admin_dashboard_contains_reindex_failure_display(self):
        response = self.client_for(self.admin).get(reverse('admin-dashboard'))
        self.assertContains(response, 'id="reindex-error"')
        self.assertContains(response, 'data.error_message')
