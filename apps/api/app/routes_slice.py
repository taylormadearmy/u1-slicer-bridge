"""Slicing endpoints for converting bundles to G-code."""

import uuid
import logging
from pathlib import Path
from datetime import datetime
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

from db import get_pg_pool
from config import get_printer_profile
from slicer import OrcaSlicer, FilamentData, ObjectData, SlicingError
from builder_3mf import ThreeMFBuilder, ObjectMeshData, ThreeMFBuildError


router = APIRouter(tags=["slicing"])


class SliceRequest(BaseModel):
    layer_height: Optional[float] = 0.2
    infill_density: Optional[int] = 15
    supports: Optional[bool] = False


def setup_job_logging(job_id: str) -> logging.Logger:
    """Setup file logger for slicing job."""
    log_path = Path(f"/data/logs/slice_{job_id}.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(f"slice_{job_id}")
    logger.setLevel(logging.INFO)

    handler = logging.FileHandler(log_path)
    handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    logger.addHandler(handler)

    return logger


@router.post("/bundles/{bundle_id}/slice")
async def slice_bundle(bundle_id: str, request: SliceRequest = SliceRequest()):
    """Slice a bundle to generate G-code for printing.

    Workflow:
    1. Validate bundle and objects
    2. Create slicing job
    3. Prepare workspace
    4. Generate Orca profile from filament settings
    5. Invoke Orca Slicer CLI
    6. Parse G-code metadata
    7. Validate bounds
    8. Save G-code to /data/slices/
    9. Update database
    10. Cleanup workspace
    """
    pool = get_pg_pool()
    job_id = f"slice_{uuid.uuid4().hex[:12]}"
    logger = setup_job_logging(job_id)

    logger.info(f"Starting slicing job for bundle {bundle_id}")
    logger.info(f"Request: layer_height={request.layer_height}, infill_density={request.infill_density}, supports={request.supports}")

    async with pool.acquire() as conn:
        # Validate bundle exists
        bundle = await conn.fetchrow(
            """
            SELECT b.id, b.bundle_id, b.name, b.filament_id,
                   f.material, f.nozzle_temp, f.bed_temp, f.print_speed
            FROM bundles b
            LEFT JOIN filaments f ON b.filament_id = f.id
            WHERE b.bundle_id = $1
            """,
            bundle_id
        )

        if not bundle:
            logger.error(f"Bundle {bundle_id} not found")
            raise HTTPException(status_code=404, detail="Bundle not found")

        if not bundle["filament_id"]:
            logger.error(f"Bundle {bundle_id} has no filament assigned")
            raise HTTPException(status_code=400, detail="Bundle has no filament assigned")

        # Get objects in bundle
        objects = await conn.fetch(
            """
            SELECT o.id, o.name, o.normalized_path, o.normalization_status
            FROM bundle_objects bo
            JOIN objects o ON bo.object_id = o.id
            WHERE bo.bundle_id = $1
            ORDER BY bo.added_at
            """,
            bundle["id"]
        )

        if not objects:
            logger.error(f"Bundle {bundle_id} has no objects")
            raise HTTPException(status_code=400, detail="Bundle has no objects")

        # Validate all objects are normalized
        for obj in objects:
            if obj["normalization_status"] != "normalized":
                logger.error(f"Object {obj['id']} is not normalized (status: {obj['normalization_status']})")
                raise HTTPException(
                    status_code=400,
                    detail=f"Object '{obj['name']}' is not normalized (status: {obj['normalization_status']})"
                )

            # Check normalized file exists
            normalized_path = Path(obj["normalized_path"])
            if not normalized_path.exists():
                logger.error(f"Normalized file missing for object {obj['id']}: {obj['normalized_path']}")
                raise HTTPException(
                    status_code=500,
                    detail=f"Normalized file not found for object '{obj['name']}'"
                )

        # Create slicing job record
        db_job_id = await conn.fetchval(
            """
            INSERT INTO slicing_jobs (job_id, bundle_id, status, started_at, log_path)
            VALUES ($1, $2, 'processing', $3, $4)
            RETURNING id
            """,
            job_id, bundle["id"], datetime.utcnow(), f"/data/logs/slice_{job_id}.log"
        )

    # Execute slicing workflow
    try:
        # Prepare data structures
        filament = FilamentData(
            material=bundle["material"],
            nozzle_temp=bundle["nozzle_temp"],
            bed_temp=bundle["bed_temp"],
            print_speed=bundle["print_speed"]
        )

        object_list = [
            ObjectData(
                id=obj["id"],
                name=obj["name"],
                normalized_path=obj["normalized_path"]
            )
            for obj in objects
        ]

        logger.info(f"Slicing {len(object_list)} objects with {filament.material} filament")

        # Initialize slicer
        printer_profile = get_printer_profile("snapmaker_u1")
        slicer = OrcaSlicer(printer_profile)

        # Build 3MF file with Snapmaker U1 profiles (M9)
        logger.info("Building 3MF file with embedded Snapmaker U1 profiles...")
        builder = ThreeMFBuilder(profile_dir=Path("/app/orca_profiles"))

        # Prepare object mesh data
        mesh_objects = [
            ObjectMeshData(
                id=obj.id,
                name=obj.name,
                stl_path=Path(obj.normalized_path)
            )
            for obj in object_list
        ]

        # Create workspace directory
        workspace = Path(f"/cache/slicing/{job_id}")
        workspace.mkdir(parents=True, exist_ok=True)
        three_mf_path = workspace / "bundle.3mf"

        # Apply request overrides to settings
        overrides = {}
        if request.layer_height != 0.2:
            overrides["layer_height"] = str(request.layer_height)
        if request.infill_density != 15:
            overrides["sparse_infill_density"] = f"{request.infill_density}%"
        if request.supports:
            overrides["enable_support"] = "1"
            overrides["support_type"] = "normal(auto)"

        try:
            builder.build_bundle_3mf(
                objects=mesh_objects,
                output_path=three_mf_path,
                settings_overrides=overrides
            )
            three_mf_size_mb = three_mf_path.stat().st_size / 1024 / 1024
            logger.info(f"3MF created: {three_mf_path} ({three_mf_size_mb:.2f} MB)")
        except ThreeMFBuildError as e:
            logger.error(f"Failed to build 3MF: {str(e)}")
            raise SlicingError(f"3MF creation failed: {str(e)}")

        # Slice 3MF
        logger.info("Invoking Orca Slicer with 3MF file...")
        result = slicer.slice_3mf(three_mf_path, workspace)

        if not result["success"]:
            logger.error(f"Orca Slicer failed with exit code {result['exit_code']}")
            logger.error(f"stdout: {result['stdout']}")
            logger.error(f"stderr: {result['stderr']}")
            raise SlicingError(f"Orca Slicer failed: {result['stderr'][:200]}")

        logger.info("Slicing completed successfully")
        logger.info(f"Orca stdout: {result['stdout'][:500]}")

        # Find generated G-code file (Orca produces plate_1.gcode for 3MF files)
        gcode_files = list(workspace.glob("plate_*.gcode"))
        if not gcode_files:
            # Fallback to output.gcode for STL workflow
            gcode_workspace_path = workspace / "output.gcode"
            if not gcode_workspace_path.exists():
                raise SlicingError("G-code file not generated")
        else:
            # Use first plate (plate_1.gcode)
            gcode_workspace_path = gcode_files[0]
            logger.info(f"Found G-code file: {gcode_workspace_path.name}")

        logger.info("Parsing G-code metadata...")
        metadata = slicer.parse_gcode_metadata(gcode_workspace_path)
        logger.info(f"Metadata: time={metadata['estimated_time_seconds']}s, filament={metadata['filament_used_mm']}mm")
        logger.info(f"Bounds: X={metadata['bounds']['max_x']}, Y={metadata['bounds']['max_y']}, Z={metadata['bounds']['max_z']}")

        # Validate bounds
        logger.info("Validating bounds...")
        slicer.validate_bounds(gcode_workspace_path)
        logger.info("Bounds validation passed")

        # Move G-code to final location
        slices_dir = Path("/data/slices")
        slices_dir.mkdir(parents=True, exist_ok=True)
        final_gcode_path = slices_dir / f"{bundle_id}.gcode"

        import shutil
        shutil.move(str(gcode_workspace_path), str(final_gcode_path))
        logger.info(f"G-code saved to {final_gcode_path}")

        gcode_size = final_gcode_path.stat().st_size

        # Update database
        async with pool.acquire() as conn:
            # Update slicing job
            await conn.execute(
                """
                UPDATE slicing_jobs
                SET status = 'completed',
                    completed_at = $1,
                    gcode_path = $2,
                    gcode_size = $3,
                    estimated_time_seconds = $4,
                    filament_used_mm = $5,
                    layer_count = $6,
                    three_mf_path = $7
                WHERE id = $8
                """,
                datetime.utcnow(), str(final_gcode_path), gcode_size,
                metadata["estimated_time_seconds"], metadata["filament_used_mm"],
                metadata["layer_count"],
                str(three_mf_path),
                db_job_id
            )

            # Update bundle
            await conn.execute(
                """
                UPDATE bundles
                SET sliced_at = $1,
                    gcode_path = $2,
                    print_time_estimate = $3,
                    filament_estimate = $4,
                    status = 'sliced'
                WHERE id = $5
                """,
                datetime.utcnow(), str(final_gcode_path),
                metadata["estimated_time_seconds"], metadata["filament_used_mm"],
                bundle["id"]
            )

        # Cleanup workspace
        logger.info("Cleaning up workspace...")
        slicer.cleanup_workspace(workspace)

        logger.info("Slicing job completed successfully")

        # Format estimated time
        time_seconds = metadata["estimated_time_seconds"]
        hours = time_seconds // 3600
        minutes = (time_seconds % 3600) // 60
        time_str = f"{hours}h {minutes}m" if hours > 0 else f"{minutes}m"

        return {
            "job_id": job_id,
            "status": "completed",
            "gcode_path": str(final_gcode_path),
            "gcode_size_mb": round(gcode_size / 1024 / 1024, 2),
            "estimated_time": time_str,
            "filament_used_mm": metadata["filament_used_mm"],
            "layer_count": metadata["layer_count"],
            "bounds_validated": True,
            "log_path": f"/data/logs/slice_{job_id}.log"
        }

    except SlicingError as e:
        logger.error(f"Slicing error: {str(e)}")

        # Update job as failed
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE slicing_jobs
                SET status = 'failed',
                    completed_at = $1,
                    error_message = $2
                WHERE id = $3
                """,
                datetime.utcnow(), str(e), db_job_id
            )

        # Cleanup workspace
        try:
            slicer.cleanup_workspace(workspace)
        except:
            pass

        return {
            "job_id": job_id,
            "status": "failed",
            "error": str(e)
        }

    except Exception as e:
        logger.exception("Unexpected error during slicing")

        # Update job as failed
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE slicing_jobs
                SET status = 'failed',
                    completed_at = $1,
                    error_message = $2
                WHERE id = $3
                """,
                datetime.utcnow(), f"Unexpected error: {str(e)}", db_job_id
            )

        raise HTTPException(status_code=500, detail=f"Slicing failed: {str(e)}")


@router.get("/slicing/jobs/{job_id}")
async def get_slicing_job(job_id: str):
    """Get slicing job status and details."""
    pool = get_pg_pool()

    async with pool.acquire() as conn:
        job = await conn.fetchrow(
            """
            SELECT j.job_id, j.status, j.started_at, j.completed_at,
                   j.log_path, j.gcode_path, j.gcode_size,
                   j.estimated_time_seconds, j.filament_used_mm, j.layer_count, j.error_message,
                   b.bundle_id, b.name as bundle_name
            FROM slicing_jobs j
            LEFT JOIN bundles b ON j.bundle_id = b.id
            WHERE j.job_id = $1
            """,
            job_id
        )

        if not job:
            raise HTTPException(status_code=404, detail="Slicing job not found")

        response = {
            "job_id": job["job_id"],
            "bundle_id": job["bundle_id"],
            "bundle_name": job["bundle_name"],
            "status": job["status"],
            "started_at": job["started_at"].isoformat() if job["started_at"] else None,
            "completed_at": job["completed_at"].isoformat() if job["completed_at"] else None,
            "log_path": job["log_path"]
        }

        if job["status"] == "completed":
            time_seconds = job["estimated_time_seconds"]
            hours = time_seconds // 3600
            minutes = (time_seconds % 3600) // 60
            time_str = f"{hours}h {minutes}m" if hours > 0 else f"{minutes}m"

            response.update({
                "gcode_path": job["gcode_path"],
                "gcode_size_mb": round(job["gcode_size"] / 1024 / 1024, 2),
                "estimated_time": time_str,
                "filament_used_mm": job["filament_used_mm"],
                "layer_count": job["layer_count"]
            })
        elif job["status"] == "failed":
            response["error"] = job["error_message"]

        return response


@router.get("/slicing/jobs/{job_id}/gcode/metadata")
async def get_gcode_metadata(job_id: str):
    """
    Get G-code preview metadata without full file.

    Returns metadata needed to initialize the preview viewer:
    - Total layer count
    - Bounding box dimensions
    - Estimated print time
    - Filament usage
    - File size
    """
    pool = get_pg_pool()

    async with pool.acquire() as conn:
        job = await conn.fetchrow(
            """
            SELECT j.job_id, j.status, j.gcode_path, j.gcode_size,
                   j.estimated_time_seconds, j.filament_used_mm, j.layer_count
            FROM slicing_jobs j
            WHERE j.job_id = $1
            """,
            job_id
        )

        if not job:
            raise HTTPException(status_code=404, detail="Slicing job not found")

        if job["status"] != "completed":
            raise HTTPException(status_code=400, detail=f"Job status is '{job['status']}', not completed")

        if not job["gcode_path"]:
            raise HTTPException(status_code=404, detail="G-code file not found")

    # Parse G-code bounds (reuse existing parser)
    from gcode_parser import parse_orca_metadata
    gcode_path = Path(job["gcode_path"])

    if not gcode_path.exists():
        raise HTTPException(status_code=404, detail="G-code file not found on disk")

    metadata = parse_orca_metadata(gcode_path)

    # Format time
    time_seconds = job["estimated_time_seconds"]
    hours = time_seconds // 3600
    minutes = (time_seconds % 3600) // 60
    time_str = f"{hours}h {minutes}m" if hours > 0 else f"{minutes}m"

    return {
        "job_id": job["job_id"],
        "layer_count": job["layer_count"],
        "bounds": metadata["bounds"],
        "estimated_time": time_str,
        "filament_used_mm": job["filament_used_mm"],
        "gcode_size_mb": round(job["gcode_size"] / 1024 / 1024, 2)
    }


@router.get("/slicing/jobs/{job_id}/gcode/layers")
async def get_gcode_layers(job_id: str, start: int = 0, count: int = 20):
    """
    Get layer-by-layer G-code geometry for preview rendering.

    Args:
        job_id: Slicing job ID
        start: Starting layer number (0-indexed)
        count: Number of layers to return (default: 20, max: 100)

    Returns:
        {
            "job_id": str,
            "total_layers": int,
            "start_layer": int,
            "layer_count": int,
            "layers": [
                {
                    "layer_num": int,
                    "z_height": float,
                    "moves": [
                        {"type": "travel|extrude", "x1": float, "y1": float, "x2": float, "y2": float}
                    ]
                }
            ]
        }
    """
    from db import get_cached_gcode_layers, cache_gcode_layers
    from gcode_layer_extractor import LayerExtractor

    # Validate parameters
    if start < 0:
        raise HTTPException(status_code=400, detail="start must be >= 0")

    if count < 1 or count > 100:
        raise HTTPException(status_code=400, detail="count must be between 1 and 100")

    pool = get_pg_pool()

    # Get job and G-code path
    async with pool.acquire() as conn:
        job = await conn.fetchrow(
            """
            SELECT j.job_id, j.status, j.gcode_path
            FROM slicing_jobs j
            WHERE j.job_id = $1
            """,
            job_id
        )

        if not job:
            raise HTTPException(status_code=404, detail="Slicing job not found")

        if job["status"] != "completed":
            raise HTTPException(status_code=400, detail=f"Job status is '{job['status']}', not completed")

        if not job["gcode_path"]:
            raise HTTPException(status_code=404, detail="G-code file not found")

    gcode_path = Path(job["gcode_path"])

    if not gcode_path.exists():
        raise HTTPException(status_code=404, detail="G-code file not found on disk")

    # Check cache first
    cached = await get_cached_gcode_layers(job_id, start, count)
    if cached:
        logger.info(f"Cache hit for job {job_id}, layers {start}-{start + count - 1}")
        return {
            "job_id": job_id,
            **cached
        }

    # Cache miss - extract layers
    logger.info(f"Cache miss for job {job_id}, extracting layers {start}-{start + count - 1}")
    extractor = LayerExtractor()

    try:
        result = extractor.extract_layers(gcode_path, start, count)
    except Exception as e:
        logger.error(f"Failed to extract layers: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to extract layers: {str(e)}")

    # Cache result
    await cache_gcode_layers(job_id, start, count, result)

    return {
        "job_id": job_id,
        **result
    }


@router.get("/slicing/jobs/{job_id}/download")
async def download_gcode(job_id: str):
    """
    Download the G-code file for a completed slicing job.

    Returns the G-code file as a download with proper filename.
    """
    from fastapi.responses import FileResponse

    pool = get_pg_pool()

    async with pool.acquire() as conn:
        job = await conn.fetchrow(
            """
            SELECT j.job_id, j.status, j.gcode_path, b.name as bundle_name
            FROM slicing_jobs j
            LEFT JOIN bundles b ON j.bundle_id = b.id
            WHERE j.job_id = $1
            """,
            job_id
        )

        if not job:
            raise HTTPException(status_code=404, detail="Slicing job not found")

        if job["status"] != "completed":
            raise HTTPException(status_code=400, detail=f"Job status is '{job['status']}', not completed")

        if not job["gcode_path"]:
            raise HTTPException(status_code=404, detail="G-code file not found")

    gcode_path = Path(job["gcode_path"])

    if not gcode_path.exists():
        raise HTTPException(status_code=404, detail="G-code file not found on disk")

    # Generate filename: bundle_name_timestamp.gcode
    import datetime
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    bundle_name = job["bundle_name"] or "print"
    # Sanitize bundle name for filename
    safe_name = "".join(c for c in bundle_name if c.isalnum() or c in (' ', '-', '_')).rstrip()
    filename = f"{safe_name}_{timestamp}.gcode"

    return FileResponse(
        path=gcode_path,
        media_type="text/plain",
        filename=filename
    )


@router.get("/slicing/jobs")
async def list_slicing_jobs(limit: int = 20, offset: int = 0):
    """
    List recent completed slicing jobs.

    Args:
        limit: Maximum number of jobs to return (default: 20, max: 100)
        offset: Number of jobs to skip (for pagination)

    Returns:
        List of slicing jobs with metadata.
    """
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 100")

    if offset < 0:
        raise HTTPException(status_code=400, detail="offset must be >= 0")

    pool = get_pg_pool()

    async with pool.acquire() as conn:
        jobs = await conn.fetch(
            """
            SELECT
                j.job_id,
                j.bundle_id,
                j.status,
                j.created_at,
                j.completed_at,
                j.gcode_size,
                j.estimated_time_seconds,
                j.filament_used_mm,
                j.layer_count,
                b.name as bundle_name,
                b.object_count,
                f.name as filament_name
            FROM slicing_jobs j
            LEFT JOIN bundles b ON j.bundle_id = b.bundle_id
            LEFT JOIN filaments f ON b.filament_id = f.id
            WHERE j.status = 'completed'
            ORDER BY j.completed_at DESC
            LIMIT $1 OFFSET $2
            """,
            limit,
            offset
        )

    results = []
    for job in jobs:
        # Format time
        time_seconds = job["estimated_time_seconds"] or 0
        hours = time_seconds // 3600
        minutes = (time_seconds % 3600) // 60
        time_str = f"{hours}h {minutes}m" if hours > 0 else f"{minutes}m"

        results.append({
            "job_id": job["job_id"],
            "bundle_id": job["bundle_id"],
            "bundle_name": job["bundle_name"],
            "object_count": job["object_count"],
            "filament_name": job["filament_name"],
            "gcode_size_mb": round(job["gcode_size"] / 1024 / 1024, 2) if job["gcode_size"] else 0,
            "estimated_time": time_str,
            "filament_used_mm": job["filament_used_mm"],
            "layer_count": job["layer_count"],
            "completed_at": job["completed_at"].isoformat() if job["completed_at"] else None
        })

    return {"jobs": results, "count": len(results)}
