import json
import time
from dataclasses import replace
from pathlib import Path

import pytest
from fpdf import FPDF

from reading_relay.config import Config
from reading_relay.models import Candidate, RelayError, Selection
from reading_relay.publishing import export
from reading_relay.store import Store
from reading_relay.workflow import enqueue, initialize, locked, mark, occupied, reconcile


@pytest.fixture
def setup(tmp_path):
    config = Config(
        reading_dir=tmp_path / "Reading",
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        notebook_roots=[],
        download_roots=[],
        capacity=2,
    )
    store = Store(config.data_dir)
    initialize(store, config)
    source = tmp_path / "source.pdf"
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=12)
    pdf.cell(text="Original research article")
    pdf.output(str(source))
    candidate = Candidate(
        "doi:10.1234/example", "Example", local_path=str(source), sources=["experiment.md"]
    )
    store.upsert(candidate)
    yield store, config, candidate
    store.close()


def queue(store, config, candidate):
    enqueue(store, config, Selection(candidate.id, "Supports the active question", "active"))
    return Path(store.get(candidate.id)["artifact"])


def test_folder_feedback_survives_rediscovery_and_deferral(setup):
    store, config, candidate = setup
    path = queue(store, config, candidate)
    deferred = config.reading_dir / "Deferred" / path.name
    path.rename(deferred)
    assert reconcile(store, config) == []
    assert store.get(candidate.id)["status"] == "deferred"
    assert store.get(candidate.id)["eligible_at"] > time.time() + 29 * 86400
    store.upsert(Candidate(candidate.id, "Updated metadata", local_path=candidate.local_path))
    assert store.eligible(10) == []
    mark(store, config, candidate.id, "read")
    assert store.get(candidate.id)["status"] == "read"
    assert store.eligible(10) == []
    assert len(list((config.reading_dir / "Read").glob("*.pdf"))) == 1


def test_missing_queue_file_holds_slot_without_republication(setup):
    store, config, candidate = setup
    path = queue(store, config, candidate)
    path.unlink()
    diagnostics = reconcile(store, config)
    assert any("slot held" in message for message in diagnostics)
    assert store.get(candidate.id)["status"] == "queued"
    assert occupied(store, config) == 1
    assert store.eligible(10) == []
    assert list((config.reading_dir / "Up Next").iterdir()) == []


def test_conflicting_copies_do_not_infer_read(setup):
    store, config, candidate = setup
    path = queue(store, config, candidate)
    copy = config.reading_dir / "Read" / path.name
    copy.write_bytes(path.read_bytes())
    assert any("Ambiguous" in message for message in reconcile(store, config))
    assert store.get(candidate.id)["status"] == "queued"


def test_restoring_deferred_preserves_annotated_bytes(setup):
    store, config, candidate = setup
    path = queue(store, config, candidate)
    annotated = path.read_bytes() + b"\n% reader annotation retained\n"
    path.write_bytes(annotated)
    mark(store, config, candidate.id, "deferred")
    deferred = Path(store.get(candidate.id)["artifact"])
    mark(store, config, candidate.id, "queued")
    assert Path(store.get(candidate.id)["artifact"]).read_bytes() == annotated
    assert not deferred.exists()
    assert Path(candidate.local_path).read_bytes() != annotated


def test_publish_never_overwrites_existing_pdf(setup):
    store, config, candidate = setup
    from reading_relay.publishing import filename

    target = config.reading_dir / "Up Next" / filename(candidate)
    target.write_bytes(b"reader's existing document")
    with pytest.raises(RelayError, match="overwrite"):
        queue(store, config, candidate)
    assert target.read_bytes() == b"reader's existing document"


def test_interrupted_publication_recovers_observed_state(setup):
    store, config, candidate = setup
    store.begin_publish(Selection(candidate.id, "why", "foundation"), "missing.pdf")
    reconcile(store, config)
    assert store.get(candidate.id)["status"] == "candidate"
    path = queue(store, config, candidate)
    store.db.execute("UPDATE papers SET status='publishing' WHERE id=?", (candidate.id,))
    store.db.commit()
    reconcile(store, config)
    assert store.get(candidate.id)["status"] == "queued"
    assert Path(store.get(candidate.id)["artifact"]) == path


def test_excluded_ineligible_until_explicit_restore(setup):
    store, config, candidate = setup
    queue(store, config, candidate)
    mark(store, config, candidate.id, "excluded")
    assert store.eligible(10) == []
    mark(store, config, candidate.id, "queued")
    assert store.get(candidate.id)["status"] == "queued"
    assert len(list((config.reading_dir / "Not Now").iterdir())) == 0


def test_ownership_rejects_different_ledger(setup, tmp_path):
    _, config, _ = setup
    other = Store(tmp_path / "other")
    try:
        with pytest.raises(RelayError, match="another ledger"):
            initialize(other, config)
    finally:
        other.close()


@pytest.mark.parametrize("alternate_state", [False, True])
def test_local_lock_serializes_writers(setup, tmp_path, alternate_state):
    _, config, _ = setup
    second = replace(config, state_dir=tmp_path / "other-state") if alternate_state else config
    with locked(config), pytest.raises(RelayError), locked(second):
        pytest.fail("Second writer must not enter")


def test_whitespace_edits_do_not_refresh_research_activity(setup):
    store, _, _ = setup
    from reading_relay.models import Activity

    now = time.time()
    original = now - 40 * 86400
    assert store.activity([Activity("EXP.md", "old question", original)], 30) == []
    assert store.activity([Activity("EXP.md", "old  question\n", now)], 30) == []
    changed = store.activity([Activity("EXP.md", "new question", now)], 30)
    assert changed[0].text == "new question"


def test_export_preserves_annotation_and_records_provenance(setup):
    store, config, candidate = setup
    path = queue(store, config, candidate)
    annotated = path.read_bytes() + b"\n% annotation\n"
    path.write_bytes(annotated)
    export(store, config, ["source temporarily unavailable"])
    assert path.read_bytes() == annotated
    snapshot = json.loads((config.reading_dir / "Ledger.json").read_text())
    paper = snapshot["papers"][0]
    assert paper["data"]["sources"] == ["experiment.md"]
    assert paper["rationale"] == "Supports the active question"
    assert snapshot["events"][-1]["action"] == "queued"
    assert "source temporarily unavailable" in (config.reading_dir / "Status.txt").read_text()


def test_ambiguous_physical_copies_each_consume_capacity(setup):
    store, config, candidate = setup
    path = queue(store, config, candidate)
    duplicate = path.with_name(f"Copy {path.stem}.PDF")
    duplicate.write_bytes(path.read_bytes())
    assert any("Ambiguous" in message for message in reconcile(store, config))
    assert occupied(store, config) == config.capacity


@pytest.mark.parametrize("status", ["read", "excluded", "deferred"])
def test_interrupted_restore_keeps_previous_decision(setup, status):
    store, config, candidate = setup
    mark(store, config, candidate.id, status)
    previous = dict(store.get(candidate.id))
    store.begin_publish(Selection(candidate.id, "Restore attempt", "active"), "missing.pdf")
    reconcile(store, config)
    current = store.get(candidate.id)
    assert current["status"] == status
    assert current["eligible_at"] == previous["eligible_at"]
    assert store.eligible(10) == []


@pytest.mark.parametrize("status", ["queued", "read", "excluded"])
def test_ambiguous_copies_block_explicit_mutation(setup, status):
    store, config, candidate = setup
    path = queue(store, config, candidate)
    mark(store, config, candidate.id, "deferred")
    deferred = Path(store.get(candidate.id)["artifact"])
    duplicate = config.reading_dir / "Read" / f"Other {path.name}"
    duplicate.write_bytes(deferred.read_bytes() + b"\n% separate annotations\n")
    before = {p: p.read_bytes() for p in (deferred, duplicate)}
    with pytest.raises(RelayError, match="Ambiguous"):
        mark(store, config, candidate.id, status)
    assert store.get(candidate.id)["status"] == "deferred"
    assert {p: p.read_bytes() for p in before} == before
    assert list((config.reading_dir / "Up Next").iterdir()) == []


def test_expired_deferred_copy_is_usable_without_original_download(setup):
    import sys

    from reading_relay.workflow import run

    store, config, candidate = setup
    path = queue(store, config, candidate)
    annotated = path.read_bytes() + b"\n% offline reading notes\n"
    path.write_bytes(annotated)
    mark(store, config, candidate.id, "deferred", until=time.time() - 1)
    Path(candidate.local_path).unlink()
    response = json.dumps(
        {
            "selections": [
                {"candidate_id": candidate.id, "rationale": "Resume reading", "category": "active"}
            ]
        }
    )
    config.command = [
        sys.executable,
        "-c",
        f"import sys; sys.stdin.read(); print({response!r})",
    ]
    run(store, config)
    row = store.get(candidate.id)
    assert row["status"] == "queued"
    assert Path(row["artifact"]).read_bytes() == annotated


def test_full_queue_rejects_new_paper_without_changing_it(setup):
    store, config, candidate = setup
    config.capacity = 1
    path = queue(store, config, candidate)
    before = path.read_bytes()
    second = Candidate("doi:10.1234/second", "Second", local_path=candidate.local_path)
    store.upsert(second)
    with pytest.raises(RelayError, match="full"):
        enqueue(store, config, Selection(second.id, "Another choice", "active"))
    assert store.get(second.id)["status"] == "candidate"
    assert store.eligible(10)[0].id == second.id
    assert [p for p in (config.reading_dir / "Up Next").iterdir()] == [path]
    assert path.read_bytes() == before


@pytest.mark.parametrize("malformed", [[], None, {"schema_version": 2}, {"schema_version": True}])
def test_malformed_ownership_record_is_rejected_without_mutation(setup, malformed):
    store, config, candidate = setup
    path = queue(store, config, candidate)
    before = path.read_bytes()
    sentinel = config.reading_dir / ".reading-relay.json"
    if isinstance(malformed, dict):
        malformed = malformed | {"instance_id": store.meta("instance_id")}
    sentinel.write_text(json.dumps(malformed))
    with pytest.raises(RelayError, match="ownership record"):
        initialize(store, config)
    assert path.read_bytes() == before
    assert store.get(candidate.id)["status"] == "queued"


def test_failed_restore_rolls_back_before_returning(setup):
    from reading_relay.publishing import filename

    store, config, candidate = setup
    mark(store, config, candidate.id, "excluded")
    conflicting_directory = config.reading_dir / "Up Next" / filename(candidate)
    conflicting_directory.mkdir()
    with pytest.raises(RelayError, match="overwrite"):
        queue(store, config, candidate)
    assert store.get(candidate.id)["status"] == "excluded"
    assert store.eligible(10) == []
    assert conflicting_directory.is_dir()


def test_legacy_intent_without_prior_decision_holds_slot(setup):
    store, config, candidate = setup
    store.begin_publish(Selection(candidate.id, "Legacy intent", "active"), "missing.pdf")
    store.db.execute("DELETE FROM meta WHERE key=?", (f"publication:{candidate.id}",))
    store.db.commit()
    diagnostics = reconcile(store, config)
    assert any("previous decision unknown" in d for d in diagnostics)
    assert store.get(candidate.id)["status"] == "publishing"
    assert store.eligible(10) == []
    assert occupied(store, config) == 1
    mark(store, config, candidate.id, "excluded")
    assert occupied(store, config) == 0
