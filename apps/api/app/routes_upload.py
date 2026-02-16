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
    if not file.filename or not file.filename.lower().endswith(".3mf"):
        raise HTTPException(status_code=400, detail="Only .3mf files are supported")

    # Generate unique filename to avoid collisions
    file_id = uuid.uuid4().hex[:12]
    safe_filename = f"{file_id}_{file.filename}"
    file_path = UPLOAD_DIR / safe_filename

    # Save uploaded file
    try:
        content = await file.read()
        file_path.write_bytes(content)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save file: {str(e)}")

    # Parse .3mf and extract object/plate metadata (single parse pass)
    try:
        plates, is_multi_plate = parse_multi_plate_3mf(file_path)
        objects = parse_3mf(file_path)
    except ValueError as e:
        file_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        file_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"Failed to parse .3mf: {str(e)}")

    if not objects:
        file_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="No valid objects found in .3mf file")

    # Validate plate bounds (pass pre-parsed plates to avoid re-parsing)
    try:
        printer_profile = get_printer_profile("snapmaker_u1")
        validator = PlateValidator(printer_profile)
        validation = validator.validate_3mf_bounds(file_path, plates=plates)

        # For multi-plate files, suppress combined-scene warnings when at least
        # one individual plate fits the build volume.
        plate_validations = []
        if validation.get('is_multi_plate'):
            for plate in validation.get('plates', []):
                try:
                    plate_id = plate['plate_id']
                    plate_validation = validator.validate_3mf_bounds(file_path, plate_id, plates=plates)
                    plate_validations.append({
                        "plate_id": plate['plate_id'],
                        "bounds": plate_validation['bounds'],
                        "warnings": plate_validation['warnings'],
                        "fits": plate_validation['fits']
                    })
                except Exception as e:
                    logger.error(f"Failed to validate plate {plate.get('plate_id')}: {str(e)}")
                    plate_validations.append({
                        "plate_id": plate.get('plate_id'),
                        "error": str(e),
                        "fits": False
                    })

            any_plate_fits = any(p.get('fits', False) for p in plate_validations)
            if any_plate_fits:
                validation['warnings'] = [
                    w for w in validation.get('warnings', [])
                    if "exceeds build volume" not in w.lower()
                    and "multi-plate file with" not in w.lower()
                ]
                validation['fits'] = True
        else:
            plate_validations = []
    except PlateValidationError as e:
        file_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=f"Plate validation failed: {str(e)}")
    except Exception as e:
        file_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"Failed to validate plate: {str(e)}")

    # Detect colors and print settings before storing (so we can cache them)
    detected_colors = []
    try:
        detected_colors = detect_colors_from_3mf(file_path)
    except Exception as e:
        logger.warning(f"Failed to detect colors: {e}")

    file_print_settings = {}
    try:
        file_print_settings = detect_print_settings(file_path)
    except Exception as e:
        logger.warning(f"Failed to detect print settings: {e}")

    # Build plate_metadata cache for multi-plate files (avoids re-parsing on every GET /plates)
    is_multi_plate = validation.get('is_multi_plate', False)
    plate_metadata_json = None
    if is_multi_plate and plates:
        from routes_slice import _index_preview_assets
        try:
            preview_assets = _index_preview_assets(file_path)
            preview_map = preview_assets.get("by_plate", {})
            has_generic_preview = isinstance(preview_assets.get("best"), str)

            colors_per_plate = {}
            try:
                colors_per_plate = detect_colors_per_plate(file_path)
            except Exception:
                pass

            plate_info_cache = []
            for plate in plates:
                plate_dict = plate.to_dict() if hasattr(plate, 'to_dict') else (plate if isinstance(plate, dict) else {})
                pid = plate_dict.get('plate_id') or (plate.plate_id if hasattr(plate, 'plate_id') else None)

                # Per-plate colors if available; else assume single-extruder (first color)
                plate_colors = colors_per_plate.get(pid, (detected_colors or [])[:1])
                pv = next((p for p in plate_validations if p.get('plate_id') == pid), {})

                plate_dict.update({
                    "detected_colors": plate_colors,
                    "has_preview": pid in preview_map if isinstance(preview_map, dict) else False,
                    "has_generic_preview": has_generic_preview,
                    "validation": {
                        "fits": pv.get('fits', False),
                        "warnings": pv.get('warnings', []),
                        "bounds": pv.get('bounds')
                    }
                })
                plate_info_cache.append(plate_dict)

            plate_metadata_json = json.dumps(plate_info_cache)
        except Exception as e:
            logger.warning(f"Failed to build plate metadata cache: {e}")

    # Store in database with bounds information and cached metadata
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        bounds = validation['bounds']
        warnings_text = '\n'.join(validation['warnings']) if validation['warnings'] else None

        upload_id = await conn.fetchval(
            """
            INSERT INTO uploads (
                filename, file_path, file_size,
                plate_validated, bounds_min_x, bounds_min_y, bounds_min_z,
                bounds_max_x, bounds_max_y, bounds_max_z, bounds_warning,
                is_multi_plate, plate_count, detected_colors,
                file_print_settings, plate_metadata
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16)
            RETURNING id
            """,
            file.filename,
            str(file_path),
            len(content),
            True,  # plate_validated
            bounds['min'][0], bounds['min'][1], bounds['min'][2],
            bounds['max'][0], bounds['max'][1], bounds['max'][2],
            warnings_text,
            is_multi_plate,
            len(plates) if is_multi_plate else 0,
            json.dumps(detected_colors) if detected_colors else None,
            json.dumps(file_print_settings) if file_print_settings else None,
            plate_metadata_json
        )

    response = {
        "upload_id": upload_id,
        "filename": file.filename,
        "file_size": len(content),
        "objects_count": len(objects),
        "bounds": validation['bounds'],
        "warnings": validation['warnings'],
        "fits": validation['fits']
    }

    if detected_colors:
        response["detected_colors"] = detected_colors
        response["has_multicolor"] = len(detected_colors) > 1

    if file_print_settings:
        response["file_print_settings"] = file_print_settings

    # Add multi-plate information if applicable
    if is_multi_plate:
        response.update({
            "is_multi_plate": True,
            "plates": validation['plates'],
            "plate_count": len(validation['plates'])
        })
        response["plate_validations"] = plate_validations

    return response


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
