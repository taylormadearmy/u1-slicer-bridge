"""Slicing endpoints for converting uploads to G-code (plate-based workflow)."""

import asyncio
import uuid
import logging
import shutil
import re
import json
import zipfile
import mimetypes
from pathlib import Path
from datetime import datetime
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from typing import Optional, List, Dict

from db import get_pg_pool
from config import get_printer_profile
from slicer import OrcaSlicer, SlicingError
from profile_embedder import ProfileEmbedder, ProfileEmbedError
from multi_plate_parser import parse_multi_plate_3mf, extract_plate_objects, get_plate_bounds
from plate_validator import PlateValidator
from parser_3mf import detect_colors_from_3mf, detect_colors_per_plate, detect_print_settings, extract_3mf_metadata_batch


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
    layer_height: Optional[float] = Field(0.2, ge=0.04, le=0.6)
    infill_density: Optional[int] = Field(15, ge=0, le=100)
    wall_count: Optional[int] = Field(3, ge=1, le=20)
    infill_pattern: Optional[str] = "gyroid"
    supports: Optional[bool] = False
    support_type: Optional[str] = None  # "normal(auto)", "tree(auto)", "tree(manual)", etc.
    support_threshold_angle: Optional[int] = Field(None, ge=0, le=90)
    brim_type: Optional[str] = None  # "auto_brim", "outer_only", "no_brim", etc.
    brim_width: Optional[float] = Field(None, ge=0, le=50)
    brim_object_gap: Optional[float] = Field(None, ge=0, le=5)
    skirt_loops: Optional[int] = Field(None, ge=0, le=20)
    skirt_distance: Optional[float] = Field(None, ge=0, le=50)
    skirt_height: Optional[int] = Field(None, ge=0, le=20)
    enable_prime_tower: Optional[bool] = False
    prime_volume: Optional[int] = Field(None, ge=1, le=500)
    prime_tower_width: Optional[int] = Field(None, ge=10, le=100)
    prime_tower_brim_width: Optional[int] = Field(None, ge=0, le=20)
    prime_tower_brim_chamfer: Optional[bool] = True
    prime_tower_brim_chamfer_max_width: Optional[int] = Field(None, ge=0, le=50)
    nozzle_temp: Optional[int] = Field(None, ge=150, le=350)
    bed_temp: Optional[int] = Field(None, ge=0, le=150)
    bed_type: Optional[str] = None
    extruder_assignments: Optional[List[int]] = None  # Per-color target extruder slots (0-based)


class SlicePlateRequest(BaseModel):
    plate_id: int
    filament_ids: Optional[List[int]] = None  # Multi-filament support
    filament_id: Optional[int] = None  # Single filament (backward compat)
    filament_colors: Optional[List[str]] = None  # Override colors per extruder
    layer_height: Optional[float] = Field(0.2, ge=0.04, le=0.6)
    infill_density: Optional[int] = Field(15, ge=0, le=100)
    wall_count: Optional[int] = Field(3, ge=1, le=20)
    infill_pattern: Optional[str] = "gyroid"
    supports: Optional[bool] = False
    support_type: Optional[str] = None
    support_threshold_angle: Optional[int] = Field(None, ge=0, le=90)
    brim_type: Optional[str] = None
    brim_width: Optional[float] = Field(None, ge=0, le=50)
    brim_object_gap: Optional[float] = Field(None, ge=0, le=5)
    skirt_loops: Optional[int] = Field(None, ge=0, le=20)
    skirt_distance: Optional[float] = Field(None, ge=0, le=50)
    skirt_height: Optional[int] = Field(None, ge=0, le=20)
    enable_prime_tower: Optional[bool] = False
    prime_volume: Optional[int] = Field(None, ge=1, le=500)
    prime_tower_width: Optional[int] = Field(None, ge=10, le=100)
    prime_tower_brim_width: Optional[int] = Field(None, ge=0, le=20)
    prime_tower_brim_chamfer: Optional[bool] = True
    prime_tower_brim_chamfer_max_width: Optional[int] = Field(None, ge=0, le=50)
    nozzle_temp: Optional[int] = Field(None, ge=150, le=350)
    bed_temp: Optional[int] = Field(None, ge=0, le=150)
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


def _merge_slicer_settings(filament_row, filament_settings: dict, extruder_count: int, job_logger) -> None:
    """Merge OrcaSlicer-native settings from an imported filament profile into filament_settings.

    This enables M13 custom filament profiles: advanced slicer parameters (retraction,
    fan speeds, flow ratio, etc.) stored during JSON import are passed through to the
    slicer engine.

    For multi-extruder jobs, scalar values are broadcast into arrays matching
    extruder_count so OrcaSlicer receives properly shaped config.
    """
    raw = filament_row.get("slicer_settings") if hasattr(filament_row, "get") else filament_row["slicer_settings"]
    if not raw:
        return

    try:
        settings = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        job_logger.warning("Failed to parse slicer_settings JSON from filament profile")
        return

    if not isinstance(settings, dict) or not settings:
        return

    merged_count = 0
    for key, value in settings.items():
        # Skip keys already explicitly set in filament_settings (temps, bed type, etc.)
        if key in filament_settings:
            continue

        # OrcaSlicer expects array values for multi-extruder; broadcast scalars.
        if extruder_count > 1:
            if isinstance(value, list):
                # Pad or trim to extruder_count
                if len(value) < extruder_count:
                    value = value + [value[-1]] * (extruder_count - len(value))
                elif len(value) > extruder_count:
                    value = value[:extruder_count]
            else:
                value = [value] * extruder_count

        filament_settings[key] = value
        merged_count += 1

    if merged_count > 0:
        job_logger.info(f"Merged {merged_count} slicer-native settings from custom filament profile")


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
        f"infill_pattern={request.infill_pattern}, supports={request.supports}, "
        f"enable_prime_tower={request.enable_prime_tower}, "
        f"prime_volume={request.prime_volume}, "
        f"prime_tower_width={request.prime_tower_width}, "
        f"prime_tower_brim_width={request.prime_tower_brim_width}, "
        f"prime_tower_brim_chamfer={request.prime_tower_brim_chamfer}, "
        f"prime_tower_brim_chamfer_max_width={request.prime_tower_brim_chamfer_max_width}"
    )

    async with pool.acquire() as conn:
        # Validate upload exists
        upload = await conn.fetchrow(
            """
            SELECT id, filename, file_path, bounds_warning, detected_colors
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
            SELECT id, name, material, nozzle_temp, bed_temp, print_speed, bed_type, color_hex, extruder_index, slicer_settings
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

        # Use cached colors from DB, fall back to re-parsing for old uploads
        detected_colors = []
        if upload["detected_colors"]:
            try:
                detected_colors = json.loads(upload["detected_colors"])
                job_logger.info(f"Using cached colors: {detected_colors}")
            except Exception:
                pass
        if not detected_colors:
            try:
                detected_colors = detect_colors_from_3mf(source_3mf)
                job_logger.info(f"Detected colors from 3MF: {detected_colors}")
            except Exception as e:
                job_logger.warning(f"Could not detect colors from 3MF: {e}")

        # Single-pass metadata extraction (avoids multiple ZIP opens)
        file_meta = extract_3mf_metadata_batch(source_3mf)
        active_extruders = file_meta["active_extruders"]
        if active_extruders:
            job_logger.info(f"Active assigned extruders: {active_extruders}")

        # Auto-expand single filament to match source file's required colour count.
        # Handles both multi-extruder (per-object assignment) and SEMM painted files
        # where detected_colors exceeds active_extruders.
        required_extruders = max(
            len(active_extruders) if active_extruders else 0,
            len(detected_colors),
        )
        if required_extruders > 1 and len(filaments) < required_extruders and required_extruders <= 4:
            job_logger.info(
                f"Auto-expanding filament list from {len(filaments)} to {required_extruders} "
                f"to match source file's active extruder/colour count"
            )
            while len(filaments) < required_extruders:
                filaments.append(filaments[-1])
            filament_ids = [f["id"] for f in filaments]

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

        multicolor_slot_count = max(
            len(active_extruders) if active_extruders else 0,
            len(detected_colors),
        )
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

        # Merge slicer-native settings from imported filament profiles (M13).
        # Only the first filament's advanced settings are applied (primary extruder).
        primary_filament = filaments[0]
        _merge_slicer_settings(primary_filament, filament_settings, extruder_count, job_logger)

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
            overrides["support_type"] = request.support_type or "normal(auto)"
            if request.support_threshold_angle is not None:
                overrides["support_threshold_angle"] = str(request.support_threshold_angle)
        if request.brim_type is not None:
            overrides["brim_type"] = request.brim_type
        if request.brim_width is not None:
            overrides["brim_width"] = str(request.brim_width)
        if request.brim_object_gap is not None:
            overrides["brim_object_gap"] = str(request.brim_object_gap)
        if request.skirt_loops is not None:
            overrides["skirt_loops"] = str(request.skirt_loops)
        if request.skirt_distance is not None:
            overrides["skirt_distance"] = str(request.skirt_distance)
        if request.skirt_height is not None:
            overrides["skirt_height"] = str(request.skirt_height)
        if request.enable_prime_tower:
            overrides["enable_prime_tower"] = "1"
            if request.prime_volume is not None:
                overrides["prime_volume"] = str(max(0, int(request.prime_volume)))
            if request.prime_tower_width is not None:
                overrides["prime_tower_width"] = str(max(1, int(request.prime_tower_width)))
            if request.prime_tower_brim_width is not None:
                overrides["prime_tower_brim_width"] = str(max(0, int(request.prime_tower_brim_width)))
            overrides["prime_tower_brim_chamfer"] = "1" if request.prime_tower_brim_chamfer else "0"
            if request.prime_tower_brim_chamfer_max_width is not None:
                overrides["prime_tower_brim_chamfer_max_width"] = str(max(0, int(request.prime_tower_brim_chamfer_max_width)))
        else:
            overrides["enable_prime_tower"] = "0"

        if extruder_count > 1:
            # U1 tool swaps are direct extruder changes, not AMS load/unload cycles.
            # Avoid inflated print-time estimates from single-nozzle MMU timing defaults.
            overrides["machine_load_filament_time"] = "0"
            overrides["machine_unload_filament_time"] = "0"

        try:
            await embedder.embed_profiles_async(
                source_3mf=source_3mf,
                output_3mf=embedded_3mf,
                filament_settings=filament_settings,
                overrides=overrides,
                requested_filament_count=extruder_count,
                extruder_remap=None,
                preserve_geometry=True,
                precomputed_is_bambu=file_meta["is_bambu"],
                precomputed_has_multi_assignments=file_meta["has_multi_extruder_assignments"],
                precomputed_has_layer_changes=file_meta["has_layer_tool_changes"],
            )
            three_mf_size_mb = embedded_3mf.stat().st_size / 1024 / 1024
            job_logger.info(f"Profile-embedded 3MF created: {embedded_3mf.name} ({three_mf_size_mb:.2f} MB)")
        except ProfileEmbedError as e:
            job_logger.error(f"Failed to embed profiles: {str(e)}")
            raise SlicingError(f"Profile embedding failed: {str(e)}")

        # Slice with Orca (async to avoid blocking other API requests)
        job_logger.info("Invoking Orca Slicer...")
        printer_profile = get_printer_profile("snapmaker_u1")
        slicer = OrcaSlicer(printer_profile)

        result = await slicer.slice_3mf_async(embedded_3mf, workspace)

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

        # Parse G-code metadata (async to avoid blocking event loop)
        job_logger.info("Parsing G-code metadata...")
        metadata = await asyncio.to_thread(slicer.parse_gcode_metadata, gcode_workspace_path)
        used_tools = await asyncio.to_thread(slicer.get_used_tools, gcode_workspace_path)
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
        # Treat all-#FFFFFF as no override (default filament profiles are white)
        has_real_override = (
            request.filament_colors
            and any(c.upper() != '#FFFFFF' for c in request.filament_colors)
        )
        if has_real_override:
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
                "filament_used_g": metadata.get('filament_used_g', []),
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
        f"infill_pattern={request.infill_pattern}, supports={request.supports}, "
        f"enable_prime_tower={request.enable_prime_tower}, "
        f"prime_volume={request.prime_volume}, "
        f"prime_tower_width={request.prime_tower_width}, "
        f"prime_tower_brim_width={request.prime_tower_brim_width}, "
        f"prime_tower_brim_chamfer={request.prime_tower_brim_chamfer}, "
        f"prime_tower_brim_chamfer_max_width={request.prime_tower_brim_chamfer_max_width}"
    )

    async with pool.acquire() as conn:
        # Validate upload exists
        upload = await conn.fetchrow(
            """
            SELECT id, filename, file_path, bounds_warning, detected_colors
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

        # Use cached colors from DB, fall back to re-parsing for old uploads
        detected_colors = []
        if upload["detected_colors"]:
            try:
                detected_colors = json.loads(upload["detected_colors"])
                job_logger.info(f"Using cached colors: {detected_colors}")
            except Exception:
                pass
        if not detected_colors:
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
            SELECT id, name, material, nozzle_temp, bed_temp, print_speed, bed_type, color_hex, extruder_index, slicer_settings
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

        # Single-pass metadata extraction (avoids multiple ZIP opens)
        file_meta = extract_3mf_metadata_batch(source_3mf)
        active_extruders = file_meta["active_extruders"]
        if active_extruders:
            job_logger.info(f"Active assigned extruders: {active_extruders}")

        # Auto-expand single filament to match source file's required colour count.
        # Handles both multi-extruder (per-object assignment) and SEMM painted files
        # where detected_colors exceeds active_extruders.
        required_extruders = max(
            len(active_extruders) if active_extruders else 0,
            len(detected_colors),
        )
        if required_extruders > 1 and len(filaments) < required_extruders and required_extruders <= 4:
            job_logger.info(
                f"Auto-expanding filament list from {len(filaments)} to {required_extruders} "
                f"to match source file's active extruder/colour count"
            )
            while len(filaments) < required_extruders:
                filaments.append(filaments[-1])
            filament_ids = [f["id"] for f in filaments]

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

        multicolor_slot_count = max(
            len(active_extruders) if active_extruders else 0,
            len(detected_colors),
        )
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

        # Merge slicer-native settings from imported filament profiles (M13).
        # Only the first filament's advanced settings are applied (primary extruder).
        primary_filament = filaments[0]
        _merge_slicer_settings(primary_filament, filament_settings, extruder_count, job_logger)

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
            overrides["support_type"] = request.support_type or "normal(auto)"
            if request.support_threshold_angle is not None:
                overrides["support_threshold_angle"] = str(request.support_threshold_angle)
        if request.brim_type is not None:
            overrides["brim_type"] = request.brim_type
        if request.brim_width is not None:
            overrides["brim_width"] = str(request.brim_width)
        if request.brim_object_gap is not None:
            overrides["brim_object_gap"] = str(request.brim_object_gap)
        if request.skirt_loops is not None:
            overrides["skirt_loops"] = str(request.skirt_loops)
        if request.skirt_distance is not None:
            overrides["skirt_distance"] = str(request.skirt_distance)
        if request.skirt_height is not None:
            overrides["skirt_height"] = str(request.skirt_height)
        if request.enable_prime_tower:
            overrides["enable_prime_tower"] = "1"
            if request.prime_volume is not None:
                overrides["prime_volume"] = str(max(0, int(request.prime_volume)))
            if request.prime_tower_width is not None:
                overrides["prime_tower_width"] = str(max(1, int(request.prime_tower_width)))
            if request.prime_tower_brim_width is not None:
                overrides["prime_tower_brim_width"] = str(max(0, int(request.prime_tower_brim_width)))
            overrides["prime_tower_brim_chamfer"] = "1" if request.prime_tower_brim_chamfer else "0"
            if request.prime_tower_brim_chamfer_max_width is not None:
                overrides["prime_tower_brim_chamfer_max_width"] = str(max(0, int(request.prime_tower_brim_chamfer_max_width)))
        else:
            overrides["enable_prime_tower"] = "0"

        if extruder_count > 1:
            overrides["machine_load_filament_time"] = "0"
            overrides["machine_unload_filament_time"] = "0"

        try:
            await embedder.embed_profiles_async(
                source_3mf=source_3mf,
                output_3mf=embedded_3mf,
                filament_settings=filament_settings,
                overrides=overrides,
                requested_filament_count=extruder_count,
                extruder_remap=None,
                precomputed_is_bambu=file_meta["is_bambu"],
                precomputed_has_multi_assignments=file_meta["has_multi_extruder_assignments"],
                precomputed_has_layer_changes=file_meta["has_layer_tool_changes"],
            )
            three_mf_size_mb = embedded_3mf.stat().st_size / 1024 / 1024
            job_logger.info(f"Profile-embedded 3MF created: {embedded_3mf.name} ({three_mf_size_mb:.2f} MB)")
        except ProfileEmbedError as e:
            job_logger.error(f"Failed to embed profiles: {str(e)}")
            raise SlicingError(f"Profile embedding failed: {str(e)}")

        # Slice with Orca (async to avoid blocking other API requests)
        job_logger.info("Invoking Orca Slicer...")
        slicer = OrcaSlicer(printer_profile)

        result = await slicer.slice_3mf_async(embedded_3mf, workspace, plate_index=request.plate_id)

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

        # Parse G-code metadata (async to avoid blocking event loop)
        job_logger.info("Parsing G-code metadata...")
        metadata = await asyncio.to_thread(slicer.parse_gcode_metadata, gcode_workspace_path)
        used_tools = await asyncio.to_thread(slicer.get_used_tools, gcode_workspace_path)
        job_logger.info(f"Tools used in G-code: {used_tools}")

        # For selected-plate slices, gracefully accept single-tool output.
        # Some multi-plate projects contain per-plate single-color geometry
        # even when file-level metadata advertises multiple colors.
        if len(filaments) > 1 and all(t == "T0" for t in used_tools):
            job_logger.warning(
                "Multicolour requested for selected plate, but slicer produced T0-only output; "
                "continuing as single-tool plate slice"
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
        # Treat all-#FFFFFF as no override (default filament profiles are white)
        has_real_override = (
            request.filament_colors
            and any(c.upper() != '#FFFFFF' for c in request.filament_colors)
        )
        if has_real_override:
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
                "filament_used_g": metadata.get('filament_used_g', []),
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
        upload = await conn.fetchrow(
            """
            SELECT id, filename, file_path, is_multi_plate, plate_count,
                   detected_colors, file_print_settings, plate_metadata
            FROM uploads
            WHERE id = $1
            """,
            upload_id
        )

        if not upload:
            raise HTTPException(status_code=404, detail="Upload not found")

    # Fast path: return cached plate metadata if available
    if upload["plate_metadata"]:
        try:
            cached_plates = json.loads(upload["plate_metadata"])
            file_ps = json.loads(upload["file_print_settings"]) if upload["file_print_settings"] else {}

            # Reconstruct preview URLs (they depend on upload_id in the path)
            for plate in cached_plates:
                pid = plate.get("plate_id")
                if plate.get("has_preview"):
                    plate["preview_url"] = f"/api/uploads/{upload_id}/plates/{pid}/preview"
                elif plate.get("has_generic_preview"):
                    plate["preview_url"] = f"/api/uploads/{upload_id}/preview"
                else:
                    plate["preview_url"] = None
                plate.pop("has_preview", None)
                plate.pop("has_generic_preview", None)

            return {
                "upload_id": upload_id,
                "filename": upload["filename"],
                "is_multi_plate": bool(upload["is_multi_plate"]),
                "plate_count": upload["plate_count"] or len(cached_plates),
                "file_print_settings": file_ps,
                "plates": cached_plates
            }
        except Exception as e:
            logger.warning(f"Failed to read cached plate metadata, falling back to re-parse: {e}")

    # Slow fallback for uploads created before the cache columns existed
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

        printer_profile = get_printer_profile("snapmaker_u1")
        validator = PlateValidator(printer_profile)
        preview_assets = _index_preview_assets(source_3mf)
        preview_map_obj = preview_assets.get("by_plate")
        preview_map: Dict[int, str] = preview_map_obj if isinstance(preview_map_obj, dict) else {}
        has_generic_preview = isinstance(preview_assets.get("best"), str)

        try:
            colors_per_plate = detect_colors_per_plate(source_3mf)
        except Exception:
            colors_per_plate = {}
        global_colors: List[str] = []
        if not colors_per_plate:
            try:
                global_colors = detect_colors_from_3mf(source_3mf)
            except Exception:
                pass

        plate_info = []
        for plate in plates:
            try:
                validation = validator.validate_3mf_bounds(source_3mf, plate.plate_id)

                plate_dict = plate.to_dict()
                plate_colors = colors_per_plate.get(plate.plate_id, global_colors[:1])
                plate_dict.update({
                    "detected_colors": plate_colors,
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
                plate_colors = colors_per_plate.get(plate.plate_id, global_colors[:1])
                plate_dict.update({
                    "detected_colors": plate_colors,
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

        file_print_settings = {}
        try:
            file_print_settings = detect_print_settings(source_3mf)
        except Exception:
            pass

        return {
            "upload_id": upload_id,
            "filename": upload["filename"],
            "is_multi_plate": True,
            "plate_count": len(plates),
            "file_print_settings": file_print_settings,
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
            SELECT j.job_id, j.upload_id, j.status, j.started_at, j.completed_at,
                   j.gcode_path, j.gcode_size, j.estimated_time_seconds, j.filament_used_mm,
                   j.layer_count, j.filament_colors, j.error_message,
                   u.detected_colors AS upload_detected_colors
            FROM slicing_jobs j
            LEFT JOIN uploads u ON u.id = j.upload_id
            WHERE j.job_id = $1
            """,
            job_id
        )

        if not job:
            raise HTTPException(status_code=404, detail="Job not found")

    filament_colors = []
    if job["filament_colors"]:
        try:
            filament_colors = json.loads(job["filament_colors"])
        except (json.JSONDecodeError, ValueError):
            pass

    detected_colors = []
    if job["upload_detected_colors"]:
        try:
            detected_colors = json.loads(job["upload_detected_colors"])
        except (json.JSONDecodeError, ValueError):
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
        "detected_colors": detected_colors,
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


@router.get("/jobs/{job_id}/download-3mf")
async def download_embedded_3mf(job_id: str):
    """Download the profile-embedded 3MF used for slicing."""
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT sj.three_mf_path, sj.status, u.filename
            FROM slicing_jobs sj
            JOIN uploads u ON sj.upload_id = u.id
            WHERE sj.job_id = $1
            """,
            job_id,
        )

        if not row:
            raise HTTPException(status_code=404, detail="Job not found")

        if row["status"] != "completed":
            raise HTTPException(status_code=400, detail="Job not completed")

        if not row["three_mf_path"]:
            raise HTTPException(status_code=404, detail="Embedded 3MF not available for this job")

        three_mf_path = Path(row["three_mf_path"])
        if not three_mf_path.exists():
            raise HTTPException(status_code=404, detail="Embedded 3MF file not found on disk (cache may have been cleared)")

    # Build download filename: original stem + _sliced.3mf
    original = row["filename"] or "model.3mf"
    stem = original.rsplit(".", 1)[0] if "." in original else original
    download_name = f"{stem}_sliced.3mf"

    return FileResponse(
        path=three_mf_path,
        media_type="application/vnd.ms-package.3dmanufacturing-3dmodel+xml",
        filename=download_name,
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
async def list_all_jobs(
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """List all slicing jobs with upload information."""
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        total = await conn.fetchval(
            "SELECT COUNT(*) FROM slicing_jobs sj JOIN uploads u ON sj.upload_id = u.id"
        )
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
            LIMIT $1 OFFSET $2
        """, limit, offset)

        job_list = [{
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

        return {
            "jobs": job_list,
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + limit < total,
        }


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
