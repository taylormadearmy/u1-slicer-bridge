# M34 — Settings Backup & Restore

## Overview
Allow users to export all settings (filaments, extruder presets, slicing defaults, printer settings, setting modes) as a single JSON file, and restore from a previously exported file.

## Current Settings Architecture

### Database Tables
| Table | Content | Rows |
|-------|---------|------|
| `printer_settings` | Moonraker URL, MakerWorld cookies, enabled flag | 1 (singleton) |
| `filaments` | Filament profiles (name, temps, speeds, colors, slicer_settings) | N (user-created) |
| `extruder_presets` | E1-E4 slot assignments (filament_id, color) | 4 (fixed) |
| `slicing_defaults` | All slicing parameters + setting_modes JSON | 1 (singleton) |

### Existing API Endpoints
- `GET /filaments` → list all filaments
- `GET /presets/extruders` → extruder presets + slicing defaults + setting modes
- `GET /printer/settings` → Moonraker URL, MakerWorld status

### Data Relationships
- `extruder_presets.filament_id` → references `filaments.id`
- Restoring presets requires filaments to exist first (FK constraint)
- MakerWorld cookies are sensitive — exclude or encrypt in backups

## Export Format

```json
{
    "version": 1,
    "exported_at": "2026-02-20T10:30:00Z",
    "app_version": "1.5.0",
    "settings": {
        "printer": {
            "moonraker_url": "http://192.168.1.100:7125",
            "makerworld_enabled": true
            // NOTE: makerworld_cookies excluded (sensitive)
        },
        "filaments": [
            {
                "name": "PLA Red",
                "material": "PLA",
                "nozzle_temp": 210,
                "bed_temp": 60,
                "print_speed": 200,
                "bed_type": "PEI",
                "color_hex": "#FF0000",
                "is_default": true,
                "source_type": "manual",
                "density": 1.24,
                "slicer_settings": null
            }
        ],
        "extruder_presets": [
            { "slot": 1, "filament_name": "PLA Red", "color_hex": "#FF0000" },
            { "slot": 2, "filament_name": null, "color_hex": "#FFFFFF" },
            { "slot": 3, "filament_name": null, "color_hex": "#FFFFFF" },
            { "slot": 4, "filament_name": null, "color_hex": "#FFFFFF" }
        ],
        "slicing_defaults": {
            "layer_height": 0.2,
            "infill_density": 15,
            "wall_count": 3,
            "infill_pattern": "gyroid",
            "supports": false,
            "support_type": "normal(auto)",
            "brim_type": "auto_brim",
            "brim_width": 5,
            "brim_object_gap": 0,
            "skirt_loops": 2,
            "skirt_distance": 3,
            "skirt_height": 1,
            "enable_prime_tower": false,
            "enable_flow_calibrate": true,
            "nozzle_temp": null,
            "bed_temp": null,
            "bed_type": null,
            "setting_modes": {
                "layer_height": "model",
                "infill_density": "override",
                "wall_count": "orca"
            }
        }
    }
}
```

Key design decisions:
- **`filament_name` instead of `filament_id`** in extruder presets — IDs are not portable across databases
- **MakerWorld cookies excluded** — sensitive data, user must re-enter after restore
- **`version` field** — allows future format changes with migration logic
- **Filament `id` excluded** — auto-generated, not portable

## Implementation Plan

### Phase 1: Backend Export Endpoint
**New endpoint**: `GET /api/settings/export`

```python
@app.get("/api/settings/export")
async def export_settings():
    """Export all settings as downloadable JSON."""
    async with db_pool.connection() as conn:
        # 1. Fetch printer_settings (row id=1)
        # 2. Fetch all filaments (exclude id, created_at)
        # 3. Fetch extruder_presets (resolve filament_id → filament_name)
        # 4. Fetch slicing_defaults (exclude id, updated_at)

    backup = {
        "version": 1,
        "exported_at": datetime.utcnow().isoformat() + "Z",
        "app_version": APP_VERSION,
        "settings": { ... }
    }

    return Response(
        content=json.dumps(backup, indent=2),
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="u1-slicer-settings-{date}.json"'
        }
    )
```

### Phase 2: Backend Import Endpoint
**New endpoint**: `POST /api/settings/import`

```python
@app.post("/api/settings/import")
async def import_settings(file: UploadFile):
    """Restore settings from exported JSON file."""
    data = json.loads(await file.read())

    # 1. Validate version and structure
    if data.get("version") != 1:
        raise HTTPException(400, "Unsupported backup version")

    async with db_pool.connection() as conn:
        async with conn.transaction():
            # 2. Import filaments (upsert by name)
            #    - Match existing filaments by name
            #    - Create new ones if not found
            #    - Do NOT delete existing filaments not in backup

            # 3. Import extruder presets
            #    - Resolve filament_name → filament_id
            #    - If filament not found, set to null

            # 4. Import slicing defaults
            #    - Overwrite all values

            # 5. Import printer settings
            #    - Update Moonraker URL
            #    - Preserve existing MakerWorld cookies (not in backup)

    return {"success": True, "imported": { ... summary ... }}
```

Import strategy: **Merge, don't replace**.
- Filaments: upsert by name (update existing, add new, keep extras)
- Presets: overwrite all 4 slots
- Slicing defaults: overwrite
- Printer URL: overwrite
- Cookies: preserve existing (not in backup)

### Phase 3: Frontend UI
Add to Settings modal, new section at bottom:

```
┌──────────────────────────────────────┐
│  Backup & Restore                    │
│                                      │
│  [Export Settings]  Download JSON     │
│                                      │
│  [Choose File...]  Import settings   │
│  [Import Settings]                   │
│                                      │
│  ⚠ Import will overwrite current     │
│    presets and slicing defaults.      │
│    Existing filaments are preserved.  │
└──────────────────────────────────────┘
```

State additions:
```javascript
importFile: null,
importStatus: null,     // { ok: bool, message: string }
exporting: false,
importing: false,
```

Methods:
```javascript
async exportSettings() {
    this.exporting = true;
    try {
        const blob = await api.exportSettings();  // Returns blob
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `u1-slicer-settings-${new Date().toISOString().slice(0,10)}.json`;
        a.click();
        URL.revokeObjectURL(url);
    } finally {
        this.exporting = false;
    }
}

async importSettings() {
    if (!this.importFile) return;
    if (!confirm('This will overwrite your current presets and slicing defaults. Continue?')) return;

    this.importing = true;
    try {
        const formData = new FormData();
        formData.append('file', this.importFile);
        const result = await api.importSettings(formData);
        this.importStatus = { ok: true, message: 'Settings restored successfully' };

        // Reload all settings from DB
        await this.loadFilaments();
        await this.loadExtruderPresets();
        await this.loadPrinterSettings();
    } catch (err) {
        this.importStatus = { ok: false, message: err.message };
    } finally {
        this.importing = false;
    }
}
```

### Phase 4: API Layer
```javascript
// In api.js
async exportSettings() {
    const response = await fetch(`${this.baseUrl}/settings/export`);
    if (!response.ok) throw new Error('Export failed');
    return response.blob();
}

async importSettings(formData) {
    const response = await fetch(`${this.baseUrl}/settings/import`, {
        method: 'POST',
        body: formData,
    });
    if (!response.ok) {
        const err = await response.json();
        throw new Error(err.detail || 'Import failed');
    }
    return response.json();
}
```

### Phase 5: Testing
- Export test: verify JSON structure, all tables represented, no sensitive data
- Import test: upload exported JSON → verify all settings applied
- Round-trip test: export → change settings → import → verify original restored
- Partial import: backup with unknown filaments → verify graceful handling
- Version check: backup with `version: 99` → 400 error
- Empty backup: minimal valid JSON → verify no crash

## Security Considerations
- **MakerWorld cookies**: never exported (sensitive auth tokens)
- **File validation**: validate JSON structure before importing (prevent injection)
- **Size limit**: cap import file size (e.g., 1MB — settings files should be tiny)
- **No code execution**: treat all imported values as data, not commands

## Future Enhancements
- **Auto-backup**: periodic export to `/data/backups/` inside Docker volume
- **Selective import**: choose which sections to restore (filaments only, presets only, etc.)
- **Backup history**: keep last N backups with timestamps
- **Share profiles**: export individual filament profiles for sharing

## Estimated Complexity
- Backend export: ~60 lines
- Backend import: ~120 lines (upsert logic, FK resolution)
- Frontend UI: ~60 lines
- API layer: ~20 lines
- Tests: ~80 lines
- **Total: ~340 lines, Medium complexity**
