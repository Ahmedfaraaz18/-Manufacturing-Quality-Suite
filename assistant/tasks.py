from celery import shared_task

from .services.ingestion import index_manual


@shared_task(name='assistant.index_manual')
def index_manual_task(manual_id: int) -> int:
    return index_manual(manual_id).pk
