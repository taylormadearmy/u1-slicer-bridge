# Memory - U1 Slicer Bridge

## Bug Fixes & Solutions

### Multi-Plate Selection Issues

#### Issue 1: Bounding Box Calculation Wrong
- **Problem**: Multi-plate files showed "Exceeds build volume" incorrectly (388mm x 347mm)
- **Root Cause**: Transform matrices were incorrectly applied to bounding box calculation in `multi_plate_parser.py`
- **Solution**: Fixed matrix transformation to correctly calculate bounds: raw mesh bounds + translation offset
- **Result**: Dragon Scale (80mm x 40mm x 20mm) now correctly shows "Fits build volume" ✅
- **Files Changed**: `apps/api/app/multi_plate_parser.py`

#### Issue 2: Loading State Stuck
- **Problem**: "Loading plate information..." shown indefinitely, or plates never appearing
- **Root Cause**: 
  1. `loadPlates()` called without uploadId in HTML template
  2. No loading state tracking
- **Solution**: 
  1. Added `platesLoading` state variable in app.js
  2. Fixed HTML to pass uploadId: `loadPlates(selectedUpload?.upload_id)`
  3. Added fallback to get uploadId from selectedUpload
- **Files Changed**: `apps/web/index.html`, `apps/web/app.js`

#### Issue 3: Wrong File Sliced
- **Problem**: After selecting one upload then another, wrong file would slice
- **Root Cause**: `startSlice()` used `this.selectedUpload` which could change during async operations
- **Solution**: Capture upload ID/filename at start of `startSlice()` before any async operations
- **Files Changed**: `apps/web/app.js`

#### Issue 4: Fresh Upload Not Detecting Plates
- **Problem**: After uploading a multi-plate file, plates weren't loading
- **Root Cause**: Frontend made redundant API call instead of using plates from upload response
- **Solution**: Now uses plates data directly from upload response (API returns `is_multi_plate`, `plates`, `plate_count`)
- **Files Changed**: `apps/web/app.js`

#### Issue 5: Wrong Plate Sliced (Wrong File Size & Time)
- **Problem**: When slicing a specific plate, it sliced ALL plates - causing 100MB+ file size, 22 hour print time
- **Root Cause**: `slice_plate` endpoint in `routes_slice.py` was using original full 3MF instead of extracting selected plate
- **Solution**: Added `extract_plate_to_3mf()` function in `multi_plate_parser.py` to extract only the selected plate before slicing
- **Result**: Single plate now slices correctly (~16MB, ~3.4 hours for Dragon Scale)
- **Files Changed**: `apps/api/app/multi_plate_parser.py`, `apps/api/app/routes_slice.py`

### Performance Issues

#### Slow Plate Parsing
- **Observation**: Plate parsing takes ~30 seconds for large multi-plate files (3-4MB)
- **Note**: This is expected behavior - the 3MF file is being parsed on the server
- **Mitigation**: Added loading indicator in UI so users know something is happening

### Temperature Override Issues

#### Issue 1: Temperature Overrides Not Being Sent
- **Problem**: User set temperature overrides in UI but they weren't applied
- **Root Cause**: `api.js` was not sending `nozzle_temp`, `bed_temp`, or `bed_type` in the request body - fields were missing from the fetch calls
- **Solution**: Added these fields to `sliceUpload()` and `slicePlate()` API calls in `apps/web/api.js`
- **Result**: Temperature overrides now correctly passed to API
- **Files Changed**: `apps/web/api.js`

#### Issue 2: Bed Temperature Not Applied in G-code
- **Problem**: Even when sent, bed temp was 35°C instead of requested value
- **Root Cause**: Printer profile uses `bed_temperature_initial_layer_single` in G-code (M140 S{bed_temperature_initial_layer_single}), but we only set `bed_temperature_initial_layer`
- **Solution**: Added `bed_temperature_initial_layer_single` to filament_settings in routes_slice.py, plus also set cool_plate_temp for PEI plates
- **Result**: Bed temp correctly set to requested value (e.g., M140 S70)
- **Files Changed**: `apps/api/app/routes_slice.py`

### Prime Tower Defaults + Overrides (M17)

- **What changed**:
  - Added machine-level prime tower defaults in settings persistence (`enable_prime_tower`, `prime_tower_width`, `prime_tower_brim_width`).
  - Added per-job prime tower overrides in Configure (visible only when per-job override toggle is enabled).
  - Added payload fields in both slice endpoints and mapped them to Orca overrides (`enable_prime_tower`, `prime_tower_width`, `prime_tower_brim_width`).
- **Compatibility note**:
  - Existing DBs need additive columns on `slicing_defaults`; handled by runtime `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` in `_ensure_preset_rows()`.

### Printer Defaults UX (2026-02-14)

- **Adjustment**: Renamed user-facing `Machine Defaults` terminology to `Printer Defaults`.
- **Override behavior**: Enabling `Override settings for this job` now copies current printer defaults into override fields (instead of carrying stale values).
- **Readability improvement**: Override controls now display inline `(... printer default)` hints so users can compare quickly.
- **Multicolour placement**: Filament/extruder override controls are shown inside job overrides, not in the detected-colors header.

### Filament Library Tidy-Up (2026-02-14)

- **Symptom**: Users were unsure where filament profiles came from and why unexpected material (e.g., ABS) could appear in defaults.
- **Root causes**:
  - API filament list was plain name-sort, so non-PLA entries could become first fallback candidates.
  - Fallback order in frontend did not explicitly prefer PLA after preset/default lookups.
- **Fixes**:
  - API ordering: defaults first, then PLA-first, then name-sort.
  - Frontend fallback order: extruder preset -> `is_default` -> first PLA -> first available.
  - Settings UX renamed and clarified as `Filament Library` with starter-library messaging.
- **Result**: More predictable material defaults and clearer mental model for filament source/purpose.

### Filament CRUD Guardrails (2026-02-14)

- Added API endpoints for library management:
  - `PUT /filaments/{id}`
  - `DELETE /filaments/{id}`
  - `POST /filaments/{id}/default`
- Safety behavior:
  - Block delete when filament is assigned in `extruder_presets` (returns slots `E1-E4` in error).
  - If deleting current default filament, auto-promote replacement (prefers PLA) to keep default fallback stable.
- UI added Add/Edit/Delete/Set Default actions in Settings -> Filament Library.

### JSON Filament Import Foundation (2026-02-14)

- Added `POST /filaments/import` for JSON profile files.
- Current parser supports practical fields (with fallbacks):
  - `name|filament_name|profile_name`
  - `material|filament_type|type`
  - `nozzle_temp|temperature|nozzle_temperature`
  - `bed_temp|bed_temperature`
  - `print_speed|speed`
  - `bed_type|build_plate_type`
  - `color_hex|color`
- Added `source_type` on filaments (`starter`, `manual`, `custom`) to explain profile origin in UI.
- Import guardrail: rejects duplicate profile name with HTTP 409.

### Additional Prime Tower Controls (2026-02-14)

- Added support for:
  - `prime_volume`
  - `prime_tower_brim_chamfer`
  - `prime_tower_brim_chamfer_max_width`
- Wired through persistence, API payloads, backend override mapping, and UI controls in both Settings and job overrides.

## Configuration Notes

### API Endpoints
- Upload: `POST /api/upload`
- Plates: `GET /api/uploads/{id}/plates`
- Slice: `POST /api/uploads/{id}/slice` (single) or `/slice-plate` (multi)

### Web Container Deployment
After editing web files:
```bash
docker compose build web && docker compose up -d web
```
Then hard refresh browser (Ctrl+Shift+R).

## Known Limitations

1. **Upload Performance**: Large multi-plate files take ~30s to parse

### Multicolour Slicing (Critical)

- **New Working Path**: For Bambu files with model-level extruder assignments, profile embedding now uses an assignment-preserving strategy.
  - Keeps original assignment semantics from `model_settings.config`
  - Replaces incompatible Bambu custom G-code with Snapmaker-safe scripts
  - Sanitizes invalid project setting values that previously caused config validation errors
  - Normalizes filament arrays to assigned extruder count
- **Validation Result**: `calib-cube-10-dual-colour-merged.3mf` now slices successfully with real multi-tool output (`T0` and `T1`).
- **Safety Fix Retained**: Slice endpoints still fail when multicolour is requested but output is single-tool.
  - Error: `Multicolour requested, but slicer produced single-tool G-code (T0 only).`

### Hollow+Cube Plate Slice Failure (2026-02-14)

- **Symptom**: `hollow+cube.3mf` failed on `POST /uploads/{id}/slice-plate` with:
  - `prime_tower_brim_width: -1 not in range [0,2147483647]`
- **Root cause**: Assignment-preserving project settings path in `profile_embedder.py` did not sanitize `prime_tower_brim_width` inherited from source `project_settings.config`.
- **Fix**: Clamp/sanitize `prime_tower_brim_width` to minimum `0` during config build.
- **Follow-on behavior**: Some selected plates still emit `T0`-only G-code despite multicolour request.
  - For full-file multicolour requests, keep fail-fast.
  - For `slice-plate`, allow completion with warning when output is `T0` only (selected plate can be effectively single-tool even if file-level metadata is multicolour).

### Prime/Wipe Tower Edge Placement (2026-02-14)

- **Symptom**: Prime tower/wipe tower could appear at or near bed edge in viewer/output for some Bambu-derived files.
- **Root cause**: Embedded source config retained low `wipe_tower_x/y` (e.g., `15`) while requested tower width/brim made the effective footprint too close to edge.
- **Fix**: Added dynamic clamp in `profile_embedder.py` for `wipe_tower_x/y` based on tower half-span (`prime_tower_width / 2 + prime_tower_brim_width + margin`) within U1 bed bounds.
- **Validation**: New hollow+cube slice writes clamped coordinates (`wipe_tower_x=165`, previous edge-hugging value was `15`).

### Multicolour Plate Time Inflation (2026-02-14)

- **Symptom**: `hollow+cube` plate 2 showed ~2h estimates when expected around ~45-50m.
- **Root cause**: Config retained MMU timing defaults (`machine_load_filament_time=30`, `machine_unload_filament_time=30`) which inflated toolchange estimation for U1 extruder-slot swaps.
- **Fix**: In both slice endpoints, when `extruder_count > 1`, override those timings to `0`.
- **Validation**:
  - Before: ~7582s (~2h06m) with prime tower.
  - After: ~3022s (~50m) with prime tower; ~1504s (~25m) without prime tower.

#### Matrix Findings (apps/api/app/multicolor_matrix.py)
- `orig_*` strategies (preserving original Bambu structure) mostly segfault (`exit 139`).
- `rebuild_strip__minimal` is stable but outputs single-tool `T0` only.
- Adding color/type multi-extruder keys to rebuilt strategy causes segfault.
- Assignment-preserving sanitization strategy (used in `profile_embedder.py`) produced successful `T1` output on the validation dual-colour file.

#### Working Theory
- Trimesh rebuild path is stable but drops semantics needed for per-object extruder tool changes.
- Preserving assignment metadata works when paired with selective project-settings sanitization and Snapmaker-safe custom G-code replacement.
- This is now the production multicolour path used by `profile_embedder.py`.

### Viewer + Upload Regression Notes

- **G-code Layer Parsing Regression**
  - **Symptom**: Viewer failed with `No layer data returned for range 0-20`.
  - **Cause**: Layer parser handled `;LAYER_CHANGE` only, while generated files used `; CHANGE_LAYER` (and may include `;LAYER:<n>`).
  - **Fix**: Broadened marker detection in `routes_slice.py::_parse_gcode_layers()`.

- **Fresh Multi-Plate Warning Regression**
  - **Symptom**: Fresh multi-plate uploads could show combined-scene build-volume warnings despite valid per-plate fits.
  - **Cause**: Upload-time response did not suppress combined-scene warnings based on per-plate validation.
  - **Fix**: Added upload-time per-plate validation and warning suppression in `routes_upload.py` when any plate fits.

### Dragon Scale Segfault Guard (>4 Colors)

- **Symptom**: `Dragon Scale infinity.3mf` could segfault Orca when users attempted multicolour slicing.
- **Cause**: Model reports 7 detected color regions; U1 supports only 4 extruders.
- **Fixes**:
  - Frontend: auto-fallback to single-filament mode with notice when detected colors exceed 4; override mode disabled.
  - Backend: fail-fast validation for multicolour requests when detected colors >4 and for `filament_ids` length >4.
- **Validation**:
  - Multicolour request now returns clear 400 message instead of segfault path.
  - Single-filament Dragon plate slice still succeeds.

### Active Color vs Palette Color

- **Observation**: Bambu metadata can include extra palette/default colors not actually assigned to model objects.
- **Fix**: `detect_colors_from_3mf()` now prefers colors mapped from assigned extruders in `model_settings.config`.
- **Impact**: Dragon and Poker now report active colors (Dragon: 3, Poker: 2) instead of inflated metadata color sets.

### Multicolour Segfault Handling

- **Observation**: Even with active color counts within U1 limits, some models still crash Orca v2.2.4 on multicolour paths.
- **Fix**: Slice endpoints convert multicolour segfaults into a stable, user-friendly 400 error with guidance to use single-filament fallback.
- **Current state**:
  - `calib-cube-10-dual-colour-merged.3mf` multicolour: works (`T0/T1`).
  - Dragon/Poker were unstable in earlier runs; now fixed for tested variants via metadata sanitization below.

### Root Cause Found: `plater_name` Metadata

- **Discovery method**: Binary/semantic diff between:
  - failing: `Dragon Scale infinity-1-plate-2-colours.3mf`
  - passing: `Dragon Scale infinity-1-plate-2-colours-new-plate.3mf`
- **Critical delta**: `Metadata/model_settings.config` differed by one meaningful field:
  - failing file had `metadata key="plater_name" value="Dual Colour"`
  - passing file had empty `plater_name`
- **Validation**:
  - Clearing `plater_name` in the failing file made multicolour slicing succeed with `T0/T1`.
  - Applying sanitization in backend also made `Pokerchips-smaller.3mf` multicolour succeed with `T0/T1`.
- **Fix implemented**: `ProfileEmbedder._sanitize_model_settings()` now clears non-empty `plater_name` before writing embedded 3MF.

### Multi-Plate `slice-plate` Stability Fix

- **Symptom**: Poker/Dragon could still fail on `slice-plate` even when full-file multicolour slice succeeded.
- **Cause**: Plate extraction path modified model structure before embedding/slicing.
- **Fix**:
  - `routes_slice.py` now embeds the original source 3MF and passes selected plate index to Orca.
  - `slicer.py::slice_3mf()` now accepts `plate_index` and invokes `--slice <plate_id>`.
- **Validation**:
  - Poker `slice-plate` plate 1 and 2: success with multicolour output.
  - Dragon multi-plate `slice-plate` plate 1: success with multicolour output.

### Bambu Negative-Z Warning Suppression

- **Symptom**: Poker uploads showed `Objects extend below bed (Z_min = -9.4mm)` even though sliced G-code did not move below bed.
- **Cause**: Validation used raw 3MF bounds that include Bambu source offsets (`source_offset_z`) from `model_settings.config`.
- **Fix**: Added heuristic in `plate_validator.py` to suppress below-bed warnings for likely Bambu source-offset artifacts (keeps build-volume checks intact).
- **Validation**:
  - Existing Poker upload (`/upload/126`) now reports `warnings: []`.
  - Fresh Poker upload also reports no below-bed warnings.

### Frontend Jobs Refresh + Time Parsing

- **Sliced history refresh bug**
  - **Symptom**: Completed slices missing from "Sliced Files" until hard refresh.
  - **Fix**: In synchronous completion path, frontend now calls `loadJobs()` immediately.

- **Estimated time = 0 bug**
  - **Symptom**: New jobs often had `estimated_time_seconds = 0`.
  - **Cause**: Parser only recognized legacy `estimated printing time (normal mode)` lines.
  - **Fix**: Added parsing for newer Orca summary comments (`model printing time`, `total estimated time`).
- **Validation**: New slice job `slice_f999ce08a74f` recorded `estimated_time_seconds = 488`.

### Extruder Slot Mapping Investigation (E2/E3 Selection)

- **What was checked**: Explicit tests selecting non-default extruder slots (e.g., map two colours to E2/E3).
- **Fixes applied**:
  - Frontend no longer collapses unassigned extruder slots when building `filament_ids` payload.
  - Backend accepts `extruder_assignments` and remaps `model_settings` extruder metadata before slicing.
  - Added post-slice G-code tool remap to convert compacted tools to requested slots.
- **Post-slice remap details**:
  - Rewrites `Tn` commands
  - Rewrites AMS/tool-change macros: `M620 S* A`, `M621 S* A`
- **Validation**:
  - Full-file test with requested E2/E3 produced `T1`, `T2` in output G-code.
  - Plate-slice test with requested E2/E3 produced non-default tools (`T1`, `T2` order depends on model/toolpath).

### Browser Path Validation (E3/E4 and E1/E3)

- **Method**: Re-tested using browser-driven flow (upload -> configure -> slice) via Playwright/DOM interactions, not direct API-only requests.
- **Results**:
  - Cube merged with E3/E4: output uses `T2`, `T3`.
  - Poker (plate 1) with E3/E4: output uses `T2`, `T3`.
  - Poker (plate 1) with E1/E3 and same filament profile: output uses `T0`, `T2` and no temperature mismatch error.

### E1/E3 False Temperature Mismatch Root Cause

- **Cause**: Frontend slot gap-fill used first global default filament (`is_default`) which could be a different material than selected filaments.
- **Fix**: Gap-fill now prefers the first selected filament for the current slice, preventing mixed-material placeholder slots.

### Custom Filament Profiles (M13) - Slicer Settings Passthrough

- **Feature**: Imported OrcaSlicer JSON profiles now pass ~35 advanced slicer-native settings through to the slicing engine.
- **Architecture**:
  - On import: `_parse_filament_profile_payload()` extracts passthrough keys from `_SLICER_PASSTHROUGH_KEYS` set and stores them as JSON in `slicer_settings` column.
  - On slice: `_merge_slicer_settings()` in `routes_slice.py` reads the JSON blob and merges it into `filament_settings` dict (both `/slice` and `/slice-plate` endpoints).
  - For multi-extruder: scalar values are broadcast to arrays matching `extruder_count`.
  - Priority: explicit filament_settings (temps, bed type) are NOT overwritten by passthrough keys.
- **Export**: `GET /filaments/{id}/export` returns OrcaSlicer-compatible JSON with stored slicer_settings merged back in.
- **Round-trip**: Export -> re-import preserves all stored settings.
- **Frontend**:
  - Import preview shows color swatch, `is_recognized` status (amber warning if unrecognized), slicer settings count badge.
  - Each filament card shows green "Slicer Settings" badge when profile has advanced settings.
  - Export button on each filament card triggers browser JSON download via `api.exportFilamentProfile()`.
- **Key implementation detail**: The merge only applies the **primary filament's** (first in `filaments` list) slicer_settings. This avoids complexity when different filaments in a multi-extruder job have conflicting advanced settings.

## SEMM (Single Extruder Multi-Material / Painted) Colour Detection

- **Problem**: Bambu-style painted multicolour files (e.g., SpeedBoatRace) were detected as single-colour even though they use 4 filament colours via per-triangle `paint_color` attributes.
- **Root Cause**: `detect_colors_from_3mf()` in `parser_3mf.py` used `_extract_assigned_extruders()` to find object-level extruder assignments and returned early with only the colours for those assignments. Painted files have a single object with `extruder="1"` but use multiple filament colours via face painting — the colour data lives in `filament_colour` in `project_settings.config` and `paint_color` attributes on mesh triangles, not in per-object extruder assignments.
- **How to detect SEMM files**: `project_settings.config` contains `"single_extruder_multi_material": "1"` and `filament_colour` has more entries than the number of object-level extruder assignments.
- **Fix**:
  1. `parser_3mf.py`: Added early check for `single_extruder_multi_material == "1"` before the assigned-extruder path. When detected, returns all `filament_colour` entries.
  2. `parser_3mf.py`: Added `detect_filament_count_from_3mf()` helper for downstream use.
  3. `routes_slice.py` (both endpoints): Changed `required_extruders` and `multicolor_slot_count` to use `max(len(active_extruders), len(detected_colors))` so SEMM painted colours are counted for auto-expand and validation.
- **Result**: SpeedBoatRace now correctly reports 4 detected colours (`#FFFFFF`, `#000000`, `#0086D6`, `#A2D8E1`) and `has_multicolor: true`.
- **Key indicators of SEMM painted files**:
  - `single_extruder_multi_material: "1"` in project settings
  - `paint_color` attributes on mesh triangles (can be 200k+ occurrences)
  - `filament_maps` in model_settings (e.g., `"1 1 1 1"`)
  - Multiple `filament_colour` entries but only one object-level `extruder` assignment
