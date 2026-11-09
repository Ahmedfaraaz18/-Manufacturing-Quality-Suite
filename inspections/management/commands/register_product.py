import csv
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.core.exceptions import ValidationError

from inspections.models import Product


REQUIRED_COLUMNS = ('product_id', 'product_name', 'description')


class ProductImportCommand(BaseCommand):
    """Import organizer product metadata without changing product activation state."""

    help = (
        'Import products from product_details.csv. Safe to re-run: reports inserted, '
        'updated, skipped and failed rows without creating duplicates.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--csv', default=str(Path(settings.ROUND2_PACK_DIR) / 'product_details.csv'))

    def handle(self, *args, **options):
        path = Path(options['csv'])
        if not path.is_file():
            raise CommandError(f'CSV not found: {path}')
        with path.open(encoding='utf-8-sig', newline='') as handle:
            reader = csv.DictReader(handle)
            columns = tuple(reader.fieldnames or ())
            missing = [column for column in REQUIRED_COLUMNS if column not in columns]
            if missing:
                raise CommandError(f'CSV is missing required column(s): {", ".join(missing)}')
            rows = list(reader)
        if not rows:
            raise CommandError('The CSV contains no products.')

        counts = dict.fromkeys(('inserted', 'updated', 'skipped', 'failed'), 0)
        for line_number, row in enumerate(rows, start=2):
            product_id = (row.get('product_id') or '').strip()
            name = (row.get('product_name') or '').strip()
            description = (row.get('description') or '').strip()
            if not product_id or not name:
                counts['failed'] += 1
                self.stderr.write(f'Failed row {line_number}: product_id and product_name are required.')
                continue
            try:
                product = Product.objects.filter(product_id=product_id).first()
                if product is None:
                    product = Product(product_id=product_id, name=name, description=description)
                    product.full_clean()
                    product.save()
                    counts['inserted'] += 1
                    self.stdout.write(f'Inserted: {product}')
                elif product.name == name and product.description == description:
                    counts['skipped'] += 1
                    self.stdout.write(f'Skipped (unchanged): {product}')
                else:
                    # Preserve the existing active/inactive state and all related history.
                    product.name, product.description = name, description
                    product.full_clean()
                    product.save(update_fields=['name', 'description', 'updated_at'])
                    counts['updated'] += 1
                    self.stdout.write(f'Updated metadata: {product}')
            except ValidationError as exc:
                counts['failed'] += 1
                self.stderr.write(f'Failed row {line_number}: {exc}')
        self.stdout.write(
            'Summary: inserted={inserted}, updated={updated}, skipped={skipped}, failed={failed}'.format(**counts)
        )


class Command(ProductImportCommand):
    """Backward-compatible command name retained for existing runbooks."""
