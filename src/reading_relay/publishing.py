"""Validate and publish PDFs without overwriting reader annotations."""

import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from fpdf import FPDF
from fpdf.enums import WrapMode
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from .config import Config
from .models import Candidate, RelayError
from .store import Store, marker

FOLDERS = {"queued": "Up Next", "read": "Read", "deferred": "Deferred", "excluded": "Not Now"}


class _HTTPSRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlsplit(newurl)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise RelayError("Refusing unsafe PDF redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def validate_pdf(path: Path, max_bytes: int) -> None:
    if not path.is_file() or path.is_symlink():
        raise RelayError(f"Not a regular PDF: {path}")
    if path.stat().st_size > max_bytes:
        raise RelayError(f"PDF exceeds size limit: {path.name}")
    with path.open("rb") as stream:
        if stream.read(5) != b"%PDF-":
            raise RelayError(f"Not a PDF response: {path.name}")
    try:
        reader = PdfReader(path)
        if reader.is_encrypted:
            raise RelayError(f"Encrypted PDF requires manual handling: {path.name}")
        if not len(reader.pages):
            raise RelayError(f"PDF has no pages: {path.name}")
    except (PdfReadError, ValueError, OSError) as exc:
        raise RelayError(f"Invalid PDF {path.name}: {exc}") from exc


def acquire(candidate: Candidate, config: Config) -> Path:
    if candidate.local_path:
        path = Path(candidate.local_path)
        if path.exists():
            validate_pdf(path, config.max_pdf_bytes)
            return path
    if not candidate.pdf_url:
        raise RelayError(f"No authorized PDF location known for {candidate.title}; add a local PDF")
    parsed = urllib.parse.urlsplit(candidate.pdf_url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise RelayError("PDF downloads require an HTTPS URL without credentials")
    cache = config.cache_dir
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / (marker(candidate.id) + ".pdf")
    if target.exists():
        validate_pdf(target, config.max_pdf_bytes)
        return target
    request = urllib.request.Request(
        candidate.pdf_url, headers={"User-Agent": "ReadingRelay/0.1 (personal reading queue)"}
    )
    started = time.monotonic()
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=cache, suffix=".pdf", delete=False) as stream:
            temporary = Path(stream.name)
            with urllib.request.build_opener(_HTTPSRedirectHandler).open(
                request, timeout=config.download_timeout
            ) as response:
                total = 0
                while chunk := response.read(64 * 1024):
                    total += len(chunk)
                    if total > config.max_pdf_bytes:
                        raise RelayError("Download exceeds configured PDF size limit")
                    if time.monotonic() - started > config.download_timeout:
                        raise RelayError("PDF download deadline exceeded")
                    stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        validate_pdf(temporary, config.max_pdf_bytes)
        os.replace(temporary, target)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RelayError(f"PDF download failed for {candidate.title}: {exc}") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return target


def filename(candidate: Candidate) -> str:
    suffix = f" [{marker(candidate.id)}].pdf"
    title = re.sub(r"[^\w .()-]+", " ", candidate.title).strip(" .")[:100] or "Article"
    title_bytes = title.encode("utf-8")[: 255 - len(suffix.encode("utf-8"))]
    title = title_bytes.decode("utf-8", errors="ignore").strip(" .") or "Article"
    return title + suffix


def publish(source: Path, target: Path, config: Config) -> None:
    """Hard-link a completed private staging file into place, never replace a PDF."""
    validate_pdf(source, config.max_pdf_bytes)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise RelayError(f"Refusing to overwrite {target.name}")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=target.parent, prefix=".relay-", delete=False
        ) as stream:
            temporary = Path(stream.name)
            with source.open("rb") as reader:
                while chunk := reader.read(64 * 1024):
                    stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def atomic_write(path: Path, content: bytes) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".relay-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def export(store: Store, config: Config, diagnostics: list[str]) -> None:
    snapshot = store.snapshot()
    atomic_write(
        config.reading_dir / "Ledger.json",
        (json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n").encode(),
    )
    lines = [
        "Reading Relay",
        time.strftime("Updated %Y-%m-%d %H:%M %Z"),
        "",
        "Move PDFs to Read, Deferred, or Not Now to record your decision.",
        "Missing files are not treated as read. Unread recommendations stay in Up Next.",
        "",
    ]
    for row in store.rows():
        if row["status"] not in {"queued", "publishing"}:
            continue
        candidate = store.candidate(row["id"])
        lines.extend(
            [
                candidate.title,
                f"ID: {row['id']}",
                f"Type: {row['category']}",
                row["rationale"],
                f"Reading scope: {row['scope'] or 'Not specified'}",
                f"Source: {candidate.url or candidate.local_path}",
                "Evidence: " + "; ".join(candidate.sources[:4]),
                "",
            ]
        )
    lines.extend(["Diagnostics", *(diagnostics or ["None."])])
    atomic_write(config.reading_dir / "Status.txt", ("\n".join(lines) + "\n").encode())
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    # Core fonts keep installation independent of machine fonts. Status.txt preserves Unicode.
    pdf.set_font("Helvetica", size=11)
    for line in lines:
        safe = line.encode("latin-1", "replace").decode("latin-1")
        pdf.multi_cell(0, 6, safe, new_x="LMARGIN", new_y="NEXT", wrapmode=WrapMode.CHAR)
    atomic_write(config.reading_dir / "Reading List.pdf", bytes(pdf.output()))
    store.meta("last_export", str(snapshot["generated_at"]))
