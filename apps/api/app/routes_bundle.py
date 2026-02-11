"""Bundle and filament management endpoints."""

import uuid
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import List, Optional

from db import get_pg_pool
from config import DEFAULT_FILAMENTS

router = APIRouter(tags=["bundles"])


# Filament endpoints
class FilamentCreate(BaseModel):
    name: str
    material: str
    nozzle_temp: int
    bed_temp: int
    print_speed: int = 60


@router.post("/filaments")
async def create_filament(filament: FilamentCreate):
    """Create a custom filament profile."""
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        # Check if name already exists
        existing = await conn.fetchrow("SELECT id FROM filaments WHERE name = $1", filament.name)
        if existing:
            raise HTTPException(status_code=400, detail=f"Filament '{filament.name}' already exists")

        filament_id = await conn.fetchval(
            """
            INSERT INTO filaments (name, material, nozzle_temp, bed_temp, print_speed)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id
            """,
            filament.name, filament.material, filament.nozzle_temp,
            filament.bed_temp, filament.print_speed
        )

    return {
        "filament_id": filament_id,
        "name": filament.name,
        "material": filament.material,
    }


@router.get("/filaments")
async def list_filaments():
    """List all available filament profiles."""
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        filaments = await conn.fetch(
            """
            SELECT id, name, material, nozzle_temp, bed_temp, print_speed, is_default
            FROM filaments
            ORDER BY is_default DESC, name ASC
            """
        )

    return {
        "filaments": [
            {
                "id": f["id"],
                "name": f["name"],
                "material": f["material"],
                "nozzle_temp": f["nozzle_temp"],
                "bed_temp": f["bed_temp"],
                "print_speed": f["print_speed"],
                "is_default": f["is_default"],
            }
            for f in filaments
        ]
    }


@router.post("/filaments/init-defaults")
async def initialize_default_filaments():
    """Initialize default filament profiles from config."""
    pool = get_pg_pool()
    created = []

    async with pool.acquire() as conn:
        for preset in DEFAULT_FILAMENTS:
            # Check if already exists
            existing = await conn.fetchrow("SELECT id FROM filaments WHERE name = $1", preset.name)
            if existing:
                continue

            filament_id = await conn.fetchval(
                """
                INSERT INTO filaments (name, material, nozzle_temp, bed_temp, print_speed, is_default)
                VALUES ($1, $2, $3, $4, $5, TRUE)
                RETURNING id
                """,
                preset.name, preset.material, preset.nozzle_temp,
                preset.bed_temp, preset.print_speed
            )
            created.append(preset.name)

    return {
        "created": created,
        "message": f"Initialized {len(created)} default filament(s)"
    }


# Bundle endpoints
class BundleCreate(BaseModel):
    name: str
    object_ids: List[int]
    filament_id: int


@router.post("/bundles")
async def create_bundle(bundle: BundleCreate):
    """Create a bundle of normalized objects for printing."""
    pool = get_pg_pool()

    async with pool.acquire() as conn:
        # Verify filament exists
        filament = await conn.fetchrow("SELECT id, name FROM filaments WHERE id = $1", bundle.filament_id)
        if not filament:
            raise HTTPException(status_code=404, detail="Filament not found")

        # Verify all objects exist and are normalized
        for obj_id in bundle.object_ids:
            obj = await conn.fetchrow(
                "SELECT id, normalization_status FROM objects WHERE id = $1",
                obj_id
            )
            if not obj:
                raise HTTPException(status_code=404, detail=f"Object {obj_id} not found")
            if obj["normalization_status"] != "normalized":
                raise HTTPException(
                    status_code=400,
                    detail=f"Object {obj_id} is not normalized (status: {obj['normalization_status']})"
                )

        # Create bundle
        bundle_id = f"bundle_{uuid.uuid4().hex[:12]}"
        db_bundle_id = await conn.fetchval(
            """
            INSERT INTO bundles (bundle_id, name, filament_id, status)
            VALUES ($1, $2, $3, 'pending')
            RETURNING id
            """,
            bundle_id, bundle.name, bundle.filament_id
        )

        # Add objects to bundle
        for obj_id in bundle.object_ids:
            await conn.execute(
                """
                INSERT INTO bundle_objects (bundle_id, object_id)
                VALUES ($1, $2)
                """,
                db_bundle_id, obj_id
            )

    return {
        "bundle_id": bundle_id,
        "name": bundle.name,
        "filament": filament["name"],
        "object_count": len(bundle.object_ids),
        "status": "pending"
    }


@router.get("/bundles/{bundle_id}")
async def get_bundle(bundle_id: str):
    """Get bundle details including objects and filament."""
    pool = get_pg_pool()

    async with pool.acquire() as conn:
        # Get bundle
        bundle = await conn.fetchrow(
            """
            SELECT b.id, b.bundle_id, b.name, b.status, b.created_at,
                   f.id as filament_id, f.name as filament_name, f.material,
                   f.nozzle_temp, f.bed_temp, f.print_speed
            FROM bundles b
            LEFT JOIN filaments f ON b.filament_id = f.id
            WHERE b.bundle_id = $1
            """,
            bundle_id
        )
        if not bundle:
            raise HTTPException(status_code=404, detail="Bundle not found")

        # Get objects in bundle
        objects = await conn.fetch(
            """
            SELECT o.id, o.name, o.object_id, o.normalized_path,
                   o.bounds_min_x, o.bounds_min_y, o.bounds_min_z,
                   o.bounds_max_x, o.bounds_max_y, o.bounds_max_z
            FROM bundle_objects bo
            JOIN objects o ON bo.object_id = o.id
            WHERE bo.bundle_id = $1
            ORDER BY bo.added_at
            """,
            bundle["id"]
        )

    return {
        "bundle_id": bundle["bundle_id"],
        "name": bundle["name"],
        "status": bundle["status"],
        "created_at": bundle["created_at"].isoformat(),
        "filament": {
            "id": bundle["filament_id"],
            "name": bundle["filament_name"],
            "material": bundle["material"],
            "nozzle_temp": bundle["nozzle_temp"],
            "bed_temp": bundle["bed_temp"],
            "print_speed": bundle["print_speed"],
        },
        "objects": [
            {
                "id": obj["id"],
                "name": obj["name"],
                "object_id": obj["object_id"],
                "normalized_path": obj["normalized_path"],
                "bounds": {
                    "min": {
                        "x": obj["bounds_min_x"],
                        "y": obj["bounds_min_y"],
                        "z": obj["bounds_min_z"],
                    },
                    "max": {
                        "x": obj["bounds_max_x"],
                        "y": obj["bounds_max_y"],
                        "z": obj["bounds_max_z"],
                    },
                },
            }
            for obj in objects
        ],
    }


@router.get("/bundles")
async def list_bundles():
    """List all bundles."""
    pool = get_pg_pool()

    async with pool.acquire() as conn:
        bundles = await conn.fetch(
            """
            SELECT b.bundle_id, b.name, b.status, b.created_at,
                   f.name as filament_name,
                   COUNT(bo.id) as object_count
            FROM bundles b
            LEFT JOIN filaments f ON b.filament_id = f.id
            LEFT JOIN bundle_objects bo ON b.id = bo.bundle_id
            GROUP BY b.id, f.name
            ORDER BY b.created_at DESC
            LIMIT 50
            """
        )

    return {
        "bundles": [
            {
                "bundle_id": b["bundle_id"],
                "name": b["name"],
                "status": b["status"],
                "filament": b["filament_name"],
                "object_count": b["object_count"],
                "created_at": b["created_at"].isoformat(),
            }
            for b in bundles
        ]
    }
