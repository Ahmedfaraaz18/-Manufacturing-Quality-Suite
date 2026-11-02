"""RAG orchestration: retrieve (machine-scoped) -> validate evidence -> LLM -> validate citations -> persist."""

import logging
from dataclasses import dataclass, field

from django.conf import settings
from django.db import transaction

from ..models import AnswerSource, Machine, QuestionHistory
from . import llm, retrieval
from .embeddings import EmbeddingError

logger = logging.getLogger(__name__)

INSUFFICIENT_MESSAGE = (
    'The available manuals for the selected machine do not contain enough information '
    'to answer this question.'
)

_VALID_STATUSES = {'answered', 'insufficient_evidence'}


class InvalidLLMResponse(llm.LLMBadResponse):
    pass


@dataclass
class ValidatedAnswer:
    status: str
    answer: str
    sources: list = field(default_factory=list)  # [(RetrievedChunk, passage)]


def validate_llm_payload(payload: dict, evidence: list[retrieval.RetrievedChunk]) -> ValidatedAnswer:
    """Keep only claims whose every evidence entry is an exact retrieved quotation."""
    by_id = {item.source_id: item for item in evidence}
    if not isinstance(payload, dict):
        raise InvalidLLMResponse('response is not a JSON object')
    status = payload.get('answered_status')
    claims = payload.get('claims')
    if not isinstance(status, str) or status not in _VALID_STATUSES or not isinstance(claims, list):
        raise InvalidLLMResponse('missing or invalid answered_status/claims')

    if status == 'insufficient_evidence':
        return ValidatedAnswer('insufficient_evidence', INSUFFICIENT_MESSAGE)

    validated_sources = []
    seen = set()
    for claim in claims:
        if not isinstance(claim, dict) or not isinstance(claim.get('claim'), str) or not claim['claim'].strip():
            logger.warning('Discarded malformed LLM claim')
            continue
        entries = claim.get('evidence')
        if not isinstance(entries, list) or not entries:
            logger.warning('Discarded LLM claim without evidence')
            continue
        claim_sources = []
        for entry in entries:
            if not isinstance(entry, dict):
                break
            source_id, quote = entry.get('source_id'), entry.get('quote')
            if not isinstance(source_id, str) or source_id not in by_id:
                break
            item = by_id[source_id]
            if not isinstance(quote, str) or not quote.strip() or quote not in item.chunk.chunk_text:
                break
            claim_sources.append((item, quote))
        else:
            for item, quote in claim_sources:
                key = (item.source_id, quote)
                if key not in seen:
                    seen.add(key)
                    validated_sources.append((item, quote))
            continue
        logger.warning('Discarded LLM claim with invalid evidence')

    if not validated_sources:
        return ValidatedAnswer('insufficient_evidence', INSUFFICIENT_MESSAGE)
    rendered = '\n\n'.join(
        f'{quote} [{item.chunk.document_id} rev {item.chunk.revision}, p.{item.chunk.original_page_number}]'
        for item, quote in validated_sources
    )
    return ValidatedAnswer('answered', rendered, validated_sources)


def _persist(user, machine, question, status, answer, sources=()) -> QuestionHistory:
    with transaction.atomic():
        record = QuestionHistory.objects.create(
            user=user, machine=machine, question=question, answer=answer, status=status
        )
        AnswerSource.objects.bulk_create(
            [
                AnswerSource(
                    question=record,
                    chunk=item.chunk,
                    manual_id=item.chunk.manual_id,
                    document_id=item.chunk.document_id,
                    document_name=item.chunk.document_name,
                    revision=item.chunk.revision,
                    page_number=item.chunk.original_page_number,
                    passage=passage,
                    score=item.score,
                    position=position,
                )
                for position, (item, passage) in enumerate(sources)
            ]
        )
    return record


def answer_question(user, machine: Machine, question: str) -> QuestionHistory:
    question = question.strip()
    if not question:
        raise ValueError('question is empty')
    if len(question) > settings.MAX_QUESTION_LENGTH:
        raise ValueError('question is too long')

    try:
        evidence = retrieval.retrieve(machine, question)
    except EmbeddingError:
        logger.exception('Embedding failure')
        return _persist(user, machine, question, QuestionHistory.Status.ERROR,
                        'The search service is unavailable. Please contact an administrator.')

    if not evidence:
        return _persist(user, machine, question, QuestionHistory.Status.INSUFFICIENT, INSUFFICIENT_MESSAGE)

    try:
        payload = llm.generate_answer(machine, question, evidence)
        result = validate_llm_payload(payload, evidence)
    except llm.LLMError as exc:
        return _persist(user, machine, question, QuestionHistory.Status.ERROR, exc.user_message)

    status = (
        QuestionHistory.Status.ANSWERED
        if result.status == 'answered'
        else QuestionHistory.Status.INSUFFICIENT
    )
    return _persist(user, machine, question, status, result.answer, result.sources)
