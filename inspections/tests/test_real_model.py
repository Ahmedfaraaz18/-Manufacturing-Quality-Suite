"""Integration tests that run the REAL supplied classifier (round2_pack/Model). Nothing is mocked except
the Celery hand-off, which runs in-process; the true broker/worker round trip is verified separately.
"""

import csv
import io
import json
import shutil
import tempfile
from pathlib import Path

from django.core.management import call_command
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, override_settings
from django.urls import reverse
from PIL import Image

from inspections import inference
from inspections.models import Inspection
from inspections.validators import validate_image_upload

from .base import PACK, SAMPLES, VALIDATION, InspectionTestCase


def read_labels():
    with (SAMPLES / 'labels.csv').open(newline='', encoding='utf-8-sig') as handle:
        return list(csv.DictReader(handle))


class RealClassifierTests(InspectionTestCase):
    def test_files_match_the_supplied_checksums(self):
        self.assertTrue(all(inference.verify_model_integrity().values()))

    def test_real_prediction_has_the_documented_shape(self):
        result = inference.predict_image(SAMPLES / 'images' / 'inspection_002.jpg')
        self.assertEqual(set(result), {'predicted_class', 'confidence', 'defect_probability', 'model_version'})
        self.assertEqual(result['model_version'], 'BIT-PLATE-CV-1.0')
        self.assertIn(result['predicted_class'], ('Normal', 'Defective'))
        expected_confidence = result['defect_probability'] if result['predicted_class'] == 'Defective' else 1 - result['defect_probability']
        self.assertAlmostEqual(result['confidence'], expected_confidence)

    def test_the_supplied_helper_gives_identical_output(self):
        """Our adapter must return exactly what the untouched reference helper returns."""
        import importlib.util

        spec = importlib.util.spec_from_file_location('reference', PACK / 'Model' / 'inference.py')
        reference = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(reference)
        for name in ('inspection_001.png', 'inspection_002.jpg', 'inspection_007.png'):
            path = SAMPLES / 'images' / name
            self.assertEqual(inference.predict_image(path), reference.predict(path))

    def test_classification_ignores_file_name_and_location(self):
        source = SAMPLES / 'images' / 'inspection_004.jpg'
        expected = inference.predict_image(source)
        with tempfile.TemporaryDirectory() as tmp:
            for name in ('defective.jpg', 'normal.jpg', 'x.JPG'):
                copy = Path(tmp) / name
                shutil.copy(source, copy)
                self.assertEqual(inference.predict_image(copy), expected, name)

    def test_color_modes_and_formats_do_not_crash_the_classifier(self):
        for fmt, mode in (('PNG', 'RGB'), ('PNG', 'RGBA'), ('PNG', 'L'), ('PNG', 'P'), ('JPEG', 'L'), ('JPEG', 'RGB')):
            fill = 128 if mode in ('L', 'P') else (128, 128, 128, 255)[: len(mode)]
            buffer = io.BytesIO()
            Image.new(mode, (200, 200), fill).save(buffer, format=fmt)
            validated = validate_image_upload(SimpleUploadedFile('m', buffer.getvalue()))
            with tempfile.NamedTemporaryFile(suffix='.' + validated.extension, delete=False) as handle:
                handle.write(buffer.getvalue())
            try:
                result = inference.predict_image(handle.name)
            finally:
                Path(handle.name).unlink()
            self.assertIn(result['predicted_class'], ('Normal', 'Defective'), f'{fmt} {mode}')

    def test_missing_or_tampered_model_files_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            pack = Path(tmp) / 'pack'
            shutil.copytree(PACK / 'Model', pack / 'Model')
            shutil.copy(PACK / 'SHA256SUMS.txt', pack / 'SHA256SUMS.txt')
            with override_settings(ROUND2_PACK_DIR=pack):
                inference.reset_cache()
                self.addCleanup(inference.reset_cache)
                (pack / 'Model' / 'classifier.npz').write_bytes(b'tampered')
                with self.assertLogs('inspections.inference', level='ERROR'):
                    with self.assertRaises(inference.ModelUnavailable):
                        inference.predict_image(SAMPLES / 'images' / 'inspection_001.png')
                inference.reset_cache()
                (pack / 'Model' / 'classifier.npz').unlink()
                with self.assertLogs('inspections.inference', level='ERROR'):
                    with self.assertRaises(inference.ModelUnavailable):
                        inference.predict_image(SAMPLES / 'images' / 'inspection_001.png')

    def test_the_helper_is_imported_once_per_process(self):
        inference.reset_cache()
        first = inference._load_helper()
        inference.predict_image(SAMPLES / 'images' / 'inspection_001.png')
        self.assertIs(inference._load_helper(), first)


class RealPipelineTests(InspectionTestCase):
    def submit(self, files):
        with self.real_worker():
            response = self.client_for(self.engineer).post(
                reverse('api-inspection-batch'), {'product': 'P-PLATE01', 'files': files}, format='multipart')
        self.assertIn(response.status_code, (202, 422), response.content)
        # the worker ran when the request's transaction committed, so read the final state back
        batch_id = response.json()['batch']['id']
        return self.client_for(self.engineer).get(reverse('api-batch-detail', args=[batch_id])).json()

    def sample_files(self, names):
        return [SimpleUploadedFile(n, (SAMPLES / 'images' / n).read_bytes()) for n in names]

    def test_single_real_image_end_to_end(self):
        with self.real_worker():
            response = self.client_for(self.engineer).post(
                reverse('api-inspection-upload'), {'product': 'P-PLATE01', 'image': self.sample_files(['inspection_001.png'])[0]}, format='multipart')
        self.assertEqual(response.status_code, 202)
        inspection = Inspection.objects.get()
        self.assertEqual(inspection.status, 'completed')
        expected = inference.predict_image(SAMPLES / 'images' / 'inspection_001.png')
        self.assertEqual((inspection.predicted_class, inspection.confidence, inspection.defect_probability, inspection.model_version),
                         (expected['predicted_class'], expected['confidence'], expected['defect_probability'], expected['model_version']))
        detail = self.client_for(self.engineer).get(reverse('inspection-detail', args=[inspection.pk]))
        self.assertContains(detail, 'BIT-PLATE-CV-1.0')

    def test_mixed_batch_with_the_supplied_invalid_files(self):
        files = self.sample_files(['inspection_001.png', 'inspection_002.jpg'])
        for name in ('not_an_image.jpg', 'corrupt_image.png', 'too_small.png'):
            files.append(SimpleUploadedFile(name, (VALIDATION / name).read_bytes()))
        batch = self.submit(files)
        self.assertEqual((batch['total_files'], batch['completed_count'], batch['failed_count'], batch['is_finished']), (5, 2, 3, True))
        by_name = {i['original_filename']: i for i in batch['inspections']}
        for name in ('not_an_image.jpg', 'corrupt_image.png', 'too_small.png'):
            self.assertEqual(by_name[name]['status'], 'failed', name)
            self.assertIsNone(by_name[name]['predicted_class'])
            self.assertTrue(by_name[name]['failure_reason'])
        for name in ('inspection_001.png', 'inspection_002.jpg'):
            self.assertEqual(by_name[name]['status'], 'completed', name)
            self.assertEqual(by_name[name]['model_version'], 'BIT-PLATE-CV-1.0')

    def test_full_review_workflow_keeps_the_real_prediction(self):
        batch = self.submit(self.sample_files(['inspection_001.png']))
        item = Inspection.objects.get(pk=batch['inspections'][0]['id'])
        original = (item.predicted_class, item.confidence, item.defect_probability)
        corrected = 'Normal' if item.predicted_class == 'Defective' else 'Defective'
        response = self.client_for(self.supervisor).post(
            reverse('api-inspection-review', args=[item.pk]), {'reviewed_class': corrected, 'comment': 'checked'}, format='json')
        self.assertEqual(response.status_code, 201)
        item.refresh_from_db()
        self.assertEqual((item.predicted_class, item.confidence, item.defect_probability), original)
        self.assertEqual(self.client_for(self.engineer).post(
            reverse('api-inspection-review', args=[item.pk]), {'reviewed_class': 'Normal'}, format='json').status_code, 403)

    def test_all_ten_images_in_a_batch_complete(self):
        names = [f'inspection_{i:03d}.{"png" if i % 2 else "jpg"}' for i in range(1, 11)]
        batch = self.submit(self.sample_files(names))
        self.assertEqual((batch['completed_count'], batch['failed_count']), (10, 0))


class EvaluationCommandTests(SimpleTestCase):
    def run_evaluation(self):
        out = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, out, ignore_errors=True)
        stdout = io.StringIO()
        call_command('evaluate_model', output_dir=out, stdout=stdout)
        return json.loads((Path(out) / 'round2_evaluation.json').read_text()), Path(out), stdout.getvalue()

    def test_all_sixty_samples_are_evaluated_with_consistent_metrics(self):
        report, out, text = self.run_evaluation()
        m = report['metrics']
        self.assertEqual((m['total_samples'], m['successful_predictions'], m['failed_predictions']), (60, 60, 0))
        self.assertEqual(m['true_positives'] + m['false_negatives'], 30)
        self.assertEqual(m['true_negatives'] + m['false_positives'], 30)
        self.assertEqual(report['samples_per_class'], {'Normal': 30, 'Defective': 30})
        self.assertAlmostEqual(m['defect_recall'], m['true_positives'] / (m['true_positives'] + m['false_negatives']))
        self.assertEqual(m['confusion_matrix']['matrix'], [[m['true_negatives'], m['false_positives']], [m['false_negatives'], m['true_positives']]])
        self.assertEqual(len((out / 'round2_predictions.csv').read_text().strip().splitlines()), 61)
        self.assertIn('Defect recall', text)
        self.assertFalse(any('Validation' in row['relative_path'] for row in report['rows']))

    def test_documented_baseline_result(self):
        report, _, _ = self.run_evaluation()
        m = report['metrics']
        self.assertEqual((m['true_positives'], m['false_negatives'], m['false_positives'], m['true_negatives']), (30, 0, 0, 30))
        self.assertEqual(m['defect_recall'], 1.0)
        self.assertTrue(any('cannot establish real-world reliability' in n for n in report['notes']))

    def test_metrics_math_and_failed_predictions_are_reported(self):
        from inspections.management.commands.evaluate_model import compute_metrics

        rows = ([{'actual': 'Defective', 'predicted': 'Defective'}] * 3 + [{'actual': 'Defective', 'predicted': 'Normal'}]
                + [{'actual': 'Normal', 'predicted': 'Defective'}] * 2 + [{'actual': 'Normal', 'predicted': 'Normal'}] * 4
                + [{'actual': 'Normal', 'predicted': None}])
        m = compute_metrics(rows)
        self.assertEqual((m['true_positives'], m['false_negatives'], m['false_positives'], m['true_negatives']), (3, 1, 2, 4))
        self.assertEqual((m['total_samples'], m['successful_predictions'], m['failed_predictions']), (11, 10, 1))
        self.assertAlmostEqual(m['defect_recall'], 0.75)
        self.assertEqual(m['confusion_matrix']['matrix'], [[4, 2], [1, 3]])

    def test_labels_are_not_read_during_inference(self):
        """Predictions must not depend on labels.csv: corrupt every label and compare the predictions."""
        with tempfile.TemporaryDirectory() as tmp:
            samples = Path(tmp) / 'Samples'
            shutil.copytree(SAMPLES, samples)
            rows = read_labels()
            with (samples / 'labels.csv').open('w', newline='') as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
                writer.writeheader()
                for row in rows:
                    row['label'] = 'Normal' if row['label'] == 'Defective' else 'Defective'    # flip every label
                    writer.writerow(row)
            out = Path(tmp) / 'out'
            call_command('evaluate_model', samples_dir=str(samples), output_dir=str(out), stdout=io.StringIO())
            flipped = json.loads((out / 'round2_evaluation.json').read_text())
        real, _, _ = self.run_evaluation()
        self.assertEqual([r['predicted'] for r in flipped['rows']], [r['predicted'] for r in real['rows']])
        self.assertEqual(flipped['metrics']['true_positives'], real['metrics']['false_positives'])


class SmokeAndRegistrationTests(InspectionTestCase):
    def test_smoke_command_runs_the_real_classifier(self):
        out = io.StringIO()
        call_command('model_smoke_test', stdout=out)
        text = out.getvalue()
        self.assertIn('BIT-PLATE-CV-1.0', text)
        self.assertIn('Smoke test passed', text)

    def test_register_product_is_idempotent(self):
        from inspections.models import Product

        Product.objects.all().delete()
        for _ in range(3):
            call_command('register_product', stdout=io.StringIO())
        product = Product.objects.get()
        self.assertEqual((product.product_id, product.name), ('P-PLATE01', 'Demonstration metal plate'))
        self.assertEqual(product.description, 'Synthetic centered plate with scratches or pits')
