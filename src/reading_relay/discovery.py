"""Bounded, offline discovery of papers and recent research activity."""

from __future__ import annotations

import hashlib
import os
import re
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

from pypdf import PdfReader
from pypdf.errors import PdfReadError, PdfStreamError

from .models import Activity, Candidate, ScanResult

MAX_TEXT_BYTES = 1_000_000
MAX_PDF_BYTES = 50 * 1024 * 1024
MAX_PDF_PAGES = 3
MAX_CONTEXT = 1200
IGNORED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    "build",
    "dist",
    "target",
    "output",
    "outputs",
    "_build",
    "site-packages",
}
ARXIV_ID = r"(?:\d{4}\.\d{4,5}|[a-z][a-z0-9.-]*/\d{7})(?:v\d+)?"
ARXIV_RE = re.compile(ARXIV_ID, re.IGNORECASE)
DOI_RE = re.compile(r"(10\.\d{4,9}/[^\s<>\"{}`]+)", re.IGNORECASE)
LINK_RE = re.compile(r"!?\[([^\]]*)\]\((https?://[^\s)]+)(?:\s+[^)]*)?\)", re.IGNORECASE)
BARE_URL_RE = re.compile(r"https?://[^\s<>\"')\]`]+", re.IGNORECASE)
BIB_ENTRY_RE = re.compile(r"@\w+\s*\{\s*([^,\s]+)\s*,", re.IGNORECASE)
BIB_FIELD_RE = re.compile(r"([A-Za-z][A-Za-z0-9_-]*)\s*=")


def _clean_url(url: str) -> str:
    return url.strip()


def _identity(url: str) -> tuple[str, str, str]:
    url = _clean_url(url)
    if not url.startswith(("http://", "https://")):
        raw_doi = DOI_RE.fullmatch(url)
        if raw_doi:
            doi = raw_doi.group(1).rstrip(".,;:)]}").lower()
            return f"doi:{doi}", f"https://doi.org/{doi}", ""
        return "", "", ""
    parsed = urlparse(url)
    host, path = (parsed.hostname or "").lower(), unquote(parsed.path)
    if host in {"arxiv.org", "www.arxiv.org"}:
        match = re.fullmatch(r"/(?:abs|pdf)/(.+?)(?:\.pdf)?", path, re.IGNORECASE)
        if match and ARXIV_RE.fullmatch(match.group(1)):
            paper_id = re.sub(r"v\d+$", "", match.group(1), flags=re.IGNORECASE)
            return (
                f"arxiv:{paper_id.lower()}",
                f"https://arxiv.org/abs/{paper_id}",
                f"https://arxiv.org/pdf/{paper_id}.pdf",
            )
    if host in {"doi.org", "dx.doi.org"}:
        doi_match = DOI_RE.fullmatch(path.lstrip("/"))
        if doi_match:
            doi = doi_match.group(1).rstrip(".,;:)]}").lower()
            return f"doi:{doi}", f"https://doi.org/{doi}", ""
    if host in {"aclanthology.org", "www.aclanthology.org"}:
        article = path.strip("/")
        if article.lower().endswith(".pdf"):
            article = article[:-4]
        if article:
            canonical = f"https://aclanthology.org/{article}/"
            return (
                f"url:{hashlib.sha256(canonical.encode()).hexdigest()[:24]}",
                canonical,
                f"https://aclanthology.org/{article}.pdf",
            )
    if not parsed.netloc:
        return "", "", ""
    return (
        f"url:{hashlib.sha256(url.encode()).hexdigest()[:24]}",
        url,
        url if path.lower().endswith(".pdf") else "",
    )


def candidate_from_url(url: str, title: str = "", source: str = "") -> Candidate | None:
    """Build a stable candidate from an explicit HTTP(S) paper URL."""
    identity, canonical, pdf_url = _identity(url)
    if not identity:
        return None
    return Candidate(
        identity,
        title.strip() or canonical,
        url=canonical,
        pdf_url=pdf_url,
        sources=[source] if source else [],
    )


def _is_paper_url(url: str) -> bool:
    parsed = urlparse(_clean_url(url).rstrip(".,;"))
    host, path = (parsed.hostname or "").lower(), unquote(parsed.path)
    return bool(
        (
            host in {"arxiv.org", "www.arxiv.org"}
            and re.fullmatch(rf"/(?:abs|pdf)/{ARXIV_ID}(?:\.pdf)?", path, re.IGNORECASE)
        )
        or host in {"doi.org", "dx.doi.org"}
        and DOI_RE.fullmatch(path.lstrip("/"))
        or host in {"aclanthology.org", "www.aclanthology.org"}
        and bool(path.strip("/"))
        or parsed.scheme in {"http", "https"}
        and path.lower().endswith(".pdf")
    )


def _retain_files(items: list[tuple[float, Path]], limit: int) -> list[tuple[float, Path]]:
    """Reserve a third of the file budget for bibliographies, glossaries, and references."""
    ordered = sorted(items, key=lambda pair: (-pair[0], str(pair[1])))
    references = [
        item
        for item in ordered
        if item[1].suffix.lower() in {".bib", ".pdf"}
        or item[1].name.upper() in {"BIBLIOGRAPHY.MD", "GLOSSARY.MD"}
    ]
    reserved = references[: min(limit, max(1, limit // 3))]
    selected = {path for _, path in reserved}
    remaining = [item for item in ordered if item[1] not in selected][
        : max(0, limit - len(reserved))
    ]
    return sorted(reserved + remaining, key=lambda pair: (-pair[0], str(pair[1])))


def _files(
    root: Path,
    remaining: int,
    diagnostics: list[str],
    *,
    notebook: bool = False,
    downloads: bool = False,
):
    """Yield recent eligible files without following symlinks or escaping root."""
    try:
        base = root.expanduser().resolve(strict=True)
    except OSError as exc:
        diagnostics.append(f"Cannot read root {root}: {exc}")
        return
    if not base.is_dir():
        diagnostics.append(f"Not a directory: {root}")
        return
    direct_notebook = base.name.lower() in {"lab-notebook", "notebook"}
    dirs_seen, dirs_limit = 0, max(4, max(1, remaining) * 8)
    files_seen, files_limit = 0, max(1, remaining) * 8
    eligible: list[tuple[float, Path]] = []
    truncated = False

    def report_walk_error(exc: OSError) -> None:
        failed_path = exc.filename or root
        diagnostics.append(f"Cannot traverse {failed_path}: {exc}")

    for directory, dirs, names in os.walk(base, followlinks=False, onerror=report_walk_error):
        current = Path(directory)
        depth = len(current.relative_to(base).parts)
        dirs_seen += 1
        if dirs_seen > dirs_limit:
            diagnostics.append(
                f"Directory traversal truncated at {dirs_limit} directories under {root}"
            )
            break
        dirs[:] = sorted(
            name
            for name in dirs
            if not name.startswith(".")
            and name.lower() not in IGNORED_DIRS
            and not (current / name).is_symlink()
            and (
                not notebook
                or direct_notebook
                or depth == 0
                or depth == 1
                and name.lower() == "lab-notebook"
                or any(part.lower() == "lab-notebook" for part in current.relative_to(base).parts)
            )
        )
        for name in names:
            files_seen += 1
            if files_seen > files_limit:
                diagnostics.append(
                    f"File traversal scan capped at {files_limit} entries under {root}"
                )
                break
            if name.startswith("."):
                continue
            path = current / name
            if path.is_symlink():
                continue
            if downloads:
                if path.suffix.lower() != ".pdf" or current != base:
                    continue
            elif notebook and not _notebook_file(path, base):
                continue
            try:
                path.resolve(strict=True).relative_to(base)
                if path.is_file():
                    eligible.append((path.stat().st_mtime, path))
                    if len(eligible) > remaining:
                        truncated = True
                        eligible = _retain_files(eligible, remaining)
            except (OSError, ValueError):
                continue
        if files_seen > files_limit:
            break
    eligible.sort(key=lambda pair: (-pair[0], str(pair[1])))
    if truncated:
        diagnostics.append(f"File traversal truncated at {remaining} eligible files under {root}")
    for _, path in eligible:
        yield path


def _text(path: Path, diagnostics: list[str]) -> str:
    try:
        if path.stat().st_size > MAX_TEXT_BYTES:
            diagnostics.append(f"Skipped oversized text file: {path}")
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        diagnostics.append(f"Cannot read {path}: {exc}")
        return ""


def _add_candidate(
    found: dict[str, Candidate],
    candidate: Candidate,
    source: str,
    context: str = "",
    modified: float = 0.0,
) -> None:
    existing = found.get(candidate.id)
    candidate.sources = [source] if source else []
    candidate.context = context[:MAX_CONTEXT]
    candidate.modified = modified
    if existing is None:
        found[candidate.id] = candidate
        return
    if source and source not in existing.sources:
        existing.sources.append(source)
    if context and context not in existing.context:
        existing.context = (existing.context + "\n" + context)[:MAX_CONTEXT]
    existing.modified = max(existing.modified, modified)
    if (not existing.title or existing.title == existing.url) and candidate.title:
        existing.title = candidate.title
    if not existing.local_path and candidate.local_path:
        existing.local_path = candidate.local_path
    if not existing.pdf_url and candidate.pdf_url:
        existing.pdf_url = candidate.pdf_url


def _bib_value(text: str, start: int) -> tuple[str, int] | None:
    """Read balanced values using the notebook reference archiver's parsing convention."""
    while start < len(text) and text[start].isspace():
        start += 1
    if start == len(text):
        return None
    opener = text[start]
    if opener not in {'"', "{"}:
        end = start
        while end < len(text) and text[end] not in ",\n}":
            end += 1
        return text[start:end].strip(), end
    depth = 1
    position = start + 1
    while position < len(text):
        char = text[position]
        if char == "\\":
            position += 2
            continue
        if opener == '"' and char == '"':
            return text[start + 1 : position], position + 1
        if opener == "{":
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return text[start + 1 : position], position + 1
        position += 1
    return None


def _bib_entries(text: str):
    starts = list(BIB_ENTRY_RE.finditer(text))
    for index, entry in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(text)
        body = text[entry.end() : end]
        fields: dict[str, str] = {}
        position = 0
        while field := BIB_FIELD_RE.search(body, position):
            value_start = field.end()
            parsed = _bib_value(body, value_start)
            if parsed is None:
                position = value_start + 1
                continue
            value, position = parsed
            fields[field.group(1).lower()] = (
                re.sub(r"\s+", " ", value).replace("{", "").replace("}", "").strip()
            )
        yield entry.group(1), fields


def _bib_candidate(fields: dict[str, str], source: str) -> Candidate | None:
    url, doi = fields.get("url", "").strip(), fields.get("doi", "").strip()
    if doi:
        url = f"https://doi.org/{doi}"
    elif fields.get("archiveprefix", "").lower() == "arxiv" and fields.get("eprint"):
        url = f"https://arxiv.org/abs/{fields['eprint']}"
    if not url:
        return None
    return candidate_from_url(url, fields.get("title", ""), source)


def _add_local_pdf(
    value: str,
    source_path: Path,
    root: Path,
    source: str,
    candidates: dict[str, Candidate],
    diagnostics: list[str],
) -> None:
    target = Path(os.path.abspath(source_path.parent / value))
    try:
        relative = target.relative_to(root)
        cursor = root
        for part in relative.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                return
        local = target.resolve(strict=True)
        if local.is_file() and local.suffix.lower() == ".pdf":
            candidate = _pdf_candidate(local, source, diagnostics)
            if candidate:
                _add_candidate(candidates, candidate, source, candidate.context, candidate.modified)
    except (OSError, ValueError):
        return


def _pdf_candidate(path: Path, source: str, diagnostics: list[str]) -> Candidate | None:
    try:
        size = path.stat().st_size
        if size > MAX_PDF_BYTES:
            diagnostics.append(f"Skipped oversized PDF: {path} ({size} bytes)")
            return None
        reader = PdfReader(str(path), strict=False)
        if reader.is_encrypted:
            diagnostics.append(f"Encrypted PDF requires manual handling: {path}")
            return None
        metadata = reader.metadata
        title = str(metadata.title if metadata and metadata.title else "").strip()
        text = "\n".join((page.extract_text() or "") for page in reader.pages[:MAX_PDF_PAGES])
        if not title:
            title = next(
                (
                    re.sub(r"\s+", " ", line).strip()
                    for line in text.splitlines()
                    if len(line.strip()) > 8
                ),
                path.stem.replace("_", " "),
            )
        header = text[:2500]
        arxiv = re.search(rf"(?im)^\s*arxiv\s*:\s*({ARXIV_ID})\b", header)
        doi_source = (
            " ".join(str(metadata.get(key, "")) for key in ("/DOI", "/Identifier"))
            if metadata
            else ""
        )
        doi = DOI_RE.search(doi_source)
        linked = (
            f"https://arxiv.org/abs/{arxiv.group(1)}"
            if arxiv
            else f"https://doi.org/{doi.group(1).rstrip('.,;:)]}')}"
            if doi
            else ""
        )
        if linked:
            candidate = candidate_from_url(linked, title, source)
        else:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            candidate = Candidate(f"pdf:{digest}", title, local_path=str(path))
        if candidate:
            candidate.local_path = str(path)
            candidate.context = re.sub(r"\s+", " ", text).strip()[:MAX_CONTEXT]
            candidate.modified = path.stat().st_mtime
            return candidate
    except (
        OSError,
        ValueError,
        TypeError,
        EOFError,
        IndexError,
        KeyError,
        PdfReadError,
        PdfStreamError,
    ) as exc:
        diagnostics.append(f"Invalid PDF {path}: {exc}")
    return None


def _notebook_file(path: Path, root: Path) -> bool:
    if path.suffix.lower() not in {".md", ".markdown", ".bib", ".pdf"}:
        return False
    parts = {part.lower() for part in path.relative_to(root).parts}
    if not ("lab-notebook" in parts or root.name.lower() in {"lab-notebook", "notebook"}):
        return False
    if path.suffix.lower() == ".pdf":
        return bool(parts & {"reference", "references", "bibliography"})
    return True


def discover(
    notebook_roots: list[Path],
    download_roots: list[Path],
    *,
    max_files: int = 300,
    max_candidates: int = 120,
    activity_days: int = 30,
) -> ScanResult:
    """Discover offline papers and recent research activity from local sources."""
    result, candidates = ScanResult(), {}
    now = time.time()
    notebook_files: list[tuple[Path, Path]] = []
    for root in notebook_roots:
        try:
            base = root.expanduser().resolve(strict=True)
            notebook_files.extend(
                (base, path) for path in _files(base, max_files, result.diagnostics, notebook=True)
            )
        except OSError as exc:
            result.diagnostics.append(f"Cannot read root {root}: {exc}")
    if len(notebook_files) > max_files:
        result.diagnostics.append(f"Notebook inputs truncated to {max_files} files across roots")
    roots_by_path = {path: base for base, path in notebook_files}
    notebook_files = [
        (roots_by_path[path], path)
        for _, path in _retain_files(
            [(path.stat().st_mtime, path) for path in roots_by_path], max_files
        )
    ]
    for base, path in notebook_files:
        modified = path.stat().st_mtime
        source = str(path)
        if path.suffix.lower() == ".pdf":
            if {"reference", "references", "bibliography"} & {
                part.lower() for part in path.relative_to(base).parts
            }:
                candidate = _pdf_candidate(path, source, result.diagnostics)
                if candidate:
                    _add_candidate(
                        candidates, candidate, source, candidate.context, candidate.modified
                    )
            continue
        text = _text(path, result.diagnostics)
        if not text:
            continue
        if (
            path.suffix.lower() in {".md", ".markdown"}
            and now - modified <= activity_days * 86400
            and path.name.upper() not in {"REVIEW-LEDGER.MD", "CHANGELOG.MD", "CHECKS.MD"}
        ):
            body = re.sub(r"(?m)^#{1,6}\s*", "", text)
            body = re.sub(r"!?(\[([^\]]*)\])\([^)]*\)", r"\2", body)
            body = re.sub(r"[`*_#>]", "", body)
            body = re.sub(r"\n{3,}", "\n\n", body).strip()
            if body:
                normalized = " ".join(body.split())
                excerpt = (
                    body
                    if len(body) <= MAX_CONTEXT
                    else (
                        body[: MAX_CONTEXT // 2].rstrip()
                        + "\n[…]\n"
                        + body[-MAX_CONTEXT // 2 :].lstrip()
                    )
                )
                digest = hashlib.sha256(normalized.encode()).hexdigest()
                result.activity.append(Activity(source, excerpt, modified, digest=digest))
        if path.suffix.lower() == ".bib":
            for _, fields in _bib_entries(text):
                candidate = _bib_candidate(fields, source)
                if candidate:
                    _add_candidate(candidates, candidate, source, fields.get("title", ""), modified)
                if fields.get("file"):
                    value = fields["file"].split(":", 1)[-1].strip()
                    _add_local_pdf(value, path, base, source, candidates, result.diagnostics)
            continue
        for match in LINK_RE.finditer(text):
            title, url = match.group(1).strip(), match.group(2).rstrip(".,;")
            if _is_paper_url(url):
                candidate = candidate_from_url(url, title)
                context = re.sub(
                    r"\s+", " ", text[max(0, match.start() - 350) : match.end() + 350]
                ).strip()
                if candidate:
                    _add_candidate(candidates, candidate, source, context, modified)
        for url in BARE_URL_RE.findall(text):
            url = url.rstrip(".,;")
            if _is_paper_url(url):
                candidate = candidate_from_url(url)
                if candidate:
                    start = text.find(url)
                    context = re.sub(
                        r"\s+", " ", text[max(0, start - 350) : start + len(url) + 350]
                    ).strip()
                    _add_candidate(candidates, candidate, source, context, modified)
        for doi in DOI_RE.findall(text):
            candidate = candidate_from_url(doi)
            if candidate:
                _add_candidate(candidates, candidate, source, text[:MAX_CONTEXT], modified)
        for match in re.finditer(r"\]\(([^)]+\.pdf)\)", text, re.IGNORECASE):
            _add_local_pdf(match.group(1), path, base, source, candidates, result.diagnostics)
    download_files: list[Path] = []
    for root in download_roots:
        download_files.extend(_files(root, max_files, result.diagnostics, downloads=True))
    download_files.sort(key=lambda path: (-path.stat().st_mtime, str(path)))
    download_limit = (max_files + 3) // 4
    if len(download_files) > download_limit:
        result.diagnostics.append(
            f"Download candidates truncated from {len(download_files)} to {download_limit}"
        )
    for path in download_files[: max(0, download_limit)]:
        candidate = _pdf_candidate(path, str(path), result.diagnostics)
        if candidate:
            _add_candidate(candidates, candidate, str(path), candidate.context, candidate.modified)
    ordered = sorted(candidates.values(), key=lambda c: (-c.modified, c.id))
    if len(ordered) > max_candidates:
        result.diagnostics.append(
            f"Candidate list truncated from {len(ordered)} to {max_candidates}"
        )
    result.candidates = ordered[: max(0, max_candidates)]
    result.activity.sort(key=lambda item: (-item.modified, item.path))
    return result
