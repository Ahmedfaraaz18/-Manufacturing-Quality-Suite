"""Corrective-action follow-up is optional and must never alter AI output or review history."""

import fitz

from django.urls import reverse

from inspections import services
from inspections.models import CorrectiveAction, InspectionReview

from .base import InspectionTestCase


class CorrectiveActionTests(InspectionTestCase):
    def setUp(self):
        super().setUp()
        self.inspection = self.make_completed(predicted='Defective')
        self.review = services.submit_review(self.inspection.pk, self.supervisor, 'Normal', 'Manual review found no defect.')
        self.url = reverse('inspection-corrective-action', args=[self.inspection.pk])
        self.original = (
            self.inspection.predicted_class, self.inspection.confidence, self.inspection.defect_probability,
            self.inspection.model_version, self.review.original_predicted_class, self.review.reviewed_class, self.review.comment,
        )

    def payload(self, **changes):
        values = {
            'observed_issue': 'Packaging alignment needs confirmation.',
            'recommended_action': 'Inspect the fixture before the next batch.',
            'status': 'in_progress',
        }
        values.update(changes)
        return values

    def assert_original_records_unchanged(self):
        self.inspection.refresh_from_db()
        review = InspectionReview.objects.get(pk=self.review.pk)
        self.assertEqual((
            self.inspection.predicted_class, self.inspection.confidence, self.inspection.defect_probability,
            self.inspection.model_version, review.original_predicted_class, review.reviewed_class, review.comment,
        ), self.original)

    def test_supervisor_can_create_and_update_an_optional_action(self):
        client = self.client_for(self.supervisor)
        response = client.post(self.url, self.payload())
        self.assertRedirects(response, reverse('inspection-detail', args=[self.inspection.pk]), fetch_redirect_response=False)
        action = CorrectiveAction.objects.get()
        self.assertEqual((action.review, action.status, action.updated_by, action.observed_issue),
                         (self.review, 'in_progress', self.supervisor, self.payload()['observed_issue']))
        client.post(self.url, self.payload(status='completed', observed_issue='Issue closed.'))
        action.refresh_from_db()
        self.assertEqual((CorrectiveAction.objects.count(), action.status, action.observed_issue), (1, 'completed', 'Issue closed.'))
        self.assert_original_records_unchanged()

    def test_only_supervisors_can_manage_corrective_actions(self):
        for user in (self.engineer, self.admin):
            self.assertEqual(self.client_for(user).post(self.url, self.payload()).status_code, 403)
            self.assertEqual(self.client_for(user).get(self.url).status_code, 403)
        self.assertEqual(CorrectiveAction.objects.count(), 0)

    def test_validation_and_review_requirement_are_enforced(self):
        response = self.client_for(self.supervisor).post(self.url, self.payload(observed_issue='', status='bad'))
        self.assertRedirects(response, reverse('inspection-detail', args=[self.inspection.pk]), fetch_redirect_response=False)
        self.assertEqual(CorrectiveAction.objects.count(), 0)
        other = self.make_completed()
        no_review_url = reverse('inspection-corrective-action', args=[other.pk])
        response = self.client_for(self.supervisor).post(no_review_url, self.payload())
        self.assertRedirects(response, reverse('inspection-detail', args=[other.pk]), fetch_redirect_response=False)
        self.assertEqual(CorrectiveAction.objects.count(), 0)

    def test_detail_visibility_and_pdf_include_corrective_action(self):
        self.client_for(self.supervisor).post(self.url, self.payload())
        detail = reverse('inspection-detail', args=[self.inspection.pk])
        supervisor_page = self.client_for(self.supervisor).get(detail)
        admin_page = self.client_for(self.admin).get(detail)
        engineer_page = self.client_for(self.engineer).get(detail)
        self.assertContains(supervisor_page, 'Corrective action')
        self.assertContains(supervisor_page, 'Packaging alignment needs confirmation.')
        self.assertContains(admin_page, 'Corrective action')
        self.assertNotContains(admin_page, 'Save corrective action')
        self.assertNotContains(engineer_page, 'Corrective action')
        response = self.client_for(self.supervisor).get(reverse('inspection-pdf-report', args=[self.inspection.pk]))
        document = fitz.open(stream=b''.join(response.streaming_content), filetype='pdf')
        self.addCleanup(document.close)
        text = ''.join(page.get_text() for page in document)
        self.assertIn('Corrective action', text)
        self.assertIn('Inspect the fixture before the next batch.', text)
