# CLAUDE.md — Project Context for Claude Code

## Key Documentation

- [AGENTS.md](AGENTS.md) — **Authoritative source.** Milestone tracker, coding conventions, architecture rules, and non-goals. Read this first for any implementation work.
- [README.md](README.md) — Project overview, tech stack, Docker setup, milestone summary.
- [TESTING.md](TESTING.md) — Testing procedures and API endpoint reference.

## Architecture (quick reference)

Docker Compose stack: **api** (Python/FastAPI) + **web** (nginx + Alpine.js) + **postgres** + **redis**

- `apps/api/app/` — Backend: routes, slicer integration, 3MF parsing, profile embedding
- `apps/web/` — Frontend: single-page app (index.html, app.js, api.js, viewer.js)
- `test-data/` — 3MF test fixtures with attribution

## Workflow

Upload 3MF → validate plate bounds → configure filament/settings → slice with Snapmaker OrcaSlicer → preview G-code → download

## Milestones & Plans

Milestone status lives in [AGENTS.md](AGENTS.md) (section: "Milestones Status"). Optional/future milestones with detailed plans:

- **M26 MakerWorld link import** — Detailed feasibility research and implementation plan in `memory/milestone-makerworld-integration.md`

## Critical Conventions

- Temperatures must be string arrays for Orca: `["200"]` not `[200]`
- Web container uses COPY not volumes — rebuild after changes: `docker compose build --no-cache web`
- API container caches aggressively — rebuild with: `docker compose build --no-cache api`
- Arc fitting (`enable_arc_fitting = 1`) is required to avoid 5-6x G-code bloat
- Bambu Studio files need trimesh rebuild before slicing (auto-detected in profile_embedder.py)
- Full 3MF sanitization docs (parameter clamping, metadata stripping, wipe tower bounds) in [README.md § 3MF Sanitization](README.md)
