# Testing Guide

## Automated Tests (Playwright)

### Prerequisites

- Docker services running: `docker compose up -d --build`
- Node dependencies installed: `npm install`
- Playwright browsers installed: `npx playwright install chromium`

### Running Tests

```bash
# Run all tests
npm test

# Run quick smoke tests only (no slicing, ~15 seconds)
npm run test:smoke

# Run specific test suites
npm run test:upload        # Upload workflow
npm run test:slice         # Slicing end-to-end (slow, requires slicer)
npm run test:viewer        # G-code viewer rendering
npm run test:multiplate    # Multi-plate detection and selection
npm run test:multicolour   # Multicolour detection and overrides
npm run test:settings      # Settings tab, presets, filament CRUD
npm run test:files         # File management (upload/job delete)

# View HTML test report
npm run test:report
```

### Test Structure

```
tests/
  helpers.ts              Shared utilities (waitForApp, uploadFile, etc.)
  smoke.spec.ts           Page load, Alpine.js init, tabs, API health
  api.spec.ts             API endpoint availability and response shapes
  responsive.spec.ts      Desktop/tablet/mobile viewport rendering
  upload.spec.ts          Upload workflow (file → configure → back)
  slicing.spec.ts         Slice end-to-end (configure → slice → complete)
  viewer.spec.ts          G-code viewer canvas, controls, metadata
  multiplate.spec.ts      Multi-plate detection, plate cards, selection
  multicolour.spec.ts     Colour detection, overrides, >4 colour guard
  file-management.spec.ts Upload/job deletion, preview endpoints
  settings.spec.ts        Settings tab, extruder presets, filament CRUD
```

### Test Fixtures

Test 3MF files live in `test-data/`:

| File | Purpose |
|------|---------|
| `calib-cube-10-dual-colour-merged.3mf` | Dual-colour calibration cube (fast slice, multicolour) |
| `Dragon Scale infinity.3mf` | Multi-plate file with 3 plates |
| `Dragon Scale infinity-1-plate-2-colours.3mf` | Single plate, 2 colours |
| `Dragon Scale infinity-1-plate-2-colours-new-plate.3mf` | Variant for plater_name bug |

### Test Categories

| Suite | Speed | Needs Slicer | What it covers |
|-------|-------|-------------|----------------|
| smoke | Fast | No | Page load, Alpine init, tabs, API health |
| api | Fast | No | All API endpoint shapes and error handling |
| responsive | Fast | No | 3 viewport sizes render correctly |
| upload | Medium | No | Upload flow, configure step, navigation |
| settings | Medium | No | Presets, filament CRUD, form interactions |
| file-management | Medium | Yes* | Upload/job deletion lifecycle |
| multiplate | Slow | No | Plate detection, cards, selection |
| multicolour | Medium | No | Colour detection, overrides, fallbacks |
| slicing | Slow | Yes | Full slice end-to-end, metadata, download |
| viewer | Slow | Yes | Canvas rendering, layer controls, API |

*file-management deletes test data created during the test

---

## Manual Testing

### Core Workflow Regression Checklist

Before submitting changes, verify these still work:

1. **Upload** - Upload a .3mf file, appears in Recent Uploads
2. **Configure** - Click upload, reaches Configure step with filament selection
3. **Slice** - Slice completes, G-code generated
4. **Preview** - View G-code in 2D layer viewer, download works
5. **File management** - Checkboxes, shift-click range select, delete single/multiple files
6. **Multi-plate** - Upload multi-plate file, see plate cards with previews, select and slice one plate
7. **Multicolour** - Upload multi-colour file, see detected colours, override colours, slice
8. **Settings** - Extruder presets save/load, filament library CRUD, slicing defaults persist

### Quick Manual Test

1. Start services: `docker compose up -d --build`
2. Verify health: `curl http://localhost:8000/healthz`
3. Open http://localhost:8080
4. Upload a `.3mf` file and walk through: Upload → Configure → Slice → Preview

---

## API Endpoints

### Health & Status
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/healthz` | Health check |
| GET | `/printer/status` | Moonraker printer status |

### Uploads
| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/upload` | Upload 3MF file (validates plate bounds) |
| GET | `/upload` | List all uploads |
| GET | `/upload/{id}` | Get upload details (re-validates bounds) |
| DELETE | `/upload/{id}` | Delete upload and associated files |
| GET | `/uploads/{id}/preview` | Get embedded 3MF preview image |

### Plates (multi-plate files)
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/uploads/{id}/plates` | Get plate info for multi-plate files |
| GET | `/uploads/{id}/plates/{plate_id}/preview` | Get plate preview image |

### Slicing
| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/uploads/{id}/slice` | Slice full upload |
| POST | `/uploads/{id}/slice-plate` | Slice specific plate from multi-plate file |

### Jobs (sliced files)
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/jobs` | List all slicing jobs |
| GET | `/jobs/{job_id}` | Get job status and metadata |
| GET | `/jobs/{job_id}/download` | Download G-code file |
| GET | `/jobs/{job_id}/gcode/metadata` | Get G-code metadata (bounds, layers, tools) |
| GET | `/jobs/{job_id}/gcode/layers` | Get layer geometry for viewer |
| DELETE | `/jobs/{job_id}` | Delete job and G-code file |

### Filaments
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/filaments` | List filament profiles |
| POST | `/filaments` | Create filament profile |
| PUT | `/filaments/{id}` | Update filament profile |
| DELETE | `/filaments/{id}` | Delete filament profile |
| POST | `/filaments/{id}/default` | Set filament as default |
| POST | `/filaments/init-defaults` | Initialize starter filament library |
| POST | `/filaments/import` | Import JSON filament profile |
| POST | `/filaments/import/preview` | Preview JSON profile before import |

### Presets
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/presets/extruders` | Get extruder presets and slicing defaults |
| PUT | `/presets/extruders` | Save extruder presets and slicing defaults |

---

## Manual curl Examples

### Upload 3MF
```bash
curl -X POST http://localhost:8000/upload -F "file=@test.3mf" | jq
```

### List Filaments
```bash
curl http://localhost:8000/filaments | jq
```

### Slice Upload
```bash
curl -X POST http://localhost:8000/uploads/1/slice \
  -H "Content-Type: application/json" \
  -d '{"filament_id": 1, "layer_height": 0.2, "infill_density": 15, "supports": false}' | jq
```

### Download G-code
```bash
curl -o output.gcode http://localhost:8000/jobs/slice_abc123/download
```

### Get Viewer Data
```bash
curl http://localhost:8000/jobs/slice_abc123/gcode/metadata | jq
curl "http://localhost:8000/jobs/slice_abc123/gcode/layers?start=0&count=10" | jq
```

---

## Viewing Logs

```bash
docker logs u1-slicer-bridge-api-1                            # API logs
docker exec u1-slicer-bridge-api-1 cat /data/logs/slice_*.log # Per-job logs
```

## Troubleshooting

### Upload fails with "No valid objects found"
- File is not a valid 3MF or contains no meshes
- Check logs: `docker logs u1-slicer-bridge-api-1`

### Upload shows bounds warning
- Plate exceeds Snapmaker U1 build volume (270x270x270mm)
- For multi-plate files, individual plates may still fit even if combined scene exceeds bounds

### Slicing fails or crashes
- Check per-job log in `/data/logs/`
- Multicolour files with >4 detected colours will be rejected (U1 supports max 4 extruders)
- Some Bambu files can trigger slicer crashes on multicolour path - try single-filament mode

### Check Docker containers
```bash
docker ps                              # All containers should be "Up"
docker logs u1-slicer-bridge-api-1     # API logs
docker logs u1-slicer-bridge-web-1     # Web/nginx logs
```

### Verify OrcaSlicer installation
```bash
docker exec u1-slicer-bridge-api-1 xvfb-run -a orca-slicer --version
# Expected: OrcaSlicer 2.2.4
```

## Performance Benchmarks

Typical timing for a simple cube:
- Upload + validation: < 1 second
- Slicing: 30-60 seconds (depends on complexity)
- Multi-plate parsing: ~30 seconds for large files (3-4MB)
- Viewer metadata extraction: 1-2 seconds
