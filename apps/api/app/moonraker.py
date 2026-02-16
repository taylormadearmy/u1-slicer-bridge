import os
from pathlib import Path
from typing import Optional, Dict, Any
import httpx


class MoonrakerClient:
    """Moonraker API client for printer communication."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.client: Optional[httpx.AsyncClient] = None

    async def connect(self):
        """Initialize HTTP client."""
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=10.0,
            follow_redirects=True
        )

    async def close(self):
        """Close HTTP client."""
        if self.client:
            await self.client.aclose()
            self.client = None

    async def reconnect(self, new_url: str):
        """Close existing connection and reconnect to a new URL."""
        await self.close()
        self.base_url = new_url.rstrip("/")
        await self.connect()

    async def get_printer_info(self) -> Dict[str, Any]:
        """Get printer information and status."""
        if not self.client:
            raise RuntimeError("Client not connected. Call connect() first.")

        response = await self.client.get("/printer/info")
        response.raise_for_status()
        return response.json()

    async def get_server_info(self) -> Dict[str, Any]:
        """Get Moonraker server information."""
        if not self.client:
            raise RuntimeError("Client not connected. Call connect() first.")

        response = await self.client.get("/server/info")
        response.raise_for_status()
        return response.json()

    async def health_check(self) -> bool:
        """Check if Moonraker is reachable."""
        try:
            await self.get_server_info()
            return True
        except Exception:
            return False

    async def upload_gcode(self, gcode_path: str, filename: str) -> Dict[str, Any]:
        """Upload a G-code file to Moonraker's virtual SD card."""
        if not self.client:
            raise RuntimeError("Client not connected. Call connect() first.")

        path = Path(gcode_path)
        if not path.exists():
            raise FileNotFoundError(f"G-code file not found: {gcode_path}")

        file_size = path.stat().st_size
        # Dynamic timeout: min 30s, ~1s per MB, max 300s
        upload_timeout = min(300.0, max(30.0, file_size / (1024 * 1024)))

        with open(path, "rb") as f:
            files = {"file": (filename, f, "application/octet-stream")}
            data = {"root": "gcodes"}
            response = await self.client.post(
                "/server/files/upload",
                files=files,
                data=data,
                timeout=upload_timeout,
            )
        response.raise_for_status()
        return response.json()

    async def start_print(self, filename: str) -> Dict[str, Any]:
        """Start printing a file already uploaded to the printer."""
        if not self.client:
            raise RuntimeError("Client not connected. Call connect() first.")

        response = await self.client.post(
            "/printer/print/start",
            params={"filename": filename},
        )
        response.raise_for_status()
        return response.json()

    async def pause_print(self) -> Dict[str, Any]:
        """Pause the current print."""
        if not self.client:
            raise RuntimeError("Client not connected. Call connect() first.")
        response = await self.client.post("/printer/print/pause")
        response.raise_for_status()
        return response.json()

    async def resume_print(self) -> Dict[str, Any]:
        """Resume a paused print."""
        if not self.client:
            raise RuntimeError("Client not connected. Call connect() first.")
        response = await self.client.post("/printer/print/resume")
        response.raise_for_status()
        return response.json()

    async def cancel_print(self) -> Dict[str, Any]:
        """Cancel the current print."""
        if not self.client:
            raise RuntimeError("Client not connected. Call connect() first.")
        response = await self.client.post("/printer/print/cancel")
        response.raise_for_status()
        return response.json()

    async def query_print_status(self) -> Dict[str, Any]:
        """Query printer objects for print status, progress, and temperatures."""
        if not self.client:
            raise RuntimeError("Client not connected. Call connect() first.")

        response = await self.client.get(
            "/printer/objects/query",
            params={
                "print_stats": "",
                "virtual_sdcard": "",
                "extruder": "",
                "heater_bed": "",
            },
        )
        response.raise_for_status()
        data = response.json().get("result", {}).get("status", {})

        print_stats = data.get("print_stats", {})
        virtual_sdcard = data.get("virtual_sdcard", {})
        extruder = data.get("extruder", {})
        heater_bed = data.get("heater_bed", {})

        return {
            "state": print_stats.get("state", "standby"),
            "progress": virtual_sdcard.get("progress", 0.0),
            "filename": print_stats.get("filename"),
            "duration": print_stats.get("print_duration", 0.0),
            "filament_used": print_stats.get("filament_used", 0.0),
            "nozzle_temp": extruder.get("temperature", 0.0),
            "nozzle_target": extruder.get("target", 0.0),
            "bed_temp": heater_bed.get("temperature", 0.0),
            "bed_target": heater_bed.get("target", 0.0),
        }


# Global client instance
_moonraker_client: Optional[MoonrakerClient] = None


async def init_moonraker(pool=None):
    """Initialize Moonraker client. Checks DB first, then env var."""
    global _moonraker_client

    moonraker_url = None

    # Try DB first
    if pool:
        try:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT moonraker_url FROM printer_settings WHERE id = 1"
                )
                if row and row["moonraker_url"]:
                    moonraker_url = row["moonraker_url"]
        except Exception:
            pass  # Table may not exist yet on first run

    # Fall back to env var
    if not moonraker_url:
        moonraker_url = os.getenv("MOONRAKER_URL")

    if not moonraker_url:
        return

    _moonraker_client = MoonrakerClient(moonraker_url)
    await _moonraker_client.connect()


async def close_moonraker():
    """Close Moonraker client."""
    global _moonraker_client

    if _moonraker_client:
        await _moonraker_client.close()


def get_moonraker() -> Optional[MoonrakerClient]:
    """Get Moonraker client (may be None if not configured)."""
    return _moonraker_client


async def set_moonraker_url(url: str):
    """Set or change the Moonraker URL at runtime."""
    global _moonraker_client

    if not url:
        if _moonraker_client:
            await _moonraker_client.close()
            _moonraker_client = None
        return

    if _moonraker_client:
        await _moonraker_client.reconnect(url)
    else:
        _moonraker_client = MoonrakerClient(url)
        await _moonraker_client.connect()
