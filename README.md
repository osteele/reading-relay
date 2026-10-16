# Reading Relay

[![CI](https://github.com/osteele/reading-relay/actions/workflows/ci.yml/badge.svg)](https://github.com/osteele/reading-relay/actions/workflows/ci.yml)

Reading Relay keeps a small research reading queue in an iCloud-synced folder.
It finds papers in research notebooks and selected downloads, invokes a configurable
coding-agent tool (the harness) to recommend papers relevant to current research,
and fills vacant reading slots. [Codex](https://github.com/openai/codex) is the default;
[Oh My Pi (OMP)](https://github.com/can1357/oh-my-pi) and custom commands are optional
alternatives. The harness runs on your computer; model inference uses its configured
provider. The default does not run a local model. Unread recommendations stay put
until you act on them.

The default destination is `~/Documents/Reading`, with a capacity of three papers.
The selection target is two papers relevant to active research and one approachable
foundational paper. This is a preference, not a quota: the model can recommend
fewer papers when the candidates are weak.

**Status:** First public release (0.1.0). Keep a backup of the ledger and verify
iCloud handoff and offline reading on your own devices before unattended use.

## Install

Paste this into your coding agent:

```text
Install Reading Relay from https://github.com/osteele/reading-relay. Follow INSTALL.md to install the CLI and reading-relay skill, then configure it with me.
```

The [agent installation guide](INSTALL.md) covers prerequisites, skill discovery,
existing installations, and verification. It asks before sending notebook context
to a model provider or enabling the optional daily job.

### Manual setup

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/), and an authenticated
coding-agent harness: [Codex](https://github.com/openai/codex) by default.
The CLI runs on macOS, Linux, and Windows. Automatic daily scheduling uses macOS
launchd; on other systems invoke `run` manually or from your own scheduler.
The reading folder can be local or cloud-synced; iPad access requires iCloud Drive.

Install and sign in to the harness first (`codex login` for Codex).
`run` sends source paths and excerpts to that harness's provider; review your
configured roots before invoking it. `scan` discovers local candidates without
calling a model or downloading papers.

Replace `~/Research` below with an existing root matching the
[notebook layout](#discovery-and-selection). Repeat `--notebook-root` for additional
roots; there is no default notebook root. For a PDF-only inbox, create
`~/Downloads/Papers`, put paper PDFs there, and omit the `--notebook-root` flag.

```sh
git clone https://github.com/osteele/reading-relay.git
cd reading-relay
uv sync
uv run reading-relay init --notebook-root ~/Research
uv run reading-relay scan
uv run reading-relay list --all
uv run reading-relay run
```

An empty inbox produces an empty queue with a diagnostic, not invented recommendations.
Inspect `~/Documents/Reading/Reading List.pdf` after the first run.

On macOS, install the daily job after confirming the first selection:

```sh
uv run reading-relay install --hour 9 --minute 0
uv run reading-relay schedule
```

Use `uv run reading-relay` from this repository. From another directory, use the
absolute `.venv/bin/reading-relay` executable on macOS/Linux or
`.venv/Scripts/reading-relay.exe` on Windows. The macOS LaunchAgent invokes the
project's Python directly; it does not resolve dependencies or download packages
on each run. Keep the checkout and its `.venv` at their installed locations.

### Agent skill

The [reading-relay skill](skills/reading-relay/SKILL.md) teaches an agent to inspect
the queue, record reading feedback and preferences, and manage the schedule. It is
installed separately from the CLI. With Node.js available, install it globally
for Claude Code and Codex:

```sh
npx skills add osteele/reading-relay --skill reading-relay -g -a claude-code -a codex -y
```

Use `bunx skills` instead of `npx skills` with Bun, choose the agents you use,
and omit `-g` for a project-scoped install. On Windows, add `--copy` if symlinks
are unavailable. The [skills CLI](https://github.com/vercel-labs/skills) manages
discovery and updates. Manual installation copies or symlinks `skills/reading-relay/`
into your agent's skills directory; see [INSTALL.md](INSTALL.md#2-install-the-agent-skill).
Review the skill before installing it and start a new agent session if needed.

### Configuration and files

Files use these locations; the cache is created when needed:

| Purpose | XDG location | Default when the variable is unset |
| --- | --- | --- |
| TOML configuration | `$XDG_CONFIG_HOME/reading-relay/config.toml` | `~/.config/reading-relay/config.toml` |
| Ledger, preferences, and writer lock | `$XDG_DATA_HOME/reading-relay` | `~/.local/share/reading-relay` |
| Logs | `$XDG_STATE_HOME/reading-relay` | `~/.local/state/reading-relay` |
| Downloaded PDF cache | `$XDG_CACHE_HOME/reading-relay` | `~/.cache/reading-relay` |
| Reading surface | `reading_dir` | `~/Documents/Reading` |

Empty or relative XDG environment variables use the defaults. XDG locations apply
on all supported operating systems. Initialization records the resolved directories
in TOML; edit that file to move an existing installation. Changing environment
variables alone does not migrate an existing ledger.

**Enable iCloud Drive's Desktop & Documents syncing** if you want the default
Documents path to appear on your iPad. Creating the folder does not enable iCloud.
In Files on the iPad, find the folder under iCloud Drive and keep the PDFs
downloaded before reading offline. Verify an actual PDF opens with networking off.

Custom roots can be supplied on first initialization:

```sh
uv run reading-relay init \
  --reading-dir ~/Documents/Reading \
  --notebook-root ~/Research \
  --download-root ~/Downloads/Papers
```

`--config /absolute/path/config.toml` goes before the subcommand. To change an
existing setup, edit its TOML configuration explicitly. `reading_dir`, `data_dir`,
`state_dir`, and `cache_dir` must be separate, non-nested directories. Keep the
ledger, state, and cache outside cloud-synced folders.

## Read and give feedback on iPad

```text
Reading/
|-- Up Next/
|   |-- Attention Is All You Need [rr-7b4a9c2e6d1f8035].pdf
|   `-- Scaling Laws for Neural Language Models [rr-3e8c0a6b9f124d57].pdf
|-- Read/             completed papers
|-- Deferred/         eligible again after the cooldown
|-- Not Now/          excluded until explicitly restored
|-- Reading List.pdf  recommendation reasons and suggested reading scope
|-- Status.txt        full Unicode text, evidence, and diagnostics
`-- Ledger.json       exported reading history and preferences
```

Move a PDF in Files to give feedback. The next run records the destination in the
ledger and fills any vacancy. `Deferred` defaults to 30 days. `Not Now` excludes
that paper, not its entire research topic. Returning a PDF to `Up Next` explicitly
restores it. Keep the `[rr-…]` identifier in its filename; the descriptive title
may be renamed. Keep one managed copy per identifier.

Deletion **does not mean read**. Missing PDFs hold their slots because an offline
iPad, iCloud eviction, and deletion cannot be reliably distinguished. Restore the
file or use an explicit `mark` command to release the slot. Conflicting copies
produce a diagnostic rather than an inferred decision. PDF annotations are kept:
Reading Relay does not replace existing PDFs, and requeuing a deferred paper uses
its managed annotated copy when available.

The generated reports and hidden `.reading-relay.json` ownership file are managed
outputs. Do not edit or remove the ownership file. Unknown PDFs and duplicate
copies in `Up Next` each count against capacity, including uppercase `.PDF` files.
Use `add` to manage unknown papers explicitly. The three-paper limit applies to
automatic publication; manually moving extra papers into `Up Next` does not cause
Reading Relay to delete them.

## Conversational control and CLI commands

A conversational coding assistant can operate the same CLI. It should inspect
`list`, resolve the paper's exact ID, and issue the explicit command. Preferences
are durable natural-language instructions; conversation history is not required.

```sh
uv run reading-relay prefer 'Favor accessible introductions to causal inference.'
uv run reading-relay prefer 'Pause GPU performance topics until I explicitly resume them.'
uv run reading-relay preferences
uv run reading-relay forget 2

uv run reading-relay add https://arxiv.org/abs/1706.03762
uv run reading-relay add ~/Downloads/article.pdf --title 'A useful introduction' --queue
uv run reading-relay mark arxiv:1706.03762 read
uv run reading-relay mark arxiv:1706.03762 deferred --until 2026-12-01
uv run reading-relay mark arxiv:1706.03762 excluded
uv run reading-relay mark arxiv:1706.03762 queued
uv run reading-relay run --reconcile-only
```

`--queue` and `mark … queued` are explicit choices and bypass model ranking, but
still respect capacity and PDF validation. They can restore a read or excluded
paper. Adding a paper alone preserves its existing reading status. `prefer` changes
future selection; it does not evict the current queue. Use an explicit status
command to release a current slot. `list --json` exposes current ledger records;
`list --all` includes undisplayed candidates.

A topic preference such as “pause X” influences the model. Paper-level read,
deferral, and exclusion states are deterministic eligibility constraints. No
recommendation engine can establish that a paper is unread from its presence in
a references directory; mark previously read papers explicitly.

## Discovery and selection

Source roots are explicit configuration. Notebook roots default to an empty array;
the download inbox defaults to `~/Downloads/Papers`. The dedicated inbox keeps unrelated
downloads, invoices, and review documents out of the ranker.
Move selected PDFs there, use `add` for individual files anywhere, or
configure another root explicitly. Notebook discovery uses lab-notebook content,
reference PDFs, bibliographies, experiment entries, glossaries, plans, and
priorities. All inputs are read-only. PDF title and text extraction is bounded.

Notebook discovery recognizes this layout:

```text
configured-root/
  lab-notebook/                   optional notebook directly under the root
  project-a/lab-notebook/
  project-b/lab-notebook/
```

A root can also point directly at a `lab-notebook` or `notebook` directory.
Within notebooks, discovery reads Markdown (`.md`, `.markdown`) and BibTeX (`.bib`).
Reference PDFs are scanned in directories named `reference`, `references`, or
`bibliography`. Markdown and BibTeX can link local PDFs inside the configured root.
Hidden directories, symlinks, and build/output directories are skipped.
It does not search arbitrary project nesting depths or interpret `.ipynb` or
Marimo source files. Add a deeper notebook as a separate root. Download roots
scan only PDFs directly inside the configured directory.

Modification time identifies activity to inspect. The ledger preserves the last
content-change time across whitespace-only edits. The model sees bounded activity
excerpts, candidate context, source paths, and explicit preferences. It weighs
current relevance, foundational value, approachability, and marginal value, with
an explanation tied to the supplied evidence. It can leave slots empty. It cannot
search for arbitrary papers or invent candidate IDs.

Discovery reserves part of its notebook budget for bibliographies, glossaries, and
reference PDFs, so recent experiment activity cannot bury older foundational
sources. Review ledgers, changelogs, and check ledgers do not count as research
activity. Papers without a known PDF location are retained but excluded from
automatic selection until a local copy or authorized PDF URL is available.

Only known local PDFs or explicit authorized PDF locations can be published. URL
metadata without an available PDF stays in the ledger. Downloads must be HTTPS,
size-bounded, and valid, non-encrypted PDFs. No paywall or access-control bypass is
attempted. The existing notebook reference-download workflow can prepare local
copies; Reading Relay does not require that skill to be installed.

Each source fails independently. Truncation and failures appear in `Status.txt`
and stderr. Source paths and excerpts are sent to the configured LLM provider;
there is no email ingestion or general web-feed ingestion. Remove sensitive roots
or use a suitable local model command before enabling daily runs.

## Daily operation and limits

The LaunchAgent label is `org.reading-relay.daily`. It runs in your logged-in
macOS user session at 09:00 local time by default. Calendar scheduling can catch
up after sleep, but cannot run while the Mac is off or logged out. This workflow
does not depend on Claude Desktop or the Codex desktop app being open.
The job uses the `PATH` captured by `install` to find the selected harness. Rerun
`install` after changing that executable's location or your shell's `PATH`.
The job permits iCloud placeholders to download when their contents are needed.
This does not keep PDFs downloaded on the iPad; use the Files app for offline access.

Each run:

1. Acquires an exclusive nonblocking lock and reconciles folder moves.
2. Leaves a full queue alone, without invoking the model.
3. Discovers candidates and incorporates research activity if a slot is free.
4. Makes at most one bounded ranker invocation, validates its entire response,
   and publishes selected PDFs without replacing existing ones.
5. Exports the ledger, recommendations, and diagnostics.

Defaults in `config.toml` include `capacity = 3`, `max_files = 600`,
`max_candidates = 120`, `activity_days = 30`, `defer_days = 30`,
`rank_timeout = 180`, `download_timeout = 35`, `run_timeout = 540`, and a 50 MiB
PDF limit. The file budgets cover up to 600 notebook inputs and 150 direct download
PDFs with these defaults. A third of notebook slots is reserved for reference sources.
A command-wide deadline bounds local work. On POSIX, ranker timeouts terminate
the process group. Windows uses best-effort process-tree termination; descendants
whose parent already exited may survive. A simultaneous command fails with an
actionable lock message. Existing recommendations survive source or model failures.

The `command` setting is an argv array, never a shell string. The prompt is supplied
on stdin and the final output must be the documented selection JSON; see
[the integration contract](documents/design.md). Other Codex models, a Claude
subscription-backed OMP command, or a local model adapter can use this interface.
Changing provider is explicit; there is no automatic fallback. Test a new command
manually before leaving it unattended. Keep its tool access disabled.
Use absolute executable and script paths in custom commands; each invocation runs
in a temporary directory rather than in a research project.

```sh
uv run reading-relay schedule
uv run reading-relay uninstall
```

On macOS, installation also creates `~/Applications/Reading Relay.app`, a small
background launcher that identifies the job as **Reading Relay** in Login Items &
Extensions. It uses the same Python environment as the installing command; keep
that environment and repository in place.

Logs are `logs/launchd.stdout.log` and `logs/launchd.stderr.log` beneath `state_dir`.
`Status.txt` reflects the last completed export; inspect its timestamp and launchd
logs if it becomes stale. Uninstall removes the LaunchAgent and its background
launcher. To preserve history across reinstallations, retain `data_dir` and the
reading folder.
Back up `data_dir`; `Ledger.json` is an audit export, not an automatic restore
format. The software refuses to attach a different ledger to an owned folder.

## Configuration

`init` writes a TOML file using the XDG configuration location above. Use
`--config /absolute/path/config.toml` to select another file. Other commands require
that file to exist; a misspelled path does not initialize a second ledger.
Paths accept `~`; relative TOML paths resolve against the configuration file's
directory. Use forward slashes or TOML literal strings for Windows paths.

The principal settings are:

| Key | Default | Use |
| --- | --- | --- |
| `reading_dir` | `~/Documents/Reading` | User-visible PDFs and reports |
| `notebook_roots` | `[]` | Research roots or direct notebook directories |
| `download_roots` | `["~/Downloads/Papers"]` | Flat PDF inboxes |
| `data_dir`, `state_dir`, `cache_dir` | XDG locations above | Ledger, operational state, and disposable downloads |
| `harness` | `"codex"` | `"codex"`, `"omp"`, or `"custom"` |
| `model` | `""` | Harness model name or provider/model selector; empty uses the harness default |
| `command` | `[]` | Explicit argv override; required for a custom harness |
| `capacity` | `3` | Maximum automatic queue size, 1–10 |
| `defer_days` | `30` | Cooldown after an observed deferral |
| `activity_days` | `30` | Recent research-context window |
| `hour`, `minute` | `9`, `0` | Default local-time schedule when installing |
| `max_files`, `max_candidates` | `600`, `120` | Discovery and ranking budgets |
| `rank_timeout`, `download_timeout`, `run_timeout` | `180`, `35`, `540` | Deadlines in seconds; total run limit at most 900 |
| `max_pdf_bytes` | `52428800` | Maximum PDF size, 50 MiB |

For example, change these entries in the generated file:

```toml
notebook_roots = ["~/Research", "~/Work/project/lab-notebook"]
download_roots = ["~/Downloads/Papers"]
capacity = 3
defer_days = 14
harness = "codex"
model = ""
command = []
```

An explicit `command` takes precedence over `harness` and `model`.
Leave it empty to use the selected harness adapter. For OMP, set `harness = "omp"`
and select a provider/model available to your authenticated OMP installation.
The `custom` adapter requires a command implementing the ranker JSON contract.
Model access and billing belong to the chosen harness and provider.
After changing schedule settings, rerun `install`; editing TOML does not alter
an already loaded LaunchAgent.

To relocate storage, stop scheduled and manual runs, back up and copy the entire
`data_dir`, then update the TOML paths. Retain the reading folder and its ownership
file. Run `run --reconcile-only` to verify attachment before reinstalling the daily
job. An empty replacement data directory cannot attach to an existing owned
reading folder. The cache is disposable; remove or relocate it while no run is active.

### Recover a lost ledger

1. Stop the daily job with `uninstall` and stop any manual runs.
2. Restore the entire `data_dir` from backup. Keep the existing reading folder
   and its `.reading-relay.json` ownership file.
3. Run `run --reconcile-only` and inspect the result before reinstalling the daily job.

Without a ledger backup, there is no supported automatic reattachment or import
from `Ledger.json`. Keep the original reading folder, ownership file, and annotated
PDFs intact as an archive. Initialize a separate empty reading folder and a new
`data_dir` using a new configuration file. This starts a new history; deleting the
ownership file does not recover the lost decisions.

## Development

Install [just](https://github.com/casey/just#installation) for the combined check.

```sh
uv sync --group dev
just check
```

The tests use temporary directories and do not need a model subscription or iCloud.
They cover identity, bounded discovery, strict selection validation, lifecycle
transitions, preserved PDFs, and failure boundaries. Live LLM quality and iPad sync
require separate end-to-end checks.

[Design and integration contract](documents/design.md) ·
[Design principles](documents/principles.md) · [MIT license](LICENSE)
