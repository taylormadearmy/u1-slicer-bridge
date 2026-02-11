# Testing Guide

## Quick Test

1. **Download a test file** from MakerWorld:
   - [Simple Cube](https://makerworld.com/en/models/1204272-a-simple-cube-test-print)
   - [Test Cube with Labels](https://makerworld.com/en/models/705572-test-cube)
   - [Calibration Cube V3](https://makerworld.com/en/models/417128-simple-calibration-cube-v3)

2. **Run the test script**:
   ```bash
   ./test_workflow.sh ~/Downloads/test_cube.3mf
   ```

That's it! The script will test the full workflow automatically.

## What Gets Tested

The test script validates the complete workflow:

```
Upload 3MF → Parse Objects → Normalize → Bundle → Slice → G-code
```

### API Endpoints Tested
- ✓ `POST /upload` - Upload 3MF file
- ✓ `GET /upload/{id}` - Retrieve upload details
- ✓ `POST /filaments/init-defaults` - Initialize default filament profiles
- ✓ `GET /filaments` - List available filaments
- ✓ `POST /normalize/{id}` - Normalize objects to Snapmaker U1 build volume
- ✓ `POST /bundles` - Create bundle from normalized objects
- ✓ `POST /bundles/{id}/slice` - Slice bundle to G-code

### Validation Steps
1. **Health Check** - API responding
2. **Upload** - 3MF file parsed, objects extracted
3. **Normalization** - Objects placed on Z=0, bounds validated
4. **Bundle Creation** - Objects grouped with filament settings
5. **Slicing** - Orca Slicer generates G-code with embedded settings
6. **Metadata Extraction** - Print time, filament usage, layer count
7. **G-code Validation** - File exists and contains expected headers

## Manual Testing

If you prefer to test manually with `curl`:

### 1. Upload 3MF
```bash
curl -X POST http://localhost:8000/upload \
  -F "file=@test.3mf" \
  | jq
```

**Expected output:**
```json
{
  "upload_id": 1,
  "filename": "test.3mf",
  "file_size": 123456,
  "objects": [
    {
      "name": "cube",
      "object_id": "1",
      "vertices": 8,
      "triangles": 12
    }
  ]
}
```

### 2. Normalize Objects
```bash
curl -X POST http://localhost:8000/normalize/1 \
  -H "Content-Type: application/json" \
  -d '{"printer_profile": "snapmaker_u1"}' \
  | jq
```

**Expected output:**
```json
{
  "job_id": "norm_abc123",
  "upload_id": 1,
  "status": "completed",
  "normalized_objects": [...]
}
```

### 3. Initialize Default Filaments
```bash
curl -X POST http://localhost:8000/filaments/init-defaults | jq
```

### 4. List Filaments
```bash
curl http://localhost:8000/filaments | jq
```

**Expected output:**
```json
{
  "filaments": [
    {
      "id": 1,
      "name": "Snapmaker Generic PLA",
      "material": "PLA",
      "nozzle_temp": 210,
      "bed_temp": 60,
      "print_speed": 60,
      "is_default": true
    }
  ]
}
```

### 5. Create Bundle
```bash
# Replace object_ids with actual IDs from step 2
curl -X POST http://localhost:8000/bundles \
  -H "Content-Type: application/json" \
  -d '{
    "name": "test_print",
    "object_ids": [1, 2],
    "filament_id": 1
  }' \
  | jq
```

**Expected output:**
```json
{
  "bundle_id": "bundle_xyz789",
  "name": "test_print",
  "filament": "Snapmaker Generic PLA",
  "object_count": 2,
  "status": "pending"
}
```

### 6. Slice Bundle
```bash
curl -X POST http://localhost:8000/bundles/bundle_xyz789/slice \
  -H "Content-Type: application/json" \
  -d '{
    "layer_height": 0.2,
    "infill_density": 15,
    "supports": false
  }' \
  | jq
```

**Expected output:**
```json
{
  "job_id": "slice_def456",
  "bundle_id": "bundle_xyz789",
  "status": "completed",
  "gcode_path": "/data/slices/slice_def456.gcode",
  "estimated_print_time_seconds": 1234,
  "filament_used_mm": 567.89,
  "total_layers": 100,
  "log_path": "/data/logs/slice_def456.log"
}
```

## Viewing Logs

Check slicing logs:
```bash
docker exec u1-slicer-bridge-api-1 tail -f /data/logs/slice_xyz123.log
```

Check G-code output:
```bash
docker exec u1-slicer-bridge-api-1 head -n 50 /data/slices/slice_xyz123.gcode
```

## Troubleshooting

### Upload fails with "No valid objects found"
- File is not a valid 3MF
- 3MF contains no meshes
- Check logs: `docker logs u1-slicer-bridge-api-1`

### Normalization fails with bounds error
- Object exceeds Snapmaker U1 build volume (300x300x300mm)
- Check normalization log in response

### Slicing fails
- Objects not normalized
- Orca Slicer error (check log_path in response)
- Missing filament settings

### Check Docker containers
```bash
docker ps  # All containers should be "Up"
docker logs u1-slicer-bridge-api-1  # API logs
```

### Verify Orca Slicer installation
```bash
docker exec u1-slicer-bridge-api-1 xvfb-run -a orca-slicer --version
```

**Expected output:**
```
OrcaSlicer 2.3.1
```

## Expected Results

After a successful test run, you should have:
1. ✅ Upload record in database
2. ✅ Extracted objects with mesh data
3. ✅ Normalized STL files in `/data/normalized/`
4. ✅ Bundle record with filament assignment
5. ✅ Programmatically generated 3MF with Snapmaker U1 settings
6. ✅ Valid G-code file in `/data/slices/`
7. ✅ G-code metadata (time, filament, layers)

## Performance Benchmarks

Typical timing for a simple cube:
- Upload: < 1 second
- Normalization: 2-5 seconds
- 3MF generation: < 5 seconds
- Slicing: 10-30 seconds (depends on complexity)

## Next Steps After Testing

Once the API workflow is validated:
1. Build the mobile-first web UI (M7)
2. Add WebSocket for real-time job updates
3. Implement G-code preview (M9)
4. Add print control via Moonraker (M10)
