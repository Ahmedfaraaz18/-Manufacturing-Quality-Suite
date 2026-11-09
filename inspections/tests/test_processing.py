"""Background processing, retries and the immutability of original predictions (classifier mocked)."""

from unittest import mock

from django.db import OperationalError, connection
from django.test.utils import CaptureQueriesContext

from inspections import services, tasks
from inspections.inference import InferenceError, ModelUnavailable
from inspections.models import ImmutablePredictionError, Inspection, InspectionReview

from .base import FAKE_RESULT, InspectionTestCase, upload


class ProcessingTests(InspectionTestCase):
    def queued(self, count=1):
        with self.queue_only():
            batch = services.create_batch(self.engineer, self.product, [upload(f'f{i}.png', color=(i * 30, 60, 60)) for i in range(count)]).batch
        return list(batch.inspections.order_by('id'))

    def test_worker_moves_item_through_processing_to_completed(self):
        item = self.queued()[0]
        seen = {}

        def classifier(path):
            seen['status_during_inference'] = Inspection.objects.get(pk=item.pk).status
            return FAKE_RESULT

        with mock.patch('inspections.services.predict_image', side_effect=classifier):
            outcome = services.process_inspection(item.pk)
        item.refresh_from_db()
        self.assertEqual(seen['status_during_inference'], 'processing')
        self.assertEqual((outcome, item.status, item.predicted_class, item.model_version), ('completed', 'completed', 'Defective', 'TEST-MODEL-1.0'))
        self.assertIsNotNone(item.started_at)

    def test_inference_error_marks_the_item_failed_with_a_readable_reason(self):
        item = self.queued()[0]
        with mock.patch('inspections.services.predict_image', side_effect=InferenceError('boom')):
            self.assertEqual(services.process_inspection(item.pk), 'failed')
        item.refresh_from_db()
        self.assertEqual((item.status, item.predicted_class, item.confidence), ('failed', None, None))
        self.assertEqual(item.failure_reason, InferenceError.user_message)

    def test_missing_model_is_reported_without_internal_details(self):
        item = self.queued()[0]
        with mock.patch('inspections.services.predict_image', side_effect=ModelUnavailable('secret path C:\\x')):
            services.process_inspection(item.pk)
        item.refresh_from_db()
        self.assertEqual(item.status, 'failed')
        self.assertIn('unavailable', item.failure_reason)
        self.assertNotIn('secret', item.failure_reason)

    def test_unexpected_exception_does_not_leak_a_stack_trace(self):
        item = self.queued()[0]
        with mock.patch('inspections.services.predict_image', side_effect=ValueError('Traceback (most recent call last)')):
            with self.assertLogs('inspections.services', level='ERROR'):
                services.process_inspection(item.pk)
        item.refresh_from_db()
        self.assertEqual(item.failure_reason, InferenceError.user_message)

    def test_reprocessing_never_overwrites_a_completed_prediction(self):
        item = self.queued()[0]
        with mock.patch('inspections.services.predict_image', return_value=FAKE_RESULT):
            services.process_inspection(item.pk)
        item.refresh_from_db()
        first = (item.predicted_class, item.confidence, item.defect_probability, item.model_version, item.completed_at)
        other = dict(FAKE_RESULT, predicted_class='Normal', confidence=0.99, defect_probability=0.01, model_version='OTHER')
        with mock.patch('inspections.services.predict_image', return_value=other) as classifier:
            outcome = services.process_inspection(item.pk)
            tasks.run_inspection.apply(args=[item.pk])
        item.refresh_from_db()
        classifier.assert_not_called()
        self.assertEqual(outcome, 'completed')
        self.assertEqual((item.predicted_class, item.confidence, item.defect_probability, item.model_version, item.completed_at), first)

    def test_a_late_duplicate_delivery_cannot_overwrite_a_stored_result(self):
        """Even if a second worker finishes after the first, the conditional update refuses to write."""
        item = self.queued()[0]
        with mock.patch('inspections.services.predict_image', return_value=FAKE_RESULT):
            services.process_inspection(item.pk)
        late = dict(FAKE_RESULT, predicted_class='Normal', confidence=0.7, defect_probability=0.3)
        updated = Inspection.objects.filter(pk=item.pk, status='processing').update(**late)
        self.assertEqual(updated, 0)
        item.refresh_from_db()
        self.assertEqual(item.predicted_class, 'Defective')

    def test_retry_after_a_crashed_worker_completes_a_processing_item(self):
        item = self.queued()[0]
        Inspection.objects.filter(pk=item.pk).update(status='processing')
        with mock.patch('inspections.services.predict_image', return_value=FAKE_RESULT):
            self.assertEqual(services.process_inspection(item.pk), 'completed')

    def test_failed_items_stay_failed_and_are_not_reprocessed(self):
        item = self.queued()[0]
        services.fail_inspection(item.pk, 'broken')
        with mock.patch('inspections.services.predict_image') as classifier:
            self.assertEqual(services.process_inspection(item.pk), 'failed')
        classifier.assert_not_called()

    def test_missing_inspection_id_is_ignored(self):
        self.assertEqual(services.process_inspection(987654), 'missing')

    def test_celery_task_runs_the_same_logic_and_retries_only_database_outages(self):
        item = self.queued()[0]
        with mock.patch('inspections.services.predict_image', return_value=FAKE_RESULT):
            result = tasks.run_inspection.apply(args=[item.pk])
        self.assertEqual(result.get(), 'completed')
        self.assertEqual(tasks.run_inspection.autoretry_for, (OperationalError,))
        self.assertEqual(tasks.run_inspection.max_retries, 3)

    def test_broker_outage_fails_items_instead_of_leaving_them_queued(self):
        with mock.patch('inspections.tasks.run_inspection.delay', side_effect=ConnectionError('redis down')):
            with self.assertLogs('inspections.services', level='ERROR'):
                with self.captureOnCommitCallbacks(execute=True):
                    batch = services.create_batch(self.engineer, self.product, [upload('a.png'), upload('b.png', color=(1, 2, 3))]).batch
        items = batch.inspections.all()
        self.assertEqual({i.status for i in items}, {'failed'})
        self.assertTrue(all('unavailable' in i.failure_reason for i in items))

    def test_tasks_are_only_enqueued_after_the_transaction_commits(self):
        with mock.patch('inspections.tasks.run_inspection.delay') as delay:
            with self.captureOnCommitCallbacks(execute=False) as callbacks:
                services.create_batch(self.engineer, self.product, [upload('a.png')])
            delay.assert_not_called()
            self.assertEqual(len(callbacks), 1)
            callbacks[0]()
            delay.assert_called_once()

    def test_requeue_command_recovers_items_that_were_never_queued(self):
        import io
        from datetime import timedelta

        from django.core.management import call_command
        from django.utils import timezone

        stale, fresh = self.queued(2)
        Inspection.objects.filter(pk=stale.pk).update(created_at=timezone.now() - timedelta(minutes=30))
        out = io.StringIO()
        with mock.patch('inspections.tasks.run_inspection.delay') as delay:
            call_command('requeue_inspections', '--dry-run', stdout=out)
            delay.assert_not_called()
            call_command('requeue_inspections', stdout=out)
        delay.assert_called_once_with(stale.pk)            # the fresh item is left alone
        with mock.patch('inspections.services.predict_image', return_value=FAKE_RESULT):
            self.assertEqual(services.process_inspection(stale.pk), 'completed')


class ImmutabilityTests(InspectionTestCase):
    def test_completed_prediction_cannot_be_changed_through_the_model(self):
        item = self.make_completed(predicted='Defective')
        for field, value in (('predicted_class', 'Normal'), ('confidence', 0.5), ('defect_probability', 0.2),
                             ('model_version', 'X'), ('status', 'failed')):
            fresh = Inspection.objects.get(pk=item.pk)
            setattr(fresh, field, value)
            with self.assertRaises(ImmutablePredictionError, msg=field):
                fresh.save()
        item.refresh_from_db()
        self.assertEqual((item.predicted_class, item.status), ('Defective', 'completed'))

    def test_unrelated_fields_of_a_completed_inspection_can_still_be_saved(self):
        item = self.make_completed()
        item.original_filename = 'renamed.png'
        item.save()
        item.refresh_from_db()
        self.assertEqual(item.original_filename, 'renamed.png')

    def test_database_constraints_protect_data_integrity(self):
        from django.db import IntegrityError, transaction

        item = self.make_completed()
        bad_updates = [
            {'confidence': 1.5}, {'defect_probability': -0.1}, {'model_version': ''},
            {'status': 'failed'},                       # failed needs a reason and no prediction
        ]
        for fields in bad_updates:
            with self.assertRaises(IntegrityError, msg=str(fields)):
                with transaction.atomic():
                    Inspection.objects.filter(pk=item.pk).update(**fields)
        with self.assertRaises(IntegrityError):         # queued items cannot carry a prediction
            with transaction.atomic():
                Inspection.objects.filter(pk=item.pk).update(status='queued')

    def test_review_history_survives_and_blocks_inspection_deletion(self):
        from django.db.models import ProtectedError

        item = self.make_completed()
        services.submit_review(item.pk, self.supervisor, 'Normal', 'ok')
        with self.assertRaises(ProtectedError):
            item.delete()
        self.assertEqual(InspectionReview.objects.count(), 1)

    def test_records_survive_a_fresh_database_read(self):
        item = self.make_completed()
        services.submit_review(item.pk, self.supervisor, 'Normal', 'persisted')
        with CaptureQueriesContext(connection):
            reloaded = Inspection.objects.select_related('review').get(pk=item.pk)
        self.assertEqual((reloaded.predicted_class, reloaded.status, reloaded.model_version), ('Defective', 'completed', 'TEST-MODEL-1.0'))
        self.assertEqual((reloaded.review.reviewed_class, reloaded.review.comment), ('Normal', 'persisted'))
        self.assertTrue(reloaded.image.storage.exists(reloaded.image.name))
