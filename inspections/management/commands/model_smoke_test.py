import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from inspections import inference


class Command(BaseCommand):
    help = 'Smoke-test the supplied classifier: checksum verification plus a real prediction on one sample image.'

    def add_arguments(self, parser):
        parser.add_argument('--image', default=str(Path(settings.ROUND2_PACK_DIR) / 'Samples' / 'images' / 'inspection_001.png'))

    def handle(self, *args, **options):
        checks = inference.verify_model_integrity()
        for name, ok in checks.items():
            self.stdout.write(f'{"OK  " if ok else "FAIL"} checksum {name}')
        if not all(checks.values()):
            raise CommandError('Model files do not match SHA256SUMS.txt.')
        image = Path(options['image'])
        if not image.is_file():
            raise CommandError(f'Image not found: {image}')
        try:
            result = inference.predict_image(image)
        except inference.InferenceError as exc:
            raise CommandError(f'Classifier failed: {exc.__cause__ or exc}') from exc
        self.stdout.write(json.dumps(result, indent=2))
        meta = inference.model_metadata()
        if meta.get('model_version') != result['model_version']:
            raise CommandError('Model version differs from model_metadata.json.')
        self.stdout.write(self.style.SUCCESS('Smoke test passed: real classifier output received.'))
