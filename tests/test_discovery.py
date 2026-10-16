from __future__ import annotations

import hashlib
import os
from pathlib import Path

from pypdf import PdfReader, PdfWriter

import reading_relay.discovery as discovery_module
from reading_relay.discovery import candidate_from_url, discover


def make_pdf(path: Path, title: str = "A Useful Paper") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.add_metadata({"/Title": title})
    with path.open("wb") as stream:
        writer.write(stream)


def test_canonicalizes_arxiv_doi_acl_and_unknown_urls() -> None:
    first = candidate_from_url("https://arxiv.org/abs/2401.01234v2")
    second = candidate_from_url("https://arxiv.org/pdf/2401.01234v3.pdf")
    assert first is not None and second is not None
    assert first.id == second.id == "arxiv:2401.01234"
    assert first.url == "https://arxiv.org/abs/2401.01234"
    assert first.pdf_url == "https://arxiv.org/pdf/2401.01234.pdf"
    doi = candidate_from_url("https://doi.org/10.1234/ABC.Def")
    raw_doi = candidate_from_url("10.1234/ABC.Def")
    assert doi is not None and raw_doi is not None
    assert doi.id == raw_doi.id == "doi:10.1234/abc.def"
    spoof = candidate_from_url("https://example.org/10.1234/ABC.Def")
    assert spoof is not None and spoof.id.startswith("url:")
    acl_html = candidate_from_url("https://aclanthology.org/2024.acl-long.1/")
    acl_pdf = candidate_from_url("https://aclanthology.org/2024.acl-long.1.pdf")
    assert acl_html is not None and acl_pdf is not None
    assert acl_html.id == acl_pdf.id
    assert acl_html.pdf_url == "https://aclanthology.org/2024.acl-long.1.pdf"
    old_arxiv = candidate_from_url("https://arxiv.org/abs/cs/9901001v2")
    assert old_arxiv is not None and old_arxiv.id == "arxiv:cs/9901001"
    unknown = candidate_from_url("https://example.org/paper")
    assert (
        unknown is not None
        and unknown.id == "url:" + hashlib.sha256(unknown.url.encode()).hexdigest()[:24]
    )
    assert candidate_from_url("javascript:alert(1)") is None


def test_generic_pdf_url_preserves_escaped_path_and_signed_query() -> None:
    from http.client import HTTPConnection
    from urllib.parse import urlsplit

    source = "https://papers.example/paper%20title.pdf?signature=a%2Fb%26c"
    candidate = candidate_from_url(source)

    assert candidate is not None
    assert candidate.url == source
    assert candidate.pdf_url == source
    parsed = urlsplit(candidate.pdf_url)
    request_target = f"{parsed.path}?{parsed.query}"
    HTTPConnection("papers.example")._validate_path(request_target)


def test_markdown_bib_and_projects_merge_identity_and_provenance(tmp_path: Path) -> None:
    notebook = tmp_path / "research"
    first = notebook / "project-a" / "lab-notebook"
    second = notebook / "project-b" / "lab-notebook"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    (first / "EXP-001.md").write_text(
        "# Reading\n\nCompare [Transformer Paper](https://arxiv.org/abs/2401.01234v1).\n",
        encoding="utf-8",
    )
    (second / "references.bib").write_text(
        "@article{paper,\n title={Transformer Paper},\n eprint={2401.01234v2},\n archivePrefix={arXiv}\n}\n",
        encoding="utf-8",
    )
    result = discover([notebook], [], activity_days=30)
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.id == "arxiv:2401.01234"
    assert set(candidate.sources) == {str(first / "EXP-001.md"), str(second / "references.bib")}
    assert candidate.title == "Transformer Paper"
    assert result.activity and "Compare" in result.activity[0].text


def test_downloads_are_flat_read_only_invalid_pdfs_diagnosed(tmp_path: Path) -> None:
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    make_pdf(downloads / "paper.pdf")
    (downloads / "broken.pdf").write_bytes(b"not a pdf")
    nested = downloads / "archive"
    make_pdf(nested / "nested.pdf")
    result = discover([], [downloads])
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert (
        candidate.id == "pdf:" + hashlib.sha256((downloads / "paper.pdf").read_bytes()).hexdigest()
    )
    assert candidate.local_path == str(downloads / "paper.pdf")
    assert candidate.title == "A Useful Paper"
    assert (nested / "nested.pdf").exists()
    assert any(
        "Invalid PDF" in message and "broken.pdf" in message for message in result.diagnostics
    )


def test_bounds_hidden_and_tool_trees_and_reports_truncation(tmp_path: Path) -> None:
    notebooks = tmp_path / "research"
    lab = notebooks / "project" / "lab-notebook"
    lab.mkdir(parents=True)
    (lab / "a.md").write_text("https://arxiv.org/abs/2401.00001", encoding="utf-8")
    (lab / "b.md").write_text("https://arxiv.org/abs/2401.00002", encoding="utf-8")
    os.utime(lab / "a.md", (1, 1))
    os.utime(lab / "b.md", (2, 2))
    hidden = lab / ".hidden"
    hidden.mkdir()
    (hidden / "secret.md").write_text("https://arxiv.org/abs/2401.00003", encoding="utf-8")
    build = lab / "build"
    build.mkdir()
    (build / "generated.md").write_text("https://arxiv.org/abs/2401.00004", encoding="utf-8")
    result = discover([notebooks], [], max_files=1)
    assert len(result.candidates) == 1
    assert result.candidates[0].id == "arxiv:2401.00002"
    assert any("truncated" in message for message in result.diagnostics)


def test_linked_local_pdf_has_content_derived_local_identity(tmp_path: Path) -> None:
    root = tmp_path / "project" / "lab-notebook"
    root.mkdir(parents=True)
    pdf = root / "citation.pdf"
    make_pdf(pdf)
    note = root / "EXP-001.md"
    note.write_text("See [this article](citation.pdf).", encoding="utf-8")
    result = discover([root], [])
    assert len(result.candidates) == 1
    assert result.candidates[0].id.startswith("pdf:")
    assert result.candidates[0].local_path == str(pdf)


def test_markdown_title_and_neighborhood_are_preserved(tmp_path: Path) -> None:
    root = tmp_path / "project" / "lab-notebook"
    root.mkdir(parents=True)
    note = root / "EXP-002.md"
    note.write_text(
        "We need this for the result section. [Clear Paper Title]"
        "(https://arxiv.org/abs/2401.00009). This improves our approach.",
        encoding="utf-8",
    )
    result = discover([root], [])
    candidate = result.candidates[0]
    assert candidate.title == "Clear Paper Title"
    assert "result section" in candidate.context
    assert "improves our approach" in candidate.context


def test_unlinked_reference_pdf_is_ingested(tmp_path: Path) -> None:
    root = tmp_path / "project" / "lab-notebook"
    pdf = root / "references" / "cited-work.pdf"
    make_pdf(pdf, "Unlinked Reference")
    result = discover([root], [])
    assert len(result.candidates) == 1
    assert result.candidates[0].title == "Unlinked Reference"


def test_notebook_and_download_budgets_are_independent(tmp_path: Path) -> None:
    root = tmp_path / "project" / "lab-notebook"
    root.mkdir(parents=True)
    (root / "EXP.md").write_text("Recent notebook activity.", encoding="utf-8")
    downloads = tmp_path / "downloads"
    make_pdf(downloads / "paper.pdf")
    result = discover([root], [downloads], max_files=1)
    assert result.activity
    assert any(candidate.local_path.endswith("paper.pdf") for candidate in result.candidates)


def test_recent_notebook_files_are_prioritized_over_old_project(tmp_path: Path) -> None:
    old = tmp_path / "research" / "a-project" / "lab-notebook" / "old.md"
    recent = tmp_path / "research" / "z-project" / "lab-notebook" / "recent.md"
    old.parent.mkdir(parents=True)
    recent.parent.mkdir(parents=True)
    old.write_text("Old activity", encoding="utf-8")
    recent.write_text("Recent activity", encoding="utf-8")
    os.utime(old, (1, 1))
    result = discover([tmp_path / "research"], [], max_files=1)
    assert result.activity[0].path == str(recent)
    assert result.activity[0].text == "Recent activity"


def test_activity_digest_covers_full_document_and_excerpt_keeps_tail(tmp_path: Path) -> None:
    root = tmp_path / "project" / "lab-notebook"
    root.mkdir(parents=True)
    note = root / "EXP.md"
    first = "START " + "a" * 700
    tail = "z" * 700 + " END"
    note.write_text(first + " middle-one " + tail, encoding="utf-8")
    first_result = discover([root], [])
    note.write_text(first + " middle-two " + tail, encoding="utf-8")
    second_result = discover([root], [])
    before, after = first_result.activity[0], second_result.activity[0]
    assert before.text.startswith("START")
    assert before.text.endswith("END")
    assert before.text == after.text
    assert before.digest != after.digest


def test_candidate_cap_prefers_recently_mentioned_papers(tmp_path: Path) -> None:
    root = tmp_path / "project" / "lab-notebook"
    root.mkdir(parents=True)
    old = root / "old.md"
    recent = root / "recent.md"
    old.write_text("https://arxiv.org/abs/2401.00001", encoding="utf-8")
    recent.write_text("https://arxiv.org/abs/2401.00002", encoding="utf-8")
    os.utime(old, (1, 1))
    result = discover([root], [], max_candidates=1)
    assert [candidate.id for candidate in result.candidates] == ["arxiv:2401.00002"]


def test_reference_pdf_larger_than_text_limit_is_still_ingested(tmp_path: Path) -> None:
    notebook = tmp_path / "lab-notebook"
    reference = notebook / "references" / "large.pdf"
    make_pdf(reference, "A large research reference")
    content = reference.read_bytes()
    reference.write_bytes(content.replace(b"startxref", b"%" + b"x" * 1_100_000 + b"\nstartxref"))
    assert len(PdfReader(reference).pages) == 1
    result = discover([notebook], [])
    assert [(candidate.title, candidate.local_path) for candidate in result.candidates] == [
        ("A large research reference", str(reference))
    ]
    assert not result.diagnostics


def test_single_line_bibtex_preserves_nested_title_and_doi(tmp_path: Path) -> None:
    notebook = tmp_path / "lab-notebook"
    notebook.mkdir()
    (notebook / "references.bib").write_text(
        "@article{foundation, title={A {Nested} Foundation}, doi={10.1234/example}}\n"
    )
    result = discover([notebook], [])
    assert [(candidate.id, candidate.title) for candidate in result.candidates] == [
        ("doi:10.1234/example", "A Nested Foundation")
    ]


def test_encrypted_download_is_diagnosed_without_blocking_other_papers(tmp_path: Path) -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.encrypt("", algorithm="RC4-128")
    with (tmp_path / "encrypted.pdf").open("wb") as stream:
        writer.write(stream)
    make_pdf(tmp_path / "readable.pdf", "Readable research")
    result = discover([], [tmp_path])
    assert [candidate.title for candidate in result.candidates] == ["Readable research"]
    assert any("Encrypted PDF" in diagnostic for diagnostic in result.diagnostics)


def test_recent_experiments_do_not_bury_foundational_bibliography(tmp_path: Path) -> None:
    notebook = tmp_path / "lab-notebook"
    notebook.mkdir()
    bibliography = notebook / "BIBLIOGRAPHY.md"
    bibliography.write_text("[A foundation](https://arxiv.org/abs/1706.03762)")
    os.utime(bibliography, (1, 1))
    for index in range(10):
        note = notebook / f"EXP-{index:03}.md"
        note.write_text(f"https://arxiv.org/abs/2401.{index:05}")
        os.utime(note, (100 + index, 100 + index))
    result = discover([notebook], [], max_files=6, max_candidates=20)
    assert {candidate.id for candidate in result.candidates} == {
        "arxiv:1706.03762",
        *(f"arxiv:2401.{index:05}" for index in range(5, 10)),
    }


def test_directory_traversal_errors_are_diagnostic(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "lab-notebook"
    blocked = root / "blocked"
    blocked.mkdir(parents=True)
    healthy = tmp_path / "healthy" / "lab-notebook"
    healthy.mkdir(parents=True)
    note = healthy / "papers.md"
    note.write_text("https://arxiv.org/abs/2401.01234", encoding="utf-8")
    original_scandir = os.scandir

    def failing_scandir(path):
        if Path(path).resolve() == blocked.resolve():
            raise PermissionError(13, "Permission denied", str(path))
        return original_scandir(path)

    monkeypatch.setattr(discovery_module.os, "scandir", failing_scandir)
    result = discover([root, healthy], [])

    assert any(
        "Cannot traverse" in message and str(blocked.resolve()) in message
        for message in result.diagnostics
    )
    assert [candidate.id for candidate in result.candidates] == ["arxiv:2401.01234"]


def test_scan_budget_counts_irrelevant_directory_entries(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "lab-notebook"
    root.mkdir()
    bibliography = root / "papers.bib"
    bibliography.write_text("@article{paper, url={https://arxiv.org/abs/2401.01234}}")
    names = [*(f"irrelevant-{index}.txt" for index in range(8)), bibliography.name]

    def ordered_walk(base, *, followlinks, onerror):
        yield str(base), [], names

    monkeypatch.setattr(discovery_module.os, "walk", ordered_walk)
    result = discover([root], [], max_files=1)

    assert not result.candidates
    assert any("scan capped at 8 entries" in message for message in result.diagnostics)

    names[:] = names[:7] + [bibliography.name]
    boundary_result = discover([root], [], max_files=1)

    assert [candidate.id for candidate in boundary_result.candidates] == ["arxiv:2401.01234"]
    assert not any("scan capped" in message for message in boundary_result.diagnostics)

    names[:] = [bibliography.name, *(f"irrelevant-{index}.txt" for index in range(8))]
    overflow_result = discover([root], [], max_files=1)

    assert [candidate.id for candidate in overflow_result.candidates] == ["arxiv:2401.01234"]
    assert any("scan capped at 8 entries" in message for message in overflow_result.diagnostics)
