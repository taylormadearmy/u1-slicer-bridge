import os
from typing import Optional, Dict, Any
import httpx


class MoonrakerClient:
    """Minimal Moonraker API client for printer communication."""

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


# Global client instance
_moonraker_client: Optional[MoonrakerClient] = None


async def init_moonraker():
    """Initialize Moonraker client."""
    global _moonraker_client

    moonraker_url = os.getenv("MOONRAKER_URL")
    if not moonraker_url:
        # Optional: printer not configured yet
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
