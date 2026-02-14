"""Slicing endpoints for converting uploads to G-code (plate-based workflow)."""

import uuid
import logging
import shutil
import re
import json
import zipfile
import mimetypes
from pathlib import Path
from datetime import datetime
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
from typing import Optional, List, Dict

from db import get_pg_pool
from config import get_printer_profile
from slicer import OrcaSlicer, SlicingError
from profile_embedder import ProfileEmbedder, ProfileEmbedError
from multi_plate_parser import parse_multi_plate_3mf, extract_plate_objects, get_plate_bounds
from plate_validator import PlateValidator
from parser_3mf import detect_colors_from_3mf, detect_active_extruders_from_3mf


router = APIRouter(tags=["slicing"])
logger = logging.getLogger(__name__)


def _index_preview_assets(source_3mf: Path) -> Dict[str, object]:
    """Index embedded preview images from a 3MF archive.

    Returns:
      {
        "by_plate": {plate_id: internal_zip_path},
        "best": internal_zip_path | None,
      }
    """
    preview_map: Dict[int, str] = {}
    best_preview: Optional[str] = None

    try:
        with zipfile.ZipFile(source_3mf, "r") as zf:
            names = zf.namelist()
            image_names = [
                n for n in names
                if n.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))
                and "/metadata/" in f"/{n.lower()}"
            ]

            # Plate-specific previews, when naming allows inference.
            for name in image_names:
                lower = name.lower()
                match = re.search(r"(?:plate|top|pick|thumbnail|preview|cover)[_\-]?(\d+)", lower)
                if not match:
                    match = re.search(r"[_\-/](\d+)\.(?:png|jpg|jpeg|webp)$", lower)
                if not match:
                    continue

                plate_id = int(match.group(1))
                if plate_id not in preview_map:
                    preview_map[plate_id] = name

            # Best generic preview (used for uploads list/single-plate fallback).
            def score(path: str) -> tuple[int, int]:
                p = path.lower()
                if "thumbnail" in p:
                    return (0, len(p))
                if "preview" in p:
                    return (1, len(p))
                if "cover" in p:
                    return (2, len(p))
                if "top" in p:
                    return (3, len(p))
                if "plate" in p:
                    return (4, len(p))
                if "pick" in p:
                    return (5, len(p))
                return (9, len(p))

            if image_names:
                best_preview = sorted(image_names, key=score)[0]
    except Exception as e:
        logger.warning(f"Failed to index preview images: {e}")

    return {
        "by_plate": preview_map,
        "best": best_preview,
    }


def _index_plate_previews(source_3mf: Path) -> Dict[int, str]:
    assets = _index_preview_assets(source_3mf)
    by_plate = assets.get("by_plate")
    if isinstance(by_plate, dict):
        return by_plate
    return {}


def _guess_image_media_type(filename: str) -> str:
    media_type, _ = mimetypes.guess_type(filename)
    return media_type or "image/png"


def get_filament_ids(request) -> List[int]:
    """Get list of filament IDs from request, supporting both single and array."""
    if request.filament_ids and len(request.filament_ids) > 0:
        return request.filament_ids
    elif request.filament_id:
        return [request.filament_id]
    else:
        raise HTTPException(status_code=400, detail="filament_id or filament_ids required")


class SliceRequest(BaseModel):
    filament_ids: Optional[List[int]] = None  # Multi-filament support (list of filament IDs)
    filament_id: Optional[int] = None  # Single filament (for backward compatibility)
    filament_colors: Optional[List[str]] = None  # Override colors per extruder (e.g., ["#FF0000", "#00FF00"])
    layer_height: Optional[float] = 0.2
    infill_density: Optional[int] = 15
    wall_count: Optional[int] = 3
    infill_pattern: Optional[str] = "gyroid"
    supports: Optional[bool] = False
    nozzle_temp: Optional[int] = None
    bed_temp: Optional[int] = None
    bed_type: Optional[str] = None
    extruder_assignments: Optional[List[int]] = None  # Per-color target extruder slots (0-based)


class SlicePlateRequest(BaseModel):
    plate_id: int
    filament_ids: Optional[List[int]] = None  # Multi-filament support
    filament_id: Optional[int] = None  # Single filament (backward compat)
    filament_colors: Optional[List[str]] = None  # Override colors per extruder
    layer_height: Optional[float] = 0.2
    infill_density: Optional[int] = 15
    wall_count: Optional[int] = 3
    infill_pattern: Optional[str] = "gyroid"
    supports: Optional[bool] = False
    nozzle_temp: Optional[int] = None
    bed_temp: Optional[int] = None
    bed_type: Optional[str] = None
    extruder_assignments: Optional[List[int]] = None  # Per-color target extruder slots (0-based)


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
    job_logger.info(
        f"Request: filament_id={request.filament_id}, layer_height={request.layer_height}, "
        f"infill_density={request.infill_density}, wall_count={request.wall_count}, "
        f"infill_pattern={request.infill_pattern}, supports={request.supports}"
    )

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

        # Get filament IDs (supports both single and array)
        filament_ids = get_filament_ids(request)
        if len(filament_ids) > 4:
            raise HTTPException(status_code=400, detail="U1 supports at most 4 extruders (max 4 filament_ids).")
        
        # Validate all filaments exist and fetch their settings
        filament_rows = await conn.fetch(
            """
            SELECT id, name, material, nozzle_temp, bed_temp, print_speed, bed_type, color_hex, extruder_index
            FROM filaments
            WHERE id = ANY($1)
            """,
            filament_ids
        )

        if not filament_rows:
            job_logger.error(f"No filaments found for IDs: {filament_ids}")
            raise HTTPException(status_code=404, detail="One or more filaments not found")

        filament_by_id = {row["id"]: row for row in filament_rows}
        missing_ids = [fid for fid in filament_ids if fid not in filament_by_id]
        if missing_ids:
            job_logger.error(f"One or more filaments not found: {missing_ids}")
            raise HTTPException(status_code=404, detail="One or more filaments not found")

        # Preserve request order and allow duplicate filament IDs
        filaments = [filament_by_id[fid] for fid in filament_ids]

        # Log filaments being used
        filament_names = [f["name"] for f in filaments]
        job_logger.info(f"Using filaments: {', '.join(filament_names)}")

        # Check original 3MF file exists
        source_3mf = Path(upload["file_path"])
        if not source_3mf.exists():
            job_logger.error(f"Source 3MF file not found: {source_3mf}")
            raise HTTPException(status_code=500, detail="Source 3MF file not found")

        # Detect colors from 3MF file for viewer
        detected_colors = []
        try:
            detected_colors = detect_colors_from_3mf(source_3mf)
            job_logger.info(f"Detected colors from 3MF: {detected_colors}")
        except Exception as e:
            job_logger.warning(f"Could not detect colors from 3MF: {e}")

        active_extruders = detect_active_extruders_from_3mf(source_3mf)
        if active_extruders:
            job_logger.info(f"Active assigned extruders: {active_extruders}")

        extruder_remap = {}
        if request.extruder_assignments and active_extruders:
            for idx, src_ext in enumerate(active_extruders):
                if idx >= len(request.extruder_assignments):
                    break
                dst_zero_based = request.extruder_assignments[idx]
                dst_ext = int(dst_zero_based) + 1
                if 1 <= dst_ext <= 4:
                    extruder_remap[src_ext] = dst_ext
            if extruder_remap:
                job_logger.info(f"Applying extruder remap: {extruder_remap}")

        multicolor_slot_count = len(active_extruders) if active_extruders else len(detected_colors)
        if len(filaments) > 1 and multicolor_slot_count > 4:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Model requires {multicolor_slot_count} color/extruder slots, but U1 supports up to 4. "
                    "Use single-filament slicing or reduce colors to 4 or fewer."
                ),
            )

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

        # Prepare filament settings for multi-extruder
        # Orca expects temperatures as arrays of strings
        # Use request overrides if provided, otherwise use filament defaults
        nozzle_temps = []
        bed_temps = []
        extruder_colors = []
        material_types = []
        profile_names = []
        
        # Get nozzle and bed temps from filaments or request overrides
        for f in filaments:
            nozzle_temps.append(str(request.nozzle_temp if request.nozzle_temp is not None else f["nozzle_temp"]))
            bed_temps.append(str(request.bed_temp if request.bed_temp is not None else f["bed_temp"]))
            extruder_colors.append(f.get("color_hex", "#FFFFFF"))
            material_types.append(str(f.get("material", "PLA") or "PLA"))
            profile_names.append(str(f.get("name", "Snapmaker PLA") or "Snapmaker PLA"))
        
        # Override colors if user specified custom colors per extruder
        if request.filament_colors:
            for idx, color in enumerate(request.filament_colors):
                if idx < len(extruder_colors):
                    extruder_colors[idx] = color
        
        # Pad to 4 extruders (use last values for unused extruders)
        while len(nozzle_temps) < 4:
            nozzle_temps.append(nozzle_temps[-1] if nozzle_temps else "200")
        while len(bed_temps) < 4:
            bed_temps.append(bed_temps[-1] if bed_temps else "60")
        while len(extruder_colors) < 4:
            extruder_colors.append("#FFFFFF")
        while len(material_types) < 4:
            material_types.append(material_types[-1] if material_types else "PLA")
        while len(profile_names) < 4:
            profile_names.append(profile_names[-1] if profile_names else "Snapmaker PLA")
        
        # Create extruder count setting (how many filaments we're using)
        remap_slots = max(extruder_remap.values()) if extruder_remap else 0
        extruder_count = max(len(filaments), remap_slots)
        
        # Get the first filament's bed type for the plate
        first_filament = filaments[0]
        bed_type = request.bed_type if request.bed_type is not None else first_filament.get("bed_type", "PEI")

        filament_settings = {
            "nozzle_temperature": nozzle_temps,
            "nozzle_temperature_initial_layer": nozzle_temps,
            "bed_temperature": bed_temps,
            "bed_temperature_initial_layer": bed_temps,
            "bed_temperature_initial_layer_single": bed_temps[0],
            "cool_plate_temp": bed_temps,
            "cool_plate_temp_initial_layer": bed_temps,
            "textured_plate_temp": bed_temps,
            "textured_plate_temp_initial_layer": bed_temps,
        }

        if extruder_count > 1:
            filament_settings.update({
                "filament_type": material_types,
                "filament_colour": extruder_colors,
                "extruder_colour": extruder_colors,
                "default_filament_profile": profile_names,
                "filament_settings_id": profile_names,
            })

        # Add bed type if specified in request
        if bed_type:
            filament_settings["default_bed_type"] = bed_type

        job_logger.info(f"Using temps: nozzle={nozzle_temps}, bed={bed_temps}, bed_type={bed_type}, extruders={extruder_count}")

        # Prepare overrides from request
        overrides = {}
        if request.layer_height != 0.2:
            overrides["layer_height"] = str(request.layer_height)
        if request.infill_density != 15:
            overrides["sparse_infill_density"] = f"{request.infill_density}%"
        if request.wall_count != 3:
            overrides["wall_loops"] = str(request.wall_count)
        if request.infill_pattern and request.infill_pattern != "gyroid":
            overrides["sparse_infill_pattern"] = request.infill_pattern
        if request.supports:
            overrides["enable_support"] = "1"
            overrides["support_type"] = "normal(auto)"

        try:
            embedder.embed_profiles(
                source_3mf=source_3mf,
                output_3mf=embedded_3mf,
                filament_settings=filament_settings,
                overrides=overrides,
                requested_filament_count=extruder_count,
                extruder_remap=None,
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

        if len(filaments) > 1 and extruder_remap:
            ordered_sources = sorted(extruder_remap.keys())
            target_tools = [extruder_remap[src] - 1 for src in ordered_sources]
            remap_result = slicer.remap_compacted_tools(gcode_workspace_path, target_tools)
            if remap_result.get("applied"):
                job_logger.info(f"Remapped compacted tools: {remap_result.get('map')}")
            else:
                job_logger.info(f"Tool remap skipped: {remap_result}")

        # Parse G-code metadata
        job_logger.info("Parsing G-code metadata...")
        metadata = slicer.parse_gcode_metadata(gcode_workspace_path)
        used_tools = slicer.get_used_tools(gcode_workspace_path)
        job_logger.info(f"Tools used in G-code: {used_tools}")

        # Strict validation: when multicolor is requested, output must use T1+
        if len(filaments) > 1 and all(t == "T0" for t in used_tools):
            raise SlicingError(
                "Multicolour requested, but slicer produced single-tool G-code (T0 only)."
            )

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
        filament_colors_json = json.dumps(extruder_colors[:len(filaments)])
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
                    three_mf_path = $8,
                    filament_colors = $9
                WHERE job_id = $1
                """,
                job_id,
                datetime.utcnow(),
                str(final_gcode_path),
                gcode_size,
                metadata['estimated_time_seconds'],
                metadata['filament_used_mm'],
                metadata.get('layer_count'),
                str(embedded_3mf),
                filament_colors_json
            )

        job_logger.info(f"Slicing job {job_id} completed successfully")

        # Determine display colors: override > detected > filament defaults
        if request.filament_colors:
            display_colors = request.filament_colors
        elif detected_colors:
            display_colors = detected_colors
        else:
            display_colors = json.loads(filament_colors_json)
        
        return {
            "job_id": job_id,
            "status": "completed",
            "gcode_path": str(final_gcode_path),
            "gcode_size": gcode_size,
            "gcode_size_mb": round(gcode_size_mb, 2),
            "filament_colors": display_colors,
            "detected_colors": detected_colors,
            "metadata": {
                "estimated_time_seconds": metadata['estimated_time_seconds'],
                "filament_used_mm": metadata['filament_used_mm'],
                "layer_count": metadata.get('layer_count'),
                "bounds": metadata['bounds']
            }
        }

    except SlicingError as e:
        err_text = str(e)
        if len(filaments) > 1 and "segmentation fault" in err_text.lower():
            err_text = (
                "Multicolour slicing is unstable for this model in Snapmaker Orca v2.2.4 "
                "(slicer crash). Try single-filament slicing for now."
            )
        job_logger.error(f"Slicing failed: {err_text}")
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
                err_text
            )
        code = 400 if "unstable for this model" in err_text else 500
        raise HTTPException(status_code=code, detail=f"Slicing failed: {err_text}")

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
    job_logger.info(
        f"Request: filament_id={request.filament_id}, layer_height={request.layer_height}, "
        f"infill_density={request.infill_density}, wall_count={request.wall_count}, "
        f"infill_pattern={request.infill_pattern}, supports={request.supports}"
    )

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

        # Detect colors from 3MF file for viewer
        detected_colors = []
        try:
            detected_colors = detect_colors_from_3mf(source_3mf)
            job_logger.info(f"Detected colors from 3MF: {detected_colors}")
        except Exception as e:
            job_logger.warning(f"Could not detect colors from 3MF: {e}")

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

        # Get filament IDs (supports both single and array)
        filament_ids = get_filament_ids(request)
        if len(filament_ids) > 4:
            raise HTTPException(status_code=400, detail="U1 supports at most 4 extruders (max 4 filament_ids).")
        
        # Validate all filaments exist and fetch their settings
        filament_rows = await conn.fetch(
            """
            SELECT id, name, material, nozzle_temp, bed_temp, print_speed, bed_type, color_hex, extruder_index
            FROM filaments
            WHERE id = ANY($1)
            """,
            filament_ids
        )

        if not filament_rows:
            job_logger.error(f"No filaments found for IDs: {filament_ids}")
            raise HTTPException(status_code=404, detail="One or more filaments not found")

        filament_by_id = {row["id"]: row for row in filament_rows}
        missing_ids = [fid for fid in filament_ids if fid not in filament_by_id]
        if missing_ids:
            job_logger.error(f"One or more filaments not found: {missing_ids}")
            raise HTTPException(status_code=404, detail="One or more filaments not found")

        # Preserve request order and allow duplicate filament IDs
        filaments = [filament_by_id[fid] for fid in filament_ids]

        active_extruders = detect_active_extruders_from_3mf(source_3mf)
        if active_extruders:
            job_logger.info(f"Active assigned extruders: {active_extruders}")

        extruder_remap = {}
        if request.extruder_assignments and active_extruders:
            for idx, src_ext in enumerate(active_extruders):
                if idx >= len(request.extruder_assignments):
                    break
                dst_zero_based = request.extruder_assignments[idx]
                dst_ext = int(dst_zero_based) + 1
                if 1 <= dst_ext <= 4:
                    extruder_remap[src_ext] = dst_ext
            if extruder_remap:
                job_logger.info(f"Applying extruder remap: {extruder_remap}")

        multicolor_slot_count = len(active_extruders) if active_extruders else len(detected_colors)
        if len(filaments) > 1 and multicolor_slot_count > 4:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Model requires {multicolor_slot_count} color/extruder slots, but U1 supports up to 4. "
                    "Use single-filament slicing or reduce colors to 4 or fewer."
                ),
            )
        
        # Log filaments being used
        filament_names = [f["name"] for f in filaments]
        job_logger.info(f"Using filaments: {', '.join(filament_names)}")

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

        embedded_3mf = workspace / "sliceable.3mf"

        # Embed profiles into source 3MF and slice only selected plate via CLI
        job_logger.info("Embedding Orca profiles into 3MF...")
        embedder = ProfileEmbedder(Path("/app/orca_profiles"))

        # Prepare filament settings for multi-extruder
        nozzle_temps = []
        bed_temps = []
        extruder_colors = []
        material_types = []
        profile_names = []
        
        for f in filaments:
            nozzle_temps.append(str(request.nozzle_temp if request.nozzle_temp is not None else f["nozzle_temp"]))
            bed_temps.append(str(request.bed_temp if request.bed_temp is not None else f["bed_temp"]))
            extruder_colors.append(f.get("color_hex", "#FFFFFF"))
            material_types.append(str(f.get("material", "PLA") or "PLA"))
            profile_names.append(str(f.get("name", "Snapmaker PLA") or "Snapmaker PLA"))
        
        # Override colors if user specified custom colors per extruder
        if request.filament_colors:
            for idx, color in enumerate(request.filament_colors):
                if idx < len(extruder_colors):
                    extruder_colors[idx] = color
        
        # Pad to 4 extruders
        while len(nozzle_temps) < 4:
            nozzle_temps.append(nozzle_temps[-1] if nozzle_temps else "200")
        while len(bed_temps) < 4:
            bed_temps.append(bed_temps[-1] if bed_temps else "60")
        while len(extruder_colors) < 4:
            extruder_colors.append("#FFFFFF")
        while len(material_types) < 4:
            material_types.append(material_types[-1] if material_types else "PLA")
        while len(profile_names) < 4:
            profile_names.append(profile_names[-1] if profile_names else "Snapmaker PLA")
        
        remap_slots = max(extruder_remap.values()) if extruder_remap else 0
        extruder_count = max(len(filaments), remap_slots)
        first_filament = filaments[0]
        bed_type = request.bed_type if request.bed_type is not None else first_filament.get("bed_type", "PEI")

        filament_settings = {
            "nozzle_temperature": nozzle_temps,
            "nozzle_temperature_initial_layer": nozzle_temps,
            "bed_temperature": bed_temps,
            "bed_temperature_initial_layer": bed_temps,
            "bed_temperature_initial_layer_single": bed_temps[0],
            "cool_plate_temp": bed_temps,
            "cool_plate_temp_initial_layer": bed_temps,
            "textured_plate_temp": bed_temps,
            "textured_plate_temp_initial_layer": bed_temps,
        }

        if extruder_count > 1:
            filament_settings.update({
                "filament_type": material_types,
                "filament_colour": extruder_colors,
                "extruder_colour": extruder_colors,
                "default_filament_profile": profile_names,
                "filament_settings_id": profile_names,
            })

        # Add bed type if specified in request
        if bed_type:
            filament_settings["default_bed_type"] = bed_type

        job_logger.info(f"Using temps: nozzle={nozzle_temps}, bed={bed_temps}, bed_type={bed_type}, extruders={extruder_count}")

        # Prepare overrides from request
        overrides = {}
        if request.layer_height != 0.2:
            overrides["layer_height"] = str(request.layer_height)
        if request.infill_density != 15:
            overrides["sparse_infill_density"] = f"{request.infill_density}%"
        if request.wall_count != 3:
            overrides["wall_loops"] = str(request.wall_count)
        if request.infill_pattern and request.infill_pattern != "gyroid":
            overrides["sparse_infill_pattern"] = request.infill_pattern
        if request.supports:
            overrides["enable_support"] = "1"
            overrides["support_type"] = "normal(auto)"

        try:
            embedder.embed_profiles(
                source_3mf=source_3mf,
                output_3mf=embedded_3mf,
                filament_settings=filament_settings,
                overrides=overrides,
                requested_filament_count=extruder_count,
                extruder_remap=None,
            )
            three_mf_size_mb = embedded_3mf.stat().st_size / 1024 / 1024
            job_logger.info(f"Profile-embedded 3MF created: {embedded_3mf.name} ({three_mf_size_mb:.2f} MB)")
        except ProfileEmbedError as e:
            job_logger.error(f"Failed to embed profiles: {str(e)}")
            raise SlicingError(f"Profile embedding failed: {str(e)}")

        # Slice with Orca
        job_logger.info("Invoking Orca Slicer...")
        slicer = OrcaSlicer(printer_profile)

        result = slicer.slice_3mf(embedded_3mf, workspace, plate_index=request.plate_id)

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

        if len(filaments) > 1 and extruder_remap:
            ordered_sources = sorted(extruder_remap.keys())
            target_tools = [extruder_remap[src] - 1 for src in ordered_sources]
            remap_result = slicer.remap_compacted_tools(gcode_workspace_path, target_tools)
            if remap_result.get("applied"):
                job_logger.info(f"Remapped compacted tools: {remap_result.get('map')}")
            else:
                job_logger.info(f"Tool remap skipped: {remap_result}")

        # Parse G-code metadata
        job_logger.info("Parsing G-code metadata...")
        metadata = slicer.parse_gcode_metadata(gcode_workspace_path)
        used_tools = slicer.get_used_tools(gcode_workspace_path)
        job_logger.info(f"Tools used in G-code: {used_tools}")

        # Strict validation: when multicolor is requested, output must use T1+
        if len(filaments) > 1 and all(t == "T0" for t in used_tools):
            raise SlicingError(
                "Multicolour requested, but slicer produced single-tool G-code (T0 only)."
            )

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
        filament_colors_json = json.dumps(extruder_colors[:len(filaments)])
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
                    three_mf_path = $8,
                    filament_colors = $9
                WHERE job_id = $1
                """,
                job_id,
                datetime.utcnow(),
                str(final_gcode_path),
                gcode_size,
                metadata['estimated_time_seconds'],
                metadata['filament_used_mm'],
                metadata.get('layer_count'),
                str(embedded_3mf),
                filament_colors_json
            )

        job_logger.info(f"Plate slicing job {job_id} completed successfully")

        # Determine display colors: override > detected > filament defaults
        if request.filament_colors:
            display_colors = request.filament_colors
        elif detected_colors:
            display_colors = detected_colors
        else:
            display_colors = json.loads(filament_colors_json)
        
        return {
            "job_id": job_id,
            "status": "completed",
            "plate_id": request.plate_id,
            "gcode_path": str(final_gcode_path),
            "gcode_size": gcode_size,
            "gcode_size_mb": round(gcode_size_mb, 2),
            "filament_colors": display_colors,
            "detected_colors": detected_colors,
            "plate_validation": plate_validation,
            "metadata": {
                "estimated_time_seconds": metadata['estimated_time_seconds'],
                "filament_used_mm": metadata['filament_used_mm'],
                "layer_count": metadata.get('layer_count'),
                "bounds": metadata['bounds']
            }
        }

    except SlicingError as e:
        err_text = str(e)
        if len(filaments) > 1 and "segmentation fault" in err_text.lower():
            err_text = (
                "Multicolour slicing is unstable for this model in Snapmaker Orca v2.2.4 "
                "(slicer crash). Try single-filament slicing for now."
            )
        job_logger.error(f"Plate slicing failed: {err_text}")
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
                err_text
            )
        code = 400 if "unstable for this model" in err_text else 500
        raise HTTPException(status_code=code, detail=f"Plate slicing failed: {err_text}")

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
        preview_assets = _index_preview_assets(source_3mf)
        preview_map_obj = preview_assets.get("by_plate")
        preview_map: Dict[int, str] = preview_map_obj if isinstance(preview_map_obj, dict) else {}
        has_generic_preview = isinstance(preview_assets.get("best"), str)
        
        plate_info = []
        for plate in plates:
            try:
                validation = validator.validate_3mf_bounds(source_3mf, plate.plate_id)
                
                plate_dict = plate.to_dict()
                plate_dict.update({
                    "preview_url": (
                        f"/api/uploads/{upload_id}/plates/{plate.plate_id}/preview"
                        if plate.plate_id in preview_map
                        else (f"/api/uploads/{upload_id}/preview" if has_generic_preview else None)
                    ),
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
                    "preview_url": (
                        f"/api/uploads/{upload_id}/plates/{plate.plate_id}/preview"
                        if plate.plate_id in preview_map
                        else (f"/api/uploads/{upload_id}/preview" if has_generic_preview else None)
                    ),
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


@router.get("/uploads/{upload_id}/plates/{plate_id}/preview")
async def get_upload_plate_preview(upload_id: int, plate_id: int):
    """Return embedded preview image for a specific plate when available."""
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        upload = await conn.fetchrow(
            """
            SELECT id, file_path
            FROM uploads
            WHERE id = $1
            """,
            upload_id,
        )

        if not upload:
            raise HTTPException(status_code=404, detail="Upload not found")

    source_3mf = Path(upload["file_path"])
    if not source_3mf.exists():
        raise HTTPException(status_code=404, detail="Source 3MF file not found")

    assets = _index_preview_assets(source_3mf)
    by_plate_obj = assets.get("by_plate")
    preview_map: Dict[int, str] = by_plate_obj if isinstance(by_plate_obj, dict) else {}
    internal_path = preview_map.get(plate_id)
    if not internal_path and plate_id == 1:
        best_preview = assets.get("best")
        if isinstance(best_preview, str):
            internal_path = best_preview
    if not internal_path:
        raise HTTPException(status_code=404, detail="Plate preview not available")

    try:
        with zipfile.ZipFile(source_3mf, "r") as zf:
            image_bytes = zf.read(internal_path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read plate preview: {e}")

    return Response(content=image_bytes, media_type=_guess_image_media_type(internal_path))


@router.get("/uploads/{upload_id}/preview")
async def get_upload_preview(upload_id: int):
    """Return best embedded upload preview image (Explorer-style thumbnail)."""
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        upload = await conn.fetchrow(
            """
            SELECT id, file_path
            FROM uploads
            WHERE id = $1
            """,
            upload_id,
        )

        if not upload:
            raise HTTPException(status_code=404, detail="Upload not found")

    source_3mf = Path(upload["file_path"])
    if not source_3mf.exists():
        raise HTTPException(status_code=404, detail="Source 3MF file not found")

    assets = _index_preview_assets(source_3mf)
    best_preview = assets.get("best")
    if not isinstance(best_preview, str) or not best_preview:
        raise HTTPException(status_code=404, detail="Upload preview not available")

    try:
        with zipfile.ZipFile(source_3mf, "r") as zf:
            image_bytes = zf.read(best_preview)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read upload preview: {e}")

    return Response(content=image_bytes, media_type=_guess_image_media_type(best_preview))


@router.get("/jobs/{job_id}")
async def get_slicing_job(job_id: str):
    """Get slicing job status and results."""
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        job = await conn.fetchrow(
            """
            SELECT job_id, upload_id, status, started_at, completed_at,
                   gcode_path, gcode_size, estimated_time_seconds, filament_used_mm,
                   layer_count, filament_colors, error_message
            FROM slicing_jobs
            WHERE job_id = $1
            """,
            job_id
        )

        if not job:
            raise HTTPException(status_code=404, detail="Job not found")

    filament_colors = []
    if job["filament_colors"]:
        try:
            filament_colors = json.loads(job["filament_colors"])
        except:
            pass

    result = {
        "job_id": job["job_id"],
        "upload_id": job["upload_id"],
        "status": job["status"],
        "started_at": job["started_at"].isoformat() if job["started_at"] else None,
        "completed_at": job["completed_at"].isoformat() if job["completed_at"] else None,
        "gcode_path": job["gcode_path"],
        "gcode_size": job["gcode_size"],
        "gcode_size_mb": round(job["gcode_size"] / 1024 / 1024, 2) if job["gcode_size"] else None,
        "filament_colors": filament_colors,
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
    layer_comment_re = re.compile(r'^;\s*(LAYER_CHANGE|CHANGE_LAYER)\b', re.IGNORECASE)
    layer_number_re = re.compile(r'^;\s*LAYER\s*:\s*(\d+)\b', re.IGNORECASE)

    def flush_layer() -> bool:
        """Flush current buffered moves if layer is in range.

        Returns True when requested count has been reached.
        """
        nonlocal layers, layer_moves, current_layer
        if current_layer >= start and layer_moves:
            layers.append({
                "layer_num": current_layer,
                "z_height": current_z,
                "moves": layer_moves
            })
            layer_moves = []
            if len(layers) >= count:
                return True
        return False

    try:
        with open(gcode_path, 'r') as f:
            for line in f:
                line = line.strip()

                # Detect layer changes
                if layer_comment_re.match(line):
                    if flush_layer():
                        break
                    current_layer += 1
                    continue

                layer_number_match = layer_number_re.match(line)
                if layer_number_match:
                    if flush_layer():
                        break
                    current_layer = int(layer_number_match.group(1))
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
            if len(layers) < count:
                flush_layer()

    except Exception as e:
        logger.error(f"Failed to parse G-code layers: {e}")
        raise HTTPException(status_code=500, detail="Failed to parse G-code")

    return layers


@router.get("/jobs")
async def list_all_jobs():
    """List all slicing jobs with upload information."""
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        jobs = await conn.fetch("""
            SELECT 
                sj.job_id,
                sj.upload_id,
                u.filename,
                sj.status,
                sj.gcode_size,
                sj.estimated_time_seconds,
                sj.filament_used_mm,
                sj.layer_count,
                sj.started_at,
                sj.completed_at
            FROM slicing_jobs sj
            JOIN uploads u ON sj.upload_id = u.id
            ORDER BY sj.completed_at DESC NULLS LAST
        """)
        
        return [{
            "job_id": job["job_id"],
            "upload_id": job["upload_id"],
            "filename": job["filename"],
            "status": job["status"],
            "gcode_size": job["gcode_size"] or 0,
            "estimated_time_seconds": job["estimated_time_seconds"] or 0,
            "filament_used_mm": job["filament_used_mm"] or 0,
            "layer_count": job["layer_count"] or 0,
            "started_at": job["started_at"].isoformat() if job["started_at"] else None,
            "completed_at": job["completed_at"].isoformat() if job["completed_at"] else None
        } for job in jobs]


@router.delete("/jobs/{job_id}")
async def delete_job(job_id: str):
    """Delete a single slicing job and its G-code file."""
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        # Get job info first
        job = await conn.fetchrow(
            "SELECT gcode_path FROM slicing_jobs WHERE job_id = $1",
            job_id
        )
        
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        
        # Delete G-code file if exists
        if job["gcode_path"]:
            gcode_path = Path(job["gcode_path"])
            if gcode_path.exists():
                gcode_path.unlink()
        
        # Delete log file if exists
        log_path = Path(f"/data/logs/slice_{job_id}.log")
        if log_path.exists():
            log_path.unlink()
        
        # Delete from database
        await conn.execute("DELETE FROM slicing_jobs WHERE job_id = $1", job_id)
    
    return {"message": "Job deleted successfully"}
