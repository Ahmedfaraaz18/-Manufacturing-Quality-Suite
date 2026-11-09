"""Evaluate the supplied classifier on the 60 labelled samples.

Inference never sees the labels: every image is classified first, and labels.csv is only read
afterwards to score the already-recorded predictions. Reviews are not involved at all.
"""

import csv
import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from inspections import inference

CLASSES = ('Normal', 'Defective')


def compute_metrics(rows):
    """rows: dicts with ``actual`` and ``predicted`` (None when the prediction failed)."""
    counts = {(a, p): 0 for a in CLASSES for p in CLASSES}
    failed = 0
    for row in rows:
        if row['predicted'] is None:
            failed += 1
        else:
            counts[(row['actual'], row['predicted'])] += 1
    tp, fn = counts[('Defective', 'Defective')], counts[('Defective', 'Normal')]
    fp, tn = counts[('Normal', 'Defective')], counts[('Normal', 'Normal')]
    successful = tp + fn + fp + tn
    precision = (tp / (tp + fp)) if (tp + fp) else None
    recall = (tp / (tp + fn)) if (tp + fn) else None
    return {
        'total_samples': len(rows), 'successful_predictions': successful, 'failed_predictions': failed,
        'true_positives': tp, 'false_negatives': fn, 'false_positives': fp, 'true_negatives': tn,
        'confusion_matrix': {
            'rows_actual': list(CLASSES), 'columns_predicted': list(CLASSES),
            'matrix': [[tn, fp], [fn, tp]],
        },
        'precision': precision,
        'recall': recall,
        'f1_score': (2 * precision * recall / (precision + recall)) if precision is not None and recall is not None and precision + recall else None,
        'defect_recall': recall,
        'accuracy': ((tp + tn) / successful) if successful else None,
    }


class Command(BaseCommand):
    help = 'Run the real classifier on all labelled samples and report the confusion matrix and defect recall.'

    def add_arguments(self, parser):
        pack = Path(settings.ROUND2_PACK_DIR)
        parser.add_argument('--samples-dir', default=str(pack / 'Samples'))
        parser.add_argument('--output-dir', default=str(Path(settings.BASE_DIR) / 'evaluation_reports'))

    def handle(self, *args, **options):
        samples = Path(options['samples_dir'])
        labels_path = samples / 'labels.csv'
        if not labels_path.is_file():
            raise CommandError(f'labels.csv not found in {samples}')
        with labels_path.open(encoding='utf-8-sig', newline='') as handle:
            records = list(csv.DictReader(handle))

        # Pass 1: inference only. Labels are not consulted.
        predictions = []
        for record in records:
            image = samples / record['relative_path']
            try:
                result = inference.predict_image(image)
                predictions.append({'result': result, 'error': None})
            except Exception as exc:  # failed predictions are reported, never silently dropped
                predictions.append({'result': None, 'error': str(exc.__cause__ or exc)})

        # Pass 2: compare recorded predictions with the ground-truth labels.
        rows = []
        for record, outcome in zip(records, predictions):
            result = outcome['result']
            rows.append({
                'image_id': record['image_id'], 'relative_path': record['relative_path'],
                'actual': record['label'],
                'predicted': result['predicted_class'] if result else None,
                'confidence': result['confidence'] if result else None,
                'defect_probability': result['defect_probability'] if result else None,
                'model_version': result['model_version'] if result else None,
                'error': outcome['error'],
            })
        metrics = compute_metrics(rows)
        counts = {c: sum(1 for r in rows if r['actual'] == c) for c in CLASSES}
        meta = inference.model_metadata()
        report = {
            'generated_at': timezone.now().isoformat(),
            'model_version': meta.get('model_version'), 'product_id': meta.get('product_id'),
            'decision_threshold': meta.get('decision_threshold'), 'samples_per_class': counts,
            'metrics': metrics, 'rows': rows,
            'notes': [
                'Labels were read only after all predictions were produced; they are never inference inputs.',
                'The three files in Validation/ are excluded from these metrics.',
                'Sixty synthetic, fixed-framing samples are development data, not a blind benchmark; '
                'these results cannot establish real-world reliability.',
            ],
        }
        if metrics['false_negatives'] == 0 and metrics['failed_predictions'] == 0:
            report['notes'].append(
                'No false negatives occurred on these samples. That does not show the model never misses defects: '
                'the set is small, synthetic and from the same generator and framing as the training data.'
            )
        out = Path(options['output_dir'])
        out.mkdir(parents=True, exist_ok=True)
        (out / 'round2_evaluation.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        with (out / 'round2_predictions.csv').open('w', encoding='utf-8', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)

        m = metrics
        self.stdout.write(f'Samples: {m["total_samples"]}  successful: {m["successful_predictions"]}  failed: {m["failed_predictions"]}')
        self.stdout.write('Confusion matrix (rows = actual, columns = predicted):')
        self.stdout.write(f'                  Normal  Defective')
        self.stdout.write(f'  Normal          {m["true_negatives"]:>6}  {m["false_positives"]:>9}')
        self.stdout.write(f'  Defective       {m["false_negatives"]:>6}  {m["true_positives"]:>9}')
        self.stdout.write(f'TP={m["true_positives"]} FN={m["false_negatives"]} FP={m["false_positives"]} TN={m["true_negatives"]}')
        recall = m['defect_recall']
        self.stdout.write(f'Defect recall = TP/(TP+FN) = {recall:.4f}' if recall is not None else 'Defect recall: undefined')
        if m['accuracy'] is not None:
            self.stdout.write(f'Accuracy = {m["accuracy"]:.4f}')
        self.stdout.write(f'Precision = {m["precision"]:.4f}' if m['precision'] is not None else 'Precision: undefined')
        self.stdout.write(f'Recall = {m["recall"]:.4f}' if m['recall'] is not None else 'Recall: undefined')
        self.stdout.write(f'F1-score = {m["f1_score"]:.4f}' if m['f1_score'] is not None else 'F1-score: undefined')
        for note in report['notes']:
            self.stdout.write(f'Note: {note}')
        self.stdout.write(self.style.SUCCESS(f'Reports written to {out}'))
