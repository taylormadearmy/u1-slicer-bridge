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

## Test Results

### Working Features
- ✅ Multi-plate detection (Dragon Scale infinity.3mf shows 3 plates)
- ✅ Correct bounding box calculation (shows "Fits build volume")
- ✅ Plate selection UI (radio buttons, selection highlighting)
- ✅ Slice specific plate functionality
- ✅ Existing upload selection workflow

### Test Files Created
- `test-e2e-multiplates.spec.js` - Full E2E Playwright tests
- `test-data/Dragon Scale infinity.3mf` - Test file

## Known Limitations

1. **Upload Performance**: Large multi-plate files take ~30s to parse
2. **Playwright Upload**: File upload tests have limitations with Playwright's setInputFiles()
