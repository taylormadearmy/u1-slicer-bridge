# AGENTS.md — AI Coding Agent Operating Manual (u1-slicer-bridge)

This repo is intended to be built with an AI coding agent (Claude Code in VS Code). Treat this document as binding.

---

## Project purpose

Self-hostable, Docker-first service for Snapmaker U1:

upload `.3mf` → validate plate → slice with Snapmaker OrcaSlicer → preview → print via Moonraker.

**Current Status:** Fully functional upload-to-preview workflow. Print control (M8) not yet implemented.

---

## Non-goals (v1)

- No MakerWorld scraping (use browser downloads)
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
⚠️ M2 moonraker - Health check only (no print control yet)  
❌ M8 print control - NOT IMPLEMENTED  

### File Lifecycle & Job Management
✅ M9 sliced file access - Browse and view previously sliced G-code files  
✅ M10 file deletion - Delete old uploads and sliced files  

### Multi-Plate & Multicolour Workflow
✅ M7.1 multi-plate support - Multi-plate 3MF detection and selection UI  
✅ M11 multifilament support - Color detection from 3MF, auto-assignment, manual override, multi-extruder slicing  
✅ M15 multicolour viewer - Show color legend in 2D viewer with all detected/assigned colors  
✅ M16 flexible filament assignment - Allow overriding color per extruder (separate material type from color)
❌ M17 prime tower options - Add configurable prime tower options for multicolour prints
✅ M18 multi-plate visual selection - Show plate names and preview images when selecting plates

### Preview & UX
✅ M7 preview - Interactive 2D layer viewer  
❌ M12 3D G-code viewer - Interactive 3D preview of sliced G-code  
❌ M20 G-code viewer zoom - Add zooming in/out controls for preview
✅ M21 upload/configure loading UX - Add progress indicator while upload is being prepared for filament/configuration selection
✅ M22 navigation consistency - Standardize actions like "Back" and "Slice Another" across the UI

### Slicing Controls & Profiles
✅ M7.2 build plate type & temperature overrides - Set bed type per filament and override temps at slice time  
❌ M13 custom filament profiles - Upload and use user-provided filament profiles  
❌ M24 extruder presets - Preconfigure default slicing settings and filament color/type per extruder
❌ M19 slicer selection - Choose between OrcaSlicer and Snapmaker Orca for slicing
❌ M23 common slicing options - Allow changing wall count, infill pattern, and infill density (%)

### Platform Expansion
❌ M14 multi-machine support - Support for other printer models beyond U1

**Current:** 16.7 / 24 complete (70%)

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

### LAN-first security
Secrets encrypted via `APP_SECRET_KEY`.

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
- worker for heavy tasks

Avoid:
- new infra unless necessary
- hidden state
- plaintext secrets

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

### Regression Testing

**Before submitting any changes, verify existing functionality still works:**

1. **Test the core workflow:**
   - Upload a .3mf file → should appear in Recent Uploads
   - Click upload → should go to Configure step
   - Slice → should generate G-code
   - View/Download → should work

2. **Test file management:**
   - Checkboxes for multi-select work
   - Shift-click for range select works
   - Delete single file works
   - Delete multiple selected files works

3. **Common regressions to watch for:**
   - Click handlers broken after UI changes
   - Event propagation issues (@click.stop)
   - State not updating after async operations
   - API endpoints returning wrong data types

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

**Fixed: Dragon Scale Segfault Path (>4 Colors on U1)**
- **Problem**: `Dragon Scale infinity.3mf` could trigger Orca segmentation faults when sliced as multicolour.
- **Root Cause**: File reports 7 detected color regions while U1 supports max 4 extruders; forcing multicolour requests caused unstable slicer behavior.
- **Fix**:
  1. Frontend now falls back to single-filament mode when detected colors exceed 4 and shows a clear notice.
  2. Override mode is disabled for this overflow case.
  3. Backend now rejects multicolour requests for >4 detected colors (and rejects `filament_ids` longer than 4) with a clear 400 error.
- **Result**: Dragon slices reliably in single-filament mode; unsupported multicolour requests fail fast with actionable error text.

**Active-Color Detection Update (Assignment-Aware)**
- **Problem**: Some Bambu files report many palette/default colors in metadata, which overstated actual extruders in use.
- **Fix**: `parser_3mf.py::detect_colors_from_3mf()` now prioritizes model-assigned extruders from `Metadata/model_settings.config` and maps them to active colors from `project_settings.config`.
- **Result**: Dragon now reports only assigned colors (e.g., 3 active colors instead of 7 metadata colors), improving UI assignment behavior and validation decisions.

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
  - `Pokerchips-smaller.3mf` multicolour path also succeeds with real tool changes.

**Fixed: Multi-Plate `slice-plate` Multicolour Crash Path**
- **Problem**: `POST /uploads/{id}/slice-plate` could still crash/fail for Poker/Dragon while full-file slice succeeded.
- **Root Cause**: Plate extraction/rebuild path altered model structure before slicing, reintroducing Orca instability on assignment-preserving multicolour files.
- **Fix**: Slice selected plates directly using Orca's built-in plate selector (`--slice <plate_id>`) on the embedded source 3MF, instead of extracting plate geometry first.
- **Files**:
  - `apps/api/app/routes_slice.py` (slice-plate workflow)
  - `apps/api/app/slicer.py` (`slice_3mf(..., plate_index=...)`)
- **Result**: Poker/Dragon multi-plate `slice-plate` now succeeds with real tool changes (`T0`, `T1`).

**Adjusted: Bambu Negative-Z Upload Warning Noise**
- **Problem**: Some Bambu exports (e.g., Pokerchips) showed `Objects extend below bed` warnings despite valid slicing/printing paths.
- **Root Cause**: Raw 3MF source offsets (`source_offset_z` in `model_settings.config`) can produce negative scene bounds in validation, even when slicer placement is valid.
- **Fix**: `plate_validator.py` now suppresses below-bed warnings for likely Bambu source-offset artifacts while keeping build-volume checks unchanged.
- **Result**: Poker no longer shows misleading below-bed warnings in upload/configure flows.

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

## Logging contract

All subprocess output must go to `/data/logs`.

---

## Testing Strategy

### Browser Testing Approach

**Primary: Native Browser Integration**
- Use native browser integration for all UI testing
- Direct DOM access for complex G-code canvas interactions
- Better debugging of visual rendering issues
- Simpler setup for local development

**When to Use Native Browser:**
- G-code viewer component testing (canvas rendering)
- Complex UI interactions and visual validation
- Performance profiling and debugging
- Quick iteration during development

**Testing Requirements:**
- Test multi-plate selection UI interactions
- Validate G-code preview rendering
- Check responsive design across viewports
- Verify API integration from browser perspective
- Test file upload and preview workflow

**Test Environment:**
- Run against `http://localhost:8080` (web container)
- Ensure API container running on `http://localhost:8000`
- Test with real 3MF files from `test/` directory
- Use browser dev tools for debugging

**Note:** Existing Playwright tests remain available but native browser integration is preferred for this app's visual testing needs.
