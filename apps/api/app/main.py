from fastapi import FastAPI, HTTPException
from contextlib import asynccontextmanager
from db import init_db, close_db
from moonraker import init_moonraker, close_moonraker, get_moonraker
from routes_upload import router as upload_router
from routes_normalize import router as normalize_router
from routes_bundle import router as bundle_router
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

# Include routers
app.include_router(upload_router)
app.include_router(normalize_router)
app.include_router(bundle_router)
app.include_router(slice_router)


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
