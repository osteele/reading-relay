"""Filesystem feedback and the daily reconcile/discover/select/publish workflow."""

import json
import os
import re
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from filelock import FileLock, Timeout

from .config import Config
from .models import RelayError, Selection
from .publishing import FOLDERS, acquire, export, filename, publish
from .store import Store, marker


@contextmanager
def locked(config: Config):
    config.data_dir.mkdir(parents=True, exist_ok=True)
    lock = FileLock(config.data_dir / "workflow.lock")
    try:
        lock.acquire(timeout=0)
    except Timeout as exc:
        raise RelayError(
            "Another Reading Relay command is running; try again after it finishes"
        ) from exc
    try:
        store = Store(config.data_dir)
        try:
            yield store
        finally:
            store.close()
    finally:
        lock.release()


def initialize(store: Store, config: Config) -> None:
    root = config.reading_dir
    if root.is_symlink():
        raise RelayError("reading_dir must be a real directory, not a symlink")
    root.mkdir(parents=True, exist_ok=True)
    sentinel = root / ".reading-relay.json"
    identity = store.meta("instance_id")
    if sentinel.exists():
        if sentinel.is_symlink():
            raise RelayError("Reading folder ownership file cannot be a symlink")
        owner = json.loads(sentinel.read_text())
        if (
            not isinstance(owner, dict)
            or set(owner) != {"schema_version", "instance_id"}
            or type(owner["schema_version"]) is not int
            or owner["schema_version"] != 1
            or not isinstance(owner["instance_id"], str)
            or not owner["instance_id"]
        ):
            raise RelayError("Invalid reading folder ownership record; restore it from backup")
        if owner.get("instance_id") != identity:
            raise RelayError(
                "Reading folder belongs to another ledger; restore its local state first"
            )
    else:
        reserved = ["Reading List.pdf", "Ledger.json", "Status.txt"]
        if any((root / name).exists() for name in reserved):
            raise RelayError("Reading folder has reserved output filenames; move them aside first")
        identity = identity or str(uuid.uuid4())
        store.meta("instance_id", identity)
        with sentinel.open("x") as stream:
            json.dump({"schema_version": 1, "instance_id": identity}, stream)
    for folder in FOLDERS.values():
        path = root / folder
        if path.is_symlink():
            raise RelayError(f"Status folder cannot be a symlink: {path}")
        path.mkdir(exist_ok=True)
    for name in ["Reading List.pdf", "Ledger.json", "Status.txt"]:
        if (root / name).is_symlink():
            raise RelayError(f"Generated output cannot be a symlink: {name}")


def _observed(store: Store, config: Config) -> tuple[dict[str, list[tuple[str, Path]]], list[str]]:
    observed: dict[str, list[tuple[str, Path]]] = {}
    diagnostics = []
    known = {marker(row["id"]): row for row in store.rows()}
    for status, folder in FOLDERS.items():
        for index, path in enumerate((config.reading_dir / folder).iterdir()):
            if index >= 2000:
                raise RelayError(f"Too many entries in {folder}; refusing partial reconciliation")
            if path.is_symlink():
                diagnostics.append(f"Ignoring symlink: {folder}/{path.name}")
                continue
            match = re.search(r"\[(rr-[a-f0-9]{16})\]", path.name)
            if match and match[1] in known:
                if path.suffix.lower() == ".pdf" and path.is_file():
                    observed.setdefault(match[1], []).append((status, path))
            elif path.suffix.lower() == ".pdf":
                diagnostics.append(f"Unmanaged PDF: {folder}/{path.name}; add it with the CLI")
    return observed, diagnostics


def _managed_source(store: Store, config: Config, identifier: str) -> Path | None:
    observed, _ = _observed(store, config)
    locations = observed.get(marker(identifier), [])
    if len(locations) > 1:
        raise RelayError(f"Ambiguous copies for {identifier}; leave one managed PDF")
    return locations[0][1] if locations else None


def reconcile(store: Store, config: Config) -> list[str]:
    observed, diagnostics = _observed(store, config)
    known = {marker(row["id"]): row for row in store.rows()}
    for token, row in known.items():
        locations = observed.get(token, [])
        if len(locations) > 1:
            diagnostics.append(f"Ambiguous copies for {row['id']}; leave one managed PDF")
            continue
        if not locations:
            if row["status"] == "publishing":
                if not store.restore_publication(row["id"]):
                    diagnostics.append(
                        f"Interrupted publication for {row['id']}; previous decision unknown, "
                        "slot held until an explicit mark"
                    )
            elif row["status"] == "queued":
                diagnostics.append(
                    f"Missing/unavailable PDF for {row['id']}; slot held, not marked read"
                )
            continue
        status, path = locations[0]
        if status != row["status"]:
            eligible = time.time() + config.defer_days * 86400 if status == "deferred" else 0
            store.transition(
                row["id"],
                status,
                f"Observed move to {path.parent.name}",
                artifact=str(path),
                eligible_at=eligible,
            )
        elif str(path) != row["artifact"]:
            store.db.execute("UPDATE papers SET artifact=? WHERE id=?", (str(path), row["id"]))
            store.db.commit()
    return diagnostics


def occupied(store: Store, config: Config) -> int:
    rows = store.rows()
    queued = sum(row["status"] in {"queued", "publishing"} for row in rows)
    known = {marker(row["id"]) for row in rows if row["status"] in {"queued", "publishing"}}
    # Unknown and ambiguous physical PDFs still occupy a visible reading slot.
    seen: set[str] = set()
    extra = 0
    for path in (config.reading_dir / "Up Next").iterdir():
        if not path.is_file() or path.suffix.lower() != ".pdf":
            continue
        match = re.search(r"\[(rr-[a-f0-9]{16})\]", path.name)
        token = match[1] if match else ""
        if token in known and token not in seen:
            seen.add(token)
        else:
            extra += 1
    return queued + extra


def enqueue(store: Store, config: Config, selection: Selection) -> None:
    candidate = store.candidate(selection.candidate_id)
    row = store.get(candidate.id)
    artifact = _managed_source(store, config, candidate.id)
    if row["status"] == "queued":
        return
    if occupied(store, config) >= config.capacity:
        raise RelayError("Reading queue is full; mark, defer, or dismiss an item first")
    source = artifact if artifact and artifact.is_file() else acquire(candidate, config)
    target = config.reading_dir / "Up Next" / filename(candidate)
    store.begin_publish(selection, target.name)
    try:
        # Move the annotated managed copy, rather than substituting the source cache.
        if artifact and artifact == source:
            if target.exists():
                raise RelayError(f"Refusing to overwrite {target}")
            os.link(source, target)
            source.unlink()
        else:
            publish(source, target, config)
    except (RelayError, OSError):
        if not target.is_file():
            store.restore_publication(candidate.id)
        raise
    store.transition(candidate.id, "queued", selection.rationale, artifact=str(target))


def mark(store: Store, config: Config, identifier: str, status: str, *, until: float = 0) -> None:
    row = store.get(identifier)
    if status not in FOLDERS:
        raise RelayError(f"Cannot mark {status}")
    if status == "queued":
        enqueue(store, config, Selection(row["id"], "Explicit user selection", "active"))
        return
    source = _managed_source(store, config, row["id"])
    target = None
    if source and source.is_file():
        target = config.reading_dir / FOLDERS[status] / source.name
        if source != target:
            if target.exists():
                raise RelayError(f"Conflicting destination: {target}")
            os.link(source, target)
            source.unlink()
    eligible = until or (time.time() + config.defer_days * 86400 if status == "deferred" else 0)
    store.transition(
        row["id"],
        status,
        "Explicit user instruction",
        artifact=str(target) if target else None,
        eligible_at=eligible,
    )


def run(store: Store, config: Config, *, reconcile_only: bool = False) -> list[str]:
    from .discovery import discover
    from .ranking import select

    initialize(store, config)
    diagnostics = reconcile(store, config)
    slots = max(0, config.capacity - occupied(store, config))
    if reconcile_only or not slots:
        export(store, config, diagnostics)
        return diagnostics
    scan = discover(
        config.notebook_roots,
        config.download_roots,
        max_files=config.max_files,
        max_candidates=config.max_candidates,
        activity_days=config.activity_days,
    )
    diagnostics.extend(scan.diagnostics)
    for candidate in scan.candidates:
        store.upsert(candidate)
    activity = store.activity(scan.activity, config.activity_days)
    store.meta("last_scan", str(time.time()))
    candidates = store.eligible(config.max_candidates)
    observed, _ = _observed(store, config)
    available = [
        candidate
        for candidate in candidates
        if len(observed.get(marker(candidate.id), [])) <= 1
        and (
            candidate.pdf_url
            or (candidate.local_path and Path(candidate.local_path).is_file())
            or observed.get(marker(candidate.id))
        )
    ]
    if len(available) < len(candidates):
        diagnostics.append(
            f"{len(candidates) - len(available)} candidates await an available, unambiguous PDF location"
        )
    candidates = available
    if not candidates:
        diagnostics.append("No eligible candidates found; queue left unchanged")
        export(store, config, diagnostics)
        return diagnostics
    foundation_needed = not any(
        row["category"] == "foundation" and row["status"] == "queued" for row in store.rows()
    )
    try:
        selections = select(
            candidates,
            activity,
            store.preferences(),
            slots=slots,
            foundation_needed=foundation_needed,
            command=config.effective_command(),
            timeout=config.rank_timeout,
        )
    except RelayError as exc:
        diagnostics.append(f"Ranking failed; queue preserved: {exc}")
        export(store, config, diagnostics)
        raise
    # Folder decisions can arrive while the model is running. They outrank its stale packet.
    diagnostics.extend(reconcile(store, config))
    for selection in selections:
        current = store.get(selection.candidate_id)
        if current["status"] not in {"candidate", "deferred"} or (
            current["status"] == "deferred" and current["eligible_at"] > time.time()
        ):
            diagnostics.append(f"Skipped stale selection after feedback: {selection.candidate_id}")
            continue
        try:
            enqueue(store, config, selection)
        except (RelayError, OSError) as exc:
            diagnostics.append(f"Could not publish {selection.candidate_id}: {exc}")
            store.event(selection.candidate_id, "publication-failed", str(exc))
            store.db.commit()
    store.meta("last_run", str(time.time()))
    export(store, config, diagnostics)
    return diagnostics
