import os
import uuid
import logging
from pathlib import Path
from datetime import datetime
from fastapi import APIRouter, UploadFile, File, HTTPException
from db import get_pg_pool

logger = logging.getLogger(__name__)
from parser_3mf import parse_3mf, detect_colors_from_3mf
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

    # Parse .3mf and extract object metadata (for informational purposes)
    try:
        # Check if this is a multi-plate file
        plates, is_multi_plate = parse_multi_plate_3mf(file_path)
        
        if is_multi_plate:
            # For multi-plate files, we still extract basic object count
            # using the existing parser for backward compatibility
            objects = parse_3mf(file_path)
        else:
            objects = parse_3mf(file_path)
    except ValueError as e:
        # Clean up failed upload
        file_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        file_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"Failed to parse .3mf: {str(e)}")

    if not objects:
        file_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="No valid objects found in .3mf file")

    # Validate plate bounds
    try:
        printer_profile = get_printer_profile("snapmaker_u1")
        validator = PlateValidator(printer_profile)
        validation = validator.validate_3mf_bounds(file_path)

        # For multi-plate files, suppress combined-scene warnings when at least
        # one individual plate fits the build volume.
        plate_validations = []
        if validation.get('is_multi_plate'):
            for plate in validation.get('plates', []):
                try:
                    plate_id = plate['plate_id']
                    plate_validation = validator.validate_3mf_bounds(file_path, plate_id)
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

    # Store in database with bounds information
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        # Insert upload record with plate bounds
        bounds = validation['bounds']
        warnings_text = '\n'.join(validation['warnings']) if validation['warnings'] else None
        is_multi_plate = validation.get('is_multi_plate', False)

        upload_id = await conn.fetchval(
            """
            INSERT INTO uploads (
                filename, file_path, file_size,
                plate_validated, bounds_min_x, bounds_min_y, bounds_min_z,
                bounds_max_x, bounds_max_y, bounds_max_z, bounds_warning
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            RETURNING id
            """,
            file.filename,
            str(file_path),
            len(content),
            True,  # plate_validated
            bounds['min'][0], bounds['min'][1], bounds['min'][2],
            bounds['max'][0], bounds['max'][1], bounds['max'][2],
            warnings_text
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
    
    # Detect colors from 3MF file
    try:
        detected_colors = detect_colors_from_3mf(file_path)
        if detected_colors:
            response["detected_colors"] = detected_colors
            response["has_multicolor"] = len(detected_colors) > 1
    except Exception as e:
        logger.warning(f"Failed to detect colors: {e}")
    
    # Add multi-plate information if applicable
    if validation.get('is_multi_plate'):
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
        # Get upload record with bounds
        upload = await conn.fetchrow(
            """
            SELECT id, filename, file_path, file_size, uploaded_at, plate_validated,
                   bounds_min_x, bounds_min_y, bounds_min_z,
                   bounds_max_x, bounds_max_y, bounds_max_z,
                   bounds_warning
            FROM uploads WHERE id = $1
            """,
            upload_id,
        )
        if not upload:
            raise HTTPException(status_code=404, detail="Upload not found")

    # Re-validate bounds to get fresh warnings (fixes stale warnings from old uploads)
    file_path = Path(upload["file_path"])
    validator = PlateValidator(get_printer_profile())
    
    try:
        validation = validator.validate_3mf_bounds(file_path)
        bounds_min = validation["bounds"]["min"]
        bounds_max = validation["bounds"]["max"]
        bounds = {
            "min": bounds_min,
            "max": bounds_max,
            "size": [
                bounds_max[0] - bounds_min[0],
                bounds_max[1] - bounds_min[1],
                bounds_max[2] - bounds_min[2]
            ]
        }
        warnings = validation.get("warnings", [])
        fits = validation.get("fits", True)

        # For multi-plate files, avoid showing combined-scene build-volume false positives.
        if validation.get("is_multi_plate"):
            plates = validation.get("plates", [])
            any_plate_fits = False

            for plate in plates:
                plate_id = plate.get("plate_id") if isinstance(plate, dict) else None
                if plate_id is None:
                    continue
                plate_validation = validator.validate_3mf_bounds(file_path, plate_id)
                if plate_validation.get("fits", False):
                    any_plate_fits = True
                    break

            if any_plate_fits:
                # Keep non-build warnings (e.g., below-bed), suppress combined "exceeds" warnings.
                warnings = [
                    w for w in warnings
                    if "exceeds build volume" not in w.lower()
                    and "multi-plate file with" not in w.lower()
                ]
                fits = True
    except Exception as e:
        logger.warning(f"Failed to re-validate bounds: {e}")
        # Fall back to stored bounds if re-validation fails
        bounds = None
        if upload['plate_validated']:
            bounds = {
                "min": [upload['bounds_min_x'], upload['bounds_min_y'], upload['bounds_min_z']],
                "max": [upload['bounds_max_x'], upload['bounds_max_y'], upload['bounds_max_z']],
                "size": [
                    upload['bounds_max_x'] - upload['bounds_min_x'],
                    upload['bounds_max_y'] - upload['bounds_min_y'],
                    upload['bounds_max_z'] - upload['bounds_min_z']
                ]
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
    
    # Detect colors from 3MF file
    try:
        file_path = Path(upload["file_path"])
        detected_colors = detect_colors_from_3mf(file_path)
        if detected_colors:
            response["detected_colors"] = detected_colors
            response["has_multicolor"] = len(detected_colors) > 1
    except Exception as e:
        logger.warning(f"Failed to detect colors: {e}")
    
    return response


@router.get("")
async def list_uploads():
    """List all uploads with plate validation status."""
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        uploads = await conn.fetch(
            """
            SELECT id, filename, file_path, file_size, uploaded_at, plate_validated, bounds_warning
            FROM uploads
            ORDER BY uploaded_at DESC
            LIMIT 50
            """
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
        "uploads": upload_list
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
