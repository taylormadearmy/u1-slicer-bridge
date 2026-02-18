from __future__ import annotations

from fastapi import FastAPI, HTTPException, UploadFile, File, Query
from pydantic import BaseModel, Field
from typing import Optional, List
import json
import os
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from db import init_db, close_db, get_pg_pool
from moonraker import init_moonraker, close_moonraker, get_moonraker, set_moonraker_url
from routes_upload import router as upload_router
from routes_slice import router as slice_router


async def _auto_init_filaments():
    """Auto-initialize default filaments if the library is empty (first run)."""
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        await _ensure_filament_schema(conn)
        count = await conn.fetchval("SELECT COUNT(*) FROM filaments")
        if count > 0:
            return

        print("Filament library empty — initializing starter filaments...")
        default_filaments = [
            {"name": "PLA Red", "material": "PLA", "nozzle_temp": 210, "bed_temp": 60, "print_speed": 200, "bed_type": "PEI", "color_hex": "#FF0000", "extruder_index": 0, "is_default": True, "source_type": "starter"},
            {"name": "PLA Blue", "material": "PLA", "nozzle_temp": 210, "bed_temp": 60, "print_speed": 200, "bed_type": "PEI", "color_hex": "#0000FF", "extruder_index": 1, "is_default": False, "source_type": "starter"},
            {"name": "PLA Green", "material": "PLA", "nozzle_temp": 210, "bed_temp": 60, "print_speed": 200, "bed_type": "PEI", "color_hex": "#00FF00", "extruder_index": 2, "is_default": False, "source_type": "starter"},
            {"name": "PLA Yellow", "material": "PLA", "nozzle_temp": 210, "bed_temp": 60, "print_speed": 200, "bed_type": "PEI", "color_hex": "#FFFF00", "extruder_index": 3, "is_default": False, "source_type": "starter"},
            {"name": "PETG", "material": "PETG", "nozzle_temp": 240, "bed_temp": 80, "print_speed": 150, "bed_type": "PEI", "color_hex": "#FF6600", "extruder_index": 0, "is_default": False, "source_type": "starter"},
            {"name": "ABS", "material": "ABS", "nozzle_temp": 250, "bed_temp": 100, "print_speed": 150, "bed_type": "Glass", "color_hex": "#333333", "extruder_index": 0, "is_default": False, "source_type": "starter"},
            {"name": "TPU", "material": "TPU", "nozzle_temp": 220, "bed_temp": 40, "print_speed": 30, "bed_type": "PEI", "color_hex": "#FF00FF", "extruder_index": 0, "is_default": False, "source_type": "starter"},
        ]
        for f in default_filaments:
            try:
                await conn.execute(
                    """INSERT INTO filaments (name, material, nozzle_temp, bed_temp, print_speed, bed_type, color_hex, extruder_index, is_default, source_type)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                    ON CONFLICT (name) DO NOTHING""",
                    f["name"], f["material"], f["nozzle_temp"], f["bed_temp"],
                    f["print_speed"], f["bed_type"], f["color_hex"], f["extruder_index"], f["is_default"], f["source_type"]
                )
            except Exception as e:
                print(f"Warning: Failed to insert starter filament {f['name']}: {e}")
        print(f"Initialized {len(default_filaments)} starter filaments")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await init_db()
    await init_moonraker(pool=get_pg_pool())
    await _auto_init_filaments()
    yield
    # Shutdown
    await close_moonraker()
    await close_db()


app = FastAPI(lifespan=lifespan)

# Configure CORS — default to allow all origins for LAN-first use (no auth).
# Set ALLOWED_ORIGINS env var (comma-separated) to restrict if needed.
_allowed_origins_env = os.environ.get("ALLOWED_ORIGINS")
if _allowed_origins_env:
    _cors_origins = [o.strip() for o in _allowed_origins_env.split(",") if o.strip()]
else:
    _cors_origins = ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(upload_router)
app.include_router(slice_router)


@app.get("/")
def root():
    return {
        "name": "U1 Slicer Bridge API",
        "version": os.getenv("APP_VERSION", "dev"),
        "web_ui": "http://localhost:8080",
        "endpoints": {
            "health": "/healthz",
            "printer": "/printer/status",
            "printer_settings": "GET/PUT /printer/settings",
            "send_to_printer": "POST /printer/print",
            "print_status": "GET /printer/print/status",
            "pause_print": "POST /printer/pause",
            "resume_print": "POST /printer/resume",
            "cancel_print": "POST /printer/cancel",
            "upload": "POST /upload",
            "uploads": "GET /upload",
            "slice": "POST /uploads/{id}/slice",
            "job_status": "GET /jobs/{job_id}"
        }
    }


@app.get("/healthz")
def health():
    return {"status": "ok", "version": os.getenv("APP_VERSION", "dev")}


@app.get("/printer/status")
async def printer_status():
    """Get printer connection status, info, and current print state."""
    client = get_moonraker()

    if not client:
        return {
            "connected": False,
            "message": "Moonraker not configured. Set printer URL in Settings."
        }

    is_healthy = await client.health_check()
    if not is_healthy:
        return {
            "connected": False,
            "message": "Cannot reach Moonraker. Check printer network connection."
        }

    try:
        server_info = await client.get_server_info()
        printer_info = await client.get_printer_info()

        # Also query print status for header display
        print_status = None
        try:
            print_status = await client.query_print_status()
        except Exception:
            pass  # Non-critical

        return {
            "connected": True,
            "server": server_info.get("result", {}),
            "printer": printer_info.get("result", {}),
            "print_status": print_status,
        }
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Printer error: {str(e)}")


# ---------------------------------------------------------------------------
# Printer Settings (configurable Moonraker URL)
# ---------------------------------------------------------------------------

@app.get("/printer/settings")
async def get_printer_settings():
    """Get printer connection settings."""
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT moonraker_url FROM printer_settings WHERE id = 1")
    return {"moonraker_url": row["moonraker_url"] if row else None}


class PrinterSettingsUpdate(BaseModel):
    moonraker_url: Optional[str] = None

@app.put("/printer/settings")
async def update_printer_settings(body: PrinterSettingsUpdate):
    """Save printer connection settings and reconnect."""
    pool = get_pg_pool()
    url = (body.moonraker_url or "").strip() or None

    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO printer_settings (id, moonraker_url, updated_at)
               VALUES (1, $1, NOW())
               ON CONFLICT (id) DO UPDATE SET moonraker_url = $1, updated_at = NOW()""",
            url,
        )

    # Reconnect (or disconnect) the global Moonraker client
    await set_moonraker_url(url or "")

    return {"moonraker_url": url, "message": "Saved"}


# ---------------------------------------------------------------------------
# Print Control
# ---------------------------------------------------------------------------

class PrintRequest(BaseModel):
    job_id: str

@app.post("/printer/print")
async def send_to_printer(body: PrintRequest):
    """Upload G-code to Moonraker and start printing."""
    client = get_moonraker()
    if not client:
        raise HTTPException(status_code=503, detail="Printer not configured. Set URL in Settings.")

    is_healthy = await client.health_check()
    if not is_healthy:
        raise HTTPException(status_code=503, detail="Printer not reachable")

    # Guard: reject if a print is already running
    print_status = await client.query_print_status()
    if print_status.get("state") in ("printing", "paused"):
        raise HTTPException(
            status_code=409,
            detail="A print is already in progress. Cancel it from the Printer Status page first.",
        )

    # Look up the G-code file from the job
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        job = await conn.fetchrow(
            "SELECT gcode_path, status FROM slicing_jobs WHERE job_id = $1",
            body.job_id,
        )

    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] != "completed":
        raise HTTPException(status_code=400, detail="Job not completed")

    from pathlib import Path
    gcode_path = Path(job["gcode_path"])
    if not gcode_path.exists():
        raise HTTPException(status_code=404, detail="G-code file not found on disk")

    filename = f"{body.job_id}.gcode"

    try:
        await client.upload_gcode(str(gcode_path), filename)
        await client.start_print(filename)
        return {"status": "printing", "filename": filename, "message": "Print started"}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to send to printer: {str(e)}")


@app.get("/printer/print/status")
async def get_print_status():
    """Get current print progress and state (lean polling endpoint)."""
    client = get_moonraker()
    if not client:
        raise HTTPException(status_code=503, detail="Printer not configured")

    try:
        return await client.query_print_status()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to query print status: {str(e)}")


@app.post("/printer/pause")
async def pause_print():
    """Pause the current print."""
    client = get_moonraker()
    if not client:
        raise HTTPException(status_code=503, detail="Printer not configured")
    try:
        await client.pause_print()
        return {"status": "paused", "message": "Print paused"}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to pause: {str(e)}")


@app.post("/printer/resume")
async def resume_print():
    """Resume a paused print."""
    client = get_moonraker()
    if not client:
        raise HTTPException(status_code=503, detail="Printer not configured")
    try:
        await client.resume_print()
        return {"status": "printing", "message": "Print resumed"}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to resume: {str(e)}")


@app.post("/printer/cancel")
async def cancel_print():
    """Cancel the current print."""
    client = get_moonraker()
    if not client:
        raise HTTPException(status_code=503, detail="Printer not configured")
    try:
        await client.cancel_print()
        return {"status": "cancelled", "message": "Print cancelled"}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to cancel: {str(e)}")


class FilamentCreate(BaseModel):
    name: str
    material: str
    nozzle_temp: int = Field(..., ge=100, le=400)
    bed_temp: int = Field(..., ge=0, le=150)
    print_speed: Optional[int] = Field(60, ge=5, le=600)
    bed_type: str = "PEI"
    color_hex: str = "#FFFFFF"
    extruder_index: int = Field(0, ge=0, le=3)
    is_default: bool = False
    source_type: str = "manual"
    density: Optional[float] = Field(1.24, ge=0.5, le=5.0)


class FilamentUpdate(BaseModel):
    name: str
    material: str
    nozzle_temp: int = Field(..., ge=100, le=400)
    bed_temp: int = Field(..., ge=0, le=150)
    print_speed: Optional[int] = Field(60, ge=5, le=600)
    bed_type: str = "PEI"
    color_hex: str = "#FFFFFF"
    extruder_index: int = Field(0, ge=0, le=3)
    is_default: bool = False
    source_type: str = "manual"
    density: Optional[float] = Field(1.24, ge=0.5, le=5.0)


async def _ensure_filament_schema(conn):
    await conn.execute("ALTER TABLE filaments ADD COLUMN IF NOT EXISTS source_type TEXT DEFAULT 'manual'")
    await conn.execute("ALTER TABLE filaments ADD COLUMN IF NOT EXISTS slicer_settings TEXT")
    await conn.execute("ALTER TABLE filaments ADD COLUMN IF NOT EXISTS density REAL DEFAULT 1.24")


class ExtruderPreset(BaseModel):
    slot: int
    filament_id: Optional[int] = None
    color_hex: str = "#FFFFFF"


class SlicingDefaults(BaseModel):
    layer_height: float = 0.2
    infill_density: int = 15
    wall_count: int = 3
    infill_pattern: str = "gyroid"
    supports: bool = False
    support_type: Optional[str] = None
    support_threshold_angle: Optional[int] = None
    brim_type: Optional[str] = None
    brim_width: Optional[float] = None
    brim_object_gap: Optional[float] = None
    skirt_loops: Optional[int] = None
    skirt_distance: Optional[float] = None
    skirt_height: Optional[int] = None
    enable_prime_tower: bool = False
    prime_volume: Optional[int] = None
    prime_tower_width: Optional[int] = None
    prime_tower_brim_width: Optional[int] = None
    prime_tower_brim_chamfer: bool = True
    prime_tower_brim_chamfer_max_width: Optional[int] = None
    enable_flow_calibrate: bool = True
    nozzle_temp: Optional[int] = None
    bed_temp: Optional[int] = None
    bed_type: Optional[str] = None
    setting_modes: Optional[dict] = None  # {"key": "model"|"orca"|"override"}


class ExtruderPresetUpdate(BaseModel):
    extruders: List[ExtruderPreset]
    slicing_defaults: Optional[SlicingDefaults] = None


async def _ensure_preset_rows(conn):
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS extruder_presets (
            slot INTEGER PRIMARY KEY,
            filament_id INTEGER REFERENCES filaments(id) ON DELETE SET NULL,
            color_hex VARCHAR(7) NOT NULL DEFAULT '#FFFFFF',
            updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
            CONSTRAINT chk_extruder_preset_slot CHECK (slot BETWEEN 1 AND 4)
        )
        """
    )

    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS slicing_defaults (
            id INTEGER PRIMARY KEY,
            layer_height REAL NOT NULL DEFAULT 0.2,
            infill_density INTEGER NOT NULL DEFAULT 15,
            wall_count INTEGER NOT NULL DEFAULT 3,
            infill_pattern TEXT NOT NULL DEFAULT 'gyroid',
            supports BOOLEAN NOT NULL DEFAULT FALSE,
            enable_prime_tower BOOLEAN NOT NULL DEFAULT FALSE,
            prime_volume INTEGER,
            prime_tower_width INTEGER,
            prime_tower_brim_width INTEGER,
            prime_tower_brim_chamfer BOOLEAN NOT NULL DEFAULT TRUE,
            prime_tower_brim_chamfer_max_width INTEGER,
            nozzle_temp INTEGER,
            bed_temp INTEGER,
            bed_type TEXT,
            updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
            CONSTRAINT chk_slicing_defaults_single_row CHECK (id = 1)
        )
        """
    )

    await conn.execute("ALTER TABLE slicing_defaults ADD COLUMN IF NOT EXISTS enable_prime_tower BOOLEAN NOT NULL DEFAULT FALSE")
    await conn.execute("ALTER TABLE slicing_defaults ADD COLUMN IF NOT EXISTS prime_volume INTEGER")
    await conn.execute("ALTER TABLE slicing_defaults ADD COLUMN IF NOT EXISTS prime_tower_width INTEGER")
    await conn.execute("ALTER TABLE slicing_defaults ADD COLUMN IF NOT EXISTS prime_tower_brim_width INTEGER")
    await conn.execute("ALTER TABLE slicing_defaults ADD COLUMN IF NOT EXISTS prime_tower_brim_chamfer BOOLEAN NOT NULL DEFAULT TRUE")
    await conn.execute("ALTER TABLE slicing_defaults ADD COLUMN IF NOT EXISTS prime_tower_brim_chamfer_max_width INTEGER")
    await conn.execute("ALTER TABLE slicing_defaults ADD COLUMN IF NOT EXISTS support_type TEXT")
    await conn.execute("ALTER TABLE slicing_defaults ADD COLUMN IF NOT EXISTS support_threshold_angle INTEGER")
    await conn.execute("ALTER TABLE slicing_defaults ADD COLUMN IF NOT EXISTS brim_type TEXT")
    await conn.execute("ALTER TABLE slicing_defaults ADD COLUMN IF NOT EXISTS brim_width REAL")
    await conn.execute("ALTER TABLE slicing_defaults ADD COLUMN IF NOT EXISTS brim_object_gap REAL")
    await conn.execute("ALTER TABLE slicing_defaults ADD COLUMN IF NOT EXISTS skirt_loops INTEGER")
    await conn.execute("ALTER TABLE slicing_defaults ADD COLUMN IF NOT EXISTS skirt_distance REAL")
    await conn.execute("ALTER TABLE slicing_defaults ADD COLUMN IF NOT EXISTS skirt_height INTEGER")
    await conn.execute("ALTER TABLE slicing_defaults ADD COLUMN IF NOT EXISTS setting_modes TEXT")
    await conn.execute("ALTER TABLE slicing_defaults ADD COLUMN IF NOT EXISTS enable_flow_calibrate BOOLEAN NOT NULL DEFAULT TRUE")

    for slot in range(1, 5):
        fallback_filament_id = await conn.fetchval(
            """
            SELECT id FROM filaments
            WHERE extruder_index = $1
            ORDER BY is_default DESC, id ASC
            LIMIT 1
            """,
            slot - 1,
        )
        await conn.execute(
            """
            INSERT INTO extruder_presets (slot, filament_id, color_hex)
            VALUES ($1, $2, '#FFFFFF')
            ON CONFLICT (slot) DO NOTHING
            """,
            slot,
            fallback_filament_id,
        )

    await conn.execute(
        """
        INSERT INTO slicing_defaults (id)
        VALUES (1)
        ON CONFLICT (id) DO NOTHING
        """
    )


@app.get("/filaments")
async def get_filaments():
    """Get all configured filament profiles."""
    from db import get_pg_pool
    pool = get_pg_pool()

    async with pool.acquire() as conn:
        await _ensure_filament_schema(conn)
        rows = await conn.fetch(
            """
            SELECT id, name, material, nozzle_temp, bed_temp, print_speed, bed_type, color_hex, extruder_index, is_default, source_type, slicer_settings, density
            FROM filaments
            ORDER BY is_default DESC,
                     CASE WHEN UPPER(material) = 'PLA' THEN 0 ELSE 1 END,
                     name
            """
        )

        filaments = [
            {
                "id": row["id"],
                "name": row["name"],
                "material": row["material"],
                "nozzle_temp": row["nozzle_temp"],
                "bed_temp": row["bed_temp"],
                "print_speed": row["print_speed"],
                "bed_type": row["bed_type"] or "PEI",
                "color_hex": row["color_hex"] or "#FFFFFF",
                "extruder_index": row["extruder_index"] or 0,
                "is_default": row["is_default"],
                "source_type": row["source_type"] or "manual",
                "has_slicer_settings": bool(row["slicer_settings"]),
                "density": round(float(row["density"]), 2) if row["density"] is not None else 1.24,
            }
            for row in rows
        ]

        return {"filaments": filaments}


@app.get("/presets/extruders")
async def get_extruder_presets():
    """Get extruder presets and default slicing settings."""
    from db import get_pg_pool
    pool = get_pg_pool()

    async with pool.acquire() as conn:
        await _ensure_preset_rows(conn)

        preset_rows = await conn.fetch(
            """
            SELECT slot, filament_id, color_hex
            FROM extruder_presets
            ORDER BY slot
            """
        )

        defaults = await conn.fetchrow(
            """
            SELECT layer_height, infill_density, wall_count, infill_pattern,
                   supports, enable_prime_tower, prime_volume, prime_tower_width, prime_tower_brim_width,
                   prime_tower_brim_chamfer, prime_tower_brim_chamfer_max_width,
                   enable_flow_calibrate,
                   nozzle_temp, bed_temp, bed_type,
                   support_type, support_threshold_angle,
                   brim_type, brim_width, brim_object_gap,
                   skirt_loops, skirt_distance, skirt_height,
                   setting_modes
            FROM slicing_defaults
            WHERE id = 1
            """
        )

    setting_modes = None
    if defaults["setting_modes"]:
        try:
            setting_modes = json.loads(defaults["setting_modes"])
        except Exception:
            pass

    return {
        "extruders": [
            {
                "slot": row["slot"],
                "filament_id": row["filament_id"],
                "color_hex": row["color_hex"] or "#FFFFFF",
            }
            for row in preset_rows
        ],
        "slicing_defaults": {
            "layer_height": round(float(defaults["layer_height"]), 3) if defaults["layer_height"] is not None else 0.2,
            "infill_density": defaults["infill_density"],
            "wall_count": defaults["wall_count"],
            "infill_pattern": defaults["infill_pattern"],
            "supports": defaults["supports"],
            "support_type": defaults["support_type"],
            "support_threshold_angle": defaults["support_threshold_angle"],
            "brim_type": defaults["brim_type"],
            "brim_width": float(defaults["brim_width"]) if defaults["brim_width"] is not None else None,
            "brim_object_gap": float(defaults["brim_object_gap"]) if defaults["brim_object_gap"] is not None else None,
            "skirt_loops": defaults["skirt_loops"],
            "skirt_distance": float(defaults["skirt_distance"]) if defaults["skirt_distance"] is not None else None,
            "skirt_height": defaults["skirt_height"],
            "enable_prime_tower": defaults["enable_prime_tower"],
            "prime_volume": defaults["prime_volume"],
            "prime_tower_width": defaults["prime_tower_width"],
            "prime_tower_brim_width": defaults["prime_tower_brim_width"],
            "prime_tower_brim_chamfer": defaults["prime_tower_brim_chamfer"],
            "prime_tower_brim_chamfer_max_width": defaults["prime_tower_brim_chamfer_max_width"],
            "enable_flow_calibrate": defaults["enable_flow_calibrate"],
            "nozzle_temp": defaults["nozzle_temp"],
            "bed_temp": defaults["bed_temp"],
            "bed_type": defaults["bed_type"],
            "setting_modes": setting_modes,
        },
    }


@app.put("/presets/extruders")
async def update_extruder_presets(payload: ExtruderPresetUpdate):
    """Update extruder presets and optional global slicing defaults."""
    from db import get_pg_pool
    pool = get_pg_pool()

    if len(payload.extruders) != 4:
        raise HTTPException(status_code=400, detail="Exactly 4 extruder presets are required (E1-E4).")

    slots = sorted(p.slot for p in payload.extruders)
    if slots != [1, 2, 3, 4]:
        raise HTTPException(status_code=400, detail="Extruder preset slots must be exactly [1,2,3,4].")

    async with pool.acquire() as conn:
        await _ensure_preset_rows(conn)

        # Validate filament IDs exist when provided.
        requested_ids = [p.filament_id for p in payload.extruders if p.filament_id is not None]
        if requested_ids:
            found = await conn.fetch(
                "SELECT id FROM filaments WHERE id = ANY($1)",
                requested_ids,
            )
            found_ids = {row["id"] for row in found}
            missing = [fid for fid in requested_ids if fid not in found_ids]
            if missing:
                raise HTTPException(status_code=404, detail=f"Filament IDs not found: {missing}")

        for preset in payload.extruders:
            await conn.execute(
                """
                INSERT INTO extruder_presets (slot, filament_id, color_hex, updated_at)
                VALUES ($1, $2, $3, NOW())
                ON CONFLICT (slot) DO UPDATE SET
                    filament_id = EXCLUDED.filament_id,
                    color_hex = EXCLUDED.color_hex,
                    updated_at = NOW()
                """,
                preset.slot,
                preset.filament_id,
                preset.color_hex,
            )

        if payload.slicing_defaults is not None:
            d = payload.slicing_defaults
            setting_modes_json = json.dumps(d.setting_modes) if d.setting_modes else None
            await conn.execute(
                """
                INSERT INTO slicing_defaults (
                    id, layer_height, infill_density, wall_count, infill_pattern,
                    supports, support_type, support_threshold_angle,
                    brim_type, brim_width, brim_object_gap,
                    skirt_loops, skirt_distance, skirt_height,
                    enable_prime_tower, prime_volume, prime_tower_width, prime_tower_brim_width,
                    prime_tower_brim_chamfer, prime_tower_brim_chamfer_max_width,
                    enable_flow_calibrate,
                    nozzle_temp, bed_temp, bed_type, setting_modes, updated_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19, $20, $21, $22, $23, $24, $25, NOW())
                ON CONFLICT (id) DO UPDATE SET
                    layer_height = EXCLUDED.layer_height,
                    infill_density = EXCLUDED.infill_density,
                    wall_count = EXCLUDED.wall_count,
                    infill_pattern = EXCLUDED.infill_pattern,
                    supports = EXCLUDED.supports,
                    support_type = EXCLUDED.support_type,
                    support_threshold_angle = EXCLUDED.support_threshold_angle,
                    brim_type = EXCLUDED.brim_type,
                    brim_width = EXCLUDED.brim_width,
                    brim_object_gap = EXCLUDED.brim_object_gap,
                    skirt_loops = EXCLUDED.skirt_loops,
                    skirt_distance = EXCLUDED.skirt_distance,
                    skirt_height = EXCLUDED.skirt_height,
                    enable_prime_tower = EXCLUDED.enable_prime_tower,
                    prime_volume = EXCLUDED.prime_volume,
                    prime_tower_width = EXCLUDED.prime_tower_width,
                    prime_tower_brim_width = EXCLUDED.prime_tower_brim_width,
                    prime_tower_brim_chamfer = EXCLUDED.prime_tower_brim_chamfer,
                    prime_tower_brim_chamfer_max_width = EXCLUDED.prime_tower_brim_chamfer_max_width,
                    enable_flow_calibrate = EXCLUDED.enable_flow_calibrate,
                    nozzle_temp = EXCLUDED.nozzle_temp,
                    bed_temp = EXCLUDED.bed_temp,
                    bed_type = EXCLUDED.bed_type,
                    setting_modes = EXCLUDED.setting_modes,
                    updated_at = NOW()
                """,
                1,
                d.layer_height,
                d.infill_density,
                d.wall_count,
                d.infill_pattern,
                d.supports,
                d.support_type,
                d.support_threshold_angle,
                d.brim_type,
                d.brim_width,
                d.brim_object_gap,
                d.skirt_loops,
                d.skirt_distance,
                d.skirt_height,
                d.enable_prime_tower,
                d.prime_volume,
                d.prime_tower_width,
                d.prime_tower_brim_width,
                d.prime_tower_brim_chamfer,
                d.prime_tower_brim_chamfer_max_width,
                d.enable_flow_calibrate,
                d.nozzle_temp,
                d.bed_temp,
                d.bed_type,
                setting_modes_json,
            )

    return {"message": "Extruder presets updated"}


@app.get("/presets/orca-defaults")
def get_orca_defaults():
    """Return Orca process profile defaults for UI display."""
    from pathlib import Path
    profile_path = Path(__file__).parent / "orca_profiles" / "process" / "0.20mm Standard @Snapmaker U1.json"
    try:
        with open(profile_path) as f:
            profile = json.load(f)
    except Exception:
        return {}
    return {
        "layer_height": float(profile.get("layer_height", "0.2")),
        "infill_density": int(str(profile.get("sparse_infill_density", "15")).rstrip("%")),
        "wall_count": int(profile.get("wall_loops", "2")),
        "infill_pattern": profile.get("sparse_infill_pattern", "grid"),
        "supports": bool(int(profile.get("enable_support", "0"))),
        "support_type": profile.get("support_type"),
        "brim_type": profile.get("brim_type", "auto_brim"),
        "brim_width": float(profile.get("brim_width", "5")),
        "skirt_loops": int(profile.get("skirt_loops", "2")),
        "skirt_distance": float(profile.get("skirt_distance", "3")),
        "skirt_height": int(profile.get("skirt_height", "1")),
        "enable_prime_tower": bool(int(profile.get("enable_prime_tower", "0"))),
    }


@app.post("/filaments")
async def create_filament(filament: FilamentCreate):
    """Create a new filament profile."""
    from db import get_pg_pool
    pool = get_pg_pool()

    async with pool.acquire() as conn:
        await _ensure_filament_schema(conn)
        async with conn.transaction():
            if filament.is_default:
                await conn.execute("UPDATE filaments SET is_default = FALSE WHERE is_default = TRUE")

            try:
                result = await conn.fetchrow(
                    """
                    INSERT INTO filaments (name, material, nozzle_temp, bed_temp, print_speed, bed_type, color_hex, extruder_index, is_default, source_type, density)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                    RETURNING id
                    """,
                    filament.name,
                    filament.material,
                    filament.nozzle_temp,
                    filament.bed_temp,
                    filament.print_speed,
                    filament.bed_type,
                    filament.color_hex,
                    filament.extruder_index,
                    filament.is_default,
                    filament.source_type,
                    filament.density,
                )
            except Exception as e:
                if "duplicate key" in str(e).lower() or "unique" in str(e).lower():
                    raise HTTPException(status_code=409, detail="Filament name already exists")
                raise

        return {"id": result["id"], "message": "Filament created"}


@app.put("/filaments/{filament_id}")
async def update_filament(filament_id: int, filament: FilamentUpdate):
    """Update a filament profile."""
    from db import get_pg_pool
    pool = get_pg_pool()

    async with pool.acquire() as conn:
        await _ensure_filament_schema(conn)
        async with conn.transaction():
            existing = await conn.fetchrow("SELECT id FROM filaments WHERE id = $1", filament_id)
            if not existing:
                raise HTTPException(status_code=404, detail="Filament not found")

            if filament.is_default:
                await conn.execute("UPDATE filaments SET is_default = FALSE WHERE id != $1", filament_id)

            try:
                await conn.execute(
                    """
                    UPDATE filaments
                    SET name = $1,
                        material = $2,
                        nozzle_temp = $3,
                        bed_temp = $4,
                        print_speed = $5,
                        bed_type = $6,
                        color_hex = $7,
                        extruder_index = $8,
                        is_default = $9,
                        source_type = $10,
                        density = $11
                    WHERE id = $12
                    """,
                    filament.name,
                    filament.material,
                    filament.nozzle_temp,
                    filament.bed_temp,
                    filament.print_speed,
                    filament.bed_type,
                    filament.color_hex,
                    filament.extruder_index,
                    filament.is_default,
                    filament.source_type,
                    filament.density,
                    filament_id,
                )
            except Exception as e:
                if "duplicate key" in str(e).lower() or "unique" in str(e).lower():
                    raise HTTPException(status_code=409, detail="Filament name already exists")
                raise

    return {"message": "Filament updated"}


@app.post("/filaments/{filament_id}/default")
async def set_default_filament(filament_id: int):
    """Set one filament as the default fallback filament."""
    from db import get_pg_pool
    pool = get_pg_pool()

    async with pool.acquire() as conn:
        async with conn.transaction():
            existing = await conn.fetchrow("SELECT id FROM filaments WHERE id = $1", filament_id)
            if not existing:
                raise HTTPException(status_code=404, detail="Filament not found")

            await conn.execute("UPDATE filaments SET is_default = FALSE")
            await conn.execute("UPDATE filaments SET is_default = TRUE WHERE id = $1", filament_id)

    return {"message": "Default filament updated"}


@app.delete("/filaments/{filament_id}")
async def delete_filament(filament_id: int):
    """Delete a filament profile with safety checks."""
    from db import get_pg_pool
    pool = get_pg_pool()

    async with pool.acquire() as conn:
        async with conn.transaction():
            existing = await conn.fetchrow("SELECT id, name, is_default FROM filaments WHERE id = $1", filament_id)
            if not existing:
                raise HTTPException(status_code=404, detail="Filament not found")

            usage = await conn.fetch("SELECT slot FROM extruder_presets WHERE filament_id = $1 ORDER BY slot", filament_id)
            if usage:
                slots = [f"E{row['slot']}" for row in usage]
                raise HTTPException(
                    status_code=400,
                    detail=f"Filament is assigned to printer presets ({', '.join(slots)}). Reassign those slots first.",
                )

            total_count = await conn.fetchval("SELECT COUNT(*) FROM filaments")
            if int(total_count or 0) <= 1:
                raise HTTPException(status_code=400, detail="Cannot delete the only filament profile")

            await conn.execute("DELETE FROM filaments WHERE id = $1", filament_id)

            if existing["is_default"]:
                replacement_id = await conn.fetchval(
                    """
                    SELECT id
                    FROM filaments
                    ORDER BY CASE WHEN UPPER(material) = 'PLA' THEN 0 ELSE 1 END, name
                    LIMIT 1
                    """
                )
                if replacement_id is not None:
                    await conn.execute("UPDATE filaments SET is_default = TRUE WHERE id = $1", replacement_id)

    return {"message": "Filament deleted"}


def _extract_profile_value(data, keys: List[str], default=None):
    if not isinstance(data, dict):
        return default
    for key in keys:
        if key in data and data[key] is not None:
            value = data[key]
            if isinstance(value, list) and value:
                return value[0]
            return value
    return default


def _normalize_color_hex(value) -> str:
    color_hex = str(value or "#FFFFFF").strip()
    if not color_hex.startswith("#"):
        color_hex = f"#{color_hex}"
    if len(color_hex) == 4:
        color_hex = f"#{color_hex[1]*2}{color_hex[2]*2}{color_hex[3]*2}"
    return color_hex[:7]


def _clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))


# OrcaSlicer filament keys that should be preserved and passed through to the slicer.
# These control advanced behavior that basic temp/speed fields don't capture.
_SLICER_PASSTHROUGH_KEYS = {
    "filament_max_volumetric_speed",
    "filament_flow_ratio",
    "filament_density",
    "filament_cost",
    "filament_shrink",
    "slow_down_layer_time",
    "fan_max_speed",
    "fan_min_speed",
    "overhang_fan_speed",
    "overhang_fan_threshold",
    "close_fan_the_first_x_layers",
    "full_fan_speed_layer",
    "reduce_fan_stop_start_freq",
    "additional_cooling_fan_speed",
    "filament_start_gcode",
    "filament_end_gcode",
    "filament_retraction_length",
    "filament_retract_before_wipe",
    "filament_retraction_speed",
    "filament_deretraction_speed",
    "filament_retract_restart_extra",
    "filament_retraction_minimum_travel",
    "filament_retract_when_changing_layer",
    "filament_wipe",
    "filament_wipe_distance",
    "filament_z_hop",
    "filament_z_hop_types",
    "cool_plate_temp",
    "cool_plate_temp_initial_layer",
    "textured_plate_temp",
    "textured_plate_temp_initial_layer",
    "nozzle_temperature_initial_layer",
    "bed_temperature_initial_layer",
    "bed_temperature_initial_layer_single",
}


def _parse_filament_profile_payload(file_name: str, payload: dict) -> dict:
    root = payload.get("filament") if isinstance(payload.get("filament"), dict) else payload

    profile_name = str(
        _extract_profile_value(root, ["name", "filament_name", "profile_name"], None)
        or file_name.rsplit(".", 1)[0]
    ).strip()

    material = str(_extract_profile_value(root, ["material", "filament_type", "type"], "PLA")).strip().upper()

    def _as_int(value, fallback: int) -> int:
        try:
            return int(float(value))
        except Exception:
            return fallback

    nozzle_temp = _clamp(_as_int(_extract_profile_value(root, ["nozzle_temp", "nozzle_temperature", "temperature"], 210), 210), 100, 400)
    bed_temp = _clamp(_as_int(_extract_profile_value(root, ["bed_temp", "bed_temperature"], 60), 60), 0, 150)

    # Detect whether this looks like an OrcaSlicer/Bambu profile
    is_orca_profile = root.get("type") == "filament" or "nozzle_temperature" in root or "filament_type" in root

    # print_speed: Bambu/Orca filament profiles don't include speed (it's a process setting).
    # Derive from filament_max_volumetric_speed if available, else use material defaults.
    explicit_speed = _extract_profile_value(root, ["print_speed", "speed"], None)
    if explicit_speed is not None:
        print_speed = _clamp(_as_int(explicit_speed, 60), 5, 600)
    elif is_orca_profile:
        vol_speed = _extract_profile_value(root, ["filament_max_volumetric_speed"], None)
        if vol_speed is not None:
            # Convert volumetric (mm³/s) to linear (mm/s) assuming 0.4mm nozzle, 0.2mm layer
            vol = _as_int(vol_speed, 0)
            print_speed = _clamp(round(vol / 0.08), 30, 500) if vol > 0 else 200
        else:
            # Material-appropriate defaults matching Bambu Studio
            _material_speeds = {"PLA": 200, "PETG": 150, "ABS": 150, "ASA": 150, "TPU": 30, "PA": 100, "PC": 100}
            print_speed = _material_speeds.get(material, 200)
    else:
        print_speed = 60

    # Extract slicer-native settings for passthrough
    slicer_settings = {}
    matched_keys = 0
    for key in _SLICER_PASSTHROUGH_KEYS:
        if key in root and root[key] is not None:
            slicer_settings[key] = root[key]
            matched_keys += 1

    return {
        "name": profile_name,
        "material": material,
        "nozzle_temp": nozzle_temp,
        "bed_temp": bed_temp,
        "print_speed": print_speed,
        "bed_type": str(_extract_profile_value(root, ["bed_type", "build_plate_type"], "PEI")).strip() or "PEI",
        "color_hex": _normalize_color_hex(_extract_profile_value(root, ["color_hex", "color", "filament_colour"], "#FFFFFF")),
        "density": _clamp(float(_extract_profile_value(root, ["filament_density", "density"], "1.24") or "1.24"), 0.5, 5.0),
        "slicer_settings": slicer_settings if slicer_settings else None,
        "is_recognized": is_orca_profile or matched_keys > 0,
    }


@app.post("/filaments/import")
async def import_filament_profile(file: UploadFile = File(...), rename_on_conflict: bool = Query(True)):
    """Import a filament profile from JSON and add to library."""
    if not file.filename:
        raise HTTPException(status_code=400, detail="Missing filename")

    if not file.filename.lower().endswith(".json"):
        raise HTTPException(status_code=400, detail="Only JSON filament profiles are supported for now")

    try:
        raw = await file.read()
        payload = json.loads(raw.decode("utf-8", errors="ignore"))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON profile file")

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Profile JSON must be an object")

    parsed = _parse_filament_profile_payload(file.filename, payload)
    profile_name = parsed["name"]

    from db import get_pg_pool
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        await _ensure_filament_schema(conn)

        existing = await conn.fetchrow("SELECT id FROM filaments WHERE name = $1", profile_name)
        if existing and not rename_on_conflict:
            raise HTTPException(status_code=409, detail="A filament with this profile name already exists")

        if existing and rename_on_conflict:
            base_name = profile_name
            suffix = 2
            while True:
                candidate = f"{base_name} ({suffix})"
                candidate_exists = await conn.fetchrow("SELECT id FROM filaments WHERE name = $1", candidate)
                if not candidate_exists:
                    profile_name = candidate
                    break
                suffix += 1

        slicer_json = json.dumps(parsed["slicer_settings"]) if parsed["slicer_settings"] else None

        row = await conn.fetchrow(
            """
            INSERT INTO filaments (
                name, material, nozzle_temp, bed_temp, print_speed,
                bed_type, color_hex, extruder_index, is_default, source_type, slicer_settings, density
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, 0, FALSE, 'custom', $8, $9)
            RETURNING id
            """,
            profile_name,
            parsed["material"],
            parsed["nozzle_temp"],
            parsed["bed_temp"],
            parsed["print_speed"],
            parsed["bed_type"],
            parsed["color_hex"],
            slicer_json,
            parsed["density"],
        )

    return {
        "id": row["id"],
        "message": "Filament profile imported",
        "name": profile_name,
        "has_slicer_settings": bool(parsed["slicer_settings"]),
        "is_recognized": parsed["is_recognized"],
    }


@app.post("/filaments/import/preview")
async def preview_filament_profile_import(file: UploadFile = File(...)):
    """Preview parsed filament profile values before import."""
    if not file.filename:
        raise HTTPException(status_code=400, detail="Missing filename")

    if not file.filename.lower().endswith(".json"):
        raise HTTPException(status_code=400, detail="Only JSON filament profiles are supported for now")

    try:
        raw = await file.read()
        payload = json.loads(raw.decode("utf-8", errors="ignore"))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON profile file")

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Profile JSON must be an object")

    parsed = _parse_filament_profile_payload(file.filename, payload)

    from db import get_pg_pool
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        await _ensure_filament_schema(conn)
        existing = await conn.fetchrow("SELECT id, name FROM filaments WHERE name = $1", parsed["name"])

    return {
        "preview": {
            "name": parsed["name"],
            "material": parsed["material"],
            "nozzle_temp": parsed["nozzle_temp"],
            "bed_temp": parsed["bed_temp"],
            "print_speed": parsed["print_speed"],
            "bed_type": parsed["bed_type"],
            "color_hex": parsed["color_hex"],
            "density": parsed["density"],
            "has_slicer_settings": bool(parsed["slicer_settings"]),
            "slicer_setting_count": len(parsed["slicer_settings"]) if parsed["slicer_settings"] else 0,
            "is_recognized": parsed["is_recognized"],
        },
        "would_conflict": existing is not None,
        "conflict_name": existing["name"] if existing else None,
    }


@app.get("/filaments/{filament_id}/export")
async def export_filament_profile(filament_id: int):
    """Export a filament profile as OrcaSlicer-compatible JSON."""
    from db import get_pg_pool
    pool = get_pg_pool()

    async with pool.acquire() as conn:
        await _ensure_filament_schema(conn)
        row = await conn.fetchrow(
            "SELECT name, material, nozzle_temp, bed_temp, print_speed, bed_type, color_hex, slicer_settings, density FROM filaments WHERE id = $1",
            filament_id,
        )
        if not row:
            raise HTTPException(status_code=404, detail="Filament not found")

    # Build an OrcaSlicer-compatible filament profile
    profile: dict = {
        "type": "filament",
        "name": row["name"],
        "from": "u1-slicer-bridge",
        "instantiation": "true",
        "filament_type": [row["material"]],
        "nozzle_temperature": [str(row["nozzle_temp"])],
        "bed_temperature": [str(row["bed_temp"])],
        "filament_colour": [row["color_hex"] or "#FFFFFF"],
        "filament_density": [str(row["density"] or 1.24)],
    }

    # Merge stored slicer-native settings if present
    if row["slicer_settings"]:
        try:
            stored = json.loads(row["slicer_settings"])
            if isinstance(stored, dict):
                profile.update(stored)
        except Exception:
            pass

    from fastapi.responses import Response
    return Response(
        content=json.dumps(profile, indent=2),
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{row["name"]}.json"',
        },
    )


@app.post("/filaments/init-defaults")
async def init_default_filaments():
    """Initialize default filament profiles."""
    from db import get_pg_pool
    pool = get_pg_pool()

    default_filaments = [
        {"name": "PLA Red", "material": "PLA", "nozzle_temp": 210, "bed_temp": 60, "print_speed": 200, "bed_type": "PEI", "color_hex": "#FF0000", "extruder_index": 0, "is_default": True, "source_type": "starter"},
        {"name": "PLA Blue", "material": "PLA", "nozzle_temp": 210, "bed_temp": 60, "print_speed": 200, "bed_type": "PEI", "color_hex": "#0000FF", "extruder_index": 1, "is_default": False, "source_type": "starter"},
        {"name": "PLA Green", "material": "PLA", "nozzle_temp": 210, "bed_temp": 60, "print_speed": 200, "bed_type": "PEI", "color_hex": "#00FF00", "extruder_index": 2, "is_default": False, "source_type": "starter"},
        {"name": "PLA Yellow", "material": "PLA", "nozzle_temp": 210, "bed_temp": 60, "print_speed": 200, "bed_type": "PEI", "color_hex": "#FFFF00", "extruder_index": 3, "is_default": False, "source_type": "starter"},
        {"name": "PETG", "material": "PETG", "nozzle_temp": 240, "bed_temp": 80, "print_speed": 150, "bed_type": "PEI", "color_hex": "#FF6600", "extruder_index": 0, "is_default": False, "source_type": "starter"},
        {"name": "ABS", "material": "ABS", "nozzle_temp": 250, "bed_temp": 100, "print_speed": 150, "bed_type": "Glass", "color_hex": "#333333", "extruder_index": 0, "is_default": False, "source_type": "starter"},
        {"name": "TPU", "material": "TPU", "nozzle_temp": 220, "bed_temp": 40, "print_speed": 30, "bed_type": "PEI", "color_hex": "#FF00FF", "extruder_index": 0, "is_default": False, "source_type": "starter"},
    ]

    async with pool.acquire() as conn:
        await _ensure_filament_schema(conn)
        for f in default_filaments:
            try:
                await conn.execute(
                    """
                    INSERT INTO filaments (name, material, nozzle_temp, bed_temp, print_speed, bed_type, color_hex, extruder_index, is_default, source_type)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                    ON CONFLICT (name) DO NOTHING
                    """,
                    f["name"], f["material"], f["nozzle_temp"], f["bed_temp"],
                    f["print_speed"], f["bed_type"], f["color_hex"], f["extruder_index"], f["is_default"], f["source_type"]
                )
            except Exception as e:
                pass

        return {"message": "Default filaments initialized"}
