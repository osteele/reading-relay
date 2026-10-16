import json
import sys

import pytest

from reading_relay.models import Activity, Candidate, RelayError, Selection
from reading_relay.ranking import select


def _command(response: str) -> list[str]:
    code = "import sys; sys.stdin.read(); print(sys.argv[1])"
    return [sys.executable, "-c", code, response]


def _select(response: str, *, slots: int = 2) -> list[Selection]:
    return select(
        [Candidate("paper-1", "A paper", sources=["notebook.md"])],
        [Activity("notes.md", "I am studying this topic", 1.0)],
        ["Prefer concise introductions"],
        slots=slots,
        foundation_needed=True,
        command=_command(response),
        timeout=3,
    )


def test_select_accepts_valid_json_and_optional_scope() -> None:
    result = _select(
        json.dumps(
            {
                "selections": [
                    {
                        "candidate_id": "paper-1",
                        "rationale": "Matches the current notes",
                        "category": "active",
                        "scope": "sections 1-2",
                    }
                ]
            }
        )
    )

    assert result == [Selection("paper-1", "Matches the current notes", "active", "sections 1-2")]


def test_select_accepts_one_fenced_json_block_and_empty_choices() -> None:
    assert _select('```json\n{"selections": []}\n```') == []


@pytest.mark.parametrize(
    "response",
    [
        "not json",
        '{"selections": [], "extra": true}',
        '{"selections": "paper-1"}',
        '{"selections": [{"candidate_id":"invented","rationale":"evidence","category":"active"}]}',
        (
            '{"selections": [{"candidate_id":"paper-1","rationale":"evidence","category":"active"},'
            '{"candidate_id":"paper-1","rationale":"again","category":"active"}]}'
        ),
        '{"selections": [{"candidate_id":"paper-1","rationale":"evidence","category":"other"}]}',
        '{"selections": [{"candidate_id":"paper-1","rationale":" ","category":"active"}]}',
        '{"selections": [{"candidate_id":"paper-1","rationale":"evidence","category":"active", "extra": 1}]}',
    ],
)
def test_select_rejects_invalid_or_duplicated_response(response: str) -> None:
    with pytest.raises(RelayError):
        _select(response)


def test_select_rejects_more_than_available_slots() -> None:
    response = json.dumps(
        {
            "selections": [
                {"candidate_id": "paper-1", "rationale": "evidence", "category": "active"},
                {"candidate_id": "paper-1", "rationale": "evidence", "category": "active"},
            ]
        }
    )
    with pytest.raises(RelayError, match="slots"):
        _select(response, slots=1)


def test_select_times_out_a_real_subprocess() -> None:
    command = [sys.executable, "-c", "import time; time.sleep(10)"]
    with pytest.raises(RelayError, match="timed out"):
        select(
            [Candidate("paper-1", "A paper")],
            [],
            [],
            slots=1,
            foundation_needed=False,
            command=command,
            timeout=0.1,
        )


@pytest.mark.parametrize(
    ("stream", "limit"),
    [("stdout", 64 * 1024), ("stderr", 512 * 1024)],
)
def test_select_rejects_subprocess_output_over_limit(stream: str, limit: int) -> None:
    code = (
        f"import sys; sys.stdin.read(); sys.{stream}.write('x' * {limit + 1}); sys.{stream}.flush()"
    )
    with pytest.raises(RelayError, match="output limit"):
        select(
            [Candidate("paper-1", "A paper")],
            [],
            [],
            slots=1,
            foundation_needed=False,
            command=[sys.executable, "-c", code],
            timeout=3,
        )


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="SIGALRM and POSIX process-group assertions; portable CLI deadline coverage is separate",
)
def test_outer_deadline_stops_ranker_process_group(tmp_path) -> None:
    import os
    import signal

    from reading_relay.cli import deadline

    pid_file = tmp_path / "child.pid"
    command = [
        sys.executable,
        "-c",
        "import os,sys,time; open(sys.argv[1],'w').write(str(os.getpid())); time.sleep(20)",
        str(pid_file),
    ]
    previous = signal.signal(signal.SIGALRM, deadline)
    signal.alarm(1)
    try:
        with pytest.raises(RelayError, match="deadline"):
            select(
                [Candidate("paper-1", "A paper")],
                [],
                [],
                slots=1,
                foundation_needed=False,
                command=command,
                timeout=10,
            )
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)
