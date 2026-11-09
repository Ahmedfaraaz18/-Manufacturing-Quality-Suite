"""Upload endpoints: single image, batches, failed-attempt records, idempotency and role gating.

Inference is mocked here (isolated unit tests); see test_real_model.py for tests with the supplied classifier.
"""

from django.urls import reverse

from inspections.models import Inspection, InspectionBatch

from .base import FAKE_RESULT, InspectionTestCase, image_bytes, upload

SINGLE = 'api-inspection-upload'
BATCH = 'api-inspection-batch'


class SingleUploadTests(InspectionTestCase):
    def post(self, user, **fields):
        data = {'product': 'P-PLATE01', **fields}
        return self.client_for(user).post(reverse(SINGLE), data, format='multipart')

    def test_valid_jpeg_upload_is_queued_then_completed(self):
        with self.queue_only() as delay:
            response = self.post(self.engineer, image=upload('a.jpg', image_bytes('JPEG'), 'image/jpeg'))
        self.assertEqual(response.status_code, 202, response.content)
        body = response.json()['inspection']
        self.assertEqual((body['status'], body['product'], body['submitting_user']), ('queued', 'P-PLATE01', 'eng1'))
        self.assertIsNone(body['predicted_class'])
        delay.assert_called_once_with(body['id'])
        inspection = Inspection.objects.get(pk=body['id'])
        self.assertEqual(inspection.image_format, 'JPEG')
        self.assertTrue(inspection.image.name.endswith('.jpg'))
        self.assertTrue(inspection.image.storage.exists(inspection.image.name))

    def test_valid_png_upload_is_processed_by_the_worker(self):
        with self.worker() as classifier:
            response = self.post(self.engineer, image=upload('a.png'))
        self.assertEqual(response.status_code, 202)
        classifier.assert_called_once()
        inspection = Inspection.objects.get()
        self.assertEqual(inspection.status, 'completed')
        self.assertEqual((inspection.predicted_class, inspection.confidence, inspection.defect_probability, inspection.model_version),
                         ('Defective', 0.9, 0.9, 'TEST-MODEL-1.0'))
        self.assertIsNotNone(inspection.completed_at)

    def test_invalid_file_creates_failed_record_and_never_reaches_the_classifier(self):
        invalid = {
            'text.jpg': upload('text.jpg', b'plain text pretending to be a picture', 'image/jpeg'),
            'empty.png': upload('empty.png', b''),
            'small.png': upload('small.png', image_bytes('PNG', size=(32, 32))),
            'big.png': upload('big.png', image_bytes('PNG', size=(4097, 100))),
        }
        for name, file in invalid.items():
            with self.worker() as classifier:
                response = self.post(self.engineer, image=file)
            self.assertEqual(response.status_code, 422, name)
            body = response.json()['inspection']
            self.assertEqual(body['status'], 'failed', name)
            self.assertTrue(body['failure_reason'])
            self.assertIsNone(body['predicted_class'])
            self.assertIsNone(body['image_url'])
            classifier.assert_not_called()
        failed = Inspection.objects.filter(status='failed')
        self.assertEqual(failed.count(), 4)
        record = failed.get(original_filename='small.png')
        self.assertEqual((record.submitting_user, record.product), (self.engineer, self.product))
        self.assertIsNotNone(record.created_at)
        self.assertEqual(record.image.name, '')     # rejected content is never stored

    def test_exactly_one_image_is_required(self):
        response = self.client_for(self.engineer).post(
            reverse(SINGLE), {'product': 'P-PLATE01', 'image': [upload('a.png'), upload('b.png')]}, format='multipart')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client_for(self.engineer).post(reverse(SINGLE), {'product': 'P-PLATE01'}, format='multipart').status_code, 400)
        self.assertEqual(Inspection.objects.count(), 0)

    def test_unknown_or_inactive_product_is_rejected(self):
        response = self.client_for(self.engineer).post(reverse(SINGLE), {'product': 'P-NOPE', 'image': upload()}, format='multipart')
        self.assertEqual(response.status_code, 400)
        self.product.is_active = False
        self.product.save()
        self.assertEqual(self.post(self.engineer, image=upload()).status_code, 400)
        self.assertEqual(Inspection.objects.count(), 0)

    def test_only_engineers_may_upload(self):
        for user in (self.supervisor, self.admin):
            self.assertEqual(self.post(user, image=upload()).status_code, 403, user.role)
        anonymous = self.client.post(reverse(SINGLE), {'product': 'P-PLATE01', 'image': upload()})
        self.assertIn(anonymous.status_code, (401, 403))
        self.assertEqual(Inspection.objects.count(), 0)

    def test_submitting_user_comes_from_the_session_not_the_request(self):
        with self.queue_only():
            self.post(self.engineer, image=upload(), submitting_user=self.engineer2.pk, submitted_by='eng2')
        self.assertEqual(Inspection.objects.get().submitting_user, self.engineer)

    def test_repeated_submission_with_same_token_is_idempotent(self):
        with self.queue_only() as delay:
            first = self.post(self.engineer, image=upload('a.png'), client_token='tok-1')
            second = self.post(self.engineer, image=upload('a.png'), client_token='tok-1')
        self.assertEqual((first.status_code, second.status_code), (202, 200))
        self.assertTrue(second.json()['duplicate'])
        self.assertEqual(first.json()['inspection']['id'], second.json()['inspection']['id'])
        self.assertEqual((Inspection.objects.count(), InspectionBatch.objects.count()), (1, 1))
        delay.assert_called_once()

    def test_oversized_request_body_is_refused_before_parsing(self):
        response = self.client_for(self.engineer).post(
            reverse(SINGLE), {'product': 'P-PLATE01', 'image': upload()}, format='multipart', CONTENT_LENGTH=str(80 * 1024 * 1024))
        self.assertEqual(response.status_code, 413)
        self.assertEqual(Inspection.objects.count(), 0)


class BatchUploadTests(InspectionTestCase):
    def post(self, files, user=None, **fields):
        return self.client_for(user or self.engineer).post(
            reverse(BATCH), {'product': 'P-PLATE01', 'files': files, **fields}, format='multipart')

    def valid(self, count, prefix='ok'):
        return [upload(f'{prefix}{i}.png', image_bytes('PNG', color=(i * 20, 90, 90))) for i in range(count)]

    def test_empty_batch_is_rejected(self):
        response = self.client_for(self.engineer).post(reverse(BATCH), {'product': 'P-PLATE01'}, format='multipart')
        self.assertEqual(response.status_code, 400)
        self.assertEqual((InspectionBatch.objects.count(), Inspection.objects.count()), (0, 0))

    def test_more_than_ten_files_is_rejected_and_nothing_is_stored(self):
        response = self.post(self.valid(11))
        self.assertEqual(response.status_code, 400)
        self.assertIn('at most 10', str(response.json()))
        self.assertEqual((InspectionBatch.objects.count(), Inspection.objects.count()), (0, 0))

    def test_ten_files_are_accepted(self):
        with self.queue_only() as delay:
            response = self.post(self.valid(10))
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()['batch']['total_files'], 10)
        self.assertEqual(delay.call_count, 10)

    def test_mixed_batch_persists_every_item_and_queues_only_valid_ones(self):
        files = [upload('good1.png'), upload('bad.jpg', b'not an image', 'image/jpeg'), upload('good2.jpg', image_bytes('JPEG'), 'image/jpeg'),
                 upload('tiny.png', image_bytes('PNG', size=(10, 10)))]
        with self.queue_only() as delay:
            response = self.post(files)
        self.assertEqual(response.status_code, 202)
        batch = response.json()['batch']
        self.assertEqual((batch['total_files'], batch['queued_count'], batch['failed_count']), (4, 2, 2))
        by_name = {i['original_filename']: i['status'] for i in batch['inspections']}
        self.assertEqual(by_name, {'good1.png': 'queued', 'bad.jpg': 'failed', 'good2.jpg': 'queued', 'tiny.png': 'failed'})
        self.assertEqual(delay.call_count, 2)

    def test_invalid_images_never_reach_inference_but_valid_ones_do(self):
        files = [upload('bad.png', b'garbage'), upload('good.png'), upload('small.png', image_bytes('PNG', size=(8, 8)))]
        with self.worker() as classifier:
            self.post(files)
        self.assertEqual(classifier.call_count, 1)
        called_path = str(classifier.call_args.args[0])
        self.assertTrue(called_path.endswith('.png'))
        self.assertEqual(Inspection.objects.filter(status='completed').count(), 1)

    def test_one_failure_does_not_stop_the_rest_of_the_batch(self):
        calls = []

        def classifier(path):
            calls.append(path)
            if len(calls) == 2:
                raise RuntimeError('simulated classifier crash')
            return FAKE_RESULT

        with self.worker(side_effect=classifier):
            response = self.post(self.valid(4))
        self.assertEqual(len(calls), 4)
        batch = self.client_for(self.engineer).get(reverse('api-batch-detail', args=[response.json()['batch']['id']])).json()
        self.assertEqual((batch['completed_count'], batch['failed_count'], batch['is_finished']), (3, 1, True))
        failed = Inspection.objects.get(status='failed')
        self.assertEqual(failed.failure_reason, 'The classifier could not process this image.')
        self.assertNotIn('simulated', failed.failure_reason)
        self.assertIsNone(failed.predicted_class)

    def test_batch_status_updates_as_the_worker_progresses(self):
        with self.queue_only():
            response = self.post(self.valid(3))
        batch_id = response.json()['batch']['id']
        url = reverse('api-batch-detail', args=[batch_id])
        client = self.client_for(self.engineer)
        body = client.get(url).json()
        self.assertEqual((body['queued_count'], body['processing_count'], body['completed_count'], body['is_finished'], body['progress_percent']), (3, 0, 0, False, 0))
        ids = [i['id'] for i in body['inspections']]
        Inspection.objects.filter(pk=ids[0]).update(status='processing')
        body = client.get(url).json()
        self.assertEqual((body['queued_count'], body['processing_count']), (2, 1))
        from inspections import services
        with self.worker():
            for pk in ids:
                services.process_inspection(pk)
        body = client.get(url).json()
        self.assertEqual((body['completed_count'], body['queued_count'], body['processing_count'], body['is_finished'], body['progress_percent']), (3, 0, 0, True, 100))

    def test_only_engineers_may_submit_batches(self):
        for user in (self.supervisor, self.admin):
            self.assertEqual(self.post(self.valid(1), user=user).status_code, 403)
        self.assertEqual(InspectionBatch.objects.count(), 0)

    def test_all_invalid_batch_is_reported_as_unprocessable_but_persisted(self):
        response = self.post([upload('a.png', b'x'), upload('b.png', b'')])
        self.assertEqual(response.status_code, 422)
        self.assertEqual(Inspection.objects.filter(status='failed').count(), 2)

    def test_batch_token_makes_resubmission_idempotent(self):
        with self.queue_only():
            first = self.post(self.valid(2), client_token='batch-token')
            second = self.post(self.valid(2), client_token='batch-token')
        self.assertEqual((first.status_code, second.status_code), (202, 200))
        self.assertEqual((InspectionBatch.objects.count(), Inspection.objects.count()), (1, 2))
