# Design principles

## Preserve attention and reading work

Capacity is a ceiling, not a target that justifies weak recommendations. Fill a
vacancy without rotating unread papers. Preserve annotations and original source
files. Never refresh an existing PDF merely because a newer copy is available.

## Explicit intent outranks inference

Read, excluded, and not-yet-eligible deferred papers are filtered before ranking.
Natural-language preferences outrank inferred interests in the model's brief.
Filesystem deletion and PDF presence are insufficient evidence of reading state.
Ambiguity holds state until the user supplies an explicit action.

## Activity is evidence, not priority

Recent edits identify material worth examining. Their semantic content, active
questions, and explicit user instructions determine relevance. Whitespace-only
edits retain their previous content-change time. The first scan cannot recover
historical intent from timestamps; recommendations should identify that uncertainty.
Foundational importance, approachability, and immediate relevance remain separate
judgments rather than one opaque numerical score.

Reserve source-discovery capacity for bibliographies, glossaries, and archived
references. An old foundational reference should remain discoverable when a busy
notebook produces hundreds of recent experiment edits. Mechanical review and
change logs are not evidence of a research interest merely because they changed.

## Keep model authority small

Models select from known candidate IDs and explain their reasoning. Deterministic
code owns eligibility, process execution, PDF validation, filenames, publication,
and reading history. Candidate text is data. A model refusal, invented ID, or
malformed response must not become a fabricated recommendation or provider switch.

## Make each recommendation inspectable

Store the source paths, paper URL, selection rationale, category, and reading
scope. Preserve provenance across projects. Do not equate heuristic title matches
with exact identity. Do not silently discard a source that failed to parse or
claim that an uninspected paper is approachable.

## Separate local authority from synchronized interaction

Keep the live ledger and process lock local. Use ordinary PDF moves as the remote
interaction, and export a human-readable report and ledger snapshot. Cloud sync
is asynchronous; local absence cannot establish remote deletion. Report staleness
and ownership conflicts rather than reconstructing authority from partial files.

## Keep unattended work bounded and reversible

Use one model call per replenishment run and explicit time and size ceilings.
Avoid broad filesystem write permissions, automatic provider fallback, or writes
to research notebooks. Scheduling should invoke the same CLI that a human can
exercise. Uninstalling the schedule must preserve papers and reading history.
