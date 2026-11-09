from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from inspections.models import Inspection
from inspections.services import enqueue_inspection


class Command(BaseCommand):
    help = (
        'Re-queue inspections that were saved but never picked up (for example after a crash between saving and '
        'queueing, or a long broker outage). Processing is idempotent, so running this repeatedly is safe.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--older-than', type=int, default=2, help='Only items created more than N minutes ago (default 2).')
        parser.add_argument('--include-processing', action='store_true', help='Also retry items stuck in PROCESSING.')
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **options):
        cutoff = timezone.now() - timedelta(minutes=options['older_than'])
        statuses = [Inspection.Status.QUEUED]
        if options['include_processing']:
            statuses.append(Inspection.Status.PROCESSING)
        ids = list(Inspection.objects.filter(status__in=statuses, created_at__lte=cutoff).values_list('pk', flat=True))
        if options['dry_run']:
            self.stdout.write(f'{len(ids)} inspection(s) would be re-queued: {ids}')
            return
        queued = sum(1 for pk in ids if enqueue_inspection(pk))
        self.stdout.write(f'Re-queued {queued} of {len(ids)} inspection(s).')
