"""Typed boundaries between discovery, selection, and queue management."""

from dataclasses import dataclass, field


@dataclass
class Candidate:
    id: str
    title: str
    url: str = ""
    pdf_url: str = ""
    local_path: str = ""
    sources: list[str] = field(default_factory=list)
    context: str = ""
    modified: float = 0.0


@dataclass
class Activity:
    path: str
    text: str
    modified: float
    digest: str = ""


@dataclass
class ScanResult:
    candidates: list[Candidate] = field(default_factory=list)
    activity: list[Activity] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)


@dataclass
class Selection:
    candidate_id: str
    rationale: str
    category: str
    scope: str = ""


class RelayError(Exception):
    """An actionable workflow failure safe to show without a traceback."""
