# U1 Slicer Bridge

**Repository:** https://github.com/taylormadearmy/u1-slicer-bridge

Self-hostable, Docker-first service for Snapmaker U1 3D printing workflow.

## Overview

U1 Slicer Bridge provides a complete workflow for 3D printing with the Snapmaker U1:

```
upload → select → normalize → slice → preview → print
```

**Key Features:**
- Upload `.3mf` files and select objects
- Automatic mesh normalization (placement on bed, bounds checking)
- Slicing with upstream Orca Slicer
- G-code preview and analysis
- Print control via Moonraker API

## Architecture

- **Docker-first:** Everything runs via `docker-compose`
- **Upstream Orca only:** Always uses official Orca Slicer (no forks)
- **LAN-first security:** Designed for local network use
- **Deterministic:** Pinned versions, per-bundle sandboxing, no global state

### Services

- **API** (`apps/api/`) - FastAPI service for workflow orchestration
- **Worker** (`apps/worker/`) - Background processing for heavy tasks
- **Web** (`apps/web/`) - Frontend interface

## Quick Start

### Prerequisites

- Docker and Docker Compose
- Snapmaker U1 with Moonraker API enabled

### Setup

1. Clone the repository:
   ```bash
   git clone https://github.com/taylormadearmy/u1-slicer-bridge.git
   cd u1-slicer-bridge
   ```

2. Configure environment:
   ```bash
   cp .env.example .env
   # Edit .env and set your APP_SECRET_KEY and MOONRAKER_URL
   ```

3. Start services:
   ```bash
   docker compose up -d --build
   ```

4. Verify health:
   ```bash
   curl http://localhost:8000/healthz
   ```

## Storage Layout

All data is stored under `/data`:
- `uploads/` - Uploaded .3mf files
- `normalized/` - Normalized meshes
- `bundles/` - Object bundles for slicing
- `slices/` - Generated G-code
- `logs/` - Per-job log files

## Documentation

- [AGENTS.md](AGENTS.md) - AI coding agent operating manual
- [TESTING.md](TESTING.md) - Testing procedures
- [TEST_RESULTS.md](TEST_RESULTS.md) - Test execution results
- [docs/spec.md](docs/spec.md) - Workflow specification

## Development

This project is designed to be built with AI coding agents (Claude Code). See [AGENTS.md](AGENTS.md) for development guidelines and invariants.

### Milestones

- M0: Skeleton
- M1: Database
- M2: Moonraker integration
- M3: Object extraction
- M4: Normalization
- M5: Bundles/filaments
- M6: Slicing
- M7: Preview
- M8: Print control

## Non-Goals (v1)

- MakerWorld scraping
- Per-object filament assignment
- Mesh repair beyond placement normalization
- Forked Snapmaker Orca versions

## License

Private repository - All rights reserved

## Author

Maintained by taylormadearmy
