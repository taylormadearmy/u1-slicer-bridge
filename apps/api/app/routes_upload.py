import os
import uuid
import json
import logging
from pathlib import Path
from datetime import datetime
from fastapi import APIRouter, UploadFile, File, HTTPException, Query
from fastapi.responses import FileResponse
from db import get_pg_pool

logger = logging.getLogger(__name__)
from parser_3mf import parse_3mf, detect_colors_from_3mf, detect_colors_per_plate, detect_print_settings
from plate_validator import PlateValidator, PlateValidationError
from config import get_printer_profile
from multi_plate_parser import parse_multi_plate_3mf
from stl_converter import convert_stl_to_3mf, STLConversionError
from upload_processor import process_3mf_file


router = APIRouter(prefix="/upload", tags=["upload"])

UPLOAD_DIR = Path("/data/uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


@router.post("")
async def upload_3mf(file: UploadFile = File(...)):
    """
    Upload a .3mf file and extract object metadata.

    Returns upload ID and list of objects found.
    """
    # Validate file extension
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")
    lower_name = file.filename.lower()
    is_stl = lower_name.endswith(".stl")
    if not lower_name.endswith(".3mf") and not is_stl:
        raise HTTPException(status_code=400, detail="Only .3mf and .stl files are supported")

    # Read file content
    try:
        content = await file.read()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read file: {str(e)}")

    # For STL files, convert to 3MF first
    if is_stl:
        try:
            conversion = convert_stl_to_3mf(content, file.filename)
            file_path = conversion["file_path"]
        except STLConversionError as e:
            raise HTTPException(status_code=400, detail=str(e))
    else:
        # Save 3MF directly
        file_id = uuid.uuid4().hex[:12]
        safe_filename = f"{file_id}_{file.filename}"
        file_path = UPLOAD_DIR / safe_filename
        try:
            file_path.write_bytes(content)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to save file: {str(e)}")

    # Both STL (converted to 3MF) and native 3MF use the shared processor
    try:
        return await process_3mf_file(file_path, file.filename, len(content))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{upload_id}")
async def get_upload(upload_id: int):
    """Get upload details including plate bounds."""
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        upload = await conn.fetchrow(
            """
            SELECT id, filename, file_path, file_size, uploaded_at, plate_validated,
                   bounds_min_x, bounds_min_y, bounds_min_z,
                   bounds_max_x, bounds_max_y, bounds_max_z,
                   bounds_warning, is_multi_plate, plate_count,
                   detected_colors, file_print_settings
            FROM uploads WHERE id = $1
            """,
            upload_id,
        )
        if not upload:
            raise HTTPException(status_code=404, detail="Upload not found")

    # Build response from cached DB data (no file re-parsing needed)
    bounds = None
    if upload['plate_validated']:
        bounds_min = [upload['bounds_min_x'], upload['bounds_min_y'], upload['bounds_min_z']]
        bounds_max = [upload['bounds_max_x'], upload['bounds_max_y'], upload['bounds_max_z']]
        bounds = {
            "min": bounds_min,
            "max": bounds_max,
            "size": [bounds_max[i] - bounds_min[i] for i in range(3)]
        }

    warnings = []
    if upload['bounds_warning']:
        warnings = upload['bounds_warning'].split('\n')
    fits = len(warnings) == 0

    response = {
        "upload_id": upload["id"],
        "filename": upload["filename"],
        "file_size": upload["file_size"],
        "uploaded_at": upload["uploaded_at"].isoformat(),
        "plate_validated": upload["plate_validated"],
        "bounds": bounds,
        "warnings": warnings,
        "fits": fits
    }

    # Cached colors
    if upload["detected_colors"]:
        try:
            detected_colors = json.loads(upload["detected_colors"])
            if detected_colors:
                response["detected_colors"] = detected_colors
                response["has_multicolor"] = len(detected_colors) > 1
        except Exception:
            pass
    else:
        # Fallback for old uploads without cached colors
        try:
            file_path = Path(upload["file_path"])
            detected_colors = detect_colors_from_3mf(file_path)
            if detected_colors:
                response["detected_colors"] = detected_colors
                response["has_multicolor"] = len(detected_colors) > 1
        except Exception as e:
            logger.warning(f"Failed to detect colors: {e}")

    # Cached print settings
    if upload["file_print_settings"]:
        try:
            fps = json.loads(upload["file_print_settings"])
            if fps:
                response["file_print_settings"] = fps
        except Exception:
            pass
    else:
        # Fallback for old uploads
        try:
            fps = detect_print_settings(Path(upload["file_path"]))
            if fps:
                response["file_print_settings"] = fps
        except Exception as e:
            logger.warning(f"Failed to detect print settings: {e}")

    # Multi-plate info
    if upload["is_multi_plate"]:
        response["is_multi_plate"] = True
        response["plate_count"] = upload["plate_count"] or 0

    return response


@router.get("/{upload_id}/download")
async def download_3mf(upload_id: int):
    """Download the original uploaded 3MF file."""
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        upload = await conn.fetchrow(
            "SELECT file_path, filename FROM uploads WHERE id = $1",
            upload_id,
        )
        if not upload:
            raise HTTPException(status_code=404, detail="Upload not found")

    file_path = Path(upload["file_path"])
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="3MF file not found on disk")

    return FileResponse(
        path=file_path,
        media_type="application/vnd.ms-package.3dmanufacturing-3dmodel+xml",
        filename=upload["filename"],
    )


@router.get("")
async def list_uploads(
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """List all uploads with plate validation status."""
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        total = await conn.fetchval("SELECT COUNT(*) FROM uploads")
        uploads = await conn.fetch(
            """
            SELECT id, filename, file_path, file_size, uploaded_at, plate_validated, bounds_warning
            FROM uploads
            ORDER BY uploaded_at DESC
            LIMIT $1 OFFSET $2
            """,
            limit,
            offset,
        )

    # Keep list endpoint fast; detailed re-validation is handled by GET /upload/{id}.
    upload_list = [
        {
            "upload_id": u["id"],
            "filename": u["filename"],
            "file_size": u["file_size"],
            "uploaded_at": u["uploaded_at"].isoformat(),
            "plate_validated": u["plate_validated"],
            "has_warnings": bool(u["bounds_warning"]),
        }
        for u in uploads
    ]

    return {
        "uploads": upload_list,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + limit < total,
    }


@router.delete("/{upload_id}")
async def delete_upload(upload_id: int):
    """Delete an upload and all associated slicing jobs."""
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        # Get upload info
        upload = await conn.fetchrow(
            "SELECT file_path FROM uploads WHERE id = $1",
            upload_id
        )
        
        if not upload:
            raise HTTPException(status_code=404, detail="Upload not found")
        
        # Delete 3MF file if exists
        if upload["file_path"]:
            file_path = Path(upload["file_path"])
            if file_path.exists():
                file_path.unlink()
        
        # Get all job IDs for this upload to delete their G-code and log files
        jobs = await conn.fetch(
            "SELECT job_id, gcode_path FROM slicing_jobs WHERE upload_id = $1",
            upload_id
        )
        
        for job in jobs:
            # Delete G-code file
            if job["gcode_path"]:
                gcode_path = Path(job["gcode_path"])
                if gcode_path.exists():
                    gcode_path.unlink()
            
            # Delete log file
            log_path = Path(f"/data/logs/slice_{job['job_id']}.log")
            if log_path.exists():
                log_path.unlink()
        
        # Delete jobs from database (FK will cascade but being explicit)
        await conn.execute("DELETE FROM slicing_jobs WHERE upload_id = $1", upload_id)
        
        # Delete upload from database
        await conn.execute("DELETE FROM uploads WHERE id = $1", upload_id)
    
    return {"message": "Upload deleted successfully"}


# -----------------------------------------------------------------------
# Multiple Copies (M32)
# -----------------------------------------------------------------------

@router.post("/{upload_id}/copies")
async def apply_copies(upload_id: int, body: dict):
    """Add multiple copies of the object arranged in a grid on the build plate.

    Body: { "copies": 4, "spacing": 5.0 }
    """
    from copy_duplicator import apply_copies_to_3mf, get_object_dimensions, estimate_max_copies

    copies = body.get("copies", 1)
    spacing = body.get("spacing", 5.0)

    if copies < 1 or copies > 100:
        raise HTTPException(400, "copies must be between 1 and 100")
    if spacing < 0 or spacing > 50:
        raise HTTPException(400, "spacing must be between 0 and 50 mm")

    pool = get_pg_pool()
    async with pool.acquire() as conn:
        upload = await conn.fetchrow(
            "SELECT file_path, filename FROM uploads WHERE id = $1", upload_id
        )
        if not upload:
            raise HTTPException(404, "Upload not found")

        source_path = Path(upload["file_path"])
        if not source_path.exists():
            raise HTTPException(404, "Upload file not found on disk")

        # Output goes alongside the original with a suffix
        copies_path = source_path.with_suffix(".copies.3mf")

        try:
            result = apply_copies_to_3mf(source_path, copies_path, copies, spacing)
        except ValueError as e:
            raise HTTPException(400, str(e))

        # Store the copies path and spacing in the upload metadata
        await conn.execute(
            "UPDATE uploads SET copies_path = $1, copies_count = $2, copies_spacing = $3 WHERE id = $4",
            str(copies_path), copies, spacing, upload_id,
        )

    return result


@router.delete("/{upload_id}/copies")
async def reset_copies(upload_id: int):
    """Remove copies and revert to the original single object."""
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        upload = await conn.fetchrow(
            "SELECT copies_path FROM uploads WHERE id = $1", upload_id
        )
        if not upload:
            raise HTTPException(404, "Upload not found")

        # Delete copies file if it exists
        if upload["copies_path"]:
            copies_path = Path(upload["copies_path"])
            if copies_path.exists():
                copies_path.unlink()

        await conn.execute(
            "UPDATE uploads SET copies_path = NULL, copies_count = 1 WHERE id = $1",
            upload_id,
        )

    return {"message": "Copies removed", "copies": 1}


@router.get("/{upload_id}/copies/info")
async def get_copies_info(upload_id: int):
    """Get object dimensions and max copy estimate for this upload."""
    from copy_duplicator import get_object_dimensions, estimate_max_copies

    pool = get_pg_pool()
    async with pool.acquire() as conn:
        upload = await conn.fetchrow(
            "SELECT file_path, copies_count FROM uploads WHERE id = $1", upload_id
        )
        if not upload:
            raise HTTPException(404, "Upload not found")

        source_path = Path(upload["file_path"])
        if not source_path.exists():
            raise HTTPException(404, "Upload file not found on disk")

    try:
        obj_w, obj_d, obj_h = get_object_dimensions(source_path)
    except ValueError as e:
        raise HTTPException(400, str(e))

    max_copies = estimate_max_copies(obj_w, obj_d)

    return {
        "object_dimensions": [round(obj_w, 1), round(obj_d, 1), round(obj_h, 1)],
        "max_copies": max_copies,
        "current_copies": upload["copies_count"] or 1,
    }
