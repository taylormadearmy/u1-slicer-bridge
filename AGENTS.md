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

## Milestones Status

✅ M0 skeleton - Docker, FastAPI, services
✅ M1 database - PostgreSQL with uploads, jobs, filaments
⚠️ M2 moonraker - Health check only (no print control yet)
✅ M3 object extraction - 3MF parser (handles MakerWorld files)
✅ M4 ~~normalization~~ → plate validation - Preserves arrangements
✅ M5 ~~bundles~~ → direct slicing with filament profiles
✅ M6 slicing - Snapmaker OrcaSlicer v2.2.4, Bambu support
✅ M7 preview - Interactive 2D layer viewer
✅ M7.1 multi-plate support - Multi-plate 3MF detection and selection UI
✅ M7.2 build plate type & temperature overrides - Set bed type per filament and override temps at slice time
❌ M8 print control - NOT IMPLEMENTED

**Current:** 7.7 / 8 complete (96%)

---

## Plate Validation Contract

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
