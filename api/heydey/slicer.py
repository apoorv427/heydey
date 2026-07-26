"""Slicer v2 — CLEAN-ROOM implementation from the audited pattern description
(07-substrate-audit.md: "atomic code/table blocks, 15% overlap, recursive
oversized-prose fallback" + enricher's "[Doc|Section] provenance, SHA-256 ids").
No intern source files were read or copied (lock-14 gate language: patterns,
not files).

Behavior:
- Markdown-aware streaming pass. Heading lines maintain a section lineage
  ("H1 > H2 > H3") that becomes the [Doc | Section] provenance prefix and the
  breadcrumb section.
- Fenced code blocks (``` ... ```) and table runs (consecutive |-rows) are
  ATOMIC — emitted whole, never split, even when larger than the size cap.
- Prose packs into ~TARGET_CHARS chunks (hard cap MAX_CHARS). Consecutive prose
  chunks within a section re-seed with the last ~15% of the previous chunk.
- A single paragraph over the cap falls back recursively: sentence split,
  then hard split.
- chunk_id = sha256(doc_id | index | text) — deterministic, so re-ingesting an
  unchanged doc rewrites the same points (idempotent with delete_document first).
"""

import hashlib
import re

TARGET_CHARS = 1200
MAX_CHARS = 1600
OVERLAP_FRACTION = 0.15

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_FENCE = re.compile(r"^(```|~~~)")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _blocks(text: str):
    """Yield (kind, block_text, lineage) — kind in {prose, code, table}."""
    lineage: list[tuple[int, str]] = []
    prose: list[str] = []
    code: list[str] = []
    table: list[str] = []
    in_fence = False
    fence_mark = ""

    def current_lineage() -> str:
        return " > ".join(title for _level, title in lineage)

    def flush_prose():
        nonlocal prose
        joined = "\n".join(prose).strip()
        prose = []
        if joined:
            for paragraph in re.split(r"\n\s*\n", joined):
                if paragraph.strip():
                    yield ("prose", paragraph.strip(), current_lineage())

    def flush_table():
        nonlocal table
        joined = "\n".join(table).strip()
        table = []
        if joined:
            yield ("table", joined, current_lineage())

    for line in text.splitlines():
        if in_fence:
            code.append(line)
            if _FENCE.match(line.strip()) and line.strip().startswith(fence_mark):
                in_fence = False
                yield ("code", "\n".join(code).strip(), current_lineage())
                code = []
            continue

        fence = _FENCE.match(line.strip())
        if fence:
            yield from flush_prose()
            yield from flush_table()
            in_fence = True
            fence_mark = fence.group(1)
            code = [line]
            continue

        if _TABLE_ROW.match(line):
            yield from flush_prose()
            table.append(line)
            continue
        elif table:
            yield from flush_table()

        heading = _HEADING.match(line)
        if heading:
            yield from flush_prose()
            yield from flush_table()
            level = len(heading.group(1))
            lineage[:] = [(lv, t) for lv, t in lineage if lv < level]
            lineage.append((level, heading.group(2).strip()))
            continue

        prose.append(line)

    if in_fence and code:  # unterminated fence at EOF — still atomic
        yield ("code", "\n".join(code).strip(), current_lineage())
    yield from flush_prose()
    yield from flush_table()


def _split_oversized(paragraph: str) -> list[str]:
    """Recursive fallback for a single prose unit over the cap."""
    if len(paragraph) <= MAX_CHARS:
        return [paragraph]
    sentences = _SENTENCE_SPLIT.split(paragraph)
    if len(sentences) > 1:
        pieces, bucket = [], ""
        for sentence in sentences:
            if bucket and len(bucket) + len(sentence) + 1 > TARGET_CHARS:
                pieces.append(bucket)
                bucket = sentence
            else:
                bucket = f"{bucket} {sentence}".strip()
        if bucket:
            pieces.append(bucket)
        out = []
        for piece in pieces:
            out.extend(_split_oversized(piece))
        return out
    return [paragraph[i:i + MAX_CHARS] for i in range(0, len(paragraph), MAX_CHARS)]


def _overlap_tail(text: str) -> str:
    """Last ~15% of a chunk, snapped to a word boundary."""
    tail = text[-int(len(text) * OVERLAP_FRACTION):]
    cut = tail.find(" ")
    return tail[cut + 1:] if cut != -1 else tail


def slice_document(text: str, *, doc_id: str, title: str, source_file: str,
                   created_at: str = "") -> list[dict]:
    """Slice one document into store_chunks-ready dicts with provenance."""
    packed: list[tuple[str, str]] = []  # (body, section)
    bucket, bucket_section = "", None

    def flush_bucket():
        nonlocal bucket, bucket_section
        if bucket.strip():
            packed.append((bucket.strip(), bucket_section or ""))
        bucket, bucket_section = "", None

    for kind, block, section in _blocks(text):
        if kind in ("code", "table"):
            flush_bucket()
            packed.append((block, section))  # atomic — size cap does not apply
            continue
        for piece in _split_oversized(block):
            if bucket and (bucket_section != section
                           or len(bucket) + len(piece) + 2 > TARGET_CHARS):
                previous = bucket
                same_section = bucket_section == section
                flush_bucket()
                if same_section:  # 15% overlap re-seed within a section...
                    tail = _overlap_tail(previous)
                    if len(tail) + len(piece) + 2 <= MAX_CHARS:  # ...unless it would breach the cap
                        bucket = tail
            bucket = f"{bucket}\n\n{piece}".strip() if bucket else piece
            bucket_section = section
    flush_bucket()

    chunks = []
    for index, (body, section) in enumerate(packed):
        provenance = f"[Doc: {title} | Section: {section or '—'}]"
        chunk_text = f"{provenance}\n{body}"
        chunk_id = hashlib.sha256(f"{doc_id}|{index}|{chunk_text}".encode()).hexdigest()
        chunks.append({
            "chunk_id": chunk_id,
            "text": chunk_text,
            "source_file": source_file,
            "chunk_index": index,
            "section": section,
            "title": title,
            "created_at": created_at,
            "char_count": len(chunk_text),
        })
    return chunks
