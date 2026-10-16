"""Seeded operation sequences checked against README-derived reading-state intent."""

import random
from pathlib import Path

import pytest
from fpdf import FPDF

from reading_relay.config import Config
from reading_relay.models import Candidate, RelayError, Selection
from reading_relay.publishing import FOLDERS
from reading_relay.store import Store
from reading_relay.workflow import enqueue, initialize, mark, occupied, reconcile


@pytest.mark.parametrize("seed", range(12))
def test_reading_state_sequences(tmp_path, seed):
    """Replay one seed with pytest 'tests/test_state_properties.py::test_reading_state_sequences[7]'."""
    rng = random.Random(seed)
    config = Config(
        reading_dir=tmp_path / "Reading",
        data_dir=tmp_path / "data",
        state_dir=tmp_path / "state",
        cache_dir=tmp_path / "cache",
        notebook_roots=[],
        download_roots=[],
        capacity=2,
    )
    source = tmp_path / "article.pdf"
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=12)
    pdf.cell(text="Sequence-model article")
    pdf.output(str(source))
    original = source.read_bytes()
    candidate = Candidate("doi:10.1234/sequence", "Sequence article", local_path=str(source))
    actions = [
        "queue",
        "duplicate",
        "move",
        "queue",
        "annotate",
        "deferred",
        "queue",
        "missing",
        "read",
        "queue",
        "excluded",
        "rediscover",
    ]
    actions += rng.choices(
        [
            "queue",
            "move",
            "annotate",
            "read",
            "deferred",
            "excluded",
            "missing",
            "rediscover",
            "duplicate",
        ],
        k=28,
    )
    coverage = dict.fromkeys(actions, 0)
    expected = "candidate"
    expected_bytes = original
    store = Store(config.data_dir)
    try:
        initialize(store, config)
        store.upsert(candidate)
        for step, action in enumerate(actions):
            context = f"seed={seed}, step={step}, action={action}"
            present = [
                p
                for folder in FOLDERS.values()
                for p in (config.reading_dir / folder).iterdir()
                if p.is_file()
            ]
            assert len(present) <= 1, context
            path = present[0] if present else None
            if action == "queue":
                if expected != "queued" and path is None:
                    expected_bytes = original
                enqueue(store, config, Selection(candidate.id, "Explicit reading choice", "active"))
                expected = "queued"
                coverage[action] += 1
            elif action in {"read", "deferred", "excluded"}:
                mark(store, config, candidate.id, action)
                expected = action
                coverage[action] += 1
            elif action == "rediscover":
                store.upsert(
                    Candidate(candidate.id, f"Metadata revision {step}", local_path=str(source))
                )
                coverage[action] += 1
            elif action == "move" and path:
                destination = rng.choice(list(FOLDERS))
                target = config.reading_dir / FOLDERS[destination] / path.name
                if path != target:
                    path.rename(target)
                reconcile(store, config)
                expected = destination
                coverage[action] += 1
            elif action == "annotate" and path:
                expected_bytes += f"\n% annotation {seed}/{step}\n".encode()
                path.write_bytes(expected_bytes)
                coverage[action] += 1
            elif action == "missing" and expected == "queued" and path:
                path.unlink()
                reconcile(store, config)
                coverage[action] += 1
            elif action == "duplicate" and path:
                duplicate = config.reading_dir / "Read" / f"Other {path.name}"
                duplicate.write_bytes(expected_bytes + b"\n% independent notes\n")
                assert any("Ambiguous" in d for d in reconcile(store, config)), context
                with pytest.raises(RelayError, match="Ambiguous"):
                    mark(store, config, candidate.id, "queued")
                assert path.read_bytes() == expected_bytes, context
                assert duplicate.read_bytes() == expected_bytes + b"\n% independent notes\n", (
                    context
                )
                duplicate.unlink()
                coverage[action] += 1
            reconcile(store, config)
            row = store.get(candidate.id)
            assert row["status"] == expected, context
            assert occupied(store, config) == (1 if expected == "queued" else 0), context
            assert [c.id for c in store.eligible(10)] == (
                [candidate.id] if expected == "candidate" else []
            ), context
            artifact = Path(row["artifact"]) if row["artifact"] else None
            if artifact and artifact.exists():
                assert artifact.read_bytes() == expected_bytes, context
            assert source.read_bytes() == original, context
        assert all(coverage.values()), f"seed={seed}, unexercised branches: {coverage}"
    finally:
        store.close()
