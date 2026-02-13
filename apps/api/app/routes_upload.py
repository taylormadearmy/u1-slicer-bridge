import os
import uuid
import logging
from pathlib import Path
from datetime import datetime
from fastapi import APIRouter, UploadFile, File, HTTPException
from db import get_pg_pool

logger = logging.getLogger(__name__)
from parser_3mf import parse_3mf
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
    
    # Add multi-plate information if applicable
    if validation.get('is_multi_plate'):
        response.update({
            "is_multi_plate": True,
            "plates": validation['plates'],
            "plate_count": len(validation['plates'])
        })
        
        # Validate each individual plate
        plate_validations = []
        for plate in validation['plates']:
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
                logger.error(f"Failed to validate plate {plate['plate_id']}: {str(e)}")
                plate_validations.append({
                    "plate_id": plate['plate_id'],
                    "error": str(e),
                    "fits": False
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
            SELECT id, filename, file_size, uploaded_at, plate_validated,
                   bounds_min_x, bounds_min_y, bounds_min_z,
                   bounds_max_x, bounds_max_y, bounds_max_z,
                   bounds_warning
            FROM uploads WHERE id = $1
            """,
            upload_id,
        )
        if not upload:
            raise HTTPException(status_code=404, detail="Upload not found")

    # Format bounds
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

    return {
        "upload_id": upload["id"],
        "filename": upload["filename"],
        "file_size": upload["file_size"],
        "uploaded_at": upload["uploaded_at"].isoformat(),
        "plate_validated": upload["plate_validated"],
        "bounds": bounds,
        "warnings": warnings,
        "fits": len(warnings) == 0
    }


@router.get("")
async def list_uploads():
    """List all uploads with plate validation status."""
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        uploads = await conn.fetch(
            """
            SELECT id, filename, file_size, uploaded_at, plate_validated, bounds_warning
            FROM uploads
            ORDER BY uploaded_at DESC
            LIMIT 50
            """
        )

    return {
        "uploads": [
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
    }
