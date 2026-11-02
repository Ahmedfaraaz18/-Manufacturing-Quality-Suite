"""Splits one page of text into overlapping chunks. Chunks never cross a page boundary,
so the original one-based page number is preserved by construction."""

import re
from dataclasses import dataclass

_WS = re.compile(r'[ \t]+')


@dataclass(frozen=True)
class PageChunk:
    page_number: int
    chunk_index: int
    text: str


def normalize_text(text: str) -> str:
    text = text.replace('\u00ad', '').replace('\ufffd', "'").replace('\x00', '')
    lines = (_WS.sub(' ', line).strip() for line in text.splitlines())
    return '\n'.join(line for line in lines if line)


def chunk_page(page_number: int, text: str, size: int, overlap: int) -> list[PageChunk]:
    """Pack whole lines into chunks of about `size` characters, repeating the trailing lines
    (up to `overlap` characters) at the start of the next chunk. Empty pages yield no chunks."""
    if page_number < 1:
        raise ValueError('page_number is one-based')
    if size <= 0 or not 0 <= overlap < size:
        raise ValueError('require size > 0 and 0 <= overlap < size')

    lines = []
    for line in normalize_text(text).splitlines():
        while len(line) > size:  # very long line: split on a space near the limit
            cut = line.rfind(' ', 0, size)
            cut = cut if cut > 0 else size
            lines.append(line[:cut])
            line = line[cut:].strip()
        lines.append(line)
    if not lines:
        return []

    chunks, current, current_len = [], [], 0
    for line in lines:
        added = len(line) + (1 if current else 0)
        if current and current_len + added > size:
            chunks.append('\n'.join(current))
            current, current_len = [], 0
            tail, tail_len = [], 0
            for prev in reversed(chunks[-1].splitlines()):
                if tail_len + len(prev) + 1 > overlap:
                    break
                tail.insert(0, prev)
                tail_len += len(prev) + 1
            current = tail
            current_len = len('\n'.join(current))
            added = len(line) + (1 if current else 0)
        current.append(line)
        current_len += added
    if current and (not chunks or '\n'.join(current) != chunks[-1]):
        chunks.append('\n'.join(current))

    return [PageChunk(page_number, i, text) for i, text in enumerate(chunks)]
