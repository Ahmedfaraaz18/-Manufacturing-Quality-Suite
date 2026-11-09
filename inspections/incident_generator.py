"""Grounded incident summary and suggested actions generator with strict citation verification.

Requirements:
- Generates factual incident summary from saved inspection metadata.
- Generates actionable next steps strictly supported by retrieved SOP passages.
- Every suggested action contains:
  - action description
  - source document ID and name
  - document revision
  - one-based PDF page number
  - supporting passage (verbatim substring of retrieved SOP)
  - valid source link
- Strict citation verification: any action without verifiable support is dropped.
"""

import json
import logging
import re
from typing import Any

from django.conf import settings

from .sop_service import RetrievedSOPPassage

logger = logging.getLogger(__name__)


def _normalize_ws(text: str) -> str:
    """Collapse consecutive whitespace and newlines for robust quotation matching."""
    return re.sub(r'\s+', ' ', text).strip()


def generate_incident_summary(inspection, product_id: str) -> str:
    """Generate a concise, factual incident summary directly from inspection data.

    Never infers defect subtype, cause or severity from the binary model prediction.
    """
    comp_at = inspection.completed_at
    time_str = comp_at.strftime('%Y-%m-%d %H:%M:%S UTC') if comp_at else 'Timestamp recorded'
    conf = f'{inspection.confidence_percent:.2f}%' if inspection.confidence is not None else 'N/A'
    defect_prob = f'{inspection.defect_probability_percent:.2f}%' if inspection.defect_probability is not None else 'N/A'
    model_ver = inspection.model_version or 'BIT-PLATE-CV-1.0'
    batch_info = f'Batch #{inspection.batch_id}' if inspection.batch_id else 'Not provided'
    filename = inspection.original_filename or 'image file'

    return (
        f"Quality incident for product {product_id} ({inspection.product.name}) generated from completed "
        f"inspection #{inspection.pk} ({filename}, {batch_info}). The automated inspection model ({model_ver}) "
        f"evaluated the item on {time_str} and produced an original prediction of {inspection.predicted_class} "
        f"with {conf} confidence (defect probability: {defect_prob}). This records the classifier output only; "
        f"no defect subtype, physical dimensions, cause, or severity is inferred."
    )


def _build_grounded_plate_actions(passages: list[RetrievedSOPPassage]) -> list[dict[str, Any]]:
    """Deterministic, verified actions mapped strictly to retrieved Q-PLATE-01 and Q-PLATE-02 passages."""
    p_by_doc_page = {(p.document_id, p.page_number): p for p in passages}
    actions: list[dict[str, Any]] = []

    # 1. Item-level hold from Q-PLATE-01 Page 1
    p1 = p_by_doc_page.get(('Q-PLATE-01', 1))
    if p1:
        quote = 'Record a proposed quality hold for the inspected item pending Supervisor review. The portal records this proposed disposition only; it does not move stock or apply controls to equipment. Keep the image linked to the incident so the reviewer can compare it with the prediction.'
        actions.append({
            'action': 'Record a proposed quality hold for this inspected item pending Supervisor review, and keep its image linked to the incident.',
            'document_id': p1.document_id,
            'document_name': p1.document_name,
            'revision': p1.revision,
            'page_number': p1.page_number,
            'supporting_passage': quote,
            'source_link': p1.source_link,
        })

    # 2. Evidence Preservation from Q-PLATE-01 Page 2
    p2 = p_by_doc_page.get(('Q-PLATE-01', 2))
    if p2:
        quote = 'Keep the original image reference, original model output, confidence, model version, and any human correction available side by side. A human correction must not erase the model prediction. Link all proposed actions to this incident and the relevant procedure page.'
        actions.append({
            'action': 'Keep the original image, model output, confidence, model version, and any human correction available side by side.',
            'document_id': p2.document_id,
            'document_name': p2.document_name,
            'revision': p2.revision,
            'page_number': p2.page_number,
            'supporting_passage': quote,
            'source_link': p2.source_link,
        })

    # 3. Escalation from Q-PLATE-01 Page 2
    p3 = p_by_doc_page.get(('Q-PLATE-01', 2))
    if p3:
        quote = (
            'Request a Supervisor review of the image, inspection record, and applicable procedures. '
            'Summarize the item as flagged by the classifier; do not describe it as a confirmed physical defect '
            'unless separately supported by a recorded human observation.'
        )
        actions.append({
            'action': 'Request Supervisor review of the image, inspection record, and applicable procedures; describe the result as classifier-flagged.',
            'document_id': p3.document_id,
            'document_name': p3.document_name,
            'revision': p3.revision,
            'page_number': p3.page_number,
            'supporting_passage': quote,
            'source_link': p3.source_link,
        })

    # 4. Review checklist from Q-PLATE-02 Page 1
    p4 = p_by_doc_page.get(('Q-PLATE-02', 1))
    if p4:
        quote = 'Confirm that the product and inspection references match the saved image and original model output.'
        actions.append({
            'action': 'Have the Supervisor confirm that the product and inspection references match the saved image and original model output.',
            'document_id': p4.document_id,
            'document_name': p4.document_name,
            'revision': p4.revision,
            'page_number': p4.page_number,
            'supporting_passage': quote,
            'source_link': p4.source_link,
        })

    return actions


def validate_citation(action: dict[str, Any], retrieved_passages: list[RetrievedSOPPassage]) -> bool:
    """Validate that every citation strictly corresponds to an actual retrieved SOP passage."""
    doc_id = action.get('document_id')
    rev = action.get('revision')
    page_num = action.get('page_number')
    passage = action.get('supporting_passage')
    source_link = action.get('source_link')

    if not all([doc_id, rev, page_num, passage, source_link]):
        return False

    norm_passage = _normalize_ws(str(passage))
    if not norm_passage:
        return False

    # Find matching retrieved passage
    for p in retrieved_passages:
        if p.document_id == doc_id and p.revision == rev and p.page_number == page_num:
            norm_chunk = _normalize_ws(p.text)
            if (
                action.get('document_name') == p.document_name
                and source_link == p.source_link
                and norm_passage in norm_chunk
            ):
                return True

    return False


def generate_suggested_actions(
    product_id: str, retrieved_passages: list[RetrievedSOPPassage]
) -> tuple[bool, str, list[dict[str, Any]]]:
    """Generate and strictly validate suggested next steps supported by retrieved SOP passages.

    Returns:
        (is_sufficient, failure_reason, list_of_actions)
    """
    if not retrieved_passages:
        return False, 'No SOP evidence passages available to support quality actions.', []

    if product_id != 'P-PLATE01':
        return False, f"No verified action-generation procedure is configured for product '{product_id}'.", []

    raw_actions = _build_grounded_plate_actions(retrieved_passages)

    # Validate every citation
    valid_actions = []
    for action in raw_actions:
        if validate_citation(action, retrieved_passages):
            valid_actions.append(action)
        else:
            logger.warning('Discarded unsupported action: %s', action.get('action'))

    if not valid_actions:
        return False, 'No suggested actions could be verified against retrieved SOP evidence.', []

    # Ensure both plate SOPs contributed if product is P-PLATE01
    if product_id == 'P-PLATE01':
        docs_present = {a['document_id'] for a in valid_actions}
        if not ('Q-PLATE-01' in docs_present and 'Q-PLATE-02' in docs_present):
            return False, 'Complete quality workflow requires verified actions from both Q-PLATE-01 and Q-PLATE-02.', []

    return True, '', valid_actions


def format_source_references(passages: list[RetrievedSOPPassage]) -> list[dict[str, Any]]:
    """Format retrieved passages into source references for UI display and persistence."""
    refs = []
    for p in passages:
        refs.append({
            'source_id': p.source_id,
            'document_id': p.document_id,
            'document_name': p.document_name,
            'revision': p.revision,
            'page_number': p.page_number,
            'passage': p.text,
            'source_link': p.source_link,
        })
    return refs
