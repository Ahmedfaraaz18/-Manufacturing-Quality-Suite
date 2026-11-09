from celery import shared_task
from django.db import OperationalError

from .services import process_inspection


@shared_task(
    name='inspections.run_inspection', bind=True,
    autoretry_for=(OperationalError,), retry_backoff=2, retry_backoff_max=30, max_retries=3,
)
def run_inspection(self, inspection_id: int) -> str:
    """Classify one inspection.

    Only infrastructure errors (a database outage) are retried. Classifier failures are recorded
    as a FAILED item by ``process_inspection``, and a completed prediction is never rewritten.
    """
    return process_inspection(inspection_id)
