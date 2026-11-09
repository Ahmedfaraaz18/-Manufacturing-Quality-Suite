"""Regression coverage for database-derived Quality Intelligence features."""

import fitz

from django.urls import reverse

from inspections import intelligence, services
from inspections.models import QualityAlert, QualityAlertConfiguration

from .base import InspectionTestCase, upload


class QualityIntelligenceTests(InspectionTestCase):
    def setUp(self):
        super().setUp()
        self.normal = self.make_completed(predicted='Normal', name='normal.png')
        self.defective = self.make_completed(predicted='Defective', name='defective.png')
        services.submit_review(self.normal.pk, self.supervisor, 'Normal', 'confirmed')
        services.submit_review(self.defective.pk, self.supervisor, 'Normal', 'corrected')

    def test_kpis_and_completed_only_batch_defect_rate(self):
        with self.queue_only():
            queued = services.create_batch(self.engineer, self.product, [upload('queued.png')]).inspections[0]
        with self.worker():
            failed = services.create_batch(self.engineer, self.product, [upload('broken.jpg', b'not an image')]).inspections[0]
        kpis = intelligence.operational_kpis({})
        self.assertEqual(kpis['total_inspections'], 4)
        self.assertEqual((kpis['completed_inspections'], kpis['queued_inspections'], kpis['failed_inspections']), (2, 1, 1))
        self.assertEqual((kpis['reviewed_inspections'], kpis['corrected_inspections'], kpis['awaiting_review']), (2, 1, 0))
        metrics = {batch.pk: batch for batch in intelligence.batch_metrics_queryset()}
        self.assertEqual(metrics[self.defective.batch_id].defect_rate, 100.0)
        self.assertIsNone(metrics[queued.batch_id].defect_rate)
        self.assertEqual(failed.status, 'failed')

    def test_agreement_excludes_unreviewed_and_incomplete_records(self):
        with self.queue_only():
            services.create_batch(self.engineer, self.product, [upload('pending.png')])
        result = intelligence.agreement_metrics({})
        self.assertEqual((result['completed'], result['reviewed'], result['confirmed'], result['corrected']), (2, 2, 1, 1))
        self.assertEqual((result['agreement_rate'], result['correction_rate']), (50.0, 50.0))

    def test_alerts_trigger_once_and_resolve_when_threshold_changes(self):
        config = QualityAlertConfiguration.current()
        config.batch_defect_rate_threshold = 0.5
        config.review_backlog_limit = 99
        config.failure_limit = 99
        config.processing_backlog_limit = 99
        config.save()
        intelligence.refresh_operational_alerts()
        self.assertEqual(QualityAlert.objects.filter(kind=QualityAlert.Kind.BATCH_DEFECT_RATE).count(), 1)
        intelligence.refresh_operational_alerts()
        self.assertEqual(QualityAlert.objects.filter(kind=QualityAlert.Kind.BATCH_DEFECT_RATE).count(), 1)
        config.batch_defect_rate_threshold = 1.0
        config.save()
        intelligence.refresh_operational_alerts()
        self.assertIsNotNone(QualityAlert.objects.get(kind=QualityAlert.Kind.BATCH_DEFECT_RATE).resolved_at)

    def test_intelligence_permissions_and_admin_threshold_configuration(self):
        url = reverse('inspection-intelligence')
        self.assertEqual(self.client_for(self.admin).get(url).status_code, 200)
        self.assertEqual(self.client_for(self.supervisor).get(url).status_code, 200)
        self.assertEqual(self.client_for(self.engineer).get(url).status_code, 403)
        self.assertEqual(self.client_for(self.supervisor).post(url, {'failure_limit': 0}).status_code, 403)
        response = self.client_for(self.admin).post(url, {
            'batch_defect_rate_threshold': '0.40', 'consecutive_high_defect_batches': '2',
            'failure_limit': '2', 'review_backlog_limit': '3', 'processing_backlog_limit': '4',
        })
        self.assertRedirects(response, url, fetch_redirect_response=False)
        config = QualityAlertConfiguration.current()
        self.assertEqual((config.batch_defect_rate_threshold, config.consecutive_high_defect_batches, config.failure_limit), (0.4, 2, 2))

    def test_timeline_uses_stored_events_and_keeps_original_prediction(self):
        page = self.client_for(self.supervisor).get(reverse('inspection-detail', args=[self.defective.pk]))
        self.assertContains(page, 'Inspection decision timeline')
        self.assertContains(page, 'AI prediction generated')
        self.assertContains(page, 'Supervisor review submitted')
        self.defective.refresh_from_db()
        self.assertEqual(self.defective.predicted_class, 'Defective')

    def test_batch_pdf_contains_actual_metrics_and_is_authorised(self):
        url = reverse('inspection-batch-pdf-report', args=[self.defective.batch_id])
        response = self.client_for(self.engineer).get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/pdf')
        data = b''.join(response.streaming_content)
        document = fitz.open(stream=data, filetype='pdf')
        self.addCleanup(document.close)
        text = ''.join(page.get_text() for page in document)
        self.assertIn('Batch Quality Summary Report', text)
        self.assertIn(f'Inspection #{self.defective.pk}', text)
        other = self.make_completed(self.engineer2)
        self.assertEqual(self.client_for(self.engineer).get(reverse('inspection-batch-pdf-report', args=[other.batch_id])).status_code, 404)
        self.assertEqual(self.client_for(self.supervisor).get(reverse('inspection-batch-pdf-report', args=[other.batch_id])).status_code, 200)
