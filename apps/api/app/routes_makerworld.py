"""
MakerWorld integration routes.

Allows users to paste a MakerWorld URL, preview model info, and download
a 3MF file directly into the existing upload pipeline.
"""

import re
import json
import uuid
import asyncio
import random
import logging
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from upload_processor import process_3mf_file
from db import get_pg_pool

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/makerworld", tags=["makerworld"])

UPLOAD_DIR = Path("/data/uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

# Browser-like headers for page navigation (GET HTML).
# Must include Sec-* headers (MakerWorld rejects requests without them).
# Must NOT request brotli encoding (httpx can't decode it without brotli lib).
_PAGE_HEADERS = {
    "User-Agent": _UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Connection": "keep-alive",
    "DNT": "1",
    "Sec-Ch-Ua": '"Chromium";v="131", "Not_A Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

# Timeout for fetching MakerWorld pages and downloading 3MF files
_TIMEOUT = httpx.Timeout(30.0, read=120.0)

# XHR/fetch headers for MakerWorld API calls (matches what their JS sends)
_API_HEADERS = {
    "User-Agent": _UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Encoding": "gzip, deflate",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
    "DNT": "1",
    "Origin": "https://makerworld.com",
    "Sec-Ch-Ua": '"Chromium";v="131", "Not_A Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "X-BBL-Client-Type": "web",
    "X-BBL-Client-Version": "00.00.00.01",
    "X-BBL-App-Source": "makerworld",
    "X-BBL-Client-Name": "MakerWorld",
}


async def _get_makerworld_cookies() -> str | None:
    """Read stored MakerWorld cookies from printer_settings."""
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT makerworld_cookies FROM printer_settings WHERE id = 1"
        )
    return row["makerworld_cookies"] if row else None


def _extract_design_id(url: str) -> str | None:
    """Extract the numeric design ID from a MakerWorld URL."""
    match = re.search(r"(?:en/)?models/(\d+)", url)
    return match.group(1) if match else None


def _parse_next_data(html: str) -> dict:
    """Extract and parse the __NEXT_DATA__ JSON from MakerWorld HTML."""
    match = re.search(
        r'<script\s+id="__NEXT_DATA__"[^>]*>(.*?)</script>',
        html,
        re.DOTALL,
    )
    if not match:
        raise ValueError("Could not find model data in MakerWorld page")
    return json.loads(match.group(1))


class LookupRequest(BaseModel):
    url: str


class DownloadRequest(BaseModel):
    url: str
    instance_id: int


@router.post("/lookup")
async def makerworld_lookup(body: LookupRequest):
    """
    Fetch a MakerWorld model page and return metadata + available print profiles.
    """
    design_id = _extract_design_id(body.url)
    if not design_id:
        raise HTTPException(
            status_code=400,
            detail="Invalid MakerWorld URL. Expected format: https://makerworld.com/models/123456",
        )

    # Send cookies on page views too (logged-in users always have cookies)
    page_headers = {**_PAGE_HEADERS}
    cookies = await _get_makerworld_cookies()
    if cookies:
        page_headers["Cookie"] = cookies

    try:
        async with httpx.AsyncClient(
            headers=page_headers, timeout=_TIMEOUT, follow_redirects=True
        ) as client:
            resp = await client.get(f"https://makerworld.com/en/models/{design_id}")
            resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise HTTPException(status_code=404, detail="Model not found on MakerWorld")
        raise HTTPException(
            status_code=502,
            detail=f"MakerWorld returned HTTP {e.response.status_code}",
        )
    except httpx.RequestError as e:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to reach MakerWorld: {str(e)}",
        )

    try:
        next_data = _parse_next_data(resp.text)
    except (ValueError, json.JSONDecodeError) as e:
        raise HTTPException(status_code=502, detail=str(e))

    # Navigate the __NEXT_DATA__ structure to find design info
    try:
        page_props = next_data["props"]["pageProps"]
        design = page_props.get("design") or page_props.get("designItem")
        if not design:
            raise KeyError("design")
    except (KeyError, TypeError):
        raise HTTPException(
            status_code=502,
            detail="Could not parse model data from MakerWorld page. The page format may have changed.",
        )

    # Extract instances (print profiles / file variants)
    instances = design.get("instances") or design.get("instanceList") or []
    profiles = []
    for inst in instances:
        profiles.append({
            "instance_id": inst.get("id"),
            "title": inst.get("title") or inst.get("name") or f"Profile {inst.get('id')}",
            "description": inst.get("description") or "",
        })

    # If no instances found, try the designFiles approach
    if not profiles:
        design_files = design.get("designFiles") or []
        for df in design_files:
            if df.get("type") == "3mf" or (df.get("fileName") or "").endswith(".3mf"):
                profiles.append({
                    "instance_id": df.get("id"),
                    "title": df.get("fileName") or df.get("title") or "3MF File",
                    "description": "",
                })

    # Extract thumbnail
    thumbnail = None
    if design.get("cover"):
        thumbnail = design["cover"]
    elif design.get("images") and len(design["images"]) > 0:
        thumbnail = design["images"][0].get("url") or design["images"][0].get("src")
    elif design.get("coverUrl"):
        thumbnail = design["coverUrl"]

    return {
        "design_id": design.get("id") or design_id,
        "title": design.get("title") or design.get("name") or "Unknown Model",
        "author": (design.get("designer") or design.get("author") or {}).get("name")
            or (design.get("designCreator") or {}).get("name")
            or "Unknown",
        "thumbnail": thumbnail,
        "profiles": profiles,
        "profile_count": len(profiles),
    }


@router.post("/download")
async def makerworld_download(body: DownloadRequest):
    """
    Download a 3MF from MakerWorld and process it through the upload pipeline.
    Returns the same response as POST /upload.
    """
    design_id = _extract_design_id(body.url)
    if not design_id:
        raise HTTPException(
            status_code=400,
            detail="Invalid MakerWorld URL",
        )

    # Step 1: Simulate natural browsing — visit the model page first, then call API
    cookies = await _get_makerworld_cookies()
    model_url = f"https://makerworld.com/en/models/{design_id}"

    page_headers = {**_PAGE_HEADERS}
    if cookies:
        page_headers["Cookie"] = cookies

    try:
        async with httpx.AsyncClient(
            headers=page_headers, timeout=_TIMEOUT, follow_redirects=True
        ) as client:
            # Page view (like opening the model page before clicking Download)
            await client.get(model_url)
    except httpx.RequestError:
        pass  # Non-critical — continue to download even if page view fails

    # Small random delay to mimic human browsing (0.5-1.5s)
    await asyncio.sleep(random.uniform(0.5, 1.5))

    # Step 2: Get download URL from MakerWorld API
    dl_headers = {
        **_API_HEADERS,
        "Referer": model_url,
    }
    if cookies:
        dl_headers["Cookie"] = cookies

    try:
        async with httpx.AsyncClient(
            headers=dl_headers, timeout=_TIMEOUT, follow_redirects=True
        ) as client:
            api_url = f"https://makerworld.com/api/v1/design-service/instance/{body.instance_id}/f3mf?type=download"
            resp = await client.get(api_url)
            resp.raise_for_status()
            download_info = resp.json()
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 403:
            if cookies:
                raise HTTPException(
                    status_code=403,
                    detail="MakerWorld rejected your session cookies (they may have expired). Update them in Settings, or download the file manually.",
                )
            raise HTTPException(
                status_code=403,
                detail="MakerWorld requires login for this download. Add your MakerWorld cookies in Settings, or download the file manually and upload it here.",
            )
        raise HTTPException(
            status_code=502,
            detail=f"Failed to get download URL from MakerWorld (HTTP {e.response.status_code})",
        )
    except httpx.RequestError as e:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to reach MakerWorld download API: {str(e)}",
        )

    download_url = download_info.get("url")
    filename = download_info.get("name") or f"makerworld_{design_id}.3mf"

    if not download_url:
        raise HTTPException(
            status_code=502,
            detail="MakerWorld did not provide a download URL",
        )

    # Ensure .3mf extension
    if not filename.lower().endswith(".3mf"):
        filename += ".3mf"

    # Step 2: Download the 3MF file
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(download_url)
            resp.raise_for_status()
            content = resp.content
    except httpx.RequestError as e:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to download 3MF file: {str(e)}",
        )

    if len(content) < 100:
        raise HTTPException(
            status_code=502,
            detail="Downloaded file appears empty or invalid",
        )

    # Step 3: Save to upload directory
    file_id = uuid.uuid4().hex[:12]
    safe_filename = f"{file_id}_{filename}"
    file_path = UPLOAD_DIR / safe_filename
    try:
        file_path.write_bytes(content)
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to save downloaded file: {str(e)}",
        )

    # Step 4: Process through standard 3MF pipeline
    try:
        result = await process_3mf_file(file_path, filename, len(content))
        result["source"] = "makerworld"
        result["makerworld_design_id"] = design_id
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
