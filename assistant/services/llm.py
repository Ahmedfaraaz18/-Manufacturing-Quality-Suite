"""Groq chat-completion client and prompt construction."""

import json
import logging
import os
import time
from functools import lru_cache

from django.conf import settings

from .retrieval import RetrievedChunk

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    'You are a manufacturing manual assistant. Answer exclusively using the supplied evidence passages. '
    'Never use external knowledge, assumptions or general model knowledge. Never invent specifications, '
    'procedures, document IDs, revisions or page references. If the evidence is insufficient, explicitly '
    'state that the available selected-machine manuals do not contain enough information. '
    'Understand ordinary paraphrases and synonyms when the passage directly supports the question, '
    'but do not assume an ambiguous equivalence or infer missing information.'
)

OUTPUT_INSTRUCTIONS = """Respond with a single JSON object and nothing else, using exactly these keys:
{
  "answered_status": "answered" or "insufficient_evidence",
  "claims": [
    {"claim": "<one concise claim>", "evidence": [
      {"source_id": "S1", "quote": "<exact verbatim substring copied from S1>"}
    ]}
  ]
}
Rules:
- Each claim must have at least one evidence entry that directly supports it. Copy each quote exactly,
  including capitalization, punctuation and whitespace. Do not combine non-contiguous text.
- Cite only evidence ids in the EVIDENCE section. The user will see the quotes, not your claim text.
- Do not write document IDs, revisions or page numbers; the system adds those from its database.
- If the evidence does not directly answer the question, set "answered_status" to "insufficient_evidence"
  and "claims" to an empty list."""


class LLMError(Exception):
    """Base class; `user_message` is safe to show to end users."""

    user_message = 'The AI service could not process this request. Please try again.'


class LLMUnavailable(LLMError):
    user_message = 'The AI service is temporarily unavailable or timed out. Please try again shortly.'


class LLMBadResponse(LLMError):
    user_message = 'The AI service returned an unusable response, so no answer is shown. Please try again.'


def build_messages(machine, question: str, evidence: list[RetrievedChunk]) -> list[dict]:
    blocks = []
    for item in evidence:
        c = item.chunk
        blocks.append(
            f'[{item.source_id}] Document {c.document_id} ({c.document_name}), '
            f'revision {c.revision}, PDF page {c.original_page_number}\n{c.chunk_text}'
        )
    user_content = (
        f'Selected machine: {machine.machine_id} ({machine.model_name}, {machine.name})\n\n'
        'EVIDENCE (the only information you may use):\n\n' + '\n\n'.join(blocks) +
        f'\n\nQUESTION: {question}\n\n{OUTPUT_INSTRUCTIONS}'
    )
    return [
        {'role': 'system', 'content': SYSTEM_PROMPT},
        {'role': 'user', 'content': user_content},
    ]


def parse_json_object(text: str) -> dict:
    if not text or not text.strip():
        raise LLMBadResponse('empty completion')
    text = text.strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMBadResponse(f'invalid JSON: {exc}') from exc
    if not isinstance(value, dict):
        raise LLMBadResponse('JSON root is not an object')
    return value


@lru_cache(maxsize=1)
def _client_for_config(process_id: int, api_key: str, timeout: float, max_retries: int):
    from groq import Groq

    # A forked worker must not reuse its parent's HTTP connection pool.
    return Groq(api_key=api_key, timeout=timeout, max_retries=max_retries)


def _client():
    if not settings.GROQ_API_KEY:
        raise LLMUnavailable('GROQ_API_KEY is not configured')
    return _client_for_config(
        os.getpid(), settings.GROQ_API_KEY, settings.GROQ_TIMEOUT_SECONDS, settings.GROQ_MAX_RETRIES
    )


def generate_answer(machine, question: str, evidence: list[RetrievedChunk]) -> dict:
    """Call Groq and return the parsed (still unvalidated) JSON object."""
    import groq

    messages = build_messages(machine, question, evidence)
    kwargs = dict(
        model=settings.GROQ_MODEL,
        messages=messages,
        temperature=0,
        max_completion_tokens=settings.GROQ_MAX_COMPLETION_TOKENS,
        response_format={'type': 'json_object'},
    )
    try:
        deadline = time.monotonic() + settings.GROQ_TIMEOUT_SECONDS
        client = _client()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LLMUnavailable('Groq request deadline expired before the request')
        try:
            response = client.chat.completions.create(reasoning_effort='low', timeout=remaining, **kwargs)
        except groq.BadRequestError:
            # Some models or SDK versions reject reasoning_effort.
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LLMUnavailable('Groq request deadline expired before retry') from None
            response = client.chat.completions.create(timeout=remaining, **kwargs)
    except LLMError:
        raise
    except (groq.APITimeoutError, groq.APIConnectionError, groq.APIStatusError) as exc:
        logger.warning('Groq request failed: %s', type(exc).__name__)
        raise LLMUnavailable(str(type(exc).__name__)) from exc
    except Exception as exc:
        logger.exception('Unexpected LLM failure')
        raise LLMUnavailable(str(type(exc).__name__)) from exc

    if not response.choices:
        raise LLMBadResponse('no choices returned')
    return parse_json_object(response.choices[0].message.content or '')
