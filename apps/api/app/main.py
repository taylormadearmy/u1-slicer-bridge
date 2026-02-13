from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from db import init_db, close_db
from moonraker import init_moonraker, close_moonraker, get_moonraker
from routes_upload import router as upload_router
from routes_slice import router as slice_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await init_db()
    await init_moonraker()
    yield
    # Shutdown
    await close_moonraker()
    await close_db()


app = FastAPI(lifespan=lifespan)

# Configure CORS to allow web UI to access API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8080", "http://127.0.0.1:8080"],
    allow_credentials=True,
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
        "version": "1.0.0",
        "web_ui": "http://localhost:8080",
        "endpoints": {
            "health": "/healthz",
            "printer": "/printer/status",
            "upload": "POST /upload",
            "uploads": "GET /upload",
            "slice": "POST /uploads/{id}/slice",
            "job_status": "GET /jobs/{job_id}"
        }
    }


@app.get("/healthz")
def health():
    return {"status": "ok"}


@app.get("/printer/status")
async def printer_status():
    """Get printer connection status and info."""
    client = get_moonraker()

    if not client:
        return {
            "connected": False,
            "message": "Moonraker not configured. Set MOONRAKER_URL environment variable."
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

        return {
            "connected": True,
            "server": server_info.get("result", {}),
            "printer": printer_info.get("result", {})
        }
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Printer error: {str(e)}")


@app.get("/filaments")
async def get_filaments():
    """Get all configured filament profiles."""
    from db import get_pg_pool
    pool = get_pg_pool()

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, name, material, nozzle_temp, bed_temp, print_speed, bed_type, is_default FROM filaments ORDER BY name"
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
                "is_default": row["is_default"]
            }
            for row in rows
        ]

        return {"filaments": filaments}


class FilamentCreate(BaseModel):
    name: str
    material: str
    nozzle_temp: int
    bed_temp: int
    print_speed: Optional[int] = 60
    bed_type: str = "PEI"
    is_default: bool = False


@app.post("/filaments")
async def create_filament(filament: FilamentCreate):
    """Create a new filament profile."""
    from db import get_pg_pool
    pool = get_pg_pool()

    async with pool.acquire() as conn:
        result = await conn.fetchrow(
            """
            INSERT INTO filaments (name, material, nozzle_temp, bed_temp, print_speed, bed_type, is_default)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            RETURNING id
            """,
            filament.name,
            filament.material,
            filament.nozzle_temp,
            filament.bed_temp,
            filament.print_speed,
            filament.bed_type,
            filament.is_default
        )

        return {"id": result["id"], "message": "Filament created"}


@app.post("/filaments/init-defaults")
async def init_default_filaments():
    """Initialize default filament profiles."""
    from db import get_pg_pool
    pool = get_pg_pool()

    default_filaments = [
        {"name": "PLA Red", "material": "PLA", "nozzle_temp": 210, "bed_temp": 60, "print_speed": 60, "bed_type": "PEI", "is_default": True},
        {"name": "PLA Blue", "material": "PLA", "nozzle_temp": 210, "bed_temp": 60, "print_speed": 60, "bed_type": "PEI", "is_default": False},
        {"name": "PETG", "material": "PETG", "nozzle_temp": 240, "bed_temp": 80, "print_speed": 50, "bed_type": "PEI", "is_default": False},
        {"name": "ABS", "material": "ABS", "nozzle_temp": 250, "bed_temp": 100, "print_speed": 50, "bed_type": "Glass", "is_default": False},
        {"name": "TPU", "material": "TPU", "nozzle_temp": 220, "bed_temp": 40, "print_speed": 30, "bed_type": "PEI", "is_default": False},
    ]

    async with pool.acquire() as conn:
        for f in default_filaments:
            try:
                await conn.execute(
                    """
                    INSERT INTO filaments (name, material, nozzle_temp, bed_temp, print_speed, bed_type, is_default)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    ON CONFLICT (name) DO NOTHING
                    """,
                    f["name"], f["material"], f["nozzle_temp"], f["bed_temp"],
                    f["print_speed"], f["bed_type"], f["is_default"]
                )
            except Exception as e:
                pass

        return {"message": "Default filaments initialized"}
