# AGENTS.md — AI Coding Agent Operating Manual (u1-slicer-bridge)

This repo is intended to be built with an AI coding agent (Claude Code in VS Code). Treat this document as binding.

---

## Project purpose

Self-hostable, Docker-first service for Snapmaker U1:

upload `.3mf` → validate plate → slice with Snapmaker OrcaSlicer → preview → print via Moonraker.

**Current Status:** Fully functional upload-to-print workflow with printer status monitoring.

---

## Non-goals (v1)

- No MakerWorld scraping (M26 adds optional authenticated link import)
- No per-object filament assignment (single filament per plate)
- No mesh repair or geometry modifications
- No multi-material/MMU support
- LAN-first by default (no cloud dependencies)

---

## Milestones Status

### Foundation & Core Pipeline
✅ M0 skeleton - Docker, FastAPI, services  
✅ M1 database - PostgreSQL with uploads, jobs, filaments  
✅ M3 object extraction - 3MF parser (handles MakerWorld files)  
✅ M4 ~~normalization~~ → plate validation - Preserves arrangements  
✅ M5 ~~bundles~~ → direct slicing with filament profiles  
✅ M6 slicing - Snapmaker OrcaSlicer v2.2.4, Bambu support  

### Device Integration & Print Execution
✅ M2 moonraker - Health check, configurable URL, print status polling
✅ M8 print control - Send to printer, pause/resume/cancel, printer status page

### File Lifecycle & Job Management
✅ M9 sliced file access - Browse and view previously sliced G-code files  
✅ M10 file deletion - Delete old uploads and sliced files  

### Multi-Plate & Multicolour Workflow
✅ M7.1 multi-plate support - Multi-plate 3MF detection and selection UI  
✅ M11 multifilament support - Color detection from 3MF, auto-assignment, manual override, multi-extruder slicing  
✅ M15 multicolour viewer - Show color legend in viewer with all detected/assigned colors  
✅ M16 flexible filament assignment - Allow overriding color per extruder (separate material type from color)
✅ M17 prime tower options - Add configurable prime tower options for multicolour prints
✅ M18 multi-plate visual selection - Show plate names and preview images when selecting plates

### Preview & UX
✅ M7 preview - Interactive G-code layer viewer (superseded by M12 3D viewer)
✅ M12 3D G-code viewer - Interactive 3D preview using gcode-preview + Three.js (orbit rotation, multi-color, arc support, build volume)
✅ M20 G-code viewer zoom - Zoom in/out buttons, scroll-wheel zoom toward cursor, click-drag pan, fit-to-bed reset
✅ M21 upload/configure loading UX - Progress indicators during upload preparation
✅ M22 navigation consistency - Standardized actions across UI
✅ M28 printer status page - Always-accessible printer status overlay with live progress, temps, and controls

### Slicing Controls & Profiles
✅ M7.2 build plate type & temperature overrides - Set bed type per filament and override temps at slice time
✅ M13 custom filament profiles - Import/export OrcaSlicer JSON profiles with advanced slicer settings passthrough
✅ M24 extruder presets - Preconfigure default slicing settings and filament color/type per extruder
❌ M19 slicer selection - Choose between OrcaSlicer and Snapmaker Orca for slicing
✅ M23 common slicing options - Wall count, infill pattern/density, supports, brim, skirt
✅ M29 3-way setting modes - Per-setting model/orca/override with file print settings detection

### Performance
✅ M25 API performance - Metadata caching at upload, async slicing (asyncio.to_thread), batch 3MF reads, profile caching
✅ M27 concurrency hardening - UUID temp files in profile_embedder, asyncio.Semaphore caps concurrent slicer processes (configurable MAX_CONCURRENT_SLICES)

### Platform Expansion
❌ M14 multi-machine support - Support for other printer models beyond U1
✅ M26 MakerWorld link import - Paste a MakerWorld URL to preview model info and download 3MF. Optional feature (off by default), cookie auth for unlimited downloads, browser-like request headers
✅ M30 STL upload support - Accept .stl files via trimesh STL→3MF wrapper. Single-filament only (no multi-plate, no color detection, no embedded print settings). OrcaSlicer slices the wrapped 3MF as normal.
❌ M31 Android companion app - Lightweight WebView wrapper (~50 lines Kotlin, ~1-2MB APK). Provides standalone app launch (no browser chrome), share target for MakerWorld URLs, configurable server IP. Works over plain HTTP on LAN. Built via GitHub Actions, distributed as APK from Releases.

### Build Plate & Workflow Enhancements
✅ M32 Multiple copies - Grid layout engine for duplicating objects on the build plate (1-100 copies, auto spacing, metadata patching for Orca compatibility)
❌ M33 Move objects on build plate - Interactive drag-to-position objects before slicing
✅ M34 Vertical layer slider - Side-mounted vertical range input for G-code layer navigation
✅ M35 Settings backup/restore - Export/import all settings (filaments, presets, defaults) as portable JSON

**Current:** 33.7 / 36 complete (94%)

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

### Snapmaker OrcaSlicer fork
Use Snapmaker's v2.2.4 fork for Bambu file compatibility.

### Preserve plate arrangements
Never normalize objects - preserve MakerWorld/Bambu layouts.

### LAN-first
Designed for local network use, no authentication required.

### Storage layout
Under `/data`:
- uploads/ - Uploaded 3MF files
- slices/ - Generated G-code
- logs/ - Per-job logs
- cache/ - Temporary processing files

---

## How Claude should behave

Prefer:
- small safe steps
- minimal moving parts
- explicit over magic
- **always run regression tests after code changes** (deploy first, then test)
- offer targeted vs full test suite choice to the user

Avoid:
- new infra unless necessary
- hidden state
- plaintext secrets
- skipping tests after changes (even "small" ones can break things)

### Release & Docker Images

After pushing commits, **always ask the user if they want to tag a release** to trigger Docker image builds on GHCR.

**IMPORTANT: Never push directly to prod.** Always release as beta first (`v1.x.x-beta.1`), then promote to stable (`v1.x.x`) after testing.

```bash
# Beta release (always do this first)
git tag v1.x.x-beta.1 && git push origin v1.x.x-beta.1

# Stable/prod release (only after beta has been tested)
git tag v1.x.x && git push origin v1.x.x
```

This triggers `.github/workflows/release.yml` which builds and pushes `ghcr.io/taylormadearmy/u1-slicer-bridge-{api,web}`. Beta tags get `:beta`, stable tags get `:latest`. Production users pulling `docker-compose.prod.yml` get updates via `:latest`.

### Third-Party License Attribution

When adding vendored libraries, CDN dependencies, or new pip/npm packages:

1. **Update [THIRD-PARTY-LICENSES.md](THIRD-PARTY-LICENSES.md)** with the library name, version, license type, copyright holder, and source URL.
2. Vendored files in `apps/web/lib/` must retain their original license headers (do not strip `@license` comments from minified files).
3. All dependencies must be compatible with the project's AGPL-3.0 license.

### Documentation Maintenance

**CRITICAL:** As you make fixes, implement features, or discover issues:

1. **Update AGENTS.md** when you:
   - Discover new invariants or patterns
   - Learn about system behavior that affects future work
   - Identify new constraints or requirements
   - Complete milestones or major features

2. **Update MEMORY.md** when you:
   - Fix bugs (document root cause + solution)
   - Discover configuration issues
   - Learn about performance or optimization patterns
   - Find differences between expected/actual behavior
   - Identify recurring problems and their solutions

**Keep these files living documents.** Don't wait to be asked - update them proactively as you work.

---

### Regression Testing (MANDATORY)

**You MUST run regression tests after every code change.** Do not skip this step. Deploy changes to Docker containers first, then test against live services.

**Testing workflow:**
1. Make code changes
2. Deploy to Docker (`docker compose build --no-cache <service> && docker compose up -d <service>`)
3. Run the appropriate tests (see table below)
4. If tests fail, fix and re-run — do not leave failing tests

**After making changes, offer the user a choice:**
- "I can run the **fast regression** (`npm run test:fast`, ~5 min, 84 tests incl. 1 slice sanity), **targeted tests** (`npm run test:<suite>`), or the **full suite** (`npm test`, ~60 min). Which do you prefer?"
- For very targeted changes (1-2 files, clear scope), default to running targeted tests or `test:fast`.
- For broad changes (multiple files, API + frontend, refactors), recommend `test:fast` or full suite.

```bash
# Fast regression (84 tests incl. calicube slice sanity, ~5 min)
npm run test:fast

# Quick smoke tests (always run, ~15 seconds, no slicer needed)
npm run test:smoke

# Run all tests (requires Docker services running with slicer, ~60 min)
npm test
```

**When to run which tests:**

| Changed files | Run these tests |
|--------------|-----------------|
| Any web file (`index.html`, `app.js`, `api.js`, `viewer.js`) | `npm run test:smoke` + relevant feature suite |
| API routes or backend logic | `npm run test:smoke` + `npm run test:slice` |
| Upload/parser changes | `npm run test:upload` + `npm run test:multiplate` |
| Filament/preset changes | `npm run test:settings` |
| Viewer changes | `npm run test:viewer` |
| Multicolour/profile embedder | `npm run test:multicolour` + `npm run test:multicolour-slice` |
| Slice settings/overrides | `npm run test:slice-overrides` |
| Error handling/edge cases | `npm run test:errors` |
| File deletion or management | `npm run test:files` |
| File-level print settings | `npm run test:file-settings` |
| Test file changes only | The affected suite(s) |
| Any significant or cross-cutting change | `npm test` (full suite) |

**After rebuilding web container** (`docker compose build web && docker compose up -d web`), always run at least `npm run test:smoke` to verify the UI still loads.

**When to add new tests:**
- Every bug fix should have a test that would have caught it
- Every new feature should have at least one happy-path test
- Add tests to existing spec files when possible; create new spec files only for new feature areas

**Common regressions to watch for:**
- Click handlers broken after UI changes
- Event propagation issues (@click.stop)
- State not updating after async operations
- API endpoints returning wrong data types
- Alpine.js x-show/x-if conditions becoming stale
- `page.waitForFunction(fn, arg, options)` — second param is `arg`, not `options`

---

### Plate Validation Contract

Entire plate must:
- Fit within 270x270x270mm build volume
- Return clear warning if exceeds bounds
- Preserve original object arrangements from 3MF

---

## G-code contract

Compute:
- bounds
- layers
- tool changes

Warn if out of bounds.

---

## Web Container Deployment

**CRITICAL:** The web service uses `COPY` in Dockerfile, not volume mounts.

After editing any web files (`index.html`, `app.js`, `api.js`, `viewer.js`):
```bash
docker compose build web && docker compose up -d web
```

Then users must hard refresh browser (Ctrl+Shift+R).

**Why:** Files are baked into the image at build time (see `apps/web/Dockerfile` lines 7-10).

---

## Multi-Plate 3MF Support (NEW - M7.1)

The system now supports multi-plate 3MF files (like BambuStudio multi-plate exports):

### Problem Solved
Multi-plate files were being treated as a single giant plate, causing:
- False "Width exceeds build volume" warnings  
- No way to choose which plate to slice

### Implementation
1. **Multi-Plate Parser** (`multi_plate_parser.py`):
   - Detects multi-plate files by parsing `<build>` section
   - Extracts individual plates with transform matrices
   - Returns plate objects with positions and validation info

2. **Enhanced Upload Endpoint**:
   - Returns `is_multi_plate: true` for multi-plate files
   - Includes individual plate validation results
   - Maintains backward compatibility

3. **New API Endpoints**:
   - `GET /uploads/{id}/plates` - Get plate information
   - `POST /uploads/{id}/slice-plate` - Slice specific plate

4. **Updated Web UI**:
   - Shows plate selection interface for multi-plate files
   - Validates individual plates against build volume
   - Allows selecting specific plate to slice

### User Workflow
1. Upload multi-plate 3MF file
2. See all plates with validation (which ones fit build volume)
3. Select specific plate to slice
4. Slice only the selected plate

### Files Modified
- `apps/api/app/multi_plate_parser.py` - NEW (includes bounding box fix)
- `apps/api/app/routes_upload.py` - Updated
- `apps/api/app/plate_validator.py` - Updated  
- `apps/api/app/routes_slice.py` - Updated
- `apps/web/index.html` - Updated (loading indicator, plate selection UI)
- `apps/web/app.js` - Updated (plate loading, state management, slice fix)
- `apps/web/api.js` - Updated

### Known Issues & Fixes Applied

**Fixed: Bounding Box Calculation**
- **Problem**: Multi-plate files showed "Exceeds build volume" incorrectly
- **Root Cause**: Transform matrices were being incorrectly applied to bounding box calculation
- **Fix**: Fixed matrix transformation in `multi_plate_parser.py` to correctly calculate bounds
- **Result**: Dragon Scale (80mm x 40mm x 20mm) now correctly shows "Fits build volume" ✅

**Fixed: Loading State**
- **Problem**: "Loading plate information..." stuck indefinitely on fresh upload
- **Root Cause**: `loadPlates()` called without uploadId in HTML template; no loading state tracking
- **Fix**: Added `platesLoading` state variable; proper loading/loaded state transitions

**Fixed: Wrong File Sliced**
- **Problem**: After selecting one upload then another, wrong file would slice
- **Root Cause**: `startSlice()` used `this.selectedUpload` which could change during async operations
- **Fix**: Capture upload ID/filename at start of `startSlice()` before async operations

**Fixed: Fresh Upload Plate Detection**
- **Problem**: Plates not loading after fresh upload
- **Root Cause**: Frontend made redundant API call instead of using plates from upload response
- **Fix**: Now uses plates data directly from upload response (no extra API call needed)

**Fixed: Wrong Plate Sliced (Wrong File Size & Time)**
- **Problem**: When slicing a specific plate, it sliced ALL plates - causing 100MB+ file size, 22 hour print time
- **Root Cause**: `slice_plate` endpoint was using original full 3MF instead of extracting selected plate
- **Fix**: Added `extract_plate_to_3mf()` in `multi_plate_parser.py` to extract only the selected plate before slicing
- **Result**: Single plate now slices correctly (~16MB, ~3.4 hours for Dragon Scale)

**Fixed: Stale Build Volume Warnings**
- **Problem**: Existing uploads showed incorrect "Exceeds build volume" warnings even after fixes
- **Root Cause**: Bounds were validated at upload time and stored; re-validation wasn't happening
- **Fix**: GET /upload/{id} now re-validates bounds each time using current validator
- **Result**: Existing uploads now show accurate build volume status ✅

**Fixed: G-code Viewer Colors**
- **Problem**: G-code viewer only showed hardcoded blue color, ignoring filament colors
- **Root Cause**: Viewer didn't receive or use filament color data
- **Fix**: 
  1. Added `filament_colors` column to `slicing_jobs` table (schema.sql)
  2. Slice endpoints now store filament colors as JSON array
  3. GET /jobs/{id} returns filament_colors
  4. viewer.js fetches job status and uses filamentColors[0] for extrusion color
- **Result**: G-code viewer now shows the filament color used for slicing ✅

**Fixed: Multicolor Viewer Legend (M15)**
- **Problem**: No way to see all colors when slicing multi-color files
- **Fix**: Added color legend below viewer showing all extruder colors (E1, E2, etc.)
- **Files**: viewer.js, index.html
- **Result**: Color legend shows when multiple colors detected ✅

**Fixed: Flexible Filament Assignment (M16)**
- **Problem**: Couldn't override filament color - color was tied to filament profile
- **Fix**: 
  1. Added `filament_colors` parameter to slice requests (array of hex colors)
  2. Color picker in UI to override detected colors per extruder
  3. Priority: user override > detected colors > filament default
- **Files**: routes_slice.py, api.js, app.js, index.html
- **Result**: Can now assign any color to any extruder ✅

**Multicolour Stability Update (Assignment-Preserving Path Added)**
- **What changed**: Added an assignment-preserving profile embedding strategy for Bambu multicolour files in `profile_embedder.py`.
  - Preserves original `model_settings.config` semantics
  - Replaces incompatible custom G-code with Snapmaker-safe machine scripts
  - Sanitizes known invalid project settings values
  - Normalizes filament arrays to match assigned extruder count
- **Result on validation file**: `calib-cube-10-dual-colour-merged.3mf` now slices successfully with real tool changes (`T0`, `T1`) instead of single-tool output.
- **Safety behavior remains**: Slice endpoints still fail fast if multicolour is requested and output is only `T0`.
  - Error: `Multicolour requested, but slicer produced single-tool G-code (T0 only).`
- **Tracking tool**: `apps/api/app/multicolor_matrix.py` remains available for additional strategy validation on other model variants.

**Fixed: >4 Color Support (Color-to-Extruder Mapping)**
- **Problem**: `Dragon Scale infinity.3mf` reports 7 detected color regions while U1 supports max 4 extruders; previously rejected with fallback to single-filament mode.
- **Fix**:
  1. Frontend maps all detected colors to available extruders (E1-E4) via round-robin assignment. Users can reassign any color to any extruder via dropdown.
  2. Multiple colors can share an extruder (no swap logic — direct assignment).
  3. Backend accepts up to 4 `filament_ids` and applies `extruder_remap` to collapse >4 source extruders in the 3MF pre-slice. Per-filament arrays are padded to match.
- **Result**: Files with any number of colors are fully supported in multicolour mode with user-configurable extruder mapping.

**Active-Color Detection Update (Assignment-Aware)**
- **Problem**: Some Bambu files report many palette/default colors in metadata, which overstated actual extruders in use.
- **Fix**: `parser_3mf.py::detect_colors_from_3mf()` now prioritizes model-assigned extruders from `Metadata/model_settings.config` and maps them to active colors from `project_settings.config`.
- **Result**: Dragon now reports only assigned colors (e.g., 3 active colors instead of 7 metadata colors), improving UI assignment behavior and validation decisions.

**Fixed: SEMM (Painted) Multicolour Files Detected as Single-Colour**
- **Problem**: Bambu-style painted files (e.g., SpeedBoatRace, colored 3DBenchy) with `paint_color` per-triangle attributes showed only 1 detected colour, hiding the multicolour UI.
- **Root Cause**: `detect_colors_from_3mf()` early-returned with only the colour at the single object-level `extruder="1"` assignment, missing the other filament colours used by the painting data.
- **Fix**:
  1. `parser_3mf.py`: Detects `paint_color` data in mesh triangles (via `_has_paint_data()`) regardless of `single_extruder_multi_material` flag — some exports set it to "0" even for painted files. Returns all `filament_colour` entries when paint data is present, preventing phantom `extruder_colour` from mixing in.
  2. `profile_embedder.py`: Added `_has_paint_data_zip()` to detect paint data. Enables SEMM mode (`single_extruder_multi_material=1`) and disables `ooze_prevention` (incompatible with SEMM+wipe tower) for painted multicolour files.
  3. `routes_slice.py` (both endpoints): `required_extruders` and `multicolor_slot_count` now use `max(active_extruders, detected_colors)` so painted files auto-expand correctly.
- **Result**: Painted files correctly report all filament colours. SEMM mode enabled for paint processing. Note: Snapmaker Orca v2.2.4 has limited per-triangle paint segmentation support.

**Fixed: Bambu Plate ID Mismatch (slice-plate 500 error)**
- **Problem**: Slicing Bambu files via `/slice-plate` returned `invalid plate id 2, total 1` error.
- **Root Cause**: Bambu files define plates via `Metadata/plate_*.json` (typically only `plate_1.json`), but our `multi_plate_parser` maps `<item>` elements to "plates" (1-indexed). UI requested plate 2 but Orca only has plate 1.
- **Fix**: `routes_slice.py` always uses `effective_plate_id = 1` for all Bambu files when calling Orca's `--slice` flag.

**Fixed: Alpine.js Double Initialization**
- **Problem**: Responsive tests timing out; all API calls firing twice during page load.
- **Root Cause**: `<body x-data="app()" x-init="init()">` called `init()` twice — Alpine v3 auto-calls `init()` on data objects from `x-data`, and `x-init` called it again. With Moonraker configured but unreachable, two sequential 10s health checks = 20s, exceeding the 15s `networkidle` timeout.
- **Fix**: Removed redundant `x-init="init()"`. Responsive tests now use `domcontentloaded` instead of `networkidle`.

**Implemented: Layer-Based Tool Change Detection (MultiAsSingle Dual-Colour)**
- **Problem**: Bambu files with mid-print filament swaps stored in `custom_gcode_per_layer.xml` (e.g., Fiddle Balls "dual colour" plates) were detected as single-colour.
- **Fix**:
  1. `parser_3mf.py`: Added `detect_layer_tool_changes()` to parse tool change entries (type=2) per plate. `detect_colors_from_3mf()` now includes colours from these entries.
  2. `profile_embedder.py`: Added `_has_layer_tool_changes()`. Files with layer tool changes now use the assignment-preserving embed path (trimesh rebuild would destroy `custom_gcode_per_layer.xml`).
- **Result**: Fiddle Balls detects 2 colours and slices with real `T0`/`T1` tool changes at the specified layer height.

**Multicolour Crash Handling Update (Clear Failure Mode)**
- **Problem**: Certain files (e.g., Dragon/Poker variants) can still segfault in Snapmaker Orca v2.2.4 when multicolour slicing is attempted.
- **Fix**: Slice endpoints now convert multicolour segfaults into a clear, actionable 400 error instead of returning raw crash output.
- **Error**: `Multicolour slicing is unstable for this model in Snapmaker Orca v2.2.4 (slicer crash). Try single-filament slicing for now.`

**Fixed: Bambu `plater_name` Segfault Trigger (Dragon/Poker Variants)**
- **Problem**: Two almost-identical files behaved differently: one multicolour slice crashed while the other succeeded.
- **Root Cause**: `Metadata/model_settings.config` carried stale `plater_name` metadata from old/deleted plate context (e.g., `Dual Colour`). Snapmaker Orca v2.2.4 can segfault on this metadata in assignment-preserving multicolour path.
- **Fix**: `profile_embedder.py` now sanitizes `model_settings.config` and clears non-empty `plater_name` values before slicing.
- **Result**:
  - `Dragon Scale infinity-1-plate-2-colours.3mf` now slices multicolour successfully (`T0`, `T1`).

**Fixed: Multi-Plate `slice-plate` Multicolour Crash Path**
- **Problem**: `POST /uploads/{id}/slice-plate` could still crash/fail for Poker/Dragon while full-file slice succeeded.
- **Root Cause**: Plate extraction/rebuild path altered model structure before slicing, reintroducing Orca instability on assignment-preserving multicolour files.
- **Fix**: Slice selected plates directly using Orca's built-in plate selector (`--slice <plate_id>`) on the embedded source 3MF, instead of extracting plate geometry first.
- **Files**:
  - `apps/api/app/routes_slice.py` (slice-plate workflow)
  - `apps/api/app/slicer.py` (`slice_3mf(..., plate_index=...)`)
- **Result**: Poker/Dragon multi-plate `slice-plate` now succeeds with real tool changes (`T0`, `T1`).

**Adjusted: Bambu Negative-Z Upload Warning Noise**
- **Problem**: Some Bambu exports showed `Objects extend below bed` warnings despite valid slicing/printing paths.
- **Root Cause**: Raw 3MF source offsets (`source_offset_z` in `model_settings.config`) can produce negative scene bounds in validation, even when slicer placement is valid.
- **Fix**: `plate_validator.py` now suppresses below-bed warnings for likely Bambu source-offset artifacts while keeping build-volume checks unchanged.
- **Result**: Bambu exports no longer show misleading below-bed warnings in upload/configure flows.

**Fixed: Sliced History Not Updating Immediately**
- **Problem**: Newly completed slices were not appearing in "Sliced Files" until browser refresh.
- **Root Cause**: Synchronous-complete slice path in frontend did not refresh jobs list.
- **Fix**: `app.js::startSlice()` now calls `loadJobs()` immediately when slice returns `status=completed`.

**Fixed: Estimated Time Showing as 0 for New Slices**
- **Problem**: Many newly sliced files showed `estimated_time_seconds = 0`.
- **Root Cause**: G-code metadata parser only handled older `estimated printing time (normal mode) = ...` comment format.
- **Fix**: `gcode_parser.py` now also parses newer summary formats such as:
  - `model printing time: ...`
  - `total estimated time: ...`
- **Result**: New slices now store and display non-zero estimated times when present in G-code comments.

**Investigated: Extruder Slot Selection vs Tool IDs**
- Frontend now preserves unassigned extruder slots in slice payload and backend supports assignment remap metadata.
- On tested models, Snapmaker Orca can still compact active tools to low indices (`T0/T1`) in output even after remap.
- Treat non-E1/E2 slot requests as best-effort until a deterministic Orca-compatible mapping strategy is found.

**Implemented: Explicit Extruder Slot Remap in Output G-code**
- **Problem**: Selecting E2/E3 in UI still produced compact tool commands (`T0/T1`) in generated G-code.
- **Fix**:
  1. Frontend now sends `extruder_assignments` in slice/slice-plate payloads and preserves slot positions.
  2. Backend remaps model-assigned extruders via `model_settings.config` when embedding.
  3. After slicing, backend rewrites compacted tool commands to requested slots in final G-code (`T*`, `M620 S* A`, `M621 S* A`).
- **Files**:
  - `apps/web/app.js`
  - `apps/web/api.js`
  - `apps/api/app/routes_slice.py`
  - `apps/api/app/profile_embedder.py`
  - `apps/api/app/slicer.py`
- **Result**: E2/E3 selections now produce non-default tool IDs in output (e.g., `T1`/`T2`) instead of collapsing to `T0`/`T1`.

**Fixed: E1/E3 Temperature Difference False Failure in UI Payload Path**
- **Problem**: Selecting non-adjacent slots (e.g., E1 and E3) from browser UI could fail with `Cannot print multiple filaments which have large difference of temperature together...` even when user selected same material profile.
- **Root Cause**: Frontend gap-fill for unused slots preferred the first global default filament (could be non-PLA), injecting a mismatched placeholder filament into `filament_ids`.
- **Fix**: In `app.js`, placeholder slot fill now prefers the first filament already selected for this slice before falling back to global defaults.
- **Result**: Browser path now slices E1/E3 without false temperature mismatch errors.

**Fixed: G-code Viewer Layer Parsing Regression**
- **Problem**: Viewer showed `No layer data returned for range 0-20` for valid G-code.
- **Root Cause**: Backend layer parser only detected `;LAYER_CHANGE`, but generated G-code used `; CHANGE_LAYER` (and some files use `;LAYER:<n>`).
- **Fix**: Updated `routes_slice.py::_parse_gcode_layers()` to detect all common layer markers.
- **Result**: `/jobs/{id}/gcode/layers` returns layer geometry again; viewer renders normally.

**Fixed: Fresh Upload Multi-Plate False Warning Path**
- **Problem**: Fresh multi-plate uploads could still show combined-scene "Exceeds build volume" warnings even when individual plates fit.
- **Root Cause**: Upload response used combined-scene validation warnings without per-plate suppression.
- **Fix**: `routes_upload.py` now performs per-plate checks during upload and suppresses combined "exceeds" warnings when any plate fits.
- **Result**: Fresh upload behavior now matches re-validation behavior from `GET /upload/{id}`.

**Implemented: Multi-Plate Visual Selection (M18)**
- **What changed**:
  1. Plate metadata now includes `plate_name` (resource object name fallback: `Plate N`).
  2. Added `GET /uploads/{id}/plates/{plate_id}/preview` to serve embedded plate preview images when present.
  3. Plate selection UI now renders plate cards with name + preview image (or `No preview` fallback).
- **Files**: `multi_plate_parser.py`, `routes_slice.py`, `index.html`
- **Result**: Selecting plates is more visual and informative, especially for large multi-plate projects.

**Implemented: Upload/Configure Loading UX (M21)**
- **Problem**: After upload bytes finished, users saw no indication while server parsing/validation was still running.
- **Fix**: Added upload phase states (`uploading` → `processing`) and an explicit "Preparing file" indicator in Step 1.
- **Files**: `api.js`, `app.js`, `index.html`
- **Result**: Users now see continuous progress from transfer through server-side preparation.

**Implemented: Navigation Consistency (M22)**
- **Fix**: Standardized secondary navigation labels to `Back to Upload` (configure) and `Start New Slice` (complete).
- **Files**: `index.html`
- **Result**: Navigation language is now consistent across the workflow.

**Fixed: Fresh Upload Plate Preview Gap**
- **Problem**: Plate previews could be missing right after a fresh upload, while the same file showed previews when selected later from Recent Uploads.
- **Root Cause**: Fresh-upload flow used plate data from upload response, which did not include preview URLs from the plates endpoint.
- **Fix**: Frontend now always reloads plate metadata via `GET /uploads/{id}/plates` after upload completes.
- **Result**: Fresh and historical upload paths now show the same plate preview behavior.

**Implemented: Embedded Upload Preview Endpoints and List Thumbnails**
- **What changed**:
  1. Added `GET /uploads/{id}/preview` to serve the best embedded 3MF preview image (Explorer-style thumbnail when present).
  2. Enhanced preview detection to include common embedded names (`thumbnail`, `preview`, `cover`, `plate`, `top`, `pick`).
  3. Upload and sliced-file lists now render thumbnail previews using upload-based preview URLs.
- **Files**: `routes_slice.py`, `index.html`
- **Result**: Users see consistent visual previews in Recent Uploads and Sliced Files.

**Improved: Single-Plate Preview + Plate Loading UX**
- **Fix**:
  1. Configure step now shows single-plate preview when available.
  2. Plate loading state now uses progress-style treatment (`Loading plate information and preview...`) to match upload UX.
- **Files**: `app.js`, `index.html`
- **Result**: Single-plate workflow feels consistent and clearer during wait states.

**Implemented: Common Slicing Options (M23)**
- **What changed**:
  1. Added Configure controls for `wall_count` and `infill_pattern` alongside existing infill density.
  2. Slice request payloads now include `wall_count` and `infill_pattern` for both full-file and plate-specific slicing.
  3. Backend applies overrides to Orca project settings (`wall_loops`, `sparse_infill_pattern`, `sparse_infill_density`).
- **Files**: `index.html`, `app.js`, `api.js`, `routes_slice.py`
- **Result**: Users can tune core print strength/speed behavior from the UI without editing profiles.

**Implemented: Extruder Presets (M24)**
- **What changed**:
  1. Added persistent extruder preset storage for E1-E4 (`filament_id` + `color_hex`) and one-row global slicing defaults.
  2. Added API endpoints:
     - `GET /presets/extruders` to fetch presets + slicing defaults.
     - `PUT /presets/extruders` to save presets + slicing defaults.
  3. Configure UI now includes an `Extruder Presets` section with `Save as Defaults`.
  4. Presets are auto-loaded and applied for new uploads (single and multi-filament paths).
- **Files**: `schema.sql`, `main.py`, `api.js`, `app.js`, `index.html`
- **Result**: Users can preconfigure default filament/color per extruder and reuse standard slicing settings across jobs.

**Implemented: Settings Tab Architecture (Machine Defaults vs Per-Job Overrides)**
- **Problem**: Machine-level defaults were mixed into per-slice Configure flow, making occasional global settings too easy to change during a single job.
- **What changed**:
  1. Added top-level `Upload` / `Settings` tabs in the web UI.
  2. Moved machine defaults editing (extruder presets + default slicing settings) to `Settings`.
  3. Configure now shows a `Machine Defaults` summary plus an `Override settings for this job` toggle.
  4. Slice payload behavior now follows: defaults when override is off, per-job override values when on.
- **Files**: `app.js`, `index.html`
- **Result**: Cleaner workflow separation between machine setup and per-job customization.

**Refined: Printer Defaults + Override Defaults UX**
- **What changed**:
  1. Renamed UI terminology from `Machine Defaults` to `Printer Defaults`.
  2. Opening `Override settings for this job` now initializes all override fields from current printer defaults.
  3. Override controls now show inline `(... printer default)` hints for quick comparison.
  4. Multicolour filament/extruder override lives under job overrides only (not in detected-colors header).
- **Files**: `app.js`, `index.html`
- **Result**: Clearer defaults model and faster per-job tuning with less accidental mismatch.

**Tidied: Filament Library UX + Default Fallbacks**
- **Problem**: Filament source/default behavior felt unclear, and fallback could appear to choose unexpected materials.
- **What changed**:
  1. `GET /filaments` ordering now prioritizes defaults and PLA entries before name-sort.
  2. Frontend filament fallback now prefers: extruder preset -> `is_default` -> first PLA -> first available.
  3. Settings section renamed to `Filament Library` with explicit starter-library explanation and `Initialize Starter Library` label.
  4. Filament cards now show `Default` badge and target extruder preset slot (`E1-E4`) for clarity.
- **Files**: `main.py`, `app.js`, `index.html`
- **Result**: More predictable automatic filament selection and clearer understanding of where library entries come from.

**Implemented: Filament Library CRUD Guardrails**
- **What changed**:
  1. Added filament update/delete/default endpoints in API.
  2. Added Settings UI controls for Add/Edit/Delete/Set Default in Filament Library.
  3. Added delete safety check: prevent deleting filaments assigned to extruder presets until reassigned.
  4. Deleting the current default now auto-promotes a replacement (prefers PLA) to keep sane fallback behavior.
- **Files**: `main.py`, `api.js`, `app.js`, `index.html`
- **Result**: Filament profiles are manageable in-app with clearer safety behavior and fewer accidental misconfigurations.

**Implemented: JSON Filament Profile Import (M13 Foundation)**
- **What changed**:
  1. Added `POST /filaments/import` to import JSON filament profiles into the Filament Library.
  2. Added `Import Profile` action in Settings with local file picker (`.json`).
  3. Added `source_type` tracking (`starter` / `manual` / `custom`) and `Custom`/`Starter` badges in UI.
- **Files**: `main.py`, `schema.sql`, `api.js`, `app.js`, `index.html`
- **Result**: Users can start bringing external filament profiles into the app while keeping source provenance visible.

**Expanded: Prime Tower Defaults/Overrides**
- **What changed**: Added additional prime tower controls in Settings + per-job overrides:
  - `prime_volume`
  - `prime_tower_brim_chamfer`
  - `prime_tower_brim_chamfer_max_width`
- **Files**: `main.py`, `schema.sql`, `routes_slice.py`, `api.js`, `app.js`, `index.html`, `profile_embedder.py`
- **Result**: More complete prime-tower tuning from UI without manual profile edits.

**Implemented: Prime Tower Options (M17)**
- **What changed**:
  1. Added machine-default prime tower settings in `Settings` (`enable_prime_tower`, `prime_tower_width`, `prime_tower_brim_width`).
  2. Added per-job prime tower overrides in Configure under `Override settings for this job`.
  3. Slice payloads now include prime tower fields for both full-file and plate slice requests.
  4. Backend applies prime tower overrides to Orca project settings and sanitizes numeric values.
- **Files**: `index.html`, `app.js`, `api.js`, `routes_slice.py`, `main.py`, `schema.sql`
- **Result**: Users can set stable machine-level prime tower behavior and selectively override per print job.

**Fixed: Hollow+Cube Multicolour Config Rejection (`prime_tower_brim_width`)**
- **Problem**: `hollow+cube.3mf` failed in multicolour path with Orca config validation error:
  - `prime_tower_brim_width: -1 not in range [0,2147483647]`
- **Root Cause**: Assignment-preserving embed path reused Bambu `project_settings.config` values but did not sanitize `prime_tower_brim_width`.
- **Fix**: Added sanitizer clamp for `prime_tower_brim_width` (min `0`) in `profile_embedder.py`.
- **Result**: Config-validation failure no longer blocks slicing for this file.

**Adjusted: Plate Slice Multicolour `T0-only` Handling**
- **Problem**: Some multi-plate projects advertise multiple colors at file level, but selected plates are effectively single-tool; strict `T0-only` fail-fast blocked valid plate slices.
- **Fix**: Kept strict `T0-only` fail-fast for full-file multicolour slices, but for `slice-plate` now continue with a warning when output is `T0` only.
- **Files**: `routes_slice.py`
- **Result**: `hollow+cube.3mf` selected-plate multicolour requests now complete instead of failing on `T0-only` guard.

**Fixed: Prime/Wipe Tower Edge Placement From Embedded Bambu Settings**
- **Problem**: Some files could place wipe/prime tower too close to bed edge (e.g., inherited `wipe_tower_x=15`), causing edge-hugging preview/print paths.
- **Root Cause**: Assignment-preserving embed path reused source `wipe_tower_x/y` without considering prime tower footprint width + brim.
- **Fix**: Added dynamic tower-position sanitizer in `profile_embedder.py` that clamps `wipe_tower_x/y` based on estimated tower half-span (`prime_tower_width`, `prime_tower_brim_width`) and U1 bed bounds.
- **Result**: New slices keep tower placement inside safer bed margins.

**Fixed: Multicolour Time Estimate Inflation on Plate Slices**
- **Problem**: Some multicolour plate slices (e.g., `hollow+cube` plate 2) reported ~2 hours when expected around ~45-50 minutes.
- **Root Cause**: Embedded config inherited single-nozzle MMU timing defaults (`machine_load_filament_time=30`, `machine_unload_filament_time=30`), inflating toolchange time estimation for U1 extruder-slot swaps.
- **Fix**: For multicolour requests (`extruder_count > 1`), slice endpoints now override load/unload times to `0` in project settings.
- **Result**: Plate 2 estimate dropped from ~2h to ~50m with prime tower enabled (and ~25m without prime tower), matching expected behavior much better.

### Performance Note
Plate parsing takes ~30 seconds for large multi-plate files (3-4MB). A loading indicator is now shown during this time.

### Testing
Upload "Dragon Scale infinity.3mf" (3 plates):
- ✅ Correctly detects 3 plates
- ✅ Each plate shows "Fits build volume" (was showing "Exceeds" before)
- ✅ Plate selection UI works correctly
- ✅ Slice Now correctly slices selected plate

---

## Build Plate Type & Temperature Overrides (M7.2)

Added ability to set build plate type per filament and override temperatures at slice time.

### Implementation
1. **Database**: Added `bed_type` column to `filaments` table (defaults to "PEI")
2. **API Endpoints**:
   - `GET /filaments` - Returns filaments with `bed_type` field
   - `POST /filaments` - Create filament with optional `bed_type`
   - `POST /filaments/init-defaults` - Initialize default filaments with bed types

3. **Slice Endpoints**:
   - Added optional `nozzle_temp`, `bed_temp`, and `bed_type` to `SliceRequest` and `SlicePlateRequest`
   - When provided, overrides the filament's default temperatures
   - Logs temperature settings used for each slice job

4. **Frontend**:
   - Shows bed type next to filament in dropdown
   - Temperature override section with nozzle and bed temp inputs
   - Build plate type dropdown (PEI, Glass, PC, FR4, CF, PVA)

### Files Modified
- `apps/api/app/schema.sql` - Added bed_type column
- `apps/api/app/main.py` - Added filament CRUD endpoints with bed_type
- `apps/api/app/routes_slice.py` - Added temp overrides to slice requests
- `apps/web/index.html` - Added temp override UI
- `apps/web/app.js` - Added sliceSettings fields
- `apps/web/api.js` - Added temp overrides to slice API calls

### Bug Fixes Applied

**Fixed: Temperature Overrides Not Being Sent**
- **Problem**: User set temperature overrides in UI but they weren't applied
- **Root Cause**: `api.js` was not sending `nozzle_temp`, `bed_temp`, or `bed_type` in the request body
- **Fix**: Added these fields to `sliceUpload()` and `slicePlate()` API calls in `apps/web/api.js`
- **Result**: Temperature overrides now correctly passed to API

**Fixed: Bed Temperature Not Applied in G-code**
- **Problem**: Even when sent, bed temp was 35°C instead of requested value
- **Root Cause**: Printer profile uses `bed_temperature_initial_layer_single` in G-code, but we only set `bed_temperature_initial_layer`
- **Fix**: Added `bed_temperature_initial_layer_single` to filament_settings in routes_slice.py
- **Result**: Bed temp correctly set to requested value (e.g., M140 S70)

---

## Custom Filament Profiles (M13)

Full import/export of OrcaSlicer-compatible filament profiles with advanced slicer settings passthrough.

### Implementation
1. **Import with Slicer Settings Passthrough**:
   - `POST /filaments/import/preview` — Previews profile before import, reports `is_recognized`, `has_slicer_settings`, `slicer_setting_count`, `color_hex`
   - `POST /filaments/import` — Stores profile with advanced settings JSON blob in `slicer_settings` column
   - ~35 OrcaSlicer-native keys are recognized and stored (retraction, fan, flow, density, cost, plate temps, etc.)

2. **Export**:
   - `GET /filaments/{id}/export` — Exports OrcaSlicer-compatible JSON with stored slicer_settings merged back in
   - Round-trip compatible: export -> re-import preserves all settings

3. **Slicer Passthrough**:
   - Both `/uploads/{id}/slice` and `/uploads/{id}/slice-plate` merge stored `slicer_settings` into `filament_settings`
   - `_merge_slicer_settings()` helper handles multi-extruder array broadcasting
   - Settings flow through `profile_embedder.py` into OrcaSlicer config
   - Explicit filament_settings (temps, bed type) take priority over passthrough keys

4. **Frontend**:
   - Import preview shows color swatch, recognition status, slicer settings count badge
   - Unrecognized profiles show amber warning
   - Each filament card has Export button and "Slicer Settings" badge
   - `api.exportFilamentProfile()` triggers browser download of JSON file

### Passthrough Keys (~35)
Retraction: `filament_retraction_length`, `filament_retraction_speed`, `filament_deretraction_speed`, `filament_retract_before_wipe`, `filament_retract_restart_extra`, `filament_retraction_minimum_travel`, `filament_retract_when_changing_layer`, `filament_wipe`, `filament_wipe_distance`, `filament_z_hop`, `filament_z_hop_types`

Fan: `fan_max_speed`, `fan_min_speed`, `overhang_fan_speed`, `overhang_fan_threshold`, `close_fan_the_first_x_layers`, `full_fan_speed_layer`, `reduce_fan_stop_start_freq`, `additional_cooling_fan_speed`

Flow/Material: `filament_max_volumetric_speed`, `filament_flow_ratio`, `filament_density`, `filament_cost`, `filament_shrink`, `slow_down_layer_time`

G-code: `filament_start_gcode`, `filament_end_gcode`

Plate temps: `cool_plate_temp`, `cool_plate_temp_initial_layer`, `textured_plate_temp`, `textured_plate_temp_initial_layer`, `nozzle_temperature_initial_layer`, `bed_temperature_initial_layer`, `bed_temperature_initial_layer_single`

### Files Modified
- `apps/api/app/schema.sql` — Added `slicer_settings TEXT` column
- `apps/api/app/main.py` — Import/export endpoints, `_SLICER_PASSTHROUGH_KEYS`, parser updates
- `apps/api/app/routes_slice.py` — `_merge_slicer_settings()` helper, both slice endpoints updated
- `apps/web/api.js` — `exportFilamentProfile()` with browser download
- `apps/web/app.js` — `exportFilamentProfile()` action
- `apps/web/index.html` — Import preview enhancements, export button, slicer settings badge
- `tests/settings.spec.ts` — Import/export test cases
- `test-data/test-filament-profile.json` — Test fixture

---

## Logging contract

All subprocess output must go to `/data/logs`.

---

## Testing Strategy

### Automated Tests (Playwright)

All tests use Playwright and live in `tests/`. Config is in `playwright.config.ts`.

**Prerequisites:** Docker services running, `npm install`, `npx playwright install chromium`.

```bash
npm run test:fast              # Fast regression (84 tests, ~5 min, incl. slice sanity)
npm test                       # Run ALL tests (122 tests, ~60 min)
npm run test:smoke             # Quick smoke tests (~15s, no slicer needed)
npm run test:upload            # Upload workflow
npm run test:slice             # Slicing end-to-end (slow, needs slicer)
npm run test:slice-overrides   # Setting overrides (temps, walls, infill, prime tower)
npm run test:slice-plate       # Individual plate slicing
npm run test:viewer            # G-code viewer
npm run test:multiplate        # Multi-plate detection and selection
npm run test:multicolour       # Multicolour detection and overrides
npm run test:multicolour-slice # Multi-colour slicing workflow
npm run test:settings          # Settings modal, presets, filament CRUD
npm run test:files             # File management (delete)
npm run test:errors            # Error handling and edge cases
npm run test:file-settings     # File-level print settings detection
npm run test:report            # View HTML test report
```

### Test File Structure

```
tests/
  global-setup.ts           Records baseline upload ID before tests
  global-teardown.ts        Cleans up test-created uploads after all tests
  helpers.ts                Shared utilities (waitForApp, uploadFile, fixture, etc.)
  smoke.spec.ts             Page load, Alpine.js init, header, API health
  api.spec.ts               API endpoint availability and response shapes
  responsive.spec.ts        Desktop/tablet/mobile viewport rendering
  upload.spec.ts            Upload workflow (file → configure → back)
  slicing.spec.ts           Slice end-to-end (configure → slice → complete)
  slice-overrides.spec.ts   Slicing setting overrides (temps, walls, infill, prime tower)
  slice-plate.spec.ts       Individual plate slicing endpoint
  viewer.spec.ts            G-code viewer canvas, controls, metadata API
  multiplate.spec.ts        Multi-plate detection, plate cards, selection
  multicolour.spec.ts       Colour detection, overrides, >4 colour guard
  multicolour-slice.spec.ts Multi-colour slicing workflow
  plate-colors.spec.ts      Per-plate colour detection accuracy
  file-management.spec.ts   Upload/job deletion, preview endpoints
  file-settings.spec.ts     File-level print settings detection
  settings.spec.ts          Settings modal, printer defaults, filament library, presets
  errors.spec.ts            Error handling, edge cases, delete safety
```

### Test Fixtures

Test 3MF files live in `test-data/`:

| File | Purpose |
|------|---------|
| `calib-cube-10-dual-colour-merged.3mf` | Dual-colour calibration cube (fast slice, multicolour) |
| `Dragon Scale infinity.3mf` | Multi-plate file with 3 plates |
| `Dragon Scale infinity-1-plate-2-colours.3mf` | Single plate, 2 colours |
| `Dragon Scale infinity-1-plate-2-colours-new-plate.3mf` | Variant for plater_name bug |
| `Shashibo-h2s-textured.3mf` | Multi-plate, tree supports, outer-only brim, multicolour |
| `PrusaSlicer_majorasmask_2colour.3mf` | PrusaSlicer 2-colour format compatibility |
| `PrusaSlicer-printables-Korok_mask_4colour.3mf` | PrusaSlicer 4-colour from Printables |

### Test Speed Categories

| Suite | Speed | Needs Slicer | Coverage |
|-------|-------|-------------|----------|
| smoke | Fast | No | Page load, Alpine init, header, API health |
| api | Fast | No | All API endpoint shapes and error handling |
| responsive | Fast | No | 3 viewport sizes render correctly |
| upload | Medium | No | Upload flow, configure step, navigation |
| errors | Medium | No | Error handling, edge cases, delete safety |
| settings | Medium | No | Settings modal, presets, filament CRUD |
| file-settings | Medium | No | File-level print settings detection |
| multicolour | Medium | No | Colour detection, overrides, fallbacks |
| plate-colors | Medium | No | Per-plate colour detection accuracy |
| multiplate | Slow | No | Plate detection, cards, selection |
| file-management | Medium | Yes* | Upload/job deletion lifecycle |
| slicing | Slow | Yes | Full slice end-to-end, metadata, download |
| slice-overrides | Slow | Yes | Setting overrides (temps, walls, infill, prime tower) |
| slice-plate | Slow | Yes | Individual plate slicing |
| multicolour-slice | Slow | Yes | Multi-colour slicing workflow |
| viewer | Slow | Yes | Canvas rendering, layer controls, API |

*file-management creates and deletes its own test data

### Writing New Tests

- Put new spec files in `tests/` following the `*.spec.ts` naming convention.
- Use helpers from `tests/helpers.ts` (`waitForApp`, `uploadFile`, `getAppState`, `fixture`, `waitForSliceComplete`).
- The UI has **no `data-testid` attributes**. Use `getByRole`, `getByText`, `getByLabel`, or `locator` with CSS selectors.
- Alpine.js state can be inspected via `getAppState(page, 'variableName')` from helpers.
- Tests run sequentially (`workers: 1`) since they share Docker services and DB state.
- Slicing tests need up to 2 minutes per slice — use the 120s default timeout or `test.setTimeout()` for longer.

### When to Add Tests

When implementing a new feature or fixing a bug:
1. Add test cases to the relevant existing spec file.
2. If the feature opens a new area (e.g., print control), create a new spec file.
3. Every bug fix should include a test that would have caught the bug.
4. After changes, deploy to Docker and run at least the targeted test suite.
5. Offer the user a choice: `test:fast` (~5 min), targeted tests, or full suite (~60 min).
