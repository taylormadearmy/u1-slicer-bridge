"""
Shared 3MF file processing logic.

Used by both the upload route (manual file upload) and the MakerWorld route
(server-side download) to avoid duplicating the parsing/validation/DB pipeline.
"""

import json
import logging
from pathlib import Path

from db import get_pg_pool
from parser_3mf import parse_3mf, detect_colors_from_3mf, detect_colors_per_plate, detect_print_settings
from plate_validator import PlateValidator, PlateValidationError
from config import get_printer_profile
from multi_plate_parser import parse_multi_plate_3mf

logger = logging.getLogger(__name__)


async def process_3mf_file(file_path: Path, filename: str, file_size: int) -> dict:
    """
    Process a .3mf file: parse metadata, validate bounds, detect colors,
    and store in the database.

    Returns the same response dict as the POST /upload endpoint.
    Raises HTTPException-compatible errors (ValueError for 400, RuntimeError for 500).
    """

    # Parse .3mf and extract object/plate metadata
    try:
        plates, is_multi_plate = parse_multi_plate_3mf(file_path)
        objects = parse_3mf(file_path)
    except ValueError as e:
        file_path.unlink(missing_ok=True)
        raise ValueError(str(e))
    except Exception as e:
        file_path.unlink(missing_ok=True)
        raise RuntimeError(f"Failed to parse .3mf: {str(e)}")

    if not objects:
        file_path.unlink(missing_ok=True)
        raise ValueError("No valid objects found in .3mf file")

    objects_count = len(objects)

    # Validate plate bounds
    try:
        printer_profile = get_printer_profile("snapmaker_u1")
        validator = PlateValidator(printer_profile)
        validation = validator.validate_3mf_bounds(file_path, plates=plates)

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
        raise ValueError(f"Plate validation failed: {str(e)}")
    except Exception as e:
        file_path.unlink(missing_ok=True)
        raise RuntimeError(f"Failed to validate plate: {str(e)}")

    # Detect colors and print settings
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

    # Build plate_metadata cache for multi-plate files
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

                plate_colors = colors_per_plate.get(pid, detected_colors or [])
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

    # Store in database
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
            filename,
            str(file_path),
            file_size,
            True,
            bounds['min'][0], bounds['min'][1], bounds['min'][2],
            bounds['max'][0], bounds['max'][1], bounds['max'][2],
            warnings_text,
            is_multi_plate,
            len(plates) if is_multi_plate else 0,
            json.dumps(detected_colors) if detected_colors else None,
            json.dumps(file_print_settings) if file_print_settings else None,
            plate_metadata_json
        )

    # Build response
    response = {
        "upload_id": upload_id,
        "filename": filename,
        "file_size": file_size,
        "objects_count": objects_count,
        "bounds": validation['bounds'],
        "warnings": validation['warnings'],
        "fits": validation['fits']
    }

    if detected_colors:
        response["detected_colors"] = detected_colors
        response["has_multicolor"] = len(detected_colors) > 1

    if file_print_settings:
        response["file_print_settings"] = file_print_settings

    if is_multi_plate:
        response.update({
            "is_multi_plate": True,
            "plates": validation['plates'],
            "plate_count": len(validation['plates'])
        })
        response["plate_validations"] = plate_validations

    return response
