"""Slicing endpoints for converting uploads to G-code (plate-based workflow)."""

import uuid
import logging
import shutil
import re
from pathlib import Path
from datetime import datetime
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional, List, Dict

from db import get_pg_pool
from config import get_printer_profile
from slicer import OrcaSlicer, SlicingError
from profile_embedder import ProfileEmbedder, ProfileEmbedError
from multi_plate_parser import parse_multi_plate_3mf, extract_plate_objects, get_plate_bounds
from plate_validator import PlateValidator


router = APIRouter(tags=["slicing"])
logger = logging.getLogger(__name__)


class SliceRequest(BaseModel):
    filament_id: int
    layer_height: Optional[float] = 0.2
    infill_density: Optional[int] = 15
    supports: Optional[bool] = False
    nozzle_temp: Optional[int] = None
    bed_temp: Optional[int] = None
    bed_type: Optional[str] = None


class SlicePlateRequest(BaseModel):
    plate_id: int
    filament_id: int
    layer_height: Optional[float] = 0.2
    infill_density: Optional[int] = 15
    supports: Optional[bool] = False
    nozzle_temp: Optional[int] = None
    bed_temp: Optional[int] = None
    bed_type: Optional[str] = None


def setup_job_logging(job_id: str) -> logging.Logger:
    """Setup file logger for slicing job."""
    log_path = Path(f"/data/logs/slice_{job_id}.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)

    job_logger = logging.getLogger(f"slice_{job_id}")
    job_logger.setLevel(logging.INFO)

    handler = logging.FileHandler(log_path)
    handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    job_logger.addHandler(handler)

    return job_logger


@router.post("/uploads/{upload_id}/slice")
async def slice_upload(upload_id: int, request: SliceRequest):
    """Slice an upload directly, preserving plate layout.

    Workflow:
    1. Validate upload and filament exist
    2. Create slicing job
    3. Embed profiles into original 3MF (preserves geometry)
    4. Invoke Orca Slicer
    5. Parse G-code metadata
    6. Validate bounds
    7. Save G-code to /data/slices/
    8. Update database
    """
    pool = get_pg_pool()
    job_id = f"slice_{uuid.uuid4().hex[:12]}"
    job_logger = setup_job_logging(job_id)

    job_logger.info(f"Starting slicing job for upload {upload_id}")
    job_logger.info(f"Request: filament_id={request.filament_id}, layer_height={request.layer_height}, "
                    f"infill_density={request.infill_density}, supports={request.supports}")

    async with pool.acquire() as conn:
        # Validate upload exists
        upload = await conn.fetchrow(
            """
            SELECT id, filename, file_path, bounds_warning
            FROM uploads
            WHERE id = $1
            """,
            upload_id
        )

        if not upload:
            job_logger.error(f"Upload {upload_id} not found")
            raise HTTPException(status_code=404, detail="Upload not found")

        # Check for bounds warnings
        if upload["bounds_warning"]:
            job_logger.warning(f"Plate has bounds warnings: {upload['bounds_warning']}")

        # Validate filament exists
        filament = await conn.fetchrow(
            """
            SELECT id, name, material, nozzle_temp, bed_temp, print_speed, bed_type
            FROM filaments
            WHERE id = $1
            """,
            request.filament_id
        )

        if not filament:
            job_logger.error(f"Filament {request.filament_id} not found")
            raise HTTPException(status_code=404, detail="Filament not found")

        job_logger.info(f"Using filament: {filament['name']} ({filament['material']})")

        # Check original 3MF file exists
        source_3mf = Path(upload["file_path"])
        if not source_3mf.exists():
            job_logger.error(f"Source 3MF file not found: {source_3mf}")
            raise HTTPException(status_code=500, detail="Source 3MF file not found")

        # Create slicing job record
        await conn.execute(
            """
            INSERT INTO slicing_jobs (job_id, upload_id, status, started_at, log_path)
            VALUES ($1, $2, 'processing', $3, $4)
            """,
            job_id, upload_id, datetime.utcnow(), f"/data/logs/slice_{job_id}.log"
        )

    # Execute slicing workflow
    try:
        # Create workspace directory
        workspace = Path(f"/cache/slicing/{job_id}")
        workspace.mkdir(parents=True, exist_ok=True)
        job_logger.info(f"Created workspace: {workspace}")

        # Embed profiles into original 3MF
        job_logger.info("Embedding Orca profiles into original 3MF...")
        embedder = ProfileEmbedder(Path("/app/orca_profiles"))
        embedded_3mf = workspace / "sliceable.3mf"

        # Prepare filament settings
        # Orca expects temperatures as arrays of strings, not integers
        # Use request overrides if provided, otherwise use filament defaults
        nozzle_temp = request.nozzle_temp if request.nozzle_temp is not None else filament["nozzle_temp"]
        bed_temp = request.bed_temp if request.bed_temp is not None else filament["bed_temp"]

        filament_settings = {
            "nozzle_temperature": [str(nozzle_temp)] * 4,  # U1 has 4 extruders
            "nozzle_temperature_initial_layer": [str(nozzle_temp)] * 4,
            "bed_temperature": [str(bed_temp)] * 4,
            "bed_temperature_initial_layer": [str(bed_temp)] * 4,
            "bed_temperature_initial_layer_single": str(bed_temp),  # Used in printer start gcode
            # Also set cool_plate_temp - Orca uses this for PEI plates
            "cool_plate_temp": [str(bed_temp)] * 4,
            "cool_plate_temp_initial_layer": [str(bed_temp)] * 4,
            "textured_plate_temp": [str(bed_temp)] * 4,
            "textured_plate_temp_initial_layer": [str(bed_temp)] * 4,
        }

        # Add bed type if specified in request
        bed_type = request.bed_type if request.bed_type is not None else filament.get("bed_type", "PEI")
        if bed_type:
            filament_settings["default_bed_type"] = bed_type

        # Prepare overrides from request
        overrides = {}
        if request.layer_height != 0.2:
            overrides["layer_height"] = str(request.layer_height)
        if request.infill_density != 15:
            overrides["sparse_infill_density"] = f"{request.infill_density}%"
        if request.supports:
            overrides["enable_support"] = "1"
            overrides["support_type"] = "normal(auto)"

        job_logger.info(f"Using temps: nozzle={nozzle_temp}°C, bed={bed_temp}°C, bed_type={bed_type}")

        try:
            embedder.embed_profiles(
                source_3mf=source_3mf,
                output_3mf=embedded_3mf,
                filament_settings=filament_settings,
                overrides=overrides
            )
            three_mf_size_mb = embedded_3mf.stat().st_size / 1024 / 1024
            job_logger.info(f"Profile-embedded 3MF created: {embedded_3mf.name} ({three_mf_size_mb:.2f} MB)")
        except ProfileEmbedError as e:
            job_logger.error(f"Failed to embed profiles: {str(e)}")
            raise SlicingError(f"Profile embedding failed: {str(e)}")

        # Slice with Orca
        job_logger.info("Invoking Orca Slicer...")
        printer_profile = get_printer_profile("snapmaker_u1")
        slicer = OrcaSlicer(printer_profile)

        result = slicer.slice_3mf(embedded_3mf, workspace)

        if not result["success"]:
            job_logger.error(f"Orca Slicer failed with exit code {result['exit_code']}")
            job_logger.error(f"stdout: {result['stdout']}")
            job_logger.error(f"stderr: {result['stderr']}")
            raise SlicingError(f"Orca Slicer failed: {result['stderr'][:200]}")

        job_logger.info("Slicing completed successfully")
        job_logger.info(f"Orca stdout: {result['stdout'][:500]}")

        # Find generated G-code file (Orca produces plate_1.gcode)
        gcode_files = list(workspace.glob("plate_*.gcode"))
        if not gcode_files:
            job_logger.error("No G-code files generated")
            raise SlicingError("G-code file not generated by Orca")

        gcode_workspace_path = gcode_files[0]
        job_logger.info(f"Found G-code file: {gcode_workspace_path.name}")

        # Parse G-code metadata
        job_logger.info("Parsing G-code metadata...")
        metadata = slicer.parse_gcode_metadata(gcode_workspace_path)
        job_logger.info(f"Metadata: time={metadata['estimated_time_seconds']}s, "
                       f"filament={metadata['filament_used_mm']}mm, "
                       f"layers={metadata.get('layer_count', 'N/A')}")
        job_logger.info(f"Bounds: X={metadata['bounds']['max_x']:.1f}, "
                       f"Y={metadata['bounds']['max_y']:.1f}, "
                       f"Z={metadata['bounds']['max_z']:.1f}")

        # Validate bounds
        job_logger.info("Validating bounds against printer build volume...")
        try:
            slicer.validate_bounds(gcode_workspace_path)
            job_logger.info("Bounds validation passed")
        except Exception as e:
            job_logger.warning(f"Bounds validation warning: {str(e)}")
            # Don't fail on bounds warning, just log it

        # Move G-code to final location
        slices_dir = Path("/data/slices")
        slices_dir.mkdir(parents=True, exist_ok=True)
        final_gcode_path = slices_dir / f"{job_id}.gcode"

        shutil.copy(gcode_workspace_path, final_gcode_path)
        gcode_size = final_gcode_path.stat().st_size
        gcode_size_mb = gcode_size / 1024 / 1024
        job_logger.info(f"G-code saved: {final_gcode_path} ({gcode_size_mb:.2f} MB)")

        # Update database with results
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE slicing_jobs SET
                    status = 'completed',
                    completed_at = $2,
                    gcode_path = $3,
                    gcode_size = $4,
                    estimated_time_seconds = $5,
                    filament_used_mm = $6,
                    layer_count = $7,
                    three_mf_path = $8
                WHERE job_id = $1
                """,
                job_id,
                datetime.utcnow(),
                str(final_gcode_path),
                gcode_size,
                metadata['estimated_time_seconds'],
                metadata['filament_used_mm'],
                metadata.get('layer_count'),
                str(embedded_3mf)
            )

        job_logger.info(f"Slicing job {job_id} completed successfully")

        return {
            "job_id": job_id,
            "status": "completed",
            "gcode_path": str(final_gcode_path),
            "gcode_size": gcode_size,
            "gcode_size_mb": round(gcode_size_mb, 2),
            "metadata": {
                "estimated_time_seconds": metadata['estimated_time_seconds'],
                "filament_used_mm": metadata['filament_used_mm'],
                "layer_count": metadata.get('layer_count'),
                "bounds": metadata['bounds']
            }
        }

    except SlicingError as e:
        job_logger.error(f"Slicing failed: {str(e)}")
        # Update job status to failed
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE slicing_jobs SET
                    status = 'failed',
                    completed_at = $2,
                    error_message = $3
                WHERE job_id = $1
                """,
                job_id,
                datetime.utcnow(),
                str(e)
            )
        raise HTTPException(status_code=500, detail=f"Slicing failed: {str(e)}")

    except Exception as e:
        job_logger.error(f"Unexpected error: {str(e)}")
        # Update job status to failed
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE slicing_jobs SET
                    status = 'failed',
                    completed_at = $2,
                    error_message = $3
                WHERE job_id = $1
                """,
                job_id,
                datetime.utcnow(),
                f"Unexpected error: {str(e)}"
            )
        raise HTTPException(status_code=500, detail=f"Slicing failed: {str(e)}")


@router.post("/uploads/{upload_id}/slice-plate")
async def slice_plate(upload_id: int, request: SlicePlateRequest):
    """Slice a specific plate from a multi-plate 3MF file.
    
    This endpoint extracts only the specified plate and slices it,
    allowing users to choose which plate to print from multi-plate files.
    
    Workflow:
    1. Validate upload exists and is multi-plate
    2. Validate requested plate exists
    3. Extract plate-specific geometry
    4. Create slicing job
    5. Embed profiles into plate-specific 3MF
    6. Invoke Orca Slicer
    7. Parse G-code metadata
    8. Validate bounds
    9. Save G-code to /data/slices/
    10. Update database
    """
    pool = get_pg_pool()
    job_id = f"slice_plate_{uuid.uuid4().hex[:12]}"
    job_logger = setup_job_logging(job_id)

    job_logger.info(f"Starting plate slicing job for upload {upload_id}, plate {request.plate_id}")
    job_logger.info(f"Request: filament_id={request.filament_id}, layer_height={request.layer_height}, "
                    f"infill_density={request.infill_density}, supports={request.supports}")

    async with pool.acquire() as conn:
        # Validate upload exists
        upload = await conn.fetchrow(
            """
            SELECT id, filename, file_path, bounds_warning
            FROM uploads
            WHERE id = $1
            """,
            upload_id
        )

        if not upload:
            job_logger.error(f"Upload {upload_id} not found")
            raise HTTPException(status_code=404, detail="Upload not found")

        # Check if this is a multi-plate file
        source_3mf = Path(upload["file_path"])
        if not source_3mf.exists():
            job_logger.error(f"Source 3MF file not found: {source_3mf}")
            raise HTTPException(status_code=500, detail="Source 3MF file not found")

        plates, is_multi_plate = parse_multi_plate_3mf(source_3mf)
        if not is_multi_plate:
            job_logger.error(f"Upload {upload_id} is not a multi-plate file")
            raise HTTPException(status_code=400, detail="Not a multi-plate file - use /uploads/{id}/slice instead")

        # Validate requested plate exists
        target_plate = None
        for plate in plates:
            if plate.plate_id == request.plate_id:
                target_plate = plate
                break

        if not target_plate:
            job_logger.error(f"Plate {request.plate_id} not found in file")
            raise HTTPException(status_code=404, detail=f"Plate {request.plate_id} not found")

        if not target_plate.printable:
            job_logger.warning(f"Plate {request.plate_id} is marked as non-printable")

        job_logger.info(f"Found plate {request.plate_id}: Object {target_plate.object_id}")

        # Validate filament exists
        filament = await conn.fetchrow(
            """
            SELECT id, name, material, nozzle_temp, bed_temp, print_speed, bed_type
            FROM filaments
            WHERE id = $1
            """,
            request.filament_id
        )

        if not filament:
            job_logger.error(f"Filament {request.filament_id} not found")
            raise HTTPException(status_code=404, detail="Filament not found")

        job_logger.info(f"Using filament: {filament['name']} ({filament['material']})")

        # Validate plate bounds
        printer_profile = get_printer_profile("snapmaker_u1")
        validator = PlateValidator(printer_profile)
        plate_validation = validator.validate_3mf_bounds(source_3mf, request.plate_id)

        if not plate_validation['fits']:
            job_logger.warning(f"Plate {request.plate_id} exceeds build volume: {'; '.join(plate_validation['warnings'])}")
            # Don't fail on bounds warning, just log it

        # Create slicing job record
        await conn.execute(
            """
            INSERT INTO slicing_jobs (job_id, upload_id, status, started_at, log_path)
            VALUES ($1, $2, 'processing', $3, $4)
            """,
            job_id, upload_id, datetime.utcnow(), f"/data/logs/slice_{job_id}.log"
        )

    # Execute plate-specific slicing workflow
    try:
        # Create workspace directory
        workspace = Path(f"/cache/slicing/{job_id}")
        workspace.mkdir(parents=True, exist_ok=True)
        job_logger.info(f"Created workspace: {workspace}")

        # Extract the selected plate to a new 3MF file
        from multi_plate_parser import extract_plate_to_3mf
        
        plate_3mf = workspace / "plate_extracted.3mf"
        job_logger.info(f"Extracting plate {request.plate_id} from 3MF...")
        extract_plate_to_3mf(source_3mf, request.plate_id, plate_3mf)
        
        embedded_3mf = workspace / "sliceable.3mf"

        # Embed profiles into extracted plate 3MF
        job_logger.info("Embedding Orca profiles into 3MF...")
        embedder = ProfileEmbedder(Path("/app/orca_profiles"))

        # Prepare filament settings
        # Use request overrides if provided, otherwise use filament defaults
        nozzle_temp = request.nozzle_temp if request.nozzle_temp is not None else filament["nozzle_temp"]
        bed_temp = request.bed_temp if request.bed_temp is not None else filament["bed_temp"]

        filament_settings = {
            "nozzle_temperature": [str(nozzle_temp)] * 4,
            "nozzle_temperature_initial_layer": [str(nozzle_temp)] * 4,
            "bed_temperature": [str(bed_temp)] * 4,
            "bed_temperature_initial_layer": [str(bed_temp)] * 4,
            "bed_temperature_initial_layer_single": str(bed_temp),  # Used in printer start gcode
            # Also set cool_plate_temp - Orca uses this for PEI plates
            "cool_plate_temp": [str(bed_temp)] * 4,
            "cool_plate_temp_initial_layer": [str(bed_temp)] * 4,
            "textured_plate_temp": [str(bed_temp)] * 4,
            "textured_plate_temp_initial_layer": [str(bed_temp)] * 4,
        }

        # Add bed type if specified in request
        bed_type = request.bed_type if request.bed_type is not None else filament.get("bed_type", "PEI")
        if bed_type:
            filament_settings["default_bed_type"] = bed_type

        # Prepare overrides from request
        overrides = {}
        if request.layer_height != 0.2:
            overrides["layer_height"] = str(request.layer_height)
        if request.infill_density != 15:
            overrides["sparse_infill_density"] = f"{request.infill_density}%"
        if request.supports:
            overrides["enable_support"] = "1"
            overrides["support_type"] = "normal(auto)"

        job_logger.info(f"Using temps: nozzle={nozzle_temp}°C, bed={bed_temp}°C, bed_type={bed_type}")

        try:
            embedder.embed_profiles(
                source_3mf=plate_3mf,
                output_3mf=embedded_3mf,
                filament_settings=filament_settings,
                overrides=overrides
            )
            three_mf_size_mb = embedded_3mf.stat().st_size / 1024 / 1024
            job_logger.info(f"Profile-embedded 3MF created: {embedded_3mf.name} ({three_mf_size_mb:.2f} MB)")
        except ProfileEmbedError as e:
            job_logger.error(f"Failed to embed profiles: {str(e)}")
            raise SlicingError(f"Profile embedding failed: {str(e)}")

        # Slice with Orca
        job_logger.info("Invoking Orca Slicer...")
        slicer = OrcaSlicer(printer_profile)

        result = slicer.slice_3mf(embedded_3mf, workspace)

        if not result["success"]:
            job_logger.error(f"Orca Slicer failed with exit code {result['exit_code']}")
            job_logger.error(f"stdout: {result['stdout']}")
            job_logger.error(f"stderr: {result['stderr']}")
            raise SlicingError(f"Orca Slicer failed: {result['stderr'][:200]}")

        job_logger.info("Slicing completed successfully")
        job_logger.info(f"Orca stdout: {result['stdout'][:500]}")

        # Find generated G-code file
        gcode_files = list(workspace.glob("plate_*.gcode"))
        if not gcode_files:
            job_logger.error("No G-code files generated")
            raise SlicingError("G-code file not generated by Orca")

        gcode_workspace_path = gcode_files[0]
        job_logger.info(f"Found G-code file: {gcode_workspace_path.name}")

        # Parse G-code metadata
        job_logger.info("Parsing G-code metadata...")
        metadata = slicer.parse_gcode_metadata(gcode_workspace_path)
        job_logger.info(f"Metadata: time={metadata['estimated_time_seconds']}s, "
                       f"filament={metadata['filament_used_mm']}mm, "
                       f"layers={metadata.get('layer_count', 'N/A')}")

        # Validate bounds
        job_logger.info("Validating bounds against printer build volume...")
        try:
            slicer.validate_bounds(gcode_workspace_path)
            job_logger.info("Bounds validation passed")
        except Exception as e:
            job_logger.warning(f"Bounds validation warning: {str(e)}")

        # Move G-code to final location
        slices_dir = Path("/data/slices")
        slices_dir.mkdir(parents=True, exist_ok=True)
        final_gcode_path = slices_dir / f"{job_id}.gcode"

        shutil.copy(gcode_workspace_path, final_gcode_path)
        gcode_size = final_gcode_path.stat().st_size
        gcode_size_mb = gcode_size / 1024 / 1024
        job_logger.info(f"G-code saved: {final_gcode_path} ({gcode_size_mb:.2f} MB)")

        # Update database with results
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE slicing_jobs SET
                    status = 'completed',
                    completed_at = $2,
                    gcode_path = $3,
                    gcode_size = $4,
                    estimated_time_seconds = $5,
                    filament_used_mm = $6,
                    layer_count = $7,
                    three_mf_path = $8
                WHERE job_id = $1
                """,
                job_id,
                datetime.utcnow(),
                str(final_gcode_path),
                gcode_size,
                metadata['estimated_time_seconds'],
                metadata['filament_used_mm'],
                metadata.get('layer_count'),
                str(embedded_3mf)
            )

        job_logger.info(f"Plate slicing job {job_id} completed successfully")

        return {
            "job_id": job_id,
            "status": "completed",
            "plate_id": request.plate_id,
            "gcode_path": str(final_gcode_path),
            "gcode_size": gcode_size,
            "gcode_size_mb": round(gcode_size_mb, 2),
            "plate_validation": plate_validation,
            "metadata": {
                "estimated_time_seconds": metadata['estimated_time_seconds'],
                "filament_used_mm": metadata['filament_used_mm'],
                "layer_count": metadata.get('layer_count'),
                "bounds": metadata['bounds']
            }
        }

    except SlicingError as e:
        job_logger.error(f"Plate slicing failed: {str(e)}")
        # Update job status to failed
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE slicing_jobs SET
                    status = 'failed',
                    completed_at = $2,
                    error_message = $3
                WHERE job_id = $1
                """,
                job_id,
                datetime.utcnow(),
                str(e)
            )
        raise HTTPException(status_code=500, detail=f"Plate slicing failed: {str(e)}")

    except Exception as e:
        job_logger.error(f"Unexpected error: {str(e)}")
        # Update job status to failed
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE slicing_jobs SET
                    status = 'failed',
                    completed_at = $2,
                    error_message = $3
                WHERE job_id = $1
                """,
                job_id,
                datetime.utcnow(),
                f"Unexpected error: {str(e)}"
            )
        raise HTTPException(status_code=500, detail=f"Plate slicing failed: {str(e)}")


@router.get("/uploads/{upload_id}/plates")
async def get_upload_plates(upload_id: int):
    """Get plate information for a multi-plate 3MF upload."""
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        # Validate upload exists
        upload = await conn.fetchrow(
            """
            SELECT id, filename, file_path
            FROM uploads
            WHERE id = $1
            """,
            upload_id
        )

        if not upload:
            raise HTTPException(status_code=404, detail="Upload not found")

    # Check if this is a multi-plate file
    source_3mf = Path(upload["file_path"])
    if not source_3mf.exists():
        raise HTTPException(status_code=500, detail="Source 3MF file not found")

    try:
        plates, is_multi_plate = parse_multi_plate_3mf(source_3mf)
        
        if not is_multi_plate:
            return {
                "upload_id": upload_id,
                "filename": upload["filename"],
                "is_multi_plate": False,
                "plates": []
            }

        # Get validation info for each plate
        printer_profile = get_printer_profile("snapmaker_u1")
        validator = PlateValidator(printer_profile)
        
        plate_info = []
        for plate in plates:
            try:
                validation = validator.validate_3mf_bounds(source_3mf, plate.plate_id)
                
                plate_dict = plate.to_dict()
                plate_dict.update({
                    "validation": {
                        "fits": validation['fits'],
                        "warnings": validation['warnings'],
                        "bounds": validation['bounds']
                    }
                })
                plate_info.append(plate_dict)
                
            except Exception as e:
                logger.error(f"Failed to validate plate {plate.plate_id}: {str(e)}")
                plate_dict = plate.to_dict()
                plate_dict.update({
                    "validation": {
                        "fits": False,
                        "warnings": [f"Validation failed: {str(e)}"],
                        "bounds": None
                    }
                })
                plate_info.append(plate_dict)

        return {
            "upload_id": upload_id,
            "filename": upload["filename"],
            "is_multi_plate": True,
            "plate_count": len(plates),
            "plates": plate_info
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to parse plates: {str(e)}")


@router.get("/jobs/{job_id}")
async def get_slicing_job(job_id: str):
    """Get slicing job status and results."""
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        job = await conn.fetchrow(
            """
            SELECT job_id, upload_id, status, started_at, completed_at,
                   gcode_path, gcode_size, estimated_time_seconds, filament_used_mm,
                   layer_count, error_message
            FROM slicing_jobs
            WHERE job_id = $1
            """,
            job_id
        )

        if not job:
            raise HTTPException(status_code=404, detail="Job not found")

    result = {
        "job_id": job["job_id"],
        "upload_id": job["upload_id"],
        "status": job["status"],
        "started_at": job["started_at"].isoformat() if job["started_at"] else None,
        "completed_at": job["completed_at"].isoformat() if job["completed_at"] else None,
        "gcode_path": job["gcode_path"],
        "gcode_size": job["gcode_size"],
        "gcode_size_mb": round(job["gcode_size"] / 1024 / 1024, 2) if job["gcode_size"] else None,
        "error_message": job["error_message"]
    }

    # Add metadata if job is completed
    if job["status"] == "completed":
        result["metadata"] = {
            "estimated_time_seconds": job["estimated_time_seconds"],
            "filament_used_mm": job["filament_used_mm"],
            "layer_count": job["layer_count"]
        }

    return result


@router.get("/jobs/{job_id}/download")
async def download_gcode(job_id: str):
    """Download the generated G-code file."""
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        job = await conn.fetchrow(
            "SELECT gcode_path, status FROM slicing_jobs WHERE job_id = $1",
            job_id
        )

        if not job:
            raise HTTPException(status_code=404, detail="Job not found")

        if job["status"] != "completed":
            raise HTTPException(status_code=400, detail="Job not completed")

        gcode_path = Path(job["gcode_path"])
        if not gcode_path.exists():
            raise HTTPException(status_code=404, detail="G-code file not found")

        return FileResponse(
            path=gcode_path,
            media_type="text/plain",
            filename=f"{job_id}.gcode"
        )


@router.get("/jobs/{job_id}/gcode/metadata")
async def get_gcode_metadata(job_id: str):
    """Get G-code metadata for visualization."""
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        job = await conn.fetchrow(
            """
            SELECT gcode_path, status, layer_count,
                   estimated_time_seconds, filament_used_mm
            FROM slicing_jobs
            WHERE job_id = $1
            """,
            job_id
        )

        if not job:
            raise HTTPException(status_code=404, detail="Job not found")

        if job["status"] != "completed":
            raise HTTPException(status_code=400, detail="Job not completed")

        gcode_path = Path(job["gcode_path"])
        if not gcode_path.exists():
            raise HTTPException(status_code=404, detail="G-code file not found")

    # Parse G-code for bounds
    bounds = _parse_gcode_bounds(gcode_path)

    return {
        "layer_count": job["layer_count"] or 0,
        "estimated_time_seconds": job["estimated_time_seconds"] or 0,
        "filament_used_mm": job["filament_used_mm"] or 0,
        "bounds": bounds
    }


@router.get("/jobs/{job_id}/gcode/layers")
async def get_gcode_layers(job_id: str, start: int = 0, count: int = 20):
    """Get G-code layer geometry for visualization."""
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        job = await conn.fetchrow(
            "SELECT gcode_path, status FROM slicing_jobs WHERE job_id = $1",
            job_id
        )

        if not job:
            raise HTTPException(status_code=404, detail="Job not found")

        if job["status"] != "completed":
            raise HTTPException(status_code=400, detail="Job not completed")

        gcode_path = Path(job["gcode_path"])
        if not gcode_path.exists():
            raise HTTPException(status_code=404, detail="G-code file not found")

    # Parse requested layers
    layers = _parse_gcode_layers(gcode_path, start, count)

    return {"layers": layers}


def _parse_gcode_bounds(gcode_path: Path) -> Dict[str, float]:
    """Parse G-code file to extract print bounds by scanning actual moves."""
    bounds = {
        "min_x": float('inf'), "max_x": float('-inf'),
        "min_y": float('inf'), "max_y": float('-inf'),
        "min_z": float('inf'), "max_z": float('-inf')
    }

    current_x, current_y, current_z = 0.0, 0.0, 0.0
    pattern = re.compile(r'([XYZ])([\d.-]+)')

    try:
        with open(gcode_path, 'r') as f:
            for line in f:
                line = line.strip()

                # Skip comments and non-move commands
                if not line or line.startswith(';') or not line.startswith('G1'):
                    continue

                # Parse coordinates
                parts = dict(pattern.findall(line))

                if 'X' in parts:
                    current_x = float(parts['X'])
                    bounds["min_x"] = min(bounds["min_x"], current_x)
                    bounds["max_x"] = max(bounds["max_x"], current_x)

                if 'Y' in parts:
                    current_y = float(parts['Y'])
                    bounds["min_y"] = min(bounds["min_y"], current_y)
                    bounds["max_y"] = max(bounds["max_y"], current_y)

                if 'Z' in parts:
                    current_z = float(parts['Z'])
                    bounds["min_z"] = min(bounds["min_z"], current_z)
                    bounds["max_z"] = max(bounds["max_z"], current_z)

        # If no coordinates found, default to 0
        if bounds["min_x"] == float('inf'):
            bounds = {
                "min_x": 0.0, "max_x": 270.0,
                "min_y": 0.0, "max_y": 270.0,
                "min_z": 0.0, "max_z": 270.0
            }

    except Exception as e:
        logger.error(f"Failed to parse bounds from G-code: {e}")
        # Return default bed bounds
        bounds = {
            "min_x": 0.0, "max_x": 270.0,
            "min_y": 0.0, "max_y": 270.0,
            "min_z": 0.0, "max_z": 270.0
        }

    return bounds


def _parse_gcode_layers(gcode_path: Path, start: int, count: int) -> List[Dict]:
    """Parse specific layers from G-code file."""
    layers = []
    current_layer = -1
    current_z = 0.0
    last_x, last_y = 0.0, 0.0
    layer_moves = []

    pattern = re.compile(r'([GXYZEF])([\d.-]+)')

    try:
        with open(gcode_path, 'r') as f:
            for line in f:
                line = line.strip()

                # Detect layer changes
                if line.startswith(";LAYER_CHANGE"):
                    if current_layer >= start and layer_moves:
                        layers.append({
                            "layer_num": current_layer,
                            "z_height": current_z,
                            "moves": layer_moves
                        })

                        if len(layers) >= count:
                            break

                    current_layer += 1
                    layer_moves = []
                    continue

                # Skip if not in range
                if current_layer < start:
                    continue
                if len(layers) >= count:
                    break

                # Parse G1 move commands
                if line.startswith("G1 "):
                    parts = dict(pattern.findall(line))

                    # Get coordinates, use last known if not specified
                    x = float(parts['X']) if 'X' in parts else last_x
                    y = float(parts['Y']) if 'Y' in parts else last_y
                    z = float(parts['Z']) if 'Z' in parts else current_z
                    e = parts.get('E')

                    if z != current_z:
                        current_z = z

                    # Only record XY moves (ignore Z-only moves)
                    if x != last_x or y != last_y:
                        # Extrude if E is present and not negative (retraction)
                        is_extrude = e is not None and float(e) >= 0
                        layer_moves.append({
                            "type": "extrude" if is_extrude else "travel",
                            "x1": last_x,
                            "y1": last_y,
                            "x2": x,
                            "y2": y
                        })

                    last_x, last_y = x, y

            # Add final layer if in range
            if current_layer >= start and layer_moves and len(layers) < count:
                layers.append({
                    "layer_num": current_layer,
                    "z_height": current_z,
                    "moves": layer_moves
                })

    except Exception as e:
        logger.error(f"Failed to parse G-code layers: {e}")
        raise HTTPException(status_code=500, detail="Failed to parse G-code")

    return layers
