# U1 Slicer Bridge

**Repository:** https://github.com/taylormadearmy/u1-slicer-bridge

A self-contained Docker service that lets you upload a 3MF file, slice it, and send it to your Snapmaker U1 printer — all from a web browser on your phone. Everything you need (slicer, profiles, web UI) is bundled in the Docker image. Just run the container and open the page.

> **WARNING — Use at your own risk.** This software is provided as-is with no warranty. It generates G-code that controls a 3D printer with a heated bed and hot end. Incorrect G-code can damage your printer, cause fires, or create other hazards. Always supervise your printer while it is running. The authors accept no responsibility for property damage, injury, or any other consequences arising from the use of this software.

## Overview

U1 Slicer Bridge provides a complete workflow for 3D printing with the Snapmaker U1:

```
upload .3mf/.stl → validate plate → configure → slice → preview → print
```

**Key Features:**
- Upload `.3mf` and `.stl` files (including MakerWorld/Bambu Studio and PrusaSlicer exports)
- **MakerWorld URL import** — paste a MakerWorld link to preview model info and download 3MF directly (optional, with cookie auth for unlimited downloads)
- Multi-plate 3MF support with per-plate validation and visual selection
- Multicolour/multi-extruder slicing (up to 4 extruders)
- Automatic plate validation (270x270x270mm build volume)
- Slicing with Snapmaker OrcaSlicer fork (v2.2.4)
- Interactive 3D G-code preview (gcode-preview + Three.js) with orbit rotation, multi-color rendering, layer slider, and zoom/pan controls
- 3-way setting modes per parameter (use file defaults / Orca defaults / custom override)
- Configurable slicing options (wall count, infill, supports, brim, skirt, prime tower)
- Persistent extruder presets and filament library with JSON profile import/export
- Temperature and build plate type overrides per job
- Print control via Moonraker (send to printer, pause/resume/cancel)
- Printer status page with live progress, temperatures, and state monitoring
- Configurable Moonraker URL (persisted in database)
- File management (browse, download, delete uploads and sliced files)
- PWA support (installable on desktop, with Share Target API for future Android companion app)
- Modern web UI with settings modal and 3-step slice workflow

## Quick Start

**Prerequisites:** Docker and Docker Compose. Snapmaker U1 with Moonraker API enabled (optional — needed for print control only).

```bash
# Pre-built images (fastest)
curl -O https://raw.githubusercontent.com/taylormadearmy/u1-slicer-bridge/master/docker-compose.prod.yml
docker compose -f docker-compose.prod.yml up -d
# Open http://localhost:8234
```

Or build from source:

```bash
git clone https://github.com/taylormadearmy/u1-slicer-bridge.git
cd u1-slicer-bridge
cp .env.example .env          # Optional: edit to set MOONRAKER_URL for printer control
docker compose up -d --build
# Open http://localhost:8080
```

Verify: `curl http://localhost:8000/healthz`

See **[DEPLOY.md](DEPLOY.md)** for the full deployment guide.

## Architecture

- **Docker-first:** Everything runs via `docker compose`
- **Snapmaker OrcaSlicer:** Uses Snapmaker's fork (v2.2.4) for Bambu file compatibility
- **Plate-based workflow:** Preserves MakerWorld/Bambu Studio arrangements, no object normalization
- **LAN-first:** Designed for local network use, no authentication required
- **Deterministic:** Pinned slicer version, per-job sandboxing, no global slicer state

### Services

| Service | Path | Description |
|---------|------|-------------|
| API | `apps/api/` | FastAPI backend - upload, parse, slice, job management |
| Web | `apps/web/` | Nginx + static frontend (Alpine.js) |
| PostgreSQL | via compose | Persistent storage for uploads, jobs, filaments, presets |

## 3MF Sanitization

MakerWorld/Bambu Studio 3MF files contain settings that cause OrcaSlicer validation errors or crashes. The profile embedder (`apps/api/app/profile_embedder.py`) automatically detects and fixes these before slicing.

### Bambu File Detection & Geometry Rebuild

Bambu Studio files use a geometry format that Snapmaker OrcaSlicer can't parse directly. When Bambu-specific metadata is detected (`slice_info.config`, `filament_sequence.json`, `model_settings.config`), the file is rebuilt with trimesh to extract clean geometry before profile embedding.

### Bambu Metadata Stripping

These Bambu-specific files are dropped during embed — they serve no purpose for OrcaSlicer and can cause crashes:

| File | Reason |
|------|--------|
| `Metadata/slice_info.config` | Bambu-specific, not used by Orca |
| `Metadata/cut_information.xml` | Bambu-specific |
| `Metadata/filament_sequence.json` | Can crash OrcaSlicer |
| `Metadata/plate*/top*/pick*` | Preview images, reduce file size |

### Parameter Clamping

Bambu exports can contain out-of-range values (typically `-1` meaning "auto") that OrcaSlicer rejects. These are clamped to safe minimums:

| Parameter | Minimum | Why |
|-----------|---------|-----|
| `raft_first_layer_expansion` | `0` | `-1` not in valid range |
| `tree_support_wall_count` | `0` | `-1` not in range `[0,2]` |
| `prime_volume` | `0` | Negative values rejected |
| `prime_tower_brim_width` | `0` | `-1` not in range `[0,2147483647]` |
| `prime_tower_brim_chamfer` | `0` | Negative values rejected |
| `prime_tower_brim_chamfer_max_width` | `0` | Negative values rejected |
| `solid_infill_filament` | `1` | `0` is not a valid extruder index |
| `sparse_infill_filament` | `1` | `0` is not a valid extruder index |
| `wall_filament` | `1` | `0` is not a valid extruder index |

### Wipe Tower Position Clamping

Inherited `wipe_tower_x`/`wipe_tower_y` values can place the prime tower outside the 270mm bed. The sanitizer computes a safe range based on `prime_tower_width` + `prime_tower_brim_width` + margin and clamps both axes to keep the tower within bounds.

### Model Settings Sanitization

`Metadata/model_settings.config` can carry stale `plater_name` values from deleted plates in Bambu Studio. Snapmaker OrcaSlicer v2.2.4 segfaults on these — they are cleared to empty strings before slicing.

### G-code Quality Defaults

| Setting | Value | Why |
|---------|-------|-----|
| `enable_arc_fitting` | `1` | Without this, G-code is 5-6x larger (linear G1 moves instead of G2/G3 arcs) |
| `layer_gcode` | `G92 E0` | Required for relative extruder addressing |
| `time_lapse_gcode` | removed | Not applicable to Snapmaker U1 |
| `machine_pause_gcode` | removed | Not applicable to Snapmaker U1 |

## Storage Layout

All data is stored under `/data`:

| Directory | Purpose |
|-----------|---------|
| `uploads/` | Uploaded .3mf files |
| `slices/` | Generated G-code files |
| `logs/` | Per-job log files |
| `cache/` | Temporary processing files |

## Milestones

### Complete

| ID | Feature |
|----|---------|
| M0 | Skeleton - Docker, FastAPI, services |
| M1 | Database - PostgreSQL with uploads, jobs, filaments |
| M2 | Moonraker integration - Health check, configurable URL, print status |
| M3 | Object extraction - 3MF parser (handles MakerWorld files) |
| M4 | Plate validation - Preserves arrangements |
| M5 | Direct slicing with filament profiles |
| M6 | Slicing - Snapmaker OrcaSlicer v2.2.4, Bambu support |
| M7 | Preview - Interactive G-code layer viewer |
| M7.1 | Multi-plate support - Detection and visual selection |
| M7.2 | Build plate type & temperature overrides |
| M8 | Print control - Send to printer, pause/resume/cancel via Moonraker |
| M9 | Sliced file access - Browse and view G-code files |
| M10 | File deletion - Delete old uploads and sliced files |
| M11 | Multifilament support - Colour detection, auto-assignment, override |
| M13 | Custom filament profiles - JSON import/export with slicer settings passthrough |
| M12 | 3D G-code viewer - gcode-preview + Three.js (orbit, multi-color, arc support) |
| M15 | Multicolour viewer - Colour legend in viewer |
| M16 | Flexible filament assignment - Override colour per extruder |
| M17 | Prime tower options - Configurable prime tower settings |
| M18 | Multi-plate visual selection - Plate names and preview images |
| M20 | G-code viewer zoom - Zoom/pan controls, scroll-wheel zoom, fit-to-bed |
| M21 | Upload/configure loading UX - Progress indicators |
| M22 | Navigation consistency - Standardized actions across UI |
| M23 | Common slicing options - Wall count, infill, supports, brim, skirt |
| M24 | Extruder presets - Default settings per extruder |
| M25 | API performance - Metadata caching, async slicing, batch 3MF reads |
| M28 | Printer status page - Always-accessible status overlay with live monitoring |
| M27 | Concurrency hardening - UUID temp files, slicer process semaphore |
| M29 | 3-way setting modes - Per-setting model/orca/override with file detection |
| M30 | STL upload support - Wrap STL in 3MF via trimesh for slicing |
| M26 | MakerWorld URL import - Paste URL to preview and download 3MF with optional cookie auth |

### Not Yet Implemented

| ID | Feature |
|----|---------|
| M14 | Multi-machine support |
| M19 | Slicer selection (OrcaSlicer vs Snapmaker Orca) |
| M31 | Android companion app - Lightweight WebView wrapper with share target |

**Progress:** 30.7 / 32 milestones complete (96%)

## Non-Goals (v1)

- Mesh repair or geometry modifications
- Cloud dependencies (LAN-first by default)

## Documentation

- [AGENTS.md](AGENTS.md) - AI coding agent operating manual (authoritative milestone tracker)
- [MEMORY.md](MEMORY.md) - Bug fix journal with root causes and solutions
- [TESTING.md](TESTING.md) - Testing procedures and API endpoint reference

## Development

This project is designed to be built with AI coding agents (Claude Code in VS Code). See [AGENTS.md](AGENTS.md) for development guidelines, invariants, and definition of done.

### Rebuilding After Changes

Web files are baked into the Docker image at build time:
```bash
docker compose build web && docker compose up -d web
```
Then hard refresh browser (Ctrl+Shift+R).

For API changes:
```bash
docker compose build api && docker compose up -d api
```

## License

This project is licensed under the [GNU Affero General Public License v3.0](LICENSE) (AGPL-3.0), matching the OrcaSlicer upstream license.

See [THIRD-PARTY-LICENSES.md](THIRD-PARTY-LICENSES.md) for attribution of all bundled and vendored dependencies.

## Author

Maintained by taylormadearmy
