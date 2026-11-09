"""Idempotently inventory the organizer data without creating live inspection records."""

import csv
import hashlib
from pathlib import Path

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

from inspections.models import DatasetSample, Product, ValidationAsset
from inspections.validators import ImageValidationError, validate_image_upload


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Command(BaseCommand):
    help = 'Register organizer samples and validation assets. Safe to re-run; no live inspections are created.'

    def handle(self, *args, **options):
        root = Path(settings.ROUND2_PACK_DIR)
        labels_path = root / 'Samples' / 'labels.csv'
        if not labels_path.is_file():
            raise CommandError(f'labels.csv not found: {labels_path}')
        call_command('import_products', stdout=self.stdout)
        with labels_path.open(encoding='utf-8-sig', newline='') as handle:
            rows = list(csv.DictReader(handle))
        if len(rows) != 60 or {r.get('label') for r in rows} != {'Normal', 'Defective'}:
            raise CommandError('labels.csv must contain the supplied 60 Normal/Defective records.')
        counts = {label: sum(r.get('label') == label for r in rows) for label in ('Normal', 'Defective')}
        if counts != {'Normal': 30, 'Defective': 30}:
            raise CommandError(f'labels.csv expected 30 Normal and 30 Defective rows; found {counts}.')

        samples = 0
        for row in rows:
            path = root / 'Samples' / row['relative_path']
            if not path.is_file():
                raise CommandError(f'Missing sample: {path}')
            product = Product.objects.filter(product_id=row['product_id']).first()
            if product is None:
                raise CommandError(f'Unknown product in labels.csv: {row["product_id"]}')
            data = path.read_bytes()
            try:
                checked = validate_image_upload(SimpleUploadedFile(path.name, data))
                fields = dict(image_format=checked.format, image_width=checked.width, image_height=checked.height,
                              file_size=checked.size, validation_status='valid', validation_reason='')
            except ImageValidationError as exc:
                fields = dict(image_format='', image_width=None, image_height=None, file_size=len(data),
                              validation_status='invalid', validation_reason=str(exc))
            DatasetSample.objects.update_or_create(
                image_id=row['image_id'],
                defaults=dict(product=product, relative_path=row['relative_path'], filename=path.name,
                              sha256=digest(path), ground_truth=row['label'], **fields),
            )
            samples += 1

        assets = 0
        for path in sorted((root / 'Validation').glob('*')):
            if not path.is_file():
                continue
            try:
                validate_image_upload(SimpleUploadedFile(path.name, path.read_bytes()))
                reason, state = 'Unexpectedly accepted by image validation.', 'valid'
            except ImageValidationError as exc:
                reason, state = str(exc), 'invalid'
            ValidationAsset.objects.update_or_create(
                filename=path.name,
                defaults=dict(relative_path=str(path.relative_to(root)).replace('\\', '/'), sha256=digest(path),
                              file_size=path.stat().st_size, validation_status=state, validation_reason=reason),
            )
            assets += 1
        self.stdout.write(self.style.SUCCESS(
            f'Registered {samples} samples (Normal={counts["Normal"]}, Defective={counts["Defective"]}) and {assets} validation assets. '
            'No Engineer inspection records were created.'
        ))
