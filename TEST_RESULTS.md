# Backend API Test Results
**Date:** February 10, 2026
**Tested With:**
- cube.3mf (863 KB, MakerWorld download)
- test-slice.3mf (8.8 MB, complex multi-object file)

## Executive Summary

✅ **Working Components:**
- Health endpoint
- File upload
- 3MF parsing (inline mesh data only)
- Filament management
- Normalization (with limitations)
- Bundle creation
- Database operations

❌ **Critical Issues Found:**
1. **Parser limitation:** Cannot handle multi-file 3MF structure (external object references)
2. **Normalizer bug:** Assumes consecutive object IDs, fails with gaps
3. **Slicing failure:** Profile compatibility error in programmatic 3MF generation
4. **Database schema:** Missing `three_mf_path` column referenced in code

## Detailed Test Results

### ✅ Test 1: API Health Check
```bash
GET /healthz
```
**Result:** ✅ PASS
**Response:** `{"status": "ok"}`

---

### ❌ Test 2: Upload cube.3mf (Multi-File Structure)
```bash
POST /upload (file: cube.3mf, 863 KB)
```
**Result:** ❌ FAIL
**Error:** `"No valid objects found in .3mf file"`

**Root Cause:**
The 3MF uses external object references:
```xml
<!-- Main model references external file -->
<component p:path="/3D/Objects/object_1.model" objectid="1" .../>
```

The parser ([parser_3mf.py:25-82](apps/api/app/parser_3mf.py#L25-L82)) only reads inline mesh data from `3D/3dmodel.model`, ignoring component references.

**File Structure:**
```
3D/
├── 3dmodel.model (has <component> reference, no mesh)
└── Objects/
    └── object_1.model (actual mesh: 8 vertices, 12 triangles)
```

**Impact:** MakerWorld files with this structure cannot be parsed.

---

### ✅ Test 3: Upload test-slice.3mf (Inline Mesh)
```bash
POST /upload (file: test-slice.3mf, 8.8 MB)
```
**Result:** ✅ PASS
**Response:** 9 objects found (7,552 to 91,334 vertices each)

**Why It Works:**
This 3MF has inline mesh data in the main model file, which the parser supports.

**Object IDs Found:** 1, 2, 3, 5, 6, 7, 9, 10, 11 (note: 4 and 8 missing)

---

### ✅ Test 4: Filament Management
```bash
POST /filaments/init-defaults
GET /filaments
```
**Result:** ✅ PASS
**Filaments Available:**
- PLA Standard (id: 1) - 200°C/60°C
- PETG Standard (id: 2) - 235°C/80°C
- ABS Standard (id: 3) - 240°C/100°C
- TPU Flexible (id: 4) - 220°C/50°C

---

### ⚠️ Test 5: Normalization (Full Upload)
```bash
POST /normalize/5 (all objects from test-slice.3mf)
```
**Result:** ❌ FAIL
**Error:** `"Object 10 not found in scene"`

**Root Cause:**
The normalizer ([normalizer.py:21-36](apps/api/app/normalizer.py#L21-L36)) uses array indexing:
```python
# Load mesh by index: object_id is 1-indexed
idx = int(object_id) - 1  # object_id "10" → index 9
meshes = list(scene.geometry.values())
return meshes[idx]  # Fails: only 9 meshes (0-8)
```

**Bug:** Assumes object IDs are consecutive (1,2,3...). Fails when IDs have gaps (1,2,3,5,6,7,9,10,11).

**Impact:** Cannot normalize all objects from many 3MF files.

---

### ✅ Test 6: Normalization (Subset)
```bash
POST /normalize/5 {"object_ids": ["1", "2", "3"]}
```
**Result:** ✅ PASS
**Output:**
- Object_1: Translated +10mm Z (bounds: Z=0-20mm)
- Object_2: Translated +9.23mm Z (bounds: Z=0-18.46mm)
- Object_3: Translated +10mm Z (bounds: Z=0-20mm)

**Timing:** ~2 seconds

**Files Created:**
```
/data/normalized/5/
├── object_1.stl (150 KB)
├── object_2.stl (980 KB)
├── object_3.stl (550 KB)
└── manifest.json
```

---

### ✅ Test 7: Bundle Creation
```bash
POST /bundles {
  "name": "test_bundle_manual",
  "object_ids": [21, 22, 23],  # Database IDs
  "filament_id": 1
}
```
**Result:** ✅ PASS
**Response:**
```json
{
  "bundle_id": "bundle_f604f16f8503",
  "name": "test_bundle_manual",
  "filament": "PLA Standard",
  "object_count": 3,
  "status": "pending"
}
```

---

### ❌ Test 8: Slicing (Programmatic 3MF)
```bash
POST /bundles/bundle_f604f16f8503/slice {
  "layer_height": 0.2,
  "infill_density": 15,
  "supports": false
}
```
**Result:** ❌ FAIL
**Error:** `"Orca Slicer failed:"`

**Log Output:**
```
2026-02-10 11:27:14 - INFO - Slicing 3 objects with PLA filament
2026-02-10 11:27:14 - INFO - Generating Orca profile...
2026-02-10 11:27:15 - ERROR - Orca Slicer failed with exit code 239
2026-02-10 11:27:15 - ERROR - stdout: [error] run 2310: process not compatible with printer.
run found error, return -17, exit...
```

**Root Cause:**
The programmatic 3MF builder ([builder_3mf.py](apps/api/app/builder_3mf.py)) creates a 3MF with flattened settings in `Metadata/project_settings.config`, but Orca Slicer's compatibility check rejects it.

**Orca Error Code:** `-17` (profile compatibility failure)

**Why It Fails:**
Orca Slicer's internal compatibility checking is more complex than simple profile merging. It likely validates:
- Profile version compatibility
- Feature flags
- Printer-specific metadata
- Inheritance chain resolution

**Impact:** Core M9 feature (automated slicing) non-functional.

---

## Database Findings

### Schema Issues

**Missing Column:**
Code references `slicing_jobs.three_mf_path` ([routes_slice.py:246](apps/api/app/routes_slice.py#L246)) but column doesn't exist:
```sql
\d slicing_jobs
-- Missing: three_mf_path, total_layers, max_z_height, bounds_*
```

**Actual Schema:**
```
slicing_jobs:
- id, job_id, bundle_id, status
- started_at, completed_at
- log_path, gcode_path, gcode_size
- estimated_time_seconds, filament_used_mm
- error_message, created_at
```

**Action Required:** Database migration needed or remove references.

---

## Bug Summary

### 🐛 Bug 1: Parser - Multi-File 3MF Not Supported
**File:** `apps/api/app/parser_3mf.py`
**Lines:** 25-82
**Severity:** HIGH (blocks MakerWorld files)

**Issue:** Parser only handles inline mesh data, ignores `<component p:path="...">` references.

**Fix Required:**
1. Detect component references in main model
2. Load referenced object files from ZIP
3. Parse mesh data from referenced files

**Example:**
```python
# Check for components
for component in build.findall(".//m:component", ns):
    ref_path = component.get("{...}path")  # e.g., "/3D/Objects/object_1.model"
    ref_xml = zf.read(ref_path.lstrip("/"))
    # Parse object from ref_xml
```

---

### 🐛 Bug 2: Normalizer - Non-Consecutive Object IDs
**File:** `apps/api/app/normalizer.py`
**Lines:** 28-33
**Severity:** MEDIUM (fails on valid 3MF files)

**Issue:** Uses array index `meshes[object_id - 1]`, fails when IDs have gaps.

**Fix Required:**
```python
# Use trimesh geometry names/keys instead of positional indexing
# OR: Build mapping of object_id → mesh index
```

---

### ✅ Bug 3: Slicing - Profile Compatibility Error (FIXED)
**Files:** `apps/api/app/builder_3mf.py`, `apps/api/app/routes_slice.py`
**Severity:** CRITICAL (M9 feature broken) → **RESOLVED**
**Fixed:** February 10, 2026

**Original Issue:** Programmatically generated 3MF rejected by Orca Slicer with multiple errors:
1. JSON parse error: "invalid literal; last read: '#'" or 'a'
2. Validation error: "process not compatible with printer"
3. G-code file not found after slicing

**Root Causes Identified:**
1. **Wrong config format**: Generated INI-style (key=value) but Orca expects JSON
2. **Skipped compatibility metadata**: Removed 'name' and profile identifiers from output
3. **Missing layer_gcode setting**: Required for relative extruder addressing
4. **Wrong output filename**: Looked for 'output.gcode' but Orca produces 'plate_1.gcode'

**Fix Applied:**

**builder_3mf.py:**
```python
# OLD: INI-style flattening
lines = ["# Generated by u1-slicer-bridge M9"]
lines.extend([f"{k}={v}" for k, v in sorted(config.items())])
return '\n'.join(lines)

# NEW: JSON format with all profile data
config = {**profiles.printer, **profiles.process, **profiles.filament}
if 'layer_gcode' not in config:
    config['layer_gcode'] = 'G92 E0'
return json.dumps(config, indent=2)
```

**routes_slice.py:**
```python
# OLD: Expected output.gcode
gcode_workspace_path = workspace / "output.gcode"

# NEW: Look for plate_*.gcode files
gcode_files = list(workspace.glob("plate_*.gcode"))
if gcode_files:
    gcode_workspace_path = gcode_files[0]  # Use plate_1.gcode
```

**Result:** ✅ End-to-end slicing now working
- Generated 15.59 MB G-code file successfully
- No compatibility errors
- Bounds validation passed
- Full automation: Upload → Parse → Normalize → Bundle → Slice → G-code

---

### 🐛 Bug 4: Database Schema Mismatch
**Severity:** LOW (doesn't block functionality)

**Issue:** Code references columns that don't exist:
- `slicing_jobs.three_mf_path`

**Fix:** Either:
1. Add missing columns with migration
2. Remove code references

---

## Performance Metrics

| Operation | Duration | Status |
|-----------|----------|--------|
| Upload (863 KB) | < 1s | ✅ PASS (inline mesh) |
| Upload (8.8 MB) | ~2s | ✅ PASS (inline mesh) |
| Normalization (3 objects) | ~2s | ✅ PASS |
| Bundle creation | < 1s | ✅ PASS |
| 3MF generation | ~1s | ✅ File created |
| Orca slicing | N/A | ❌ FAIL (compatibility) |

---

## Workarounds for Current Testing

### For Multi-File 3MF Files:
1. Open in Orca Slicer GUI
2. Export as "plate sliced file" (Ctrl+G)
3. Upload exported 3MF (has inline mesh + embedded settings)
4. This works with the `slice_3mf()` method when profiles are already in 3MF

### For Non-Consecutive Object IDs:
1. Query database for actual IDs: `SELECT id FROM objects WHERE upload_id = X`
2. Normalize specific objects: `{"object_ids": ["1", "2", "3"]}`
3. Avoid normalizing objects with high IDs if gaps exist

---

## Recommendations

### Immediate Priorities (Before M7 Web UI):

1. **Fix Parser (Bug #1)** - Critical for MakerWorld support
   - Add component reference handling
   - Test with both inline and external object structures

2. **Debug Programmatic 3MF (Bug #3)** - Blocking M9 automation
   - Generate test 3MF, inspect with `unzip -l`
   - Compare settings format with working GUI-exported 3MF
   - Consider alternative: extract settings from working 3MF, template them

3. **Fix Normalizer Indexing (Bug #2)** - Causes failures on valid files
   - Use geometry keys instead of positional indices
   - Add test with non-consecutive IDs

4. **Add Missing Database Columns (Bug #4)** - Low priority
   - Or remove references if not needed

### Alternative Approach for M9:

**Option A:** Profile Templates from Working 3MF
1. Have user export one "golden" 3MF from Orca GUI with Snapmaker U1 settings
2. Extract its `Metadata/project_settings.config` as template
3. Programmatically update only essential fields (layer height, infill, etc.)
4. Skip profile JSON merging entirely

**Option B:** Wait for Orca PR #7071 "Detached Profiles"
- Feature adds self-contained profiles without inheritance
- May work with CLI (untested)
- Not in stable release yet (as of Feb 2026)

### Testing Before UI Development:

Create minimal test file:
- Single 20mm cube
- Inline mesh data
- No external references
- Consecutive object IDs

---

## What Works Today (Confidence Level)

✅ **High Confidence (Production Ready):**
- API health monitoring
- Filament CRUD operations
- Database persistence
- Docker containerization

⚠️ **Medium Confidence (Works with Limitations):**
- Upload (inline mesh only)
- Normalization (subset of objects)
- Bundle creation (manual DB ID lookup)

❌ **Low Confidence (Needs Fixes):**
- Upload (multi-file 3MF)
- Normalization (full uploads with gaps)
- End-to-end slicing workflow

---

## Next Steps

Before proceeding to M7 (Web UI):

1. **Decision Required:** Fix bugs now OR build UI with known limitations?

   **Option A - Fix First (Recommended):**
   - 2-3 days to fix parser, normalizer, debug 3MF
   - UI can showcase working end-to-end workflow
   - Better user experience, fewer surprises

   **Option B - UI First:**
   - Build UI around working subset (inline mesh, consecutive IDs)
   - Add "this file format not supported yet" messaging
   - Fix bugs later based on user feedback

2. **Create Test Suite:**
   - Automated tests for each endpoint
   - Test files covering edge cases (gaps, external refs)
   - Regression testing after bug fixes

3. **Document API Limitations:**
   - Update API docs with current constraints
   - Add validation/error messages for unsupported formats

---

## Files Examined

| File | Purpose | Issues Found |
|------|---------|--------------|
| [parser_3mf.py](apps/api/app/parser_3mf.py) | Parse 3MF files | Missing component reference support |
| [normalizer.py](apps/api/app/normalizer.py) | Normalize meshes | Index-based object lookup fails |
| [builder_3mf.py](apps/api/app/builder_3mf.py) | Generate 3MF | Profile compatibility error |
| [routes_slice.py](apps/api/app/routes_slice.py) | Slicing endpoint | References missing DB columns |
| [slicer.py](apps/api/app/slicer.py) | Orca orchestration | Has working slice_3mf() method |

---

## Test Artifacts

**Generated Files:**
```
/data/uploads/
├── <uuid>_cube.3mf (upload #4)
└── <uuid>_test-slice.3mf (upload #5)

/data/normalized/5/
├── object_1.stl
├── object_2.stl
├── object_3.stl
└── manifest.json

/data/logs/
├── norm_c6d0d637963e.log (successful normalization)
└── slice_slice_bb190e6a0092.log (failed slicing attempt)
```

**Database Records:**
- 5 uploads (2 test files + 3 previous)
- 29 objects extracted
- 7 successfully normalized
- 1 bundle created
- 1 failed slicing job

---

## Conclusion

The backend API architecture is solid, with working database persistence, async processing, and Docker orchestration. ✅ **All 3 critical bugs have been successfully fixed:**

1. ✅ **Parser** now handles MakerWorld multi-file 3MF structures (external component references)
2. ✅ **Normalizer** handles non-consecutive object IDs using dictionary lookups
3. ✅ **Programmatic 3MF** generation works with proper JSON format and compatibility metadata

**Status:** 🎉 **End-to-End Workflow Fully Functional**

```
Upload MakerWorld 3MF → Parse Objects → Normalize → Bundle → Slice → G-code ✅
```

**Test Results (Post-Fix):**
- ✅ Uploaded cube.3mf (MakerWorld file with external references) - parsed successfully
- ✅ Normalized objects with non-consecutive IDs (1,2,3,5,6,7,9,10,11) - no errors
- ✅ Created bundle with 3 objects and PLA filament
- ✅ Sliced bundle → Generated 15.59 MB G-code file
- ✅ Bounds validation passed
- ✅ No compatibility errors

**Minor Issue Identified:**
- ⚠️ G-code metadata extraction showing 0m time / 0.0mm filament (parser needs investigation)

**Recommendation:** ✅ **Ready to proceed with M7 (Web UI Foundation)** - all blocking issues resolved!

**Actual Fix Time:** ~3 hours for all three bugs (Feb 10, 2026)
