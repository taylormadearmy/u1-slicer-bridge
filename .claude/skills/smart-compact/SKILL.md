---
name: smart-compact
description: Hybrid session compaction. Writes a structured handoff markdown AND copies the raw session JSONL to a gitignored folder, so the next session can resume from a curated summary with the full transcript as fallback. Local-only (relies on ~/.claude/projects/ JSONL transcripts; web sessions should rely on the harness's built-in compaction). Use with no args to compact, or "resume" to load the latest handoff in a fresh session.
---

# smart-compact

A hybrid session-compaction skill: writes a curated handoff doc, copies the raw session JSONL alongside it, then hands control back so the user can `/clear` and resume in a fresh session.

## Modes

This skill takes one optional argument:

- **No args** (`/smart-compact`) — Compact mode. Write the handoff and raw transcript, then tell the user what to do next.
- **`resume`** (`/smart-compact resume`) — Resume mode. Read the most recent handoff into context. Run this in a fresh session right after `/clear`.

Pick the mode from the argument. If unclear, ask the user.

## Environment check

This skill is **local-only**. It depends on the Claude Code session transcript being on disk at `~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`. On Claude Code on the web the transcript is managed server-side and this path will not exist.

Before doing anything, check the environment:

```bash
test -d ~/.claude/projects && echo "local" || echo "cloud"
```

If the directory doesn't exist (cloud environment), tell the user:

> smart-compact is local-only — it needs the session JSONL at `~/.claude/projects/`, which doesn't exist in cloud containers. Web sessions get automatic compaction from the harness; rely on that instead.

Stop. Do not proceed.

## Compact mode (`/smart-compact`)

### 1. Set up the sessions folder

Ensure `.claude/sessions/` exists in the project (relative to the current working directory):

```bash
mkdir -p .claude/sessions
```

Ensure it is gitignored. Check `.gitignore` for an entry covering `.claude/sessions/`. If not present, append:

```
# Claude Code session compaction artifacts (smart-compact skill)
.claude/sessions/
```

### 2. Locate the current session JSONL

The current session's transcript lives at `~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`. The encoded cwd is the absolute project path with `/` and `.` replaced by `-`.

Compute it and find the newest matching JSONL (the active session):

```bash
ENCODED_CWD=$(pwd | sed 's|[/.]|-|g')
SESSION_DIR="$HOME/.claude/projects/$ENCODED_CWD"
ls -t "$SESSION_DIR"/*.jsonl 2>/dev/null | head -1
```

If multiple JSONL files exist and you're unsure which is the current session, prefer the one most recently modified. If nothing is found, list `~/.claude/projects/` and ask the user which directory matches their project — the encoding scheme can vary.

### 3. Build the timestamp and slug

```bash
TIMESTAMP=$(date +%Y-%m-%d-%H%M)
```

Pick a short kebab-case slug (3-6 words) describing the session's main topic. Examples: `fix-slicer-bambu-temps`, `add-copies-grid-layout`, `debug-viewer-modelviewmatrix`. Derive it from what the session was actually about — read your own recent context.

Final filenames:

- `.claude/sessions/<TIMESTAMP>-<slug>.md` — handoff
- `.claude/sessions/<TIMESTAMP>-<slug>.raw.jsonl` — raw transcript copy

### 4. Copy the raw transcript

```bash
cp "<path-from-step-2>" ".claude/sessions/<TIMESTAMP>-<slug>.raw.jsonl"
```

### 5. Write the handoff markdown

Write a curated handoff at `.claude/sessions/<TIMESTAMP>-<slug>.md` using this structure. Fill it from your own context — be specific, include file paths with line numbers, name actual decisions, list real next steps. Do not pad with generic prose.

```markdown
# <Topic title — what this session was about>

**Date:** <YYYY-MM-DD HH:MM>
**Branch:** <current git branch>
**Raw transcript:** [<TIMESTAMP>-<slug>.raw.jsonl](./<TIMESTAMP>-<slug>.raw.jsonl)

## Goal

<1-3 sentences: what the user was trying to accomplish.>

## Current state

<Where things stand right now. What works, what doesn't, what's half-done.>

## Decisions made

- <Decision 1 — and why>
- <Decision 2 — and why>
- <Ruled out: X because Y>

## Files touched

- `path/to/file.py:123` — <what changed and why>
- `path/to/other.ts:45-67` — <what changed and why>

## Open questions

- <Question the user hasn't answered yet>
- <Ambiguity to resolve before continuing>

## Next steps

1. <Concrete next action>
2. <Then this>
3. <Then this>

## Context the next agent needs

<Anything non-obvious: gotchas discovered, error messages seen, commands that failed and why, environment quirks, things you tried that didn't work and shouldn't be retried.>

## How to dig deeper

If this handoff doesn't cover what you need, read the raw JSONL:

```
Read .claude/sessions/<TIMESTAMP>-<slug>.raw.jsonl
```

It contains the full conversation including all tool calls and results.
```

### 6. Report back to the user

Tell the user, concisely:

- Path to the handoff MD
- Path to the raw JSONL
- Next step: run `/clear`, then `/smart-compact resume` in the fresh session

Example:

> Wrote handoff to `.claude/sessions/2026-05-16-1430-fix-bambu-temps.md` and raw transcript to `.claude/sessions/2026-05-16-1430-fix-bambu-temps.raw.jsonl`. Run `/clear` to start fresh, then `/smart-compact resume` to load the handoff.

Do not run `/clear` yourself — it's a CLI control command, not a tool. The user has to do it.

## Resume mode (`/smart-compact resume`)

### 1. Find the most recent handoff

```bash
ls -t .claude/sessions/*.md 2>/dev/null | head -1
```

If none exists, tell the user there's no handoff to resume and stop.

### 2. Read it

Use the Read tool on that path. Read the full file — these are short by design.

### 3. Acknowledge and orient

Briefly confirm what you loaded (the topic, branch, and next steps from the handoff) so the user knows you're caught up. Keep it to 3-4 lines. Then wait for the user's instruction or start on the first item under "Next steps" if they told you to continue.

### 4. If you need more detail later

The handoff links to a `.raw.jsonl` file alongside it. Read that file directly when the handoff is missing something specific (e.g. exact error text, a tool result body). Don't read it preemptively — it's large; only pull it in when needed.

## Conventions

- Slugs are kebab-case, lowercase, no spaces or punctuation other than `-`.
- Timestamps are local time, `YYYY-MM-DD-HHMM` format.
- Never commit anything from `.claude/sessions/` — it's gitignored on purpose. Raw transcripts can contain sensitive info.
- Don't delete old handoffs automatically. The user can prune manually.
