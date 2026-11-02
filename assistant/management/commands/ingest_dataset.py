import csv
from pathlib import Path

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management.base import BaseCommand, CommandError

from assistant.models import Machine, Manual, User
from assistant.services.ingestion import IngestionError, register_manual

TITLES = {
    'HP-OPS-01': 'HP 200 Operations Manual',
    'HP-MNT-01': 'HP 200 Maintenance Manual',
    'AC-OM-01': 'AC 90 Operations and Maintenance Manual',
}


class Command(BaseCommand):
    help = (
        'Register machines and index manuals listed in machine_manual_index.csv. '
        'Only the PDFs under --manuals-dir are indexed; the CSV is used purely as registration metadata.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--csv', required=True, help='Path to machine_manual_index.csv')
        parser.add_argument('--manuals-dir', required=True, help='Folder containing the manual PDFs')
        parser.add_argument('--user', default='admin_demo', help='Administrator recorded as uploader')

    def handle(self, *args, **options):
        csv_path, manuals_dir = Path(options['csv']), Path(options['manuals_dir'])
        if not csv_path.is_file() or not manuals_dir.is_dir():
            raise CommandError('CSV file or manuals directory not found.')
        uploader = User.objects.filter(username=options['user']).first()

        with csv_path.open(newline='', encoding='utf-8-sig') as handle:
            rows = list(csv.DictReader(handle))

        for row in rows:
            machine, _ = Machine.objects.get_or_create(
                machine_id=row['machine_id'],
                defaults={'model_name': row['model'], 'name': row['machine_name']},
            )
            if Manual.objects.filter(document_id=row['document_id'], revision=row['revision']).exists():
                self.stdout.write(f'Skipping {row["document_id"]}: already registered')
                continue
            pdf = manuals_dir / row['pdf_filename']
            if not pdf.is_file():
                raise CommandError(f'Missing PDF: {pdf}')
            upload = SimpleUploadedFile(pdf.name, pdf.read_bytes(), content_type='application/pdf')
            try:
                manual = register_manual(
                    machine=machine,
                    document_id=row['document_id'],
                    document_name=TITLES.get(row['document_id'], pdf.stem),
                    revision=row['revision'],
                    uploaded_file=upload,
                    user=uploader,
                )
            except IngestionError as exc:
                raise CommandError(f'{row["document_id"]}: {exc}') from exc
            self.stdout.write(
                f'{manual.document_id} rev {manual.revision} -> {machine.machine_id}: '
                f'{manual.status}, {manual.page_count} pages, {manual.chunks.count()} chunks'
            )
