# AGENTS.md — AI Coding Agent Operating Manual (u1-slicer-bridge)

This repo is intended to be built with an AI coding agent (Claude Code in VS Code). Treat this document as binding.

---

## Project purpose

Self-hostable, Docker-first service for Snapmaker U1:

upload `.3mf` → select objects → normalize → slice with upstream Orca → preview → print via Moonraker.

---

## Non-goals (v1)

- No MakerWorld scraping
- No per-object filament assignment
- No mesh repair beyond placement normalization
- No Snapmaker Orca fork
- LAN-first by default

---

## Definition of Done (DoD)

A change is complete only if:

### Docker works
`docker compose up -d --build` must succeed.

### Health works
`curl http://localhost:8000/healthz` returns JSON.

### Deterministic
- Orca version pinned
- per-bundle sandbox
- no global slicer state

### Logs
Every job writes `/data/logs/{job_id}.log`.

### Errors
Errors must be understandable and visible in API/UI.

---

## Core invariants (do not break)

### Docker-first
Everything runs via compose.

### Upstream Orca only
Always.

### Normalize ourselves
Never depend on GUI placement.

### LAN-first security
Secrets encrypted via `APP_SECRET_KEY`.

### Storage layout
Under `/data`:
- uploads
- normalized
- bundles
- slices
- logs

---

## How Claude should behave

Prefer:
- small safe steps
- minimal moving parts
- explicit over magic
- worker for heavy tasks

Avoid:
- new infra unless necessary
- hidden state
- plaintext secrets

---

## Milestones (in order)

M0 skeleton  
M1 database  
M2 moonraker  
M3 object extraction  
M4 normalization  
M5 bundles/filaments  
M6 slicing  
M7 preview  
M8 print control

---

## Normalization contract

Objects must:
- sit on bed
- be inside bounds
- or fail with a clear message

---

## G-code contract

Compute:
- bounds
- layers
- tool changes

Warn if out of bounds.

---

## Logging contract

All subprocess output must go to `/data/logs`.
