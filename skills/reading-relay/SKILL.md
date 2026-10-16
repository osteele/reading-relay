---
name: reading-relay
description: Install or operate Reading Relay, manage a persistent research-paper reading queue, record read/deferred/excluded feedback, adjust paper-selection preferences, or inspect its notebook-driven recommendations and optional macOS schedule.
---

# Reading Relay

Reading Relay is a Python CLI, not an agent plugin or model provider. It discovers
papers from configured Markdown research notebooks and PDF download folders, asks
a configured Codex/OMP/custom harness to choose relevant reading, and preserves
reading decisions and annotated PDFs across runs. The reading folder may be local
or cloud-synced; iCloud enables iPad access.

## Installation and command location

If the CLI is missing, follow the repository's `INSTALL.md`:
https://github.com/osteele/reading-relay/blob/main/INSTALL.md

Installing this skill alone does not install the CLI. Prefer `reading-relay` on
PATH. A checkout installation may instead use `uv run reading-relay` from that
checkout, or its absolute `.venv/bin/reading-relay` executable
(`.venv/Scripts/reading-relay.exe` on Windows). Do not assume where the checkout
lives. Read `--help` for the installed version before choosing flags.

The default TOML is `~/.config/reading-relay/config.toml`, subject to
`XDG_CONFIG_HOME`. Respect the user's existing configuration and XDG paths. Put
`--config /absolute/path/config.toml` **before** the subcommand when needed. The
CLI's `install` subcommand installs a daily macOS job; it does not install the CLI
or this skill.

## Inspect before acting

```sh
reading-relay list --json
reading-relay preferences
reading-relay schedule
```

Only use `schedule` on macOS. `list --all --json` includes undisplayed candidates;
plain `list` shows recorded reading states. Obtain each paper's exact canonical
ID from the command output; do not confuse it with the filename's `[rr-…]` marker.
The CLI is authoritative for ledger state. `Status.txt` and `Ledger.json` are
exports of the last completed operation, not necessarily fresh status.

Use `scan` to discover candidates without a model call or paper download. It
updates the local ledger and reports. Use `run --reconcile-only` to record folder
feedback and refresh reports without requesting new recommendations. A full `run`
may contact the harness provider and download PDFs. Before the first provider
call, confirm the user accepts sending the configured source paths and excerpts.
Never expand notebook roots to unrelated personal folders on your own.

## Reading feedback

Use the CLI for decisions requested through chat:

```sh
reading-relay mark PAPER_ID read
reading-relay mark PAPER_ID deferred --until 2026-12-01
reading-relay mark PAPER_ID excluded
reading-relay mark PAPER_ID queued
reading-relay add https://arxiv.org/abs/1706.03762 --queue
```

Substitute an actual ID and the user's date. `deferred` without `--until` uses the
configured cooldown. `excluded` corresponds to `Not Now`, excluding the paper,
not its whole topic. `queued` explicitly restores it when capacity permits.
`add` accepts an existing PDF or supported paper URL; omit `--queue` to register
it as a candidate without requesting a reading slot.

The user can also move PDFs among `Up Next/`, `Read/`, `Deferred/`, and `Not Now/`
in their reading folder; the next run records this feedback. Preserve `[rr-…]`
in managed filenames and retain one managed copy per identifier. Do not replace
annotated PDFs, guess among conflicting copies, or interpret deletion as read.
Missing files hold their slots until restored or explicitly marked.

## Persistent selection preferences

```sh
reading-relay prefer "Prefer accessible surveys before specialist papers."
reading-relay preferences
reading-relay forget PREFERENCE_ID
```

Preferences persist in local SQLite and guide later selections. They do not
retroactively replace unread recommendations. Record a preference only when the
user asks for it; do not turn a passing comment into a durable rule. Removing a
preference uses the numeric ID returned by `preferences`.

## Scheduling and recovery

On macOS, install or change the daily job only when requested:

```sh
reading-relay install --hour 9 --minute 0
reading-relay schedule
reading-relay uninstall
```

It runs in the logged-in user's session and uses the installing Python
environment. Uninstall removes the job and its background launcher, retaining
papers and ledger. On other systems, use manual runs or the user's scheduler.

Surface diagnostics instead of claiming an empty queue is a successful selection.
For stale reports, inspect their timestamp, the configured logs, and macOS
`schedule` output if applicable. Do not edit SQLite directly, delete the hidden
`.reading-relay.json` ownership file, or create a second empty ledger to bypass
an attachment error. Preserve backups and annotated files. Restore the complete
ledger backup, or use a distinct new reading folder and configuration.

Never claim iCloud handoff or offline iPad access from a desktop-only check;
the user must open an actual downloaded PDF on that device with networking off.
