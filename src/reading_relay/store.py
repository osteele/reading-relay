"""Local SQLite ledger. One CLI writer holds the workflow lock."""

import hashlib
import json
import sqlite3
import time
from dataclasses import asdict
from pathlib import Path

from .models import Activity, Candidate, RelayError, Selection

STATUSES = {"candidate", "publishing", "queued", "read", "deferred", "excluded"}
PUBLICATION_FIELDS = (
    "status",
    "filename",
    "rationale",
    "category",
    "scope",
    "eligible_at",
    "artifact",
    "updated",
)


def marker(candidate_id: str) -> str:
    return "rr-" + hashlib.sha256(candidate_id.encode()).hexdigest()[:16]


class Store:
    def __init__(self, data_dir: Path):
        data_dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(data_dir / "ledger.sqlite3")
        self.db.row_factory = sqlite3.Row
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in {0, 1}:
            raise RelayError(f"Unsupported ledger schema version {version}")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS papers (
                id TEXT PRIMARY KEY, data TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'candidate',
                filename TEXT NOT NULL DEFAULT '', rationale TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL DEFAULT '', scope TEXT NOT NULL DEFAULT '',
                eligible_at REAL NOT NULL DEFAULT 0, artifact TEXT NOT NULL DEFAULT '',
                updated REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL,
                paper_id TEXT, action TEXT NOT NULL, detail TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS preferences (
                id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL, at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS activity (
                path TEXT PRIMARY KEY, digest TEXT NOT NULL, text TEXT NOT NULL, changed REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            PRAGMA user_version=1;
        """)

    def close(self) -> None:
        self.db.close()

    def event(self, paper_id: str | None, action: str, detail: str = "") -> None:
        self.db.execute(
            "INSERT INTO events(at,paper_id,action,detail) VALUES(?,?,?,?)",
            (time.time(), paper_id, action, detail),
        )

    def upsert(self, candidate: Candidate) -> None:
        previous = self.db.execute("SELECT data FROM papers WHERE id=?", (candidate.id,)).fetchone()
        if previous:
            old = Candidate(**json.loads(previous[0]))
            candidate.sources = list(dict.fromkeys(old.sources + candidate.sources))[:40]
            candidate.context = (candidate.context or old.context)[:6000]
            candidate.local_path = candidate.local_path or old.local_path
            candidate.pdf_url = candidate.pdf_url or old.pdf_url
            candidate.url = candidate.url or old.url
            if not candidate.title or candidate.title == candidate.url:
                candidate.title = old.title
        self.db.execute(
            """INSERT INTO papers(id,data,updated) VALUES(?,?,?)
                           ON CONFLICT(id) DO UPDATE SET data=excluded.data,updated=excluded.updated""",
            (candidate.id, json.dumps(asdict(candidate)), time.time()),
        )
        self.db.commit()

    def rows(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM papers ORDER BY updated DESC,id").fetchall()

    def get(self, identifier: str) -> sqlite3.Row:
        rows = [
            row for row in self.rows() if identifier == row["id"] or identifier == marker(row["id"])
        ]
        if len(rows) != 1:
            raise RelayError(f"Unknown or ambiguous paper: {identifier}. Use IDs from `list`.")
        return rows[0]

    def candidate(self, identifier: str) -> Candidate:
        return Candidate(**json.loads(self.get(identifier)["data"]))

    def transition(
        self,
        identifier: str,
        status: str,
        detail: str,
        *,
        artifact: str | None = None,
        eligible_at: float = 0,
    ) -> None:
        if status not in STATUSES:
            raise RelayError(f"Invalid status {status}")
        row = self.get(identifier)
        self.db.execute(
            "UPDATE papers SET status=?,eligible_at=?,artifact=?,updated=? WHERE id=?",
            (
                status,
                eligible_at,
                artifact if artifact is not None else row["artifact"],
                time.time(),
                row["id"],
            ),
        )
        if status != "publishing":
            self.db.execute("DELETE FROM meta WHERE key=?", (f"publication:{row['id']}",))
        self.event(row["id"], status, detail)
        self.db.commit()

    def begin_publish(self, selection: Selection, filename: str) -> None:
        previous = self.get(selection.candidate_id)
        if previous["status"] == "publishing":
            raise RelayError("An interrupted publication must be reconciled before retrying")
        self.db.execute(
            "INSERT OR REPLACE INTO meta VALUES(?,?)",
            (
                f"publication:{selection.candidate_id}",
                json.dumps({key: previous[key] for key in PUBLICATION_FIELDS}),
            ),
        )
        self.db.execute(
            """UPDATE papers SET status='publishing',filename=?,rationale=?,
                           category=?,scope=?,updated=? WHERE id=?""",
            (
                filename,
                selection.rationale,
                selection.category,
                selection.scope,
                time.time(),
                selection.candidate_id,
            ),
        )
        self.event(selection.candidate_id, "publishing", selection.rationale)
        self.db.commit()

    def restore_publication(self, identifier: str) -> bool:
        """Restore a failed intent without making a read or excluded paper eligible."""
        previous = self.meta(f"publication:{identifier}")
        if not previous:
            return False
        saved = json.loads(previous)
        if (
            not isinstance(saved, dict)
            or set(saved) != set(PUBLICATION_FIELDS)
            or saved["status"] not in STATUSES - {"publishing"}
        ):
            raise RelayError(f"Invalid publication recovery record for {identifier}")
        assignments = ",".join(f"{key}=?" for key in PUBLICATION_FIELDS)
        self.db.execute(
            f"UPDATE papers SET {assignments} WHERE id=?",
            (*[saved[key] for key in PUBLICATION_FIELDS], identifier),
        )
        self.db.execute("DELETE FROM meta WHERE key=?", (f"publication:{identifier}",))
        self.event(
            identifier, "publication-restored", "No completed PDF; previous decision retained"
        )
        self.db.commit()
        return True

    def eligible(self, limit: int) -> list[Candidate]:
        rows = self.db.execute(
            """SELECT data FROM papers WHERE status='candidate'
            OR (status='deferred' AND eligible_at<=?) ORDER BY updated DESC,id LIMIT ?""",
            (time.time(), limit),
        ).fetchall()
        return [Candidate(**json.loads(row[0])) for row in rows]

    def preferences(self) -> list[str]:
        return [row[0] for row in self.db.execute("SELECT text FROM preferences ORDER BY id")]

    def prefer(self, text: str) -> int:
        cursor = self.db.execute(
            "INSERT INTO preferences(text,at) VALUES(?,?)", (text, time.time())
        )
        self.event(None, "preference", text)
        self.db.commit()
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    def forget(self, preference_id: int) -> None:
        cursor = self.db.execute("DELETE FROM preferences WHERE id=?", (preference_id,))
        if not cursor.rowcount:
            raise RelayError(f"Unknown preference {preference_id}")
        self.event(None, "forget-preference", str(preference_id))
        self.db.commit()

    def activity(self, items: list[Activity], days: int) -> list[Activity]:
        for item in items:
            # Whitespace-only edits retain the previous semantic-change timestamp.
            digest = item.digest or hashlib.sha256(" ".join(item.text.split()).encode()).hexdigest()
            old = self.db.execute(
                "SELECT digest FROM activity WHERE path=?", (item.path,)
            ).fetchone()
            if not old or old[0] != digest:
                self.db.execute(
                    "INSERT OR REPLACE INTO activity VALUES(?,?,?,?)",
                    (item.path, digest, item.text, min(item.modified, time.time())),
                )
        self.db.commit()
        rows = self.db.execute(
            "SELECT path,text,changed FROM activity WHERE changed>=? "
            "ORDER BY changed DESC LIMIT 30",
            (time.time() - days * 86400,),
        )
        return [
            Activity(*row)
            for row in rows
            if Path(row[0]).name.upper() not in {"REVIEW-LEDGER.MD", "CHANGELOG.MD", "CHECKS.MD"}
        ]

    def meta(self, key: str, value: str | None = None) -> str:
        if value is not None:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, value))
            self.db.commit()
            return value
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else ""

    def snapshot(self) -> dict:
        return {
            "schema_version": 1,
            "generated_at": time.time(),
            "papers": [dict(row) | {"data": json.loads(row["data"])} for row in self.rows()],
            "preferences": self.preferences(),
            "events": [
                dict(row) for row in self.db.execute("SELECT * FROM events ORDER BY sequence")
            ],
        }
