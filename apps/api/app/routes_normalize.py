"""Normalization endpoints."""

import uuid
import json
import logging
from pathlib import Path
from datetime import datetime
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import List, Optional

from db import get_pg_pool
from normalizer import Normalizer, NormalizationError
from config import get_printer_profile

router = APIRouter(prefix="/normalize", tags=["normalize"])

NORMALIZED_DIR = Path("/data/normalized")
LOGS_DIR = Path("/data/logs")
NORMALIZED_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)


class NormalizeRequest(BaseModel):
    object_ids: Optional[List[str]] = None  # None = all objects
    printer_profile: str = "snapmaker_u1"


@router.post("/{upload_id}")
async def normalize_upload(upload_id: int, request: NormalizeRequest):
    """Normalize objects from an upload."""

    job_id = f"norm_{uuid.uuid4().hex[:12]}"
    log_path = LOGS_DIR / f"{job_id}.log"

    # Setup logging
    logger = logging.getLogger(job_id)
    logger.setLevel(logging.INFO)
    fh = logging.FileHandler(log_path)
    fh.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    logger.addHandler(fh)

    try:
        pool = get_pg_pool()
        async with pool.acquire() as conn:
            # Get upload
            upload = await conn.fetchrow(
                "SELECT id, filename, file_path FROM uploads WHERE id = $1",
                upload_id
            )
            if not upload:
                raise HTTPException(status_code=404, detail="Upload not found")

            # Get objects to normalize
            if request.object_ids:
                objects = await conn.fetch(
                    "SELECT id, name, object_id FROM objects WHERE upload_id = $1 AND object_id = ANY($2)",
                    upload_id, request.object_ids
                )
            else:
                objects = await conn.fetch(
                    "SELECT id, name, object_id FROM objects WHERE upload_id = $1",
                    upload_id
                )

            if not objects:
                raise HTTPException(status_code=404, detail="No objects found")

            # Create job record
            await conn.execute(
                """
                INSERT INTO normalization_jobs (job_id, upload_id, status, started_at, log_path)
                VALUES ($1, $2, 'running', NOW(), $3)
                """,
                job_id, upload_id, str(log_path)
            )

        logger.info(f"Starting normalization job {job_id} for upload {upload_id}")

        # Setup normalizer
        printer = get_printer_profile(request.printer_profile)
        normalizer = Normalizer(printer, logger)

        # Create output directory
        output_dir = NORMALIZED_DIR / str(upload_id)
        output_dir.mkdir(parents=True, exist_ok=True)

        # Normalize each object
        source_3mf = Path(upload["file_path"])
        normalized_objects = []
        errors = []

        for obj in objects:
            try:
                output_stl = output_dir / f"object_{obj['object_id']}.stl"
                result = normalizer.normalize_object(
                    source_3mf,
                    obj['object_id'],
                    obj['name'],
                    output_stl
                )
                normalized_objects.append(result)

                # Update database
                async with pool.acquire() as conn:
                    await conn.execute(
                        """
                        UPDATE objects SET
                            bounds_min_x = $1, bounds_min_y = $2, bounds_min_z = $3,
                            bounds_max_x = $4, bounds_max_y = $5, bounds_max_z = $6,
                            normalized_at = NOW(),
                            normalized_path = $7,
                            transform_data = $8,
                            normalization_status = 'normalized'
                        WHERE id = $9
                        """,
                        result['normalized_bounds']['x'][0],
                        result['normalized_bounds']['y'][0],
                        result['normalized_bounds']['z'][0],
                        result['normalized_bounds']['x'][1],
                        result['normalized_bounds']['y'][1],
                        result['normalized_bounds']['z'][1],
                        str(output_stl),
                        json.dumps(result['transform']),
                        obj['id']
                    )

            except NormalizationError as e:
                logger.error(f"Failed to normalize object {obj['object_id']}: {e}")
                errors.append({
                    "object_id": obj['object_id'],
                    "name": obj['name'],
                    "error": str(e)
                })

                # Mark as failed
                async with pool.acquire() as conn:
                    await conn.execute(
                        """
                        UPDATE objects SET
                            normalization_status = 'failed',
                            normalization_error = $1
                        WHERE id = $2
                        """,
                        str(e), obj['id']
                    )

        # Save manifest
        manifest = {
            "upload_id": upload_id,
            "normalized_at": datetime.utcnow().isoformat(),
            "job_id": job_id,
            "printer_profile": request.printer_profile,
            "objects": normalized_objects
        }
        (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

        # Update job status
        status = "completed" if not errors else "failed"
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE normalization_jobs SET
                    status = $1,
                    completed_at = NOW(),
                    error_message = $2
                WHERE job_id = $3
                """,
                status,
                json.dumps(errors) if errors else None,
                job_id
            )

        logger.info(f"Job {job_id} {status}")

        return {
            "job_id": job_id,
            "upload_id": upload_id,
            "status": status,
            "normalized_objects": normalized_objects,
            "errors": errors,
            "log_path": str(log_path)
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unexpected error")
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE normalization_jobs SET status = 'failed', error_message = $1, completed_at = NOW() WHERE job_id = $2",
                str(e), job_id
            )
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        # Cleanup logger handlers
        for handler in logger.handlers[:]:
            handler.close()
            logger.removeHandler(handler)
