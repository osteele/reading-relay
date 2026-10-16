# Install Reading Relay

Instructions for a coding agent installing the CLI and its portable Agent Skill.
Read the repository's README and `skills/reading-relay/SKILL.md` before making
changes. Use the user's existing installation when present; preserve its TOML,
ledger, preferences, reading folder, and scheduled job.

## 1. Install the CLI

Requires Git, Python 3.11+, and [uv](https://docs.astral.sh/uv/). Check which are
already available. If a prerequisite is missing, explain the required installation
and use the platform's documented method with the user's approval.

Clone `https://github.com/osteele/reading-relay.git` into a persistent directory
chosen for this installation. Do not assume a particular source-tree layout or
replace an existing directory. From that checkout:

```sh
uv tool install --editable .
reading-relay --help
```

The editable tool installation keeps the command pointing at this checkout.
Keep both the checkout and uv's tool environment in place. If the command is not
on PATH, `uv tool dir --bin` prints its executable directory; use the absolute
executable and tell the user which directory to add to PATH. Do not silently
rewrite shell startup files.

## 2. Install the agent skill

The CLI and skill are separate installs. `reading-relay install` installs the
**daily macOS job**, not the application or agent skill.

Use the [skills CLI](https://github.com/vercel-labs/skills) if Node.js or Bun is
available. Select only the agents the user uses; for Claude Code and Codex, from
the checkout:

```sh
npx skills add . --skill reading-relay -g -a claude-code -a codex -y
```

With Bun, use `bunx skills` in place of `npx skills`. Omit `-g` to install into a
project instead of the user's global skill directories. On Windows, add `--copy`
if symlink creation is unavailable. Review any existing skill with the same name;
do not overwrite unrelated instructions.

Without that installer, copy or symlink `skills/reading-relay/` into the selected
agent's skills directory. Typical global directories are `~/.claude/skills/`
for Claude Code and `~/.agents/skills/` for Codex. Reuse any existing shared-skill
symlink arrangement instead of creating a second convention. Copying also works
on Windows without symlink privileges. Start a fresh agent session if it does not
rediscover skills automatically.

## 3. Configure the reading queue

Ask for any choices the user has not supplied:

- Reading-folder location; for an iPad, choose a folder actually synced by iCloud.
- Notebook roots and/or PDF download folders. There is no default notebook root.
- Authenticated harness: Codex, OMP, or a custom argv command; optional model.

Do not sign in on the user's behalf, request credentials in chat, enable iCloud,
or scan unrelated personal folders. Explain that `run` sends source paths and
excerpts to the configured harness's provider before its first invocation.

For a new installation, run `reading-relay init` with the agreed directory and
harness flags. Consult `reading-relay init --help` for the current options. Custom
commands are configured as an argv array in TOML. Existing installations should
keep their configuration; inspect it before changing any path or harness.

The default configuration is `~/.config/reading-relay/config.toml`, subject to
`XDG_CONFIG_HOME`. Durable data, logs, and cache use their corresponding XDG
locations. Keep these outside the cloud-synced reading folder. For another
configuration, put `--config /absolute/path/config.toml` before the subcommand.
Never delete an ownership marker or substitute an empty ledger to bypass an
attachment error; restore a full ledger backup or use a separate reading folder.

## 4. Verify, then offer scheduling

Run the real installed executable:

```sh
reading-relay scan
reading-relay list --all
reading-relay run --reconcile-only
```

`scan` updates the local candidate ledger and reports but makes no model call or
paper download. Inspect its roots and diagnostics with the user. An empty inbox
is valid. After the user agrees to provider access, run `reading-relay run` and
inspect the generated `Reading List.pdf` and `Status.txt` in their reading folder.
Do not invent a successful selection when there are no candidates or a source
fails.

On macOS, offer the daily job after the first selection has been checked:

```sh
reading-relay install --hour 9 --minute 0
reading-relay schedule
```

Only install it if requested or accepted. On Linux and Windows, use manual runs
or the user's chosen scheduler. For iPad use, the user must verify that a PDF
opens with networking off; a desktop check cannot establish offline availability.

Report the installed executable, skill scope/agents, configuration and reading
paths, observed scan result, and whether a scheduled job was installed. Preserve
existing reading history throughout.
