import os
import uuid
from pathlib import Path
from datetime import datetime
from fastapi import APIRouter, UploadFile, File, HTTPException
from db import get_pg_pool
from parser_3mf import parse_3mf


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

    # Parse .3mf and extract objects
    try:
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

    # Store in database
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        # Insert upload record
        upload_id = await conn.fetchval(
            """
            INSERT INTO uploads (filename, file_path, file_size)
            VALUES ($1, $2, $3)
            RETURNING id
            """,
            file.filename,
            str(file_path),
            len(content),
        )

        # Insert object records
        for obj in objects:
            await conn.execute(
                """
                INSERT INTO objects (upload_id, name, object_id, vertices, triangles)
                VALUES ($1, $2, $3, $4, $5)
                """,
                upload_id,
                obj.name,
                obj.object_id,
                obj.vertices,
                obj.triangles,
            )

    return {
        "upload_id": upload_id,
        "filename": file.filename,
        "file_size": len(content),
        "objects": [obj.to_dict() for obj in objects],
    }


@router.get("/{upload_id}")
async def get_upload(upload_id: int):
    """Get upload details and associated objects."""
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        # Get upload record
        upload = await conn.fetchrow(
            "SELECT id, filename, file_size, uploaded_at FROM uploads WHERE id = $1",
            upload_id,
        )
        if not upload:
            raise HTTPException(status_code=404, detail="Upload not found")

        # Get objects with normalization status
        objects = await conn.fetch(
            """
            SELECT name, object_id, vertices, triangles,
                   normalization_status, normalized_path
            FROM objects
            WHERE upload_id = $1
            ORDER BY id
            """,
            upload_id,
        )

        # Determine overall normalization status
        # 'normalized' if all normalized, 'failed' if any failed, 'pending' otherwise
        statuses = [obj['normalization_status'] for obj in objects]
        if all(s == 'normalized' for s in statuses):
            overall_status = 'normalized'
        elif any(s == 'failed' for s in statuses):
            overall_status = 'failed'
        elif any(s == 'normalized' for s in statuses):
            overall_status = 'processing'
        else:
            overall_status = 'pending'

    return {
        "upload_id": upload["id"],
        "filename": upload["filename"],
        "file_size": upload["file_size"],
        "uploaded_at": upload["uploaded_at"].isoformat(),
        "normalization_status": overall_status,
        "objects": [dict(obj) for obj in objects],
        "object_count": len(objects),
    }


@router.get("/{upload_id}/objects")
async def get_upload_object_ids(upload_id: int):
    """Get database IDs of normalized objects for an upload."""
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        # Get normalized objects for this upload
        objects = await conn.fetch(
            """
            SELECT id FROM objects
            WHERE upload_id = $1 AND normalization_status = 'normalized'
            ORDER BY id
            """,
            upload_id
        )

    return {
        "object_ids": [obj["id"] for obj in objects]
    }


@router.get("")
async def list_uploads():
    """List all uploads."""
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        uploads = await conn.fetch(
            """
            SELECT u.id, u.filename, u.file_size, u.uploaded_at, COUNT(o.id) as object_count
            FROM uploads u
            LEFT JOIN objects o ON o.upload_id = u.id
            GROUP BY u.id
            ORDER BY u.uploaded_at DESC
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
                "object_count": u["object_count"],
            }
            for u in uploads
        ]
    }
