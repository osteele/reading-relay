# Design

## Scope and goals

Reading Relay maintains a small, stable queue of unread research articles. Its
reading surface is a folder usable from iPad Files; its control surface is a CLI
that both conversation and launchd invoke. Discovery draws from research notebook
activity and already collected papers. Relevance and approachability require
judgment, while reading state and artifact safety require deterministic rules.

Email, general-purpose feed aggregation, a mobile app, and autonomous writing to
research notebooks are outside the implementation boundary. References directories
are sources, not a record of what the user has read.

## Authority boundaries

SQLite under the XDG data directory owns candidate identity, reading state,
preferences, selection rationale, and the event history. Configuration is TOML
under the XDG config directory. The writer lock lives beside the ledger; logs use
XDG state and disposable downloads use XDG cache. Folder destinations are user commands observed at
reconciliation time. `Ledger.json` is a read-only export, not a second writer.
The live database stays outside the cloud-synced reading surface.

A hidden ownership file binds one reading directory to one local ledger instance.
It prevents accidental reuse of a populated reading surface with an empty or
unrelated database. This is not an authentication mechanism.

Notebook files, PDFs, candidate titles, and activity excerpts are untrusted data.
They may influence selection but cannot authorize writes or process execution.
The model receives a bounded packet and returns candidate IDs and explanations.
The application validates the entire selection before any recommendation is
published. It never executes model output as shell text.

The default Codex invocation disables user configuration, execution rules, and
shell tools, and uses a read-only sandbox in a temporary working directory. Provider
credentials remain owned by the CLI. A custom command must preserve this boundary;
an arbitrary configured program has the permissions of its operating-system user.

The harness and model are configuration, separate from the ranker protocol.
Built-in Codex and OMP adapters construct tool-restricted argv arrays. An explicit
command replaces the adapter rather than adding shell interpolation. Notebook
roots are opt-in, so a shared installation cannot inherit the author's source tree.

## State transitions

The principal statuses are `candidate`, `queued`, `read`, `deferred`, and `excluded`.
`publishing` is a durable publication intent, used to recover an interrupted
filesystem operation.

- Discovery merges metadata into existing identities without resetting status.
- Automatic selection admits candidates and deferred papers whose eligibility
  time has passed. Read and excluded papers are not eligible.
- Publication records its intent and the previous reading decision before creating
  the visible PDF, then records `queued` after success. If no completed copy exists,
  recovery restores the prior decision and cooldown. Legacy intents without a
  recovery record hold their slot until an explicit mark.
- A single managed copy observed in a status directory updates status. Multiple
  copies produce ambiguity and block automatic selection and explicit file moves.
- A missing queued PDF retains its status and slot. Missing data cannot prove a
  user action. An explicit CLI mark remains authoritative.
- Deferral time starts when a move is observed, since iCloud does not supply an
  authoritative user-action timestamp through this filesystem interface.

Unread recommendations are not evicted when research priorities change. Explicit
preferences affect future vacancies. Deferral is paper-specific; a natural-language
topic preference is model guidance rather than a hard taxonomy.

The writer lock covers discovery, ranking, reconciliation, and export. The lock
coordinates local CLI processes, not the iCloud service or an offline iPad. Stable
filename identifiers make annotation edits independent of identity. Readers should
move rather than copy managed PDFs and retain the identifier if renaming them.

The lock uses the platform's filesystem locking primitive through `filelock`.
It lives beside the SQLite database in the XDG data directory, so configurations
sharing a ledger share its lock even when their state directories differ.
Local cloud clients and other devices do not participate in this lock.

## Artifact and failure handling

Original downloads and notebook references are read-only sources. Published PDFs
are independent copies. Private staging plus an exclusive hard link installs a
completed file without replacing an existing PDF. Managed annotated copies are
moved back into the queue when restored; the source cache is not substituted for
an available annotated copy.

There is no cross-filesystem SQLite/filesystem transaction. Publication intent and
reconciliation provide crash recovery. A crash between linking a managed copy and
unlinking its previous location leaves two copies; this is deliberately surfaced
as ambiguity, since automatically deleting one could discard annotations.

Generated reports use atomic replacement. They are not transactionally committed
with every PDF or ledger write, so their timestamps describe export time. Failure
before export can leave an older report; launchd logs remain the diagnostic path.

Source failures are independent and diagnostic. Existing candidates and queued
papers survive them. Ingestion limits are explicit and reported, not interpreted
as proof that no further papers exist. The ranker fails closed on invalid output
or process failure. PDF acquisition failures are recorded per paper; they do not
remove successful recommendations.

## Ranker protocol

The configured command is an argv array. The application sends one bounded prompt
on stdin, containing candidate metadata, activity excerpts, saved preferences,
free-slot count, and whether a foundation paper is needed. It expects JSON:

```json
{
  "selections": [
    {
      "candidate_id": "arxiv:1706.03762",
      "rationale": "An explanation grounded in supplied research activity.",
      "category": "foundation",
      "scope": "Read the architecture overview first."
    }
  ]
}
```

The ranker may return an empty `selections` array. It must not exceed the free-slot count,
duplicate IDs, select unknown IDs, or use categories other than `active` and
`foundation`. A single JSON Markdown fence is accepted. Rationale and scope are
text, never executable actions. Inspect `ranking.py` for the exact response
validator and prompt packet limits.

## Operational constraints

The LaunchAgent uses an absolute interpreter and configuration path. The project
checkout and environment are therefore part of the installation. A fresh ranking
session reads durable preferences rather than depending on transcript state.
Discovery, prompt size, subprocess time, PDF bytes, download duration, and total
command duration are bounded. Model quality is a separate empirical question:
a successful JSON response does not prove that the papers are worth reading.

The default reading folder is under Documents. iCloud sync requires operating-system
configuration and cannot be established by a directory name. End-to-end proof
requires opening a published document on the actual iPad, then moving it and
observing reconciliation on the computer. Core CLI behavior is portable;
the built-in daily scheduler uses macOS launchd.
