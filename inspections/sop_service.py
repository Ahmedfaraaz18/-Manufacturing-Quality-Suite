"""Dedicated Quality SOP mapping and isolated retrieval service for Round 3.

Enforces strict document isolation based on product_sop_mapping.csv:
- Only SOPs explicitly mapped to the product are retrieved.
- Unrelated products (e.g. P-GASKET02) and Round 1 machine manuals are strictly excluded.
- Validates the physical presence and readability of all required SOP PDFs.
- Preserves document ID, revision, and 1-based PDF page numbers.
"""

import csv
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from django.conf import settings
from django.urls import reverse

logger = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r'[a-z0-9]+')
_STOPWORDS = frozenset(
    'a an and are as at be by can could do does for from has have how i if in is it its me my of on or '
    'should that the their there this to was we what when where which who why will with you your'.split()
)


@dataclass(frozen=True)
class SOPMappingEntry:
    product_id: str
    product_name: str
    document_id: str
    revision: str
    pdf_filename: str
    required: bool


@dataclass
class RetrievedSOPPassage:
    source_id: str
    product_id: str
    document_id: str
    document_name: str
    revision: str
    page_number: int  # 1-based
    chunk_index: int
    text: str
    score: float = 0.0
    source_link: str = ''


def find_mapping_file() -> Optional[Path]:
    """Locate product_sop_mapping.csv in the project root or round3_pack."""
    candidates = [
        Path(settings.BASE_DIR) / 'product_sop_mapping.csv',
        Path(settings.BASE_DIR) / 'round3_pack' / 'product_sop_mapping.csv',
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def find_sops_dir() -> Optional[Path]:
    """Locate the SOPs directory containing the SOP PDFs."""
    candidates = [
        Path(settings.BASE_DIR) / 'SOPs',
        Path(settings.BASE_DIR) / 'round3_pack' / 'SOPs',
    ]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


def get_product_sop_mappings(product_id: str) -> tuple[bool, str, list[SOPMappingEntry]]:
    """Read product_sop_mapping.csv and return mappings for the specified product_id."""
    mapping_path = find_mapping_file()
    if not mapping_path:
        return False, 'Product SOP mapping file (product_sop_mapping.csv) is missing.', []

    mappings: list[SOPMappingEntry] = []
    try:
        with open(mapping_path, 'r', encoding='utf-8', errors='replace') as f:
            reader = csv.DictReader(f)
            required_columns = {'product_id', 'product_name', 'document_id', 'revision', 'pdf_filename', 'required'}
            if not reader.fieldnames or not required_columns.issubset(reader.fieldnames):
                return False, 'Product SOP mapping file is missing required columns.', []
            for row in reader:
                pid = (row.get('product_id') or '').strip()
                if pid == product_id:
                    required_str = (row.get('required') or 'true').strip().lower()
                    if required_str not in ('true', 'false', '1', '0', 'yes', 'no'):
                        return False, f"Invalid required flag for SOP mapping '{row.get('document_id', '').strip()}'.", []
                    mappings.append(
                        SOPMappingEntry(
                            product_id=pid,
                            product_name=(row.get('product_name') or '').strip(),
                            document_id=(row.get('document_id') or '').strip(),
                            revision=(row.get('revision') or '').strip(),
                            pdf_filename=(row.get('pdf_filename') or '').strip(),
                            required=required_str in ('true', '1', 'yes'),
                        )
                    )
    except Exception as exc:
        logger.error('Failed reading product SOP mapping: %s', exc)
        return False, f'Error reading SOP mapping file: {exc}', []

    if not mappings:
        return False, f"No SOP document mapping found for product '{product_id}'.", []

    if any(not entry.document_id or not entry.revision or not entry.pdf_filename for entry in mappings):
        return False, f"Product SOP mapping for '{product_id}' contains an incomplete document reference.", []
    if len({entry.document_id for entry in mappings}) != len(mappings):
        return False, f"Product SOP mapping for '{product_id}' contains duplicate document IDs.", []
    if product_id == 'P-PLATE01':
        expected = {'Q-PLATE-01': '1.0', 'Q-PLATE-02': '1.0'}
        by_document = {entry.document_id: entry for entry in mappings}
        for document_id, revision in expected.items():
            entry = by_document.get(document_id)
            if not entry or entry.revision != revision or not entry.required:
                return False, (
                    f"Required P-PLATE01 mapping for {document_id} revision {revision} "
                    "is missing or incorrect."
                ), []

    return True, '', mappings


def find_mapped_sop_file(document_id: str, revision: str) -> tuple[bool, str, Optional[Path]]:
    """Resolve a source link only to a document/revision explicitly present in the mapping CSV."""
    mapping_path = find_mapping_file()
    if not mapping_path:
        return False, 'Product SOP mapping file (product_sop_mapping.csv) is missing.', None
    try:
        with open(mapping_path, 'r', encoding='utf-8', errors='replace') as f:
            reader = csv.DictReader(f)
            matches = [
                SOPMappingEntry(
                    product_id=(row.get('product_id') or '').strip(),
                    product_name=(row.get('product_name') or '').strip(),
                    document_id=(row.get('document_id') or '').strip(),
                    revision=(row.get('revision') or '').strip(),
                    pdf_filename=(row.get('pdf_filename') or '').strip(),
                    required=(row.get('required') or 'true').strip().lower() in ('true', '1', 'yes'),
                )
                for row in reader
                if (row.get('document_id') or '').strip() == document_id
                and (row.get('revision') or '').strip() == revision
            ]
    except Exception as exc:
        logger.exception('Could not read the SOP mapping for source %s revision %s', document_id, revision)
        return False, f'Error reading SOP mapping file: {exc}', None
    if len(matches) != 1:
        return False, f"No unique mapped SOP was found for {document_id} revision {revision}.", None
    valid, reason, paths = validate_sop_files(matches)
    if not valid:
        return False, reason, None
    return True, '', paths.get((document_id, revision))


def validate_sop_files(mappings: list[SOPMappingEntry]) -> tuple[bool, str, dict[tuple[str, str], Path]]:
    """Validate that all required mapped SOP PDF files exist and can be opened with PyMuPDF."""
    sops_dir = find_sops_dir()
    if not sops_dir:
        return False, 'SOPs directory is missing from repository.', {}

    try:
        import fitz  # PyMuPDF
    except ImportError:
        return False, 'PDF reader is unavailable; mapped SOP documents cannot be verified.', {}

    file_paths: dict[tuple[str, str], Path] = {}
    for entry in mappings:
        if Path(entry.pdf_filename).name != entry.pdf_filename:
            if entry.required:
                return False, f"Required SOP mapping for {entry.document_id} has an invalid filename.", {}
            continue
        sop_file = (sops_dir / entry.pdf_filename).resolve()
        if sop_file.parent != sops_dir.resolve():
            if entry.required:
                return False, f"Required SOP mapping for {entry.document_id} points outside the SOP directory.", {}
            continue
        if not sop_file.is_file():
            if entry.required:
                return False, f"Required SOP file '{entry.pdf_filename}' for {entry.document_id} rev {entry.revision} is missing.", {}
            continue
        doc = None
        try:
            doc = fitz.open(str(sop_file))
            if len(doc) == 0:
                doc.close()
                doc = None
                if entry.required:
                    return False, f"Required SOP file '{entry.pdf_filename}' contains 0 pages.", {}
                continue
            text = '\n'.join(page.get_text() for page in doc)
            doc.close()
            doc = None
            identity = re.search(
                r'Document ID:\s*(?P<document_id>[A-Z0-9-]+)\s*\|\s*Product:\s*(?P<product_id>[A-Z0-9-]+)\s*\|\s*Revision:\s*(?P<revision>[0-9.]+)',
                text,
            )
            if (
                not identity
                or identity.group('document_id') != entry.document_id
                or identity.group('product_id') != entry.product_id
                or identity.group('revision') != entry.revision
            ):
                if entry.required:
                    return False, (
                        f"Required SOP '{entry.pdf_filename}' does not match mapping "
                        f"{entry.document_id} rev {entry.revision} for {entry.product_id}."
                    ), {}
                continue
        except Exception as exc:
            if doc is not None:
                doc.close()
            if entry.required:
                return False, f"Required SOP file '{entry.pdf_filename}' is unreadable: {exc}", {}
            continue
        file_paths[(entry.document_id, entry.revision)] = sop_file

    # Ensure all required mappings have valid paths
    for entry in mappings:
        if entry.required and (entry.document_id, entry.revision) not in file_paths:
            return False, f"Required SOP {entry.document_id} rev {entry.revision} could not be validated.", {}

    return True, '', file_paths


def normalize_sop_text(text: str) -> str:
    """Normalize extracted PDF text, removing invalid/bullet characters."""
    text = text.replace('\uf0b7', '-').replace('\u2022', '-').replace('\u00ad', '').replace('\ufffd', "'")
    lines = [re.sub(r'[ \t]+', ' ', line).strip() for line in text.splitlines()]
    return '\n'.join(line for line in lines if line)


def extract_sop_chunks(
    mappings: list[SOPMappingEntry], file_paths: dict[tuple[str, str], Path]
) -> list[RetrievedSOPPassage]:
    """Extract one source-verifiable passage per actual PDF page."""
    import fitz

    passages: list[RetrievedSOPPassage] = []
    doc_titles = {
        'Q-PLATE-01': 'Plate Quality Containment Procedure',
        'Q-PLATE-02': 'Plate Quality Review Procedure',
        'Q-GASKET-01': 'Gasket Quality Review Procedure',
    }

    for entry in mappings:
        path = file_paths.get((entry.document_id, entry.revision))
        if not path:
            continue
        doc = fitz.open(str(path))
        try:
            for page_idx, page in enumerate(doc):
                page_num = page_idx + 1  # 1-based page
                raw_text = page.get_text()
                clean_text = normalize_sop_text(raw_text)
                if not clean_text:
                    continue

                doc_name = doc_titles.get(entry.document_id, entry.product_name)

                # Filter out running header lines like 'Q-PLATE-01 | Rev 1.0  |  Page 1'
                content_lines = [
                    l for l in clean_text.splitlines()
                    if not re.match(r'^Q-[A-Z]+-\d+\s*\|\s*Rev', l)
                ]
                filtered_text = '\n'.join(content_lines)
                if filtered_text.strip():
                    passages.append(
                        RetrievedSOPPassage(
                            source_id='',
                            product_id=entry.product_id,
                            document_id=entry.document_id,
                            document_name=doc_name,
                            revision=entry.revision,
                            page_number=page_num,
                            chunk_index=1,
                            text=filtered_text,
                            score=0.0,
                            source_link=(
                                reverse(
                                    'quality-sop-source',
                                    kwargs={
                                        'document_id': entry.document_id,
                                        'revision': entry.revision,
                                        'page_number': page_num,
                                    },
                                )
                                + f'#page={page_num}'
                            ),
                        )
                    )
        finally:
            doc.close()

    return passages


def _content_tokens(text: str) -> set[str]:
    return {t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS}


def _lexical_overlap(query_tokens: set[str], text: str) -> float:
    if not query_tokens:
        return 0.0
    text_tokens = _content_tokens(text)
    if not text_tokens:
        return 0.0
    return len(query_tokens & text_tokens) / len(query_tokens)


def retrieve_sop_evidence(product_id: str, query: str = '') -> tuple[bool, str, list[RetrievedSOPPassage]]:
    """Retrieve grounded SOP evidence for a product.

    Returns:
        (is_sufficient, failure_reason, list_of_passages)
    """
    valid_map, reason, mappings = get_product_sop_mappings(product_id)
    if not valid_map:
        return False, reason, []

    valid_files, file_reason, file_paths = validate_sop_files(mappings)
    if not valid_files:
        return False, file_reason, []

    try:
        all_passages = extract_sop_chunks(mappings, file_paths)
    except Exception as exc:
        logger.exception('Could not extract mapped quality SOPs for product %s', product_id)
        return False, f'Mapped SOP text could not be extracted: {exc}', []
    if not all_passages:
        return False, 'No text passages could be extracted from the mapped SOP documents.', []

    # Scoring query
    default_query = 'quality incident containment hold supervisor review evidence checklist next steps defective plate'
    search_query = query.strip() or default_query
    query_tokens = _content_tokens(search_query)

    for passage in all_passages:
        lex = _lexical_overlap(query_tokens, passage.text)
        passage.score = lex

    # Keep only genuinely relevant pages, while requiring each required SOP to contribute.
    selected = [passage for passage in all_passages if passage.score > 0]
    for doc_entry in mappings:
        if doc_entry.required and not any(
            passage.document_id == doc_entry.document_id
            and passage.revision == doc_entry.revision
            and passage.score > 0
            for passage in selected
        ):
            return False, f"No relevant passages found for required SOP {doc_entry.document_id}.", []

    if not selected:
        return False, 'No relevant passages were found in the mapped SOP documents.', []

    # Assign source IDs
    for idx, passage in enumerate(selected, start=1):
        passage.source_id = f'S{idx}'

    return True, '', selected
