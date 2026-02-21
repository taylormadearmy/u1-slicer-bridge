# Memory - U1 Slicer Bridge

> Concise bug fix journal. For full implementation history, see [AGENTS.md](AGENTS.md).

## Pi Arm64 Regression Stability (2026-02-21)

- **Symptom**: Fast regression on Raspberry Pi arm64 timed out in multiplate/multicolour upload flows and removed uploaded files after tests.
- **Cause**:
  1. Test cleanup was enabled by default in this branch.
  2. Upload/UI wait timeouts were too tight for slower arm64 hardware.
- **Fix**:
  1. Playwright cleanup now runs only when `TEST_CLEANUP_UPLOADS=1`.
  2. `tests/helpers.ts` now scales upload/UI/API upload timeouts on arm64 (or when `PLAYWRIGHT_SLOW_ENV=1`).
- **Files**: `tests/global-setup.ts`, `tests/global-teardown.ts`, `tests/helpers.ts`

## Multi-Arch Slicer Packaging (2026-02-20)

### AppImage → Flatpak Migration
- **Symptom**: AppImage-based Orca install blocked multi-arch container builds.
- **Cause**: The pinned AppImage path was not portable/reliable across both `amd64` and `aarch64` targets.
- **Fix**: Switched API Docker image to install Snapmaker Orca from architecture-specific GitHub Flatpak bundles (`x86_64`/`aarch64`) and invoke the installed binary directly from the Flatpak payload (no `flatpak run` sandbox) to avoid Docker `bwrap` namespace failures. Build now installs the pinned bundle with `--no-deps` and does not add Flathub remotes, reducing non-deterministic external runtime drift.
- **Files**: `apps/api/Dockerfile`
- **Note**: API Dockerfile now resolves `fdm_process_common.json` dynamically from the Flatpak installation path.

## 3D G-code Viewer (M12) — 2026-02-17

### Implementation
- Replaced 2D canvas viewer with gcode-preview v2.18.0 + Three.js r159 (vendored in `apps/web/lib/`)
- Alpine.js component wraps gcode-preview; Three.js objects stored in closure (NOT Alpine properties — Proxy breaks non-configurable props)
- Full G-code fetched via `/api/jobs/{id}/download`, parsed client-side by gcode-preview
- Mouse controls match OrcaSlicer: left=rotate, middle/right=pan, scroll=zoom

### Key Bugs Fixed
1. **TIMELAPSE tool color bug** — `TIMELAPSE_START`/`TIMELAPSE_TAKE_FRAME` parsed as `gcode="t"`, misidentified as tool changes → `state.t=undefined` → hotpink fallback. Fix: comment out with regex before processGCode.
2. **Black rendered as white** — Gradient replaces lightness (0.1-0.8), black (S=0) becomes gray/white. Fix: `disableGradient: true`.
3. **Auto filament colors ignoring presets** — `mappedColors` from `mapDetectedColorsToPresetSlots()` was unused; `syncFilamentColors()` used wrong preset index. Fix: use `mappedFromPresets.mappedColors` and `assignments[idx]`.

## Recent Fixes (2026-02-16)

### Filament Loading Race Condition
- **Symptom**: `selectedFilaments = [null, null]`, API returns "filaments not found"
- **Cause**: `init()` still loading filaments when fast upload completes
- **Fix**: Guard `if (this.filaments.length === 0) await this.loadFilaments()` before `applyDetectedColors()`. Move `currentStep = 'configure'` to AFTER filaments/colors ready.
- **Files**: `apps/web/app.js`

### Test Filament Deletion
- **Symptom**: Extruder preset filaments permanently deleted by error tests
- **Cause**: `errors.spec.ts` sent only 1 extruder slot (API requires 4), PUT silently failed
- **Fix**: Send all 4 slots, verify PUT with `expect(putRes.ok()).toBe(true)`
- **Files**: `tests/errors.spec.ts`

### waitForSliceComplete Timeout
- **Symptom**: Tests wait 2.5 min instead of failing fast on slice errors
- **Fix**: Early exit if `currentStep` reverts to 'configure' or 'upload'
- **Files**: `tests/helpers.ts`

## Upload Performance Fix (2026-02-16)
- Replaced trimesh with XML vertex scanning for upload-time bounds
- `_calculate_xml_bounds()` scans `<vertex>` elements directly
- trimesh still used at slice time for Bambu geometry rebuild

## Multicolour Stability

### Key Fixes Applied
1. **plater_name metadata** — cleared in `model_settings.config` (segfault trigger)
2. **>4 colors** — rejected at API level, frontend falls back to single-filament
3. **Plate extraction** — uses `--slice <plate_id>` instead of geometry extraction
4. **SEMM painted files** — detected via `paint_color` attributes + `single_extruder_multi_material`
5. **Layer tool changes** — `custom_gcode_per_layer.xml` type=2 entries detected and preserved
6. **Machine load/unload times** — zeroed for multicolour (prevents 2x time inflation)

### Working Paths
- Trimesh rebuild: stable but single-tool output only (drops assignment semantics)
- Assignment-preserving: works when paired with metadata sanitization + Snapmaker-safe G-code

## Configuration Notes

### Extruder Presets API
- Requires exactly 4 slots (E1-E4) in PUT
- Safety check blocks deleting filaments assigned to presets
- `_ensure_preset_rows()` handles schema migration at runtime

### 3MF Sanitization
- Parameter clamping: `raft_first_layer_expansion`, `tree_support_wall_count`, `prime_volume`, `prime_tower_brim_width` etc. (Bambu `-1` → `0`)
- Metadata stripped: `slice_info.config`, `cut_information.xml`, `filament_sequence.json`
- Wipe tower position clamped within 270mm bed bounds
- `plater_name` cleared to prevent segfaults

### Docker Deployment
- Web: `docker compose build --no-cache web && docker compose up -d web`
- API: `docker compose build --no-cache api && docker compose up -d api`
- Regular `docker compose build` may miss Python file changes due to layer caching

### Temperature Format
Orca requires string arrays: `["200"]` not `[200]`. Wrap with `str()`.

### Database
asyncpg can't handle multi-statement SQL. Schema split into individual statements in `db.py`.
Runtime schema migration via `ALTER TABLE ADD COLUMN IF NOT EXISTS`.

## Pi Arm64 Playwright Timeout Tuning (2026-02-21)
- **Symptom**: `test:fast` on Raspberry Pi timed out at exactly 120s on large multi-plate/multicolour uploads even after helper timeout increases.
- **Cause**: Playwright global per-test timeout remained fixed at `120_000`, capping slower arm64 runs before helper-level waits could complete.
- **Fix**: `playwright.config.ts` now uses adaptive test timeout:
  - `240_000` for arm64, non-localhost base URLs, or `PLAYWRIGHT_SLOW_ENV=1`
  - `120_000` for standard local runs
- **Files**: `playwright.config.ts`
