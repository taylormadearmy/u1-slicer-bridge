"""
Shared 3MF file processing logic.

Used by both the upload route (manual file upload) and the MakerWorld route
(server-side download) to avoid duplicating the parsing/validation/DB pipeline.
"""

import asyncio
import json
import logging
from pathlib import Path

from db import get_pg_pool
from parser_3mf import extract_upload_metadata
from plate_validator import PlateValidator, PlateValidationError
from config import get_printer_profile
from multi_plate_parser import parse_multi_plate_3mf, calculate_all_bounds

logger = logging.getLogger(__name__)


def _process_3mf_sync(file_path: Path, filename: str):
    """CPU-bound 3MF processing: parsing, validation, color detection.

    Runs in a worker thread via asyncio.to_thread() to avoid blocking the
    event loop for large/complex files.

    Optimised to minimise ZIP opens:
      1. parse_multi_plate_3mf  → ZIP open #1 (plates + structure)
      2. calculate_all_bounds   → ZIP open #2 (single-pass vertex scan)
      3. extract_upload_metadata → ZIP open #3 (colors, settings, previews)
      4. validate_precomputed_bounds → pure math, no I/O

    Returns a dict with all parsed data needed for DB insert + response.
    Raises ValueError (400) or RuntimeError (500).
    """
    # ── Step 1: Parse plates (ZIP open #1) ────────────────────────
    try:
        plates, is_multi_plate = parse_multi_plate_3mf(file_path)
    except ValueError as e:
        file_path.unlink(missing_ok=True)
        raise ValueError(str(e))
    except Exception as e:
        file_path.unlink(missing_ok=True)
        raise RuntimeError(f"Failed to parse .3mf: {str(e)}")

    # ── Step 2: Single-pass bounds for ALL plates (ZIP open #2) ───
    try:
        all_bounds = calculate_all_bounds(file_path, plates)
    except ValueError as e:
        file_path.unlink(missing_ok=True)
        raise ValueError(str(e))
    except Exception as e:
        file_path.unlink(missing_ok=True)
        raise RuntimeError(f"Failed to calculate bounds: {str(e)}")

    objects_count = all_bounds["objects_count"]
    if objects_count == 0:
        file_path.unlink(missing_ok=True)
        raise ValueError("No valid objects found in .3mf file")

    # ── Step 3: Single-pass metadata extraction (ZIP open #3) ─────
    metadata = extract_upload_metadata(file_path)
    detected_colors = metadata["detected_colors"]
    file_print_settings = metadata["print_settings"]
    colors_per_plate = metadata["colors_per_plate"]
    preview_assets = metadata["preview_assets"]
    has_bambu_z_offset = metadata["has_bambu_z_offset"]

    # ── Step 4: Validate bounds (no I/O) ──────────────────────────
    try:
        printer_profile = get_printer_profile("snapmaker_u1")
        validator = PlateValidator(printer_profile)

        # Validate combined bounds
        combined_bounds = all_bounds["combined"]
        validation = validator.validate_precomputed_bounds(
            combined_bounds, is_multi_plate=is_multi_plate,
            is_bambu_z_offset=has_bambu_z_offset,
        )
        validation["is_multi_plate"] = is_multi_plate
        validation["plates"] = [p.to_dict() for p in plates] if is_multi_plate else []

        # Validate per-plate bounds
        plate_validations = []
        if is_multi_plate:
            for plate in plates:
                pid = plate.plate_id
                plate_bounds = all_bounds["per_plate"].get(pid)
                if plate_bounds:
                    pv = validator.validate_precomputed_bounds(
                        plate_bounds, is_bambu_z_offset=has_bambu_z_offset,
                    )
                    plate_validations.append({
                        "plate_id": pid,
                        "bounds": pv["bounds"],
                        "warnings": pv["warnings"],
                        "fits": pv["fits"],
                    })
                else:
                    plate_validations.append({
                        "plate_id": pid,
                        "error": "No geometry found",
                        "fits": False,
                    })

            # If any individual plate fits, suppress combined-level size warnings
            any_plate_fits = any(p.get("fits", False) for p in plate_validations)
            if any_plate_fits:
                validation["warnings"] = [
                    w for w in validation.get("warnings", [])
                    if "exceeds build volume" not in w.lower()
                    and "multi-plate file with" not in w.lower()
                ]
                validation["fits"] = True

            # Add multi-plate advisory
            validation["warnings"].append(
                f"Multi-plate file with {len(plates)} plates. "
                "Individual plates may fit even if combined bounds exceed build volume."
            )

    except PlateValidationError as e:
        file_path.unlink(missing_ok=True)
        raise ValueError(f"Plate validation failed: {str(e)}")
    except Exception as e:
        file_path.unlink(missing_ok=True)
        raise RuntimeError(f"Failed to validate plate: {str(e)}")

    # ── Build plate_metadata cache for multi-plate files ──────────
    plate_metadata_json = None
    if is_multi_plate and plates:
        try:
            preview_map = preview_assets.get("by_plate", {})
            has_generic_preview = isinstance(preview_assets.get("best"), str)

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

    return {
        "objects_count": objects_count,
        "validation": validation,
        "plate_validations": plate_validations,
        "detected_colors": detected_colors,
        "file_print_settings": file_print_settings,
        "is_multi_plate": is_multi_plate,
        "plates": plates,
        "plate_metadata_json": plate_metadata_json,
    }


async def process_3mf_file(file_path: Path, filename: str, file_size: int) -> dict:
    """
    Process a .3mf file: parse metadata, validate bounds, detect colors,
    and store in the database.

    Returns the same response dict as the POST /upload endpoint.
    Raises HTTPException-compatible errors (ValueError for 400, RuntimeError for 500).
    """

    # Run all CPU-bound parsing/validation in a worker thread
    parsed = await asyncio.to_thread(_process_3mf_sync, file_path, filename)

    objects_count = parsed["objects_count"]
    validation = parsed["validation"]
    plate_validations = parsed["plate_validations"]
    detected_colors = parsed["detected_colors"]
    file_print_settings = parsed["file_print_settings"]
    is_multi_plate = parsed["is_multi_plate"]
    plates = parsed["plates"]
    plate_metadata_json = parsed["plate_metadata_json"]

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
