"""S2: slicer v2 — atomic blocks, 15% overlap, provenance, deterministic ids."""

from heydey.slicer import MAX_CHARS, slice_document


def _slice(text):
    return slice_document(text, doc_id="d", title="Doc", source_file="/tmp/doc.md")


def test_code_block_is_atomic():
    code_body = "\n".join(f"line_{i} = {i}" for i in range(200))  # far over the cap
    doc = f"# Setup\n\nIntro prose.\n\n```python\n{code_body}\n```\n\nOutro."
    chunks = _slice(doc)
    code_chunks = [c for c in chunks if "```python" in c["text"]]
    assert len(code_chunks) == 1  # never split
    assert "line_0 = 0" in code_chunks[0]["text"]
    assert "line_199 = 199" in code_chunks[0]["text"]


def test_table_is_atomic():
    rows = "\n".join(f"| row{i} | value that pads this table row {i} |" for i in range(80))
    doc = f"# Pricing\n\n| col_a | col_b |\n|---|---|\n{rows}\n\nAfter table."
    chunks = _slice(doc)
    table_chunks = [c for c in chunks if "| row0 |" in c["text"]]
    assert len(table_chunks) == 1
    assert "| row79 |" in table_chunks[0]["text"]


def test_prose_packs_with_overlap():
    paragraphs = "\n\n".join(
        f"Paragraph {i} talks about topic {i} in a moderately long sentence "
        "that fills space for the packing logic to work with." for i in range(40)
    )
    chunks = _slice(f"# Section\n\n{paragraphs}")
    assert len(chunks) > 1
    first_body = chunks[0]["text"].split("\n", 1)[1]
    second_body = chunks[1]["text"].split("\n", 1)[1]
    tail = first_body[-100:]
    # the 15% re-seed: some tail of chunk 0 opens chunk 1
    assert any(part and part in second_body for part in [tail[-60:], tail[-40:]])


def test_provenance_and_lineage():
    doc = "# Alpha\n\ntop prose\n\n## Beta\n\nnested prose"
    chunks = _slice(doc)
    assert all(c["text"].startswith("[Doc: Doc | Section: ") for c in chunks)
    assert any(c["section"] == "Alpha" for c in chunks)
    assert any(c["section"] == "Alpha > Beta" for c in chunks)


def test_oversized_wall_of_text_respects_cap():
    wall = "x" * 6000  # no sentence boundaries at all
    chunks = _slice(wall)
    bodies = [c["text"].split("\n", 1)[1] for c in chunks]
    assert all(len(c["text"]) <= MAX_CHARS + 100 for c in chunks)  # +provenance line
    # overlap duplicates content BY DESIGN — the invariant is complete coverage
    assert sum(len(b.replace("\n\n", "")) for b in bodies) >= len(wall)
    assert bodies[0].startswith("x") and bodies[-1].endswith("x")


def test_deterministic_ids_and_empty_doc():
    doc = "# A\n\nsome prose"
    ids_first = [c["chunk_id"] for c in _slice(doc)]
    ids_second = [c["chunk_id"] for c in _slice(doc)]
    assert ids_first == ids_second
    assert _slice("") == []
