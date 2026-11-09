"""End-to-end coverage for the downloadable inspection report."""

import fitz

from django.core.files.storage import default_storage
from django.urls import reverse

from inspections import services

from .base import InspectionTestCase, upload


class InspectionReportTests(InspectionTestCase):
    def _download(self, user, inspection):
        return self.client_for(user).get(reverse('inspection-pdf-report', args=[inspection.pk]))

    def _pdf_bytes(self, response):
        self.assertEqual(response.status_code, 200, getattr(response, 'content', b''))
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertEqual(response['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(response['Cache-Control'], 'private, no-store')
        return b''.join(response.streaming_content)

    def _pdf_text(self, data):
        document = fitz.open(stream=data, filetype='pdf')
        self.addCleanup(document.close)
        self.assertGreaterEqual(document.page_count, 3)
        return ''.join(page.get_text() for page in document)

    def test_completed_defective_report_is_downloadable_and_embeds_evidence(self):
        inspection = self.make_completed(predicted='Defective', name='surface-scratch.png')
        response = self._download(self.engineer, inspection)
        data = self._pdf_bytes(response)

        self.assertIn(f'Inspection_Report_{inspection.pk}_P-PLATE01.pdf', response['Content-Disposition'])
        document = fitz.open(stream=data, filetype='pdf')
        self.addCleanup(document.close)
        self.assertTrue(any(page.get_images(full=True) for page in document))
        text = ''.join(page.get_text() for page in document)
        for value in (
            'AI Visual Quality Inspection Report', 'DEFECTIVE', 'surface-scratch.png',
            'Decision threshold', 'Awaiting Supervisor Review.', 'Technical information',
            'uncalibrated model scores', 'TEST-MODEL-1.0',
        ):
            self.assertIn(value, text)

    def test_normal_report_with_supervisor_correction_keeps_both_decisions(self):
        inspection = self.make_completed(predicted='Normal', name='clean-panel.png')
        services.submit_review(inspection.pk, self.supervisor, 'Defective', 'Visible scratch after manual inspection.')
        text = self._pdf_text(self._pdf_bytes(self._download(self.supervisor, inspection)))

        for value in (
            'NORMAL', 'Original AI prediction', 'Final supervisor-reviewed decision',
            'Corrected', 'Visible scratch after manual inspection.', 'sup1',
        ):
            self.assertIn(value, text)

    def test_missing_stored_image_produces_an_accurate_report(self):
        inspection = self.make_completed()
        default_storage.delete(inspection.image.name)
        text = self._pdf_text(self._pdf_bytes(self._download(self.engineer, inspection)))
        self.assertIn('could not be accessed or decoded', text)

    def test_only_authorised_roles_can_download_visible_completed_records(self):
        mine = self.make_completed(self.engineer, name='mine.png')
        theirs = self.make_completed(self.engineer2, name='theirs.png')

        self.assertEqual(self._download(self.engineer, theirs).status_code, 404)
        self.assertEqual(self._download(self.engineer2, mine).status_code, 404)
        self.assertEqual(self._download(self.supervisor, theirs).status_code, 200)
        self.assertEqual(self._download(self.admin, theirs).status_code, 200)
        self.assertEqual(self.client.get(reverse('inspection-pdf-report', args=[mine.pk])).status_code, 302)
        self.assertEqual(self.client_for(self.supervisor).get(reverse('inspection-pdf-report', args=[999999])).status_code, 404)

    def test_incomplete_inspection_has_no_button_or_final_report(self):
        with self.queue_only():
            inspection = services.create_batch(self.engineer, self.product, [upload('queued.png')]).inspections[0]

        detail = self.client_for(self.engineer).get(reverse('inspection-detail', args=[inspection.pk]))
        self.assertNotContains(detail, 'Download PDF Report')
        response = self._download(self.engineer, inspection)
        self.assertRedirects(response, reverse('inspection-detail', args=[inspection.pk]), fetch_redirect_response=False)
