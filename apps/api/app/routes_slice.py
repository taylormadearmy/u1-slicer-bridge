"""Slicing endpoints for converting uploads to G-code (plate-based workflow)."""

import asyncio
import uuid
import logging
import shutil
import re
import json
import zipfile
import mimetypes
import time
from pathlib import Path
from datetime import datetime
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from typing import Any, Optional, List, Dict, Tuple

from db import get_pg_pool
from config import get_printer_profile
from slicer import OrcaSlicer, SlicingError, SlicingCancelledError, cancel_slice_job
from profile_embedder import ProfileEmbedder, ProfileEmbedError
from multi_plate_parser import (
    parse_multi_plate_3mf,
    extract_plate_objects,
    get_plate_bounds,
    list_build_items_3mf,
    list_build_item_geometry_3mf,
    _apply_affine_to_bounds_3x4,
)
from plate_validator import PlateValidator
from parser_3mf import detect_colors_from_3mf, detect_colors_per_plate, detect_print_settings
from threemf_model import parse_threemf, apply_user_moves
from scale_3mf import apply_uniform_scale_to_3mf, apply_layout_scale_to_3mf
from transform_3mf import apply_object_transforms_to_3mf
from gcode_thumbnails import inject_gcode_thumbnails


router = APIRouter(tags=["slicing"])
logger = logging.getLogger(__name__)
INT32_MAX = 2_147_483_647

# Module-level compiled regex patterns for G-code parsing (avoid per-call recompilation)
_RE_GCODE_COORD = re.compile(r'([XYZ])([\d.-]+)')
_RE_GCODE_FIELDS = re.compile(r'([GXYZEF])([\d.-]+)')
_RE_LAYER_CHANGE = re.compile(r'^;\s*(LAYER_CHANGE|CHANGE_LAYER)\b', re.IGNORECASE)
_RE_LAYER_NUMBER = re.compile(r'^;\s*LAYER\s*:\s*(\d+)\b', re.IGNORECASE)

# ---------------------------------------------------------------------------
# In-memory progress store for active slicing jobs.
# Keys are job_id strings.  Values: {"progress": 0-100, "message": str}
# Thread-safe for simple dict assignments under CPython's GIL.
# ---------------------------------------------------------------------------
_job_progress: Dict[str, Dict] = {}


def _update_progress(job_id: str, progress: int, message: str = ""):
    _job_progress[job_id] = {"progress": min(max(progress, 0), 100), "message": message}


def _get_progress(job_id: str) -> Dict:
    return _job_progress.get(job_id, {"progress": 0, "message": ""})


def _clear_progress(job_id: str):
    _job_progress.pop(job_id, None)


def _load_bambu_plate_metadata(source_3mf: Path) -> Optional[Dict[str, Any]]:
    """Single-pass extraction of all Bambu plate metadata.

    Opens the ZIP once and returns all plate mapping info needed by the
    slice-plate route, replacing 4-5 separate ZIP opens.

    Returns None if not a Bambu file. Otherwise returns:
        {
            "object_to_plater": {object_id_str: plater_id_int},
            "plater_to_objects": {plater_id_int: [object_id_str, ...]},
            "oid_to_build_idx": {object_id_str: build_item_index_1based},
        }
    """
    try:
        import xml.etree.ElementTree as ET
        with zipfile.ZipFile(source_3mf, 'r') as zf:
            if 'Metadata/model_settings.config' not in zf.namelist():
                return None

            # Parse model_settings.config for plate→object mappings
            ms_root = ET.fromstring(zf.read('Metadata/model_settings.config'))
            object_to_plater: Dict[str, int] = {}
            plater_to_objects: Dict[int, List[str]] = {}
            for plate in ms_root.findall('.//plate'):
                plater_id = None
                for meta in plate.findall('metadata'):
                    if meta.get('key') == 'plater_id':
                        try:
                            plater_id = int(meta.get('value'))
                        except (TypeError, ValueError):
                            pass
                if plater_id is None:
                    continue
                plate_oids: List[str] = []
                for mi in plate.findall('model_instance'):
                    for m in mi.findall('metadata'):
                        if m.get('key') == 'object_id':
                            oid = str(m.get('value'))
                            object_to_plater[oid] = plater_id
                            plate_oids.append(oid)
                if plate_oids:
                    plater_to_objects[plater_id] = plate_oids

            # Build object_id → build_item_index map from model XML
            oid_to_build_idx: Dict[str, int] = {}
            if '3D/3dmodel.model' in zf.namelist():
                model_root = ET.fromstring(zf.read('3D/3dmodel.model'))
                ns_3mf = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
                build = model_root.find(f"{{{ns_3mf}}}build")
                if build is not None:
                    for i, item in enumerate(build.findall(f"{{{ns_3mf}}}item"), start=1):
                        oid = item.get("objectid")
                        if oid:
                            oid_to_build_idx[oid] = i

            return {
                "object_to_plater": object_to_plater,
                "plater_to_objects": plater_to_objects,
                "oid_to_build_idx": oid_to_build_idx,
            }
    except Exception as e:
        logger.warning(f"Could not load Bambu plate metadata: {e}")
        return None


def _get_bambu_plate_for_object(source_3mf: Path, object_id: str,
                                preloaded: Optional[Dict] = None) -> Optional[int]:
    """Look up the Bambu plater_id that contains the given object_id.

    If `preloaded` is provided (from _load_bambu_plate_metadata), avoids ZIP open.
    """
    if preloaded is not None:
        return preloaded["object_to_plater"].get(str(object_id))

    try:
        import xml.etree.ElementTree as ET
        with zipfile.ZipFile(source_3mf, 'r') as zf:
            if 'Metadata/model_settings.config' not in zf.namelist():
                return None
            root = ET.fromstring(zf.read('Metadata/model_settings.config'))
            for plate in root.findall('.//plate'):
                plater_id = None
                for meta in plate.findall('metadata'):
                    if meta.get('key') == 'plater_id':
                        plater_id = meta.get('value')
                for mi in plate.findall('model_instance'):
                    for m in mi.findall('metadata'):
                        if m.get('key') == 'object_id' and m.get('value') == str(object_id):
                            return int(plater_id) if plater_id else None
    except Exception as e:
        logger.warning(f"Could not look up Bambu plate for object {object_id}: {e}")
    return None


def _get_bambu_plate_object_ids(source_3mf: Path, plater_id: int,
                                preloaded: Optional[Dict] = None) -> List[str]:
    """Return all object_ids assigned to the given Bambu plater_id.

    If `preloaded` is provided (from _load_bambu_plate_metadata), avoids ZIP open.
    """
    if preloaded is not None:
        return preloaded["plater_to_objects"].get(plater_id, [])

    result: List[str] = []
    try:
        import xml.etree.ElementTree as ET
        with zipfile.ZipFile(source_3mf, 'r') as zf:
            if 'Metadata/model_settings.config' not in zf.namelist():
                return result
            root = ET.fromstring(zf.read('Metadata/model_settings.config'))
            for plate in root.findall('.//plate'):
                pid = None
                for meta in plate.findall('metadata'):
                    if meta.get('key') == 'plater_id':
                        pid = meta.get('value')
                if pid is not None and int(pid) == plater_id:
                    for mi in plate.findall('model_instance'):
                        for m in mi.findall('metadata'):
                            if m.get('key') == 'object_id':
                                result.append(str(m.get('value')))
    except Exception as e:
        logger.warning(f"Could not get Bambu plate {plater_id} objects: {e}")
    return result


def _get_bambu_co_plate_indices(source_3mf: Path, plate_id: int,
                                preloaded: Optional[Dict] = None) -> Optional[List[int]]:
    """Return all build_item indices sharing the same Bambu plate as `plate_id`.

    If `preloaded` is provided (from _load_bambu_plate_metadata), avoids all ZIP opens.
    """
    if preloaded is not None:
        oid_to_idx = preloaded["oid_to_build_idx"]
        items_count = max(oid_to_idx.values()) if oid_to_idx else 0
        if plate_id < 1 or plate_id > items_count:
            return None
        # Find the object_id for the requested plate_id
        target_oid = None
        for oid, idx in oid_to_idx.items():
            if idx == plate_id:
                target_oid = oid
                break
        if not target_oid:
            return None
        bambu_pid = preloaded["object_to_plater"].get(target_oid)
        if bambu_pid is None:
            return None
        co_oids = preloaded["plater_to_objects"].get(bambu_pid, [])
        if len(co_oids) <= 1:
            return None
        co_indices = sorted(oid_to_idx[oid] for oid in co_oids if oid in oid_to_idx)
        return co_indices if len(co_indices) > 1 else None

    try:
        import xml.etree.ElementTree as ET
        with zipfile.ZipFile(source_3mf, 'r') as zf:
            if 'Metadata/model_settings.config' not in zf.namelist():
                return None
            # Build object_id → build_item_index map from build section
            model_xml = zf.read('3D/3dmodel.model')
            model_root = ET.fromstring(model_xml)
            ns_3mf = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
            build = model_root.find(f"{{{ns_3mf}}}build")
            if build is None:
                return None
            oid_to_idx: Dict[str, int] = {}
            items = build.findall(f"{{{ns_3mf}}}item")
            if plate_id < 1 or plate_id > len(items):
                return None
            for i, item in enumerate(items, start=1):
                oid = item.get("objectid")
                if oid:
                    oid_to_idx[oid] = i

            # Find the object_id for the requested plate_id
            target_item = items[plate_id - 1]
            target_oid = target_item.get("objectid")
            if not target_oid:
                return None

            # Find which Bambu plate contains this object
            bambu_pid = _get_bambu_plate_for_object(source_3mf, target_oid)
            if bambu_pid is None:
                return None

            # Get all objects on that Bambu plate
            co_oids = _get_bambu_plate_object_ids(source_3mf, bambu_pid)
            if len(co_oids) <= 1:
                return None  # Single object — no expansion needed

            # Map back to build item indices
            co_indices = sorted(
                oid_to_idx[oid] for oid in co_oids if oid in oid_to_idx
            )
            return co_indices if len(co_indices) > 1 else None
    except Exception as e:
        logger.warning(f"Could not expand Bambu co-plate indices for plate {plate_id}: {e}")
        return None


def _is_wipe_tower_conflict(result: Dict[str, object]) -> bool:
    """Detect Orca wipe-tower conflict failures from CLI output."""
    stdout = str(result.get("stdout") or "").lower()
    stderr = str(result.get("stderr") or "").lower()
    combined = f"{stdout}\n{stderr}"
    return (
        "gcode path conflicts found between wipetower" in combined
        or "found slicing result conflict" in combined
        or (
            "calc_exclude_triangles" in combined
            and "nothing to be sliced" in combined
        )
    )


async def _apply_scale_if_needed(
    input_3mf: Path,
    workspace: Path,
    scale_percent: float,
    job_logger: logging.Logger,
    suffix: str = "",
) -> Path:
    """Apply uniform scale when requested; otherwise return original file."""
    if abs(scale_percent - 100.0) < 0.001:
        return input_3mf

    safe_suffix = f"_{suffix}" if suffix else ""
    scaled_path = workspace / f"{input_3mf.stem}_scaled{safe_suffix}.3mf"
    job_logger.info(f"Applying object scale: {scale_percent:.1f}%")
    await asyncio.to_thread(apply_uniform_scale_to_3mf, input_3mf, scaled_path, scale_percent)
    return scaled_path


async def _apply_object_transforms_if_needed(
    input_3mf: Path,
    workspace: Path,
    object_transforms: Optional[List[Dict[str, object]]],
    job_logger: logging.Logger,
    suffix: str = "",
) -> Path:
    """Apply M33 object transforms to a workspace 3MF if requested."""
    if not object_transforms:
        return input_3mf

    safe_suffix = f"_{suffix}" if suffix else ""
    transformed_path = workspace / f"{input_3mf.stem}_transformed{safe_suffix}.3mf"
    job_logger.info(f"Applying {len(object_transforms)} object transform(s) to 3MF build items")
    try:
        result = await asyncio.to_thread(
            apply_object_transforms_to_3mf,
            input_3mf,
            transformed_path,
            object_transforms,
        )
    except ValueError as e:
        raise SlicingError(f"Invalid object transforms: {e}") from e
    job_logger.info(f"Applied object transforms: {result.get('applied')}")
    return transformed_path


def _enforce_transformed_bounds_or_raise(
    file_path: Path,
    printer_profile,
    job_logger: logging.Logger,
    plate_id: Optional[int] = None,
    baseline_file_path: Optional[Path] = None,
) -> None:
    """Fail fast on out-of-bounds object transform layouts (M33)."""
    validator = PlateValidator(printer_profile)
    validation = validator.validate_3mf_bounds(file_path, plate_id=plate_id)
    build_volume_warnings = validation.get("build_volume_warnings", [])
    if build_volume_warnings:
        detail = "; ".join(build_volume_warnings)
        if plate_id:
            raise SlicingError(f"Object transforms place plate {plate_id} outside build volume: {detail}")
        raise SlicingError(f"Object transforms place model outside build volume: {detail}")
    if validation.get("warnings"):
        job_logger.info(f"Post-transform layout warnings: {validation.get('warnings')}")

    # Orca can also fail when the transformed plate bounds still "fit" overall, but
    # no printable object remains fully inside the print volume (e.g. moved mostly off-bed).
    try:
        items = list_build_items_3mf(file_path, plate_id=plate_id)
    except Exception as e:
        job_logger.warning(f"Skipping transformed item-inside-volume validation: {e}")
        return

    printable_items = [it for it in items if bool(it.get("printable", True))]
    items_with_bounds = [it for it in printable_items if isinstance(it.get("world_bounds"), dict)]
    if not items_with_bounds:
        return

    # Bambu/Snapmaker paths can effectively use Metadata/model_settings.config
    # assemble_item transforms. Prefer those as the authoritative pose when present.
    assemble_transforms = _read_bambu_assemble_item_transforms(file_path)
    assemble_transforms_by_object = _read_bambu_assemble_item_transforms_by_object_id(file_path)
    assemble_object_ids_by_index = _read_bambu_assemble_item_object_ids_by_index(file_path)
    baseline_assemble_transforms = _read_bambu_assemble_item_transforms(baseline_file_path) if baseline_file_path else {}
    baseline_assemble_transforms_by_object = (
        _read_bambu_assemble_item_transforms_by_object_id(baseline_file_path) if baseline_file_path else {}
    )

    if plate_id:
        selected = next((it for it in items_with_bounds if int(it.get("build_item_index") or 0) == int(plate_id)), None)
        selected_oid = str(selected.get("object_id") or "") if selected else ""
        indexed_oid = str(assemble_object_ids_by_index.get(int(plate_id)) or "")
        has_object_keyed_assemble = bool(selected_oid and assemble_transforms_by_object.get(selected_oid))
        if selected_oid and indexed_oid and selected_oid != indexed_oid and not has_object_keyed_assemble:
            job_logger.warning(
                f"Skipping strict transformed 'fully inside' precheck for plate {plate_id}: "
                f"assemble_item[{plate_id}] object_id={indexed_oid} != build item object_id={selected_oid}"
            )
            return

    tol = 1e-6
    vol_x = float(printer_profile.build_volume_x)
    vol_y = float(printer_profile.build_volume_y)
    vol_z = float(printer_profile.build_volume_z)

    def _bounds_center_xy(wb: Dict[str, object]) -> Optional[tuple[float, float]]:
        mins = wb.get("min") if isinstance(wb, dict) else None
        maxs = wb.get("max") if isinstance(wb, dict) else None
        if not (isinstance(mins, list) and isinstance(maxs, list) and len(mins) >= 2 and len(maxs) >= 2):
            return None
        try:
            return ((float(mins[0]) + float(maxs[0])) / 2.0, (float(mins[1]) + float(maxs[1])) / 2.0)
        except Exception:
            return None

    def _bounds_from_local_and_transform(it: Dict[str, object], t3: Optional[List[float]]) -> Optional[Dict[str, List[float]]]:
        if not t3:
            return None
        local_bounds = it.get("local_bounds") or {}
        if not isinstance(local_bounds, dict):
            return None
        local_min = local_bounds.get("min")
        local_max = local_bounds.get("max")
        if not (isinstance(local_min, list) and isinstance(local_max, list) and len(local_min) >= 3 and len(local_max) >= 3):
            return None
        try:
            tbmin, tbmax = _apply_affine_to_bounds_3x4(
                [float(local_min[0]), float(local_min[1]), float(local_min[2])],
                [float(local_max[0]), float(local_max[1]), float(local_max[2])],
                t3,
            )
            return {"min": [float(tbmin[0]), float(tbmin[1]), float(tbmin[2])], "max": [float(tbmax[0]), float(tbmax[1]), float(tbmax[2])]}
        except Exception:
            return None

    # Prefer the same packed-Bambu plate-translation adapter used by /layout, anchored
    # to the baseline (pre-transform) plate translation so transformed deltas are kept.
    assemble_preview_offset_xy: Optional[tuple[float, float]] = None
    assemble_preview_offset_xy = _get_bambu_plate_translation_ui_offset(
        baseline_file_path if baseline_file_path else file_path,
        plate_id=plate_id,
        bed_x=vol_x,
        bed_y=vol_y,
    )
    if assemble_preview_offset_xy is not None and plate_id is not None:
        job_logger.info(
            f"Using Bambu packed plate translation offset for transformed precheck (plate {plate_id}): "
            f"({assemble_preview_offset_xy[0]:.3f}, {assemble_preview_offset_xy[1]:.3f})"
        )

    # Fallback: derive a fixed display-like offset from the baseline selected plate
    # so validation aligns better with the normalized Object Placement preview.
    # Only apply this normalization for explicit plate selections (slice-plate path).
    if assemble_preview_offset_xy is None and plate_id is not None and baseline_assemble_transforms and items_with_bounds:
        baseline_wbs: List[Dict[str, List[float]]] = []
        for it in items_with_bounds:
            idx = int(it.get("build_item_index") or 0)
            oid = str(it.get("object_id") or "")
            t0 = baseline_assemble_transforms_by_object.get(oid) or baseline_assemble_transforms.get(idx)
            wb0 = _bounds_from_local_and_transform(it, t0)
            if wb0:
                baseline_wbs.append(wb0)
        if baseline_wbs:
            bx_min = min(float(wb["min"][0]) for wb in baseline_wbs)
            by_min = min(float(wb["min"][1]) for wb in baseline_wbs)
            bx_max = max(float(wb["max"][0]) for wb in baseline_wbs)
            by_max = max(float(wb["max"][1]) for wb in baseline_wbs)
            bcx = (bx_min + bx_max) / 2.0
            bcy = (by_min + by_max) / 2.0
            assemble_preview_offset_xy = ((vol_x / 2.0) - bcx, (vol_y / 2.0) - bcy)

    # Fallback for embed/rebuild paths that drop Bambu assemble metadata but keep the
    # same packed build-item coordinates. Normalize using baseline core build-item bounds
    # so tiny on-bed moves don't get falsely rejected in packed coordinate space.
    if assemble_preview_offset_xy is None and plate_id is not None and baseline_file_path:
        try:
            baseline_items = list_build_items_3mf(baseline_file_path, plate_id=plate_id)
        except Exception:
            baseline_items = []
        baseline_printable = [it for it in baseline_items if bool(it.get("printable", True))]
        baseline_with_bounds = [it for it in baseline_printable if isinstance(it.get("world_bounds"), dict)]
        if baseline_with_bounds:
            bx_min = min(float(it["world_bounds"]["min"][0]) for it in baseline_with_bounds)
            by_min = min(float(it["world_bounds"]["min"][1]) for it in baseline_with_bounds)
            bx_max = max(float(it["world_bounds"]["max"][0]) for it in baseline_with_bounds)
            by_max = max(float(it["world_bounds"]["max"][1]) for it in baseline_with_bounds)
            bcx = (bx_min + bx_max) / 2.0
            bcy = (by_min + by_max) / 2.0
            assemble_preview_offset_xy = ((vol_x / 2.0) - bcx, (vol_y / 2.0) - bcy)

    # Final fallback: detect bed-center vs bed-corner origin from the transformed file's
    # world_bounds. Most slicers use bed-center origin (0,0 = center), so world_bounds
    # can have negative XY values for objects near center. Without this offset, the
    # _fully_inside check falsely rejects valid positions.
    if assemble_preview_offset_xy is None and items_with_bounds:
        off_x, off_y = _detect_origin_offset_xy(items_with_bounds, vol_x, vol_y)
        if abs(off_x) > 1e-6 or abs(off_y) > 1e-6:
            assemble_preview_offset_xy = (off_x, off_y)
    def _check_wb_in_volume(wb: Dict[str, object]) -> bool:
        mins = (wb.get("min") or [None, None, None]) if isinstance(wb, dict) else [None, None, None]
        maxs = (wb.get("max") or [None, None, None]) if isinstance(wb, dict) else [None, None, None]
        try:
            min_x, min_y, min_z = float(mins[0]), float(mins[1]), float(mins[2])
            max_x, max_y, max_z = float(maxs[0]), float(maxs[1]), float(maxs[2])
        except Exception:
            return False
        return (
            min_x >= -tol and min_y >= -tol and min_z >= -tol and
            max_x <= (vol_x + tol) and max_y <= (vol_y + tol) and max_z <= (vol_z + tol)
        )

    def _fully_inside(it: Dict[str, object]) -> bool:
        wb = it.get("world_bounds") or {}

        # First check: world_bounds directly (correct for recentered/bed-local files).
        # After embed_profiles + recentering, build-item world_bounds are authoritative.
        # Check these BEFORE assemble transforms, which may be in a different coordinate
        # space (Bambu assemble coords) and not updated by recentering.
        if _check_wb_in_volume(wb):
            return True

        # world_bounds failed — they may be in packed Bambu coordinates.
        # Try assemble transforms as an alternative coordinate source, but only
        # when the assemble transform itself is in packed space (XY >> bed_size).
        # Normal-range assemble transforms (XY within bed) often have stale Z=0
        # and would produce incorrect Z bounds that falsely reject valid positions.
        local_bounds = it.get("local_bounds") or {}
        idx = int(it.get("build_item_index") or 0)
        oid = str(it.get("object_id") or "")
        t3 = assemble_transforms_by_object.get(oid) or assemble_transforms.get(idx)
        if t3 and isinstance(local_bounds, dict):
            t3_normal_range = False
            try:
                t3x, t3y = float(t3[9]), float(t3[10])
                if abs(t3x) <= vol_x + 10 and abs(t3y) <= vol_y + 10:
                    t3_normal_range = True
            except Exception:
                pass
            if not t3_normal_range:
                wb_from_assemble = _bounds_from_local_and_transform(it, t3)
                if wb_from_assemble:
                    if _check_wb_in_volume(wb_from_assemble):
                        return True
                    wb = wb_from_assemble

        # Fallback: apply the assemble-derived display offset. This handles packed
        # Bambu coordinates where world_bounds aren't in bed-local space.
        # Only apply when bounds center is clearly in packed coordinate space
        # (far from the bed region).  After bed recentering + user transforms,
        # coordinates are bed-local and the offset must NOT mask off-bed positions.
        if assemble_preview_offset_xy:
            ox, oy = assemble_preview_offset_xy
            try:
                wb_cx = (float(wb["min"][0]) + float(wb["max"][0])) / 2.0
                wb_cy = (float(wb["min"][1]) + float(wb["max"][1])) / 2.0
                bounds_are_bed_local = (
                    -vol_x * 0.5 < wb_cx < vol_x * 1.5
                    and -vol_y * 0.5 < wb_cy < vol_y * 1.5
                )
                if not bounds_are_bed_local:
                    wb_offset = {
                        "min": [float(wb["min"][0]) + ox, float(wb["min"][1]) + oy, float(wb["min"][2])],
                        "max": [float(wb["max"][0]) + ox, float(wb["max"][1]) + oy, float(wb["max"][2])],
                    }
                    if _check_wb_in_volume(wb_offset):
                        return True
            except Exception:
                pass
        return False

    if any(_fully_inside(it) for it in items_with_bounds):
        return

    first = items_with_bounds[0]
    wb = first.get("world_bounds") or {}
    detail = ""
    if isinstance(wb, dict):
        mins = wb.get("min")
        maxs = wb.get("max")
        detail = f" first printable item bounds={mins}..{maxs}"
    if plate_id:
        raise SlicingError(
            f"Object transforms place plate {plate_id} so no printable object is fully inside the print volume.{detail}"
        )
    raise SlicingError(
        f"Object transforms place model so no printable object is fully inside the print volume.{detail}"
    )


def _read_bambu_assemble_item_transforms(file_path: Path) -> Dict[int, List[float]]:
    """Return 1-based assemble_item transform map from model_settings.config (best effort)."""
    result: Dict[int, List[float]] = {}
    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            if "Metadata/model_settings.config" not in zf.namelist():
                return result
            raw = zf.read("Metadata/model_settings.config").decode("utf-8", errors="ignore")
    except Exception:
        return result

    idx = 0
    for m in re.finditer(r"<assemble_item\b[^>]*\btransform=(['\"])(.*?)\1", raw, flags=re.IGNORECASE | re.DOTALL):
        idx += 1
        vals_raw = m.group(2).strip().split()
        if len(vals_raw) != 12:
            continue
        try:
            vals = [float(v) for v in vals_raw]
        except ValueError:
            continue
        result[idx] = vals
    return result


def _read_bambu_assemble_item_object_ids_by_index(file_path: Path) -> Dict[int, str]:
    """Return 1-based assemble_item object_id map from model_settings.config (best effort)."""
    result: Dict[int, str] = {}
    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            if "Metadata/model_settings.config" not in zf.namelist():
                return result
            raw = zf.read("Metadata/model_settings.config").decode("utf-8", errors="ignore")
    except Exception:
        return result

    idx = 0
    tag_pat = re.compile(r"<assemble_item\b(?P<tag>[^>]*)/?>", flags=re.IGNORECASE | re.DOTALL)
    oid_pat = re.compile(r"\bobject_id=(['\"])(.*?)\1", flags=re.IGNORECASE | re.DOTALL)
    for m in tag_pat.finditer(raw):
        idx += 1
        tag = m.group("tag") or ""
        moid = oid_pat.search(tag)
        if moid:
            result[idx] = str(moid.group(2))
    return result


def _read_bambu_assemble_item_transforms_by_object_id(file_path: Path) -> Dict[str, List[float]]:
    """Return assemble_item transforms keyed by object_id when uniquely present."""
    result: Dict[str, List[float]] = {}
    duplicates: set[str] = set()
    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            if "Metadata/model_settings.config" not in zf.namelist():
                return result
            raw = zf.read("Metadata/model_settings.config").decode("utf-8", errors="ignore")
    except Exception:
        return result

    tag_pat = re.compile(r"<assemble_item\b(?P<tag>[^>]*)/?>", flags=re.IGNORECASE | re.DOTALL)
    oid_pat = re.compile(r"\bobject_id=(['\"])(.*?)\1", flags=re.IGNORECASE | re.DOTALL)
    transform_pat = re.compile(r"\btransform=(['\"])(.*?)\1", flags=re.IGNORECASE | re.DOTALL)
    for m in tag_pat.finditer(raw):
        tag = m.group("tag") or ""
        moid = oid_pat.search(tag)
        mt = transform_pat.search(tag)
        if not moid or not mt:
            continue
        oid = str(moid.group(2))
        vals_raw = (mt.group(2) or "").strip().split()
        if len(vals_raw) != 12:
            continue
        try:
            vals = [float(v) for v in vals_raw]
        except ValueError:
            continue
        if oid in result:
            duplicates.add(oid)
        else:
            result[oid] = vals
    for oid in duplicates:
        result.pop(oid, None)
    return result


def _fold_packed_plate_coord(value: float, step: float, bed_size: float) -> float:
    """Fold packed Bambu plate-grid coordinates back into an approximate bed-local range."""
    v = float(value)
    s = float(step)
    b = max(1.0, float(bed_size))
    if not (s > 0):
        return v
    for _ in range(8):
        if v >= 0:
            break
        v += s
    for _ in range(8):
        if v <= b:
            break
        v -= s
    return v


def _layout_item_base_xyz(it: Dict[str, object], *, is_multi_plate: bool) -> tuple[float, float, float]:
    """Return preview base pose source for a layout item before ui-frame normalization."""
    core_t = it.get("translation") if isinstance(it.get("translation"), list) else [0, 0, 0]
    asm_t = it.get("assemble_translation") if isinstance(it.get("assemble_translation"), list) else None
    use_asm_xy = bool(is_multi_plate and asm_t and len(asm_t) >= 3)
    t = asm_t if use_asm_xy else core_t
    # Keep Z from core build-item transform; Bambu assemble Z can be packed/project-space.
    return (float(t[0] or 0), float(t[1] or 0), float((core_t[2] if len(core_t) >= 3 else 0) or 0))


def _infer_bambu_packed_grid_steps(source_3mf: Path, *, bed_x: float, bed_y: float) -> tuple[Optional[float], Optional[float]]:
    """Infer packed Bambu plate-grid spacing from plate translations (e.g. ~307.2mm)."""
    try:
        plates, multi = parse_multi_plate_3mf(source_3mf)
        if not (multi and len(plates) > 1):
            return None, None

        xs = sorted({float(p.get_translation()[0]) for p in plates})
        ys = sorted({float(p.get_translation()[1]) for p in plates})

        def _axis_step(vals: List[float], bed_dim: float) -> Optional[float]:
            diffs = []
            threshold = float(bed_dim) * 0.9
            for i in range(1, len(vals)):
                d = abs(vals[i] - vals[i - 1])
                if d > threshold:
                    diffs.append(d)
            return min(diffs) if diffs else None

        return _axis_step(xs, bed_x), _axis_step(ys, bed_y)
    except Exception:
        return None, None


def _read_multi_plate_translations_by_id(source_3mf: Path) -> Dict[int, Tuple[float, float, float]]:
    """Best-effort map of parser plate_id -> packed translation from multi-plate parser."""
    try:
        plates, multi = parse_multi_plate_3mf(source_3mf)
        if not (multi and plates):
            return {}
        result: Dict[int, Tuple[float, float, float]] = {}
        for p in plates:
            t = p.get_translation()
            if isinstance(t, (list, tuple)) and len(t) >= 3:
                result[int(p.plate_id)] = (float(t[0]), float(t[1]), float(t[2]))
        return result
    except Exception:
        return {}


def _get_bambu_plate_translation_ui_offset(
    reference_file_path: Optional[Path],
    *,
    plate_id: Optional[int],
    bed_x: float,
    bed_y: float,
) -> Optional[Tuple[float, float]]:
    """Return packed->bed translation-only offset using a baseline plate translation."""
    if reference_file_path is None or plate_id is None:
        return None
    plate_translations = _read_multi_plate_translations_by_id(reference_file_path)
    pt = plate_translations.get(int(plate_id))
    if not pt:
        return None
    ptx, pty, _ = pt
    return ((float(bed_x) / 2.0) - float(ptx), (float(bed_y) / 2.0) - float(pty))


def _detect_origin_offset_xy(
    items: List[Dict[str, object]],
    bed_x: float,
    bed_y: float,
) -> tuple[float, float]:
    """Detect whether 3MF coordinates use bed-center or bed-corner origin.

    Most slicers (OrcaSlicer, BambuStudio, PrusaSlicer) use bed-center origin
    where (0,0) is the center of the build plate.  Some tools (Cura, certain
    modelers, MakerWorld exports) use bed-corner origin where (0,0) is the
    lower-left corner.

    Returns (offset_x, offset_y) to add to 3MF world coords to convert to
    the viewer's bed-local coordinate system (0,0)→(bed_x, bed_y).
    """
    bed_cx = float(bed_x) / 2.0
    bed_cy = float(bed_y) / 2.0

    all_min_x = all_min_y = float("inf")
    all_max_x = all_max_y = float("-inf")
    for it in items:
        wb = it.get("world_bounds")
        if not wb or not wb.get("min") or not wb.get("max"):
            continue
        all_min_x = min(all_min_x, float(wb["min"][0]))
        all_max_x = max(all_max_x, float(wb["max"][0]))
        all_min_y = min(all_min_y, float(wb["min"][1]))
        all_max_y = max(all_max_y, float(wb["max"][1]))

    if all_min_x == float("inf"):
        return bed_cx, bed_cy  # no bounds → default bed-center

    margin = 2.0  # mm tolerance for floating-point rounding and slight overhangs

    # Test bed-center: shifted bounds should fit within (0, bed)
    shifted_fits = (
        (all_min_x + bed_cx) >= -margin
        and (all_max_x + bed_cx) <= bed_x + margin
        and (all_min_y + bed_cy) >= -margin
        and (all_max_y + bed_cy) <= bed_y + margin
    )
    # Test bed-corner: raw bounds already within (0, bed)
    raw_fits = (
        all_min_x >= -margin
        and all_max_x <= bed_x + margin
        and all_min_y >= -margin
        and all_max_y <= bed_y + margin
    )

    if raw_fits and shifted_fits:
        # Both interpretations place objects on the bed. Prefer bed-corner (no
        # offset) to avoid weakening off-bed transform rejection: applying the
        # bed-center offset as a second-chance pass in _enforce_transformed_bounds
        # can falsely validate objects that are actually off-bed.
        return 0.0, 0.0
    if shifted_fits:
        # Only the bed-center interpretation fits → file uses bed-center origin.
        return bed_cx, bed_cy
    if raw_fits:
        # Only the bed-corner interpretation fits → file uses bed-corner origin.
        return 0.0, 0.0

    # Neither fits well → default to bed-center
    return bed_cx, bed_cy


def _compute_bed_recenter_offset(
    source_3mf: Optional[Path],
    bed_x: float,
    bed_y: float,
) -> tuple:
    """Compute the offset to apply to source build-item coordinates to display
    them in the target (Snapmaker) bed-local coordinate system.

    Reads the source file's printable_area to determine its bed center,
    then computes the delta to the target bed center.
    """
    target_cx = bed_x / 2.0
    target_cy = bed_y / 2.0
    if source_3mf is None:
        return 0.0, 0.0
    try:
        with zipfile.ZipFile(source_3mf, 'r') as zf:
            if 'Metadata/project_settings.config' not in zf.namelist():
                return 0.0, 0.0
            config = json.loads(zf.read('Metadata/project_settings.config'))
            pa = config.get('printable_area')
            if not isinstance(pa, list) or len(pa) < 3:
                return 0.0, 0.0
            xs, ys = [], []
            for pt in pa:
                parts = str(pt).split('x')
                if len(parts) == 2:
                    xs.append(float(parts[0]))
                    ys.append(float(parts[1]))
            if not xs:
                return 0.0, 0.0
            src_cx = (min(xs) + max(xs)) / 2.0
            src_cy = (min(ys) + max(ys)) / 2.0
            dx = target_cx - src_cx
            dy = target_cy - src_cy
            if abs(dx) < 0.5 and abs(dy) < 0.5:
                return 0.0, 0.0
            return dx, dy
    except Exception:
        return 0.0, 0.0


def _apply_layout_direct_mapping(
    frame: Dict[str, object],
    items: List[Dict[str, object]],
    *,
    is_multi_plate: bool,
    bed_x: float = 270.0,
    bed_y: float = 270.0,
    source_3mf: Optional[Path] = None,
) -> Dict[str, object]:
    """Exact/direct bed-local mapping (single-plate and any future exact multi-plate adapters).

    Computes the offset from the source file's printable_area center to the target
    bed center, so objects appear at the correct position in the viewer's
    (0,0)→(bed_x,bed_y) coordinate system.  Falls back to bounds-based detection
    when the source printable_area is not available.
    """
    offset_x, offset_y = _compute_bed_recenter_offset(source_3mf, bed_x, bed_y)
    if abs(offset_x) < 0.01 and abs(offset_y) < 0.01:
        # No printable_area info or same bed — fall back to bounds detection
        offset_x, offset_y = _detect_origin_offset_xy(items, bed_x, bed_y)
    origin_mode = "bed_center" if abs(offset_x) > 1 else "bed_corner"
    frame.update({
        "confidence": "exact",
        "mapping": "direct",
        "offset_xy": [0.0, 0.0],
        "origin_detected": origin_mode,
        "capabilities": {
            "object_transform_edit": True,  # exact mapping allows editing
            "prime_tower_edit": True,
        },
    })
    for it in items:
        x, y, z = _layout_item_base_xyz(it, is_multi_plate=is_multi_plate)
        it["ui_base_pose"] = {"x": x + offset_x, "y": y + offset_y, "z": z, "rotate_z_deg": 0.0}
    return frame


def _apply_layout_bambu_plate_translation_offset_mapping(
    frame: Dict[str, object],
    items: List[Dict[str, object]],
    *,
    plate_translations: Dict[int, Tuple[float, float, float]],
    bed_x: float,
    bed_y: float,
    allow_object_edit: bool,
) -> Dict[str, object]:
    """Map Bambu packed coordinates into plate-local bed coords using per-plate packed translation.

    This is a more deterministic adapter than grid-fold heuristics:
    ui_xy = effective_xy - packed_plate_xy + bed_center_xy
    """
    if not plate_translations:
        return frame

    bed_cx = float(bed_x) / 2.0
    bed_cy = float(bed_y) / 2.0
    for it in items:
        idx = int(it.get("build_item_index") or 0)
        if idx <= 0 or idx not in plate_translations:
            return frame

    frame.update({
        "confidence": "exact" if allow_object_edit else "approximate",
        "mapping": "bambu_plate_translation_offset",
        "offset_xy": [0.0, 0.0],
        "capabilities": {
            "object_transform_edit": bool(allow_object_edit),
            "prime_tower_edit": True,
        },
    })
    if allow_object_edit:
        frame["notes"] = [
            "Packed multi-plate Bambu layout mapped via plate translation offset (exact for selected-plate object editing path)."
        ]
    else:
        frame["notes"] = [
            "Packed multi-plate Bambu layout normalized via plate translation offset; object move/rotate remains disabled until exact selected-plate mapping is available."
        ]
    if len(items) == 1:
        idx = int(items[0].get("build_item_index") or 0)
        pt = plate_translations.get(idx)
        if pt:
            frame["plate_translation_mm"] = [round(pt[0], 6), round(pt[1], 6), round(pt[2], 6)]

    # For co-plate groups (multiple items sharing a Bambu plate), use the
    # group center as the shared offset so relative positions are preserved.
    # For single items, each item's own translation is fine (maps to center).
    if len(items) > 1 and allow_object_edit:
        group_pts = [plate_translations[int(it.get("build_item_index") or 0)] for it in items]
        shared_ptx = sum(pt[0] for pt in group_pts) / len(group_pts)
        shared_pty = sum(pt[1] for pt in group_pts) / len(group_pts)
    else:
        shared_ptx = shared_pty = None

    for it in items:
        idx = int(it.get("build_item_index") or 0)
        ptx, pty, _ = plate_translations[idx]
        # Use build-item translation (core_t), NOT assemble_translation.
        # plate_translations are derived from build items; assemble_translation
        # lives in Bambu's internal assembly space with wildly different offsets.
        core_t = it.get("translation") if isinstance(it.get("translation"), list) else [0, 0, 0]
        x = float(core_t[0] or 0)
        y = float(core_t[1] or 0)
        z = float(core_t[2] if len(core_t) >= 3 else 0)
        # For co-plate groups, use the shared group center offset
        ref_ptx = shared_ptx if shared_ptx is not None else float(ptx)
        ref_pty = shared_pty if shared_pty is not None else float(pty)
        ux = (x - ref_ptx) + bed_cx
        uy = (y - ref_pty) + bed_cy
        it["ui_base_pose"] = {"x": ux, "y": uy, "z": z, "rotate_z_deg": 0.0}
    return frame


def _apply_layout_bambu_packed_grid_fold_mapping(
    frame: Dict[str, object],
    items: List[Dict[str, object]],
    *,
    packed_grid_step_x: float,
    packed_grid_step_y: float,
    bed_x: float,
    bed_y: float,
    note: str,
) -> Dict[str, object]:
    """Approximate Bambu packed-grid -> bed-local normalization by per-axis folding."""
    frame.update({
        "confidence": "approximate",
        "mapping": "bambu_packed_grid_fold",
        "packed_grid_step_x_mm": round(float(packed_grid_step_x), 6),
        "packed_grid_step_y_mm": round(float(packed_grid_step_y), 6),
        "capabilities": {
            "object_transform_edit": False,
            "prime_tower_edit": True,
        },
        "notes": [note],
    })
    for it in items:
        x, y, z = _layout_item_base_xyz(it, is_multi_plate=True)
        ux = _fold_packed_plate_coord(x, packed_grid_step_x, bed_x)
        uy = _fold_packed_plate_coord(y, packed_grid_step_y, bed_y)
        it["ui_base_pose"] = {"x": ux, "y": uy, "z": z, "rotate_z_deg": 0.0}
    return frame


def _apply_layout_centered_preview_offset_mapping(
    frame: Dict[str, object],
    items: List[Dict[str, object]],
    *,
    is_multi_plate: bool,
    bed_x: float,
    bed_y: float,
    validation_bounds: Optional[Dict[str, object]],
) -> Dict[str, object]:
    """Legacy explicit fallback: center current scene in preview (approximate for multi-plate)."""
    center_x: Optional[float] = None
    center_y: Optional[float] = None
    assemble_bounds = [
        b for b in [it.get("assemble_world_bounds") for it in items]
        if isinstance(b, dict) and isinstance(b.get("min"), list) and isinstance(b.get("max"), list)
    ]
    if assemble_bounds:
        min_x = min(float(b["min"][0]) for b in assemble_bounds)
        min_y = min(float(b["min"][1]) for b in assemble_bounds)
        max_x = max(float(b["max"][0]) for b in assemble_bounds)
        max_y = max(float(b["max"][1]) for b in assemble_bounds)
        center_x = (min_x + max_x) / 2.0
        center_y = (min_y + max_y) / 2.0
    elif isinstance(validation_bounds, dict):
        mins = validation_bounds.get("min")
        maxs = validation_bounds.get("max")
        if isinstance(mins, list) and isinstance(maxs, list) and len(mins) >= 2 and len(maxs) >= 2:
            center_x = (float(mins[0]) + float(maxs[0])) / 2.0
            center_y = (float(mins[1]) + float(maxs[1])) / 2.0

    if center_x is None or center_y is None:
        pts = [_layout_item_base_xyz(it, is_multi_plate=is_multi_plate) for it in items]
        center_x = sum(p[0] for p in pts) / max(1, len(pts))
        center_y = sum(p[1] for p in pts) / max(1, len(pts))

    off_x = (float(bed_x) / 2.0) - float(center_x)
    off_y = (float(bed_y) / 2.0) - float(center_y)
    frame.update({
        "confidence": "approximate" if is_multi_plate else "exact",
        "mapping": "centered_preview_offset",
        "offset_xy": [off_x, off_y],
        "capabilities": {
            "object_transform_edit": (not is_multi_plate),
            "prime_tower_edit": True,
        },
    })
    if is_multi_plate:
        frame["notes"] = [
            "Multi-plate preview uses centered normalization; object move/rotate disabled until exact plate-local mapping is available."
        ]

    for it in items:
        x, y, z = _layout_item_base_xyz(it, is_multi_plate=is_multi_plate)
        it["ui_base_pose"] = {"x": x + off_x, "y": y + off_y, "z": z, "rotate_z_deg": 0.0}
    return frame


def _derive_layout_placement_frame(
    items: List[Dict[str, object]],
    *,
    source_3mf: Path,
    is_multi_plate: bool,
    plate_id: Optional[int],
    bed_x: float,
    bed_y: float,
    validation_bounds: Optional[Dict[str, object]],
    bambu_co_plate_expanded: bool = False,
) -> Dict[str, object]:
    """Return canonical (bed-local) placement frame metadata + per-item ui pose hints.

    This centralizes the preview coordinate mapping logic so the frontend can consume a
    single explicit contract instead of duplicating Bambu/packed/recentered heuristics.
    """
    frame: Dict[str, object] = {
        "version": 2,
        "canonical": "bed_local_xy_mm",
        "viewer_frame": "bed_local_xy_mm",
        "confidence": "exact" if not is_multi_plate else "approximate",
        "mapping": "direct",
        "offset_xy": [0.0, 0.0],
        "capabilities": {
            "object_transform_edit": (not is_multi_plate),
            "prime_tower_edit": True,
        },
    }

    if not items:
        return frame

    # Exact/direct path for non-multi-plate files.
    # Compute recenter offset from the source file's printable_area to the target bed.
    if not is_multi_plate:
        return _apply_layout_direct_mapping(
            frame, items, is_multi_plate=False, bed_x=bed_x, bed_y=bed_y, source_3mf=source_3mf,
        )

    packed_grid_step_x, packed_grid_step_y = _infer_bambu_packed_grid_steps(
        source_3mf,
        bed_x=bed_x,
        bed_y=bed_y,
    )
    plate_translations = _read_multi_plate_translations_by_id(source_3mf)
    if plate_translations:
        can_exact_selected_plate = bool(
            plate_id is not None and (len(items) == 1 or bambu_co_plate_expanded)
        )
        mapped = _apply_layout_bambu_plate_translation_offset_mapping(
            frame,
            items,
            plate_translations=plate_translations,
            bed_x=bed_x,
            bed_y=bed_y,
            allow_object_edit=can_exact_selected_plate,
        )
        if mapped.get("mapping") == "bambu_plate_translation_offset":
            return mapped

    if (
        packed_grid_step_x and packed_grid_step_x > 0
        and packed_grid_step_y and packed_grid_step_y > 0
    ):
        return _apply_layout_bambu_packed_grid_fold_mapping(
            frame,
            items,
            packed_grid_step_x=float(packed_grid_step_x),
            packed_grid_step_y=float(packed_grid_step_y),
            bed_x=bed_x,
            bed_y=bed_y,
            note="Packed multi-plate Bambu layout normalized with inferred grid folding; object move/rotate disabled until exact mapping is available.",
        )

    return _apply_layout_centered_preview_offset_mapping(
        frame,
        items,
        is_multi_plate=is_multi_plate,
        bed_x=bed_x,
        bed_y=bed_y,
        validation_bounds=validation_bounds,
    )


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


def _clamp_int32(value: Optional[int]) -> Optional[int]:
    """Clamp integer values to PostgreSQL INTEGER range used by schema."""
    if value is None:
        return None
    return max(0, min(int(value), INT32_MAX))


class SliceRequest(BaseModel):
    job_id: Optional[str] = None  # Client-provided job ID for progress polling
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
    wipe_tower_x: Optional[float] = Field(None, ge=0, le=270)
    wipe_tower_y: Optional[float] = Field(None, ge=0, le=270)
    nozzle_temp: Optional[int] = Field(None, ge=150, le=350)
    bed_temp: Optional[int] = Field(None, ge=0, le=150)
    bed_type: Optional[str] = None
    scale_percent: Optional[float] = Field(100.0, ge=10.0, le=500.0)
    enable_flow_calibrate: Optional[bool] = True
    extruder_assignments: Optional[List[int]] = None  # Per-color target extruder slots (0-based)
    object_transforms: Optional[List[Dict[str, object]]] = None  # M33 foundation: per-build-item deltas


class SlicePlateRequest(BaseModel):
    job_id: Optional[str] = None  # Client-provided job ID for progress polling
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
    wipe_tower_x: Optional[float] = Field(None, ge=0, le=270)
    wipe_tower_y: Optional[float] = Field(None, ge=0, le=270)
    nozzle_temp: Optional[int] = Field(None, ge=150, le=350)
    bed_temp: Optional[int] = Field(None, ge=0, le=150)
    bed_type: Optional[str] = None
    scale_percent: Optional[float] = Field(100.0, ge=10.0, le=500.0)
    enable_flow_calibrate: Optional[bool] = True
    extruder_assignments: Optional[List[int]] = None  # Per-color target extruder slots (0-based)
    object_transforms: Optional[List[Dict[str, object]]] = None  # M33 foundation: per-build-item deltas


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
    job_id = request.job_id or f"slice_{uuid.uuid4().hex[:12]}"
    job_logger = setup_job_logging(job_id)
    _update_progress(job_id, 1, "Validating request")

    job_logger.info(f"Starting slicing job for upload {upload_id}")
    job_logger.info(
        f"Request: filament_id={request.filament_id}, layer_height={request.layer_height}, "
        f"infill_density={request.infill_density}, wall_count={request.wall_count}, "
        f"infill_pattern={request.infill_pattern}, supports={request.supports}, "
        f"scale_percent={request.scale_percent}, "
        f"enable_prime_tower={request.enable_prime_tower}, "
        f"prime_volume={request.prime_volume}, "
        f"prime_tower_width={request.prime_tower_width}, "
        f"prime_tower_brim_width={request.prime_tower_brim_width}, "
        f"prime_tower_brim_chamfer={request.prime_tower_brim_chamfer}, "
        f"prime_tower_brim_chamfer_max_width={request.prime_tower_brim_chamfer_max_width}, "
        f"wipe_tower_x={request.wipe_tower_x}, wipe_tower_y={request.wipe_tower_y}"
    )

    async with pool.acquire() as conn:
        # Validate upload exists
        upload = await conn.fetchrow(
            """
            SELECT id, filename, file_path, bounds_warning, detected_colors,
                   copies_path, copies_count, copies_spacing
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

        # Always use the ORIGINAL file for profile embedding and metadata.
        # Copies are re-applied AFTER embedding to avoid trimesh destroying
        # the multi-item layout (Bambu files get trimesh-processed during embedding).
        source_3mf = Path(upload["file_path"])
        copies_count = upload["copies_count"] or 1
        copies_spacing = upload["copies_spacing"] or 5.0
        if copies_count > 1:
            job_logger.info(f"Will apply {copies_count} copies (spacing={copies_spacing}mm) after embedding")
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

        # Parse 3MF model once — single source of truth for all detection
        model = await asyncio.to_thread(parse_threemf, source_3mf)
        active_extruders = model.active_extruders
        if active_extruders:
            job_logger.info(f"Active assigned extruders: {active_extruders}")

        # Auto-expand single filament to match source file's required colour count.
        # Handles both multi-extruder (per-object assignment) and SEMM painted files
        # where detected_colors exceeds active_extruders.  Cap at 4 (U1 max).
        required_extruders = min(4, max(
            len(active_extruders) if active_extruders else 0,
            len(detected_colors),
        ))
        if required_extruders > 1 and len(filaments) < required_extruders:
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

        # For >4 source extruders, we must remap in the 3MF pre-slice
        has_overflow_extruders = any(s > 4 for s in extruder_remap) if extruder_remap else False

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
        _update_progress(job_id, 3, "Preparing workspace")

        # Create workspace directory
        workspace = Path(f"/cache/slicing/{job_id}")
        workspace.mkdir(parents=True, exist_ok=True)
        job_logger.info(f"Created workspace: {workspace}")

        # Embed profiles into original 3MF
        _update_progress(job_id, 5, "Embedding profiles")
        job_logger.info("Embedding Orca profiles into original 3MF...")
        embedder = ProfileEmbedder(Path("/app/orca_profiles"))
        embedded_3mf = workspace / "embedded.3mf"

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
        
        # Place filament settings into correct positional slots.
        # When extruder_assignments maps filaments to non-default positions
        # (e.g. filament_ids=[A,B] + assignments=[2,3]), scatter each
        # filament's properties into the assigned slot so temps/colors
        # align with physical extruder positions.
        if request.extruder_assignments:
            pos_nozzle = ["0"] * 4
            default_bed = bed_temps[-1] if bed_temps else "60"
            pos_bed = [default_bed] * 4
            pos_colors = ["#FFFFFF"] * 4
            default_mat = material_types[-1] if material_types else "PLA"
            pos_materials = [default_mat] * 4
            default_prof = profile_names[-1] if profile_names else "Snapmaker PLA"
            pos_profiles = [default_prof] * 4

            for i, pos in enumerate(request.extruder_assignments):
                if pos < 4:
                    if i < len(nozzle_temps):
                        pos_nozzle[pos] = nozzle_temps[i]
                    if i < len(bed_temps):
                        pos_bed[pos] = bed_temps[i]
                    if i < len(extruder_colors):
                        pos_colors[pos] = extruder_colors[i]
                    if i < len(material_types):
                        pos_materials[pos] = material_types[i]
                    if i < len(profile_names):
                        pos_profiles[pos] = profile_names[i]

            nozzle_temps = pos_nozzle
            bed_temps = pos_bed
            extruder_colors = pos_colors
            material_types = pos_materials
            profile_names = pos_profiles
            job_logger.info(f"Positioned filament settings to extruder slots: {sorted(set(request.extruder_assignments))}, nozzle_temps={nozzle_temps}")
        else:
            # No assignments — pad sequentially (unused nozzles get 0°C)
            while len(nozzle_temps) < 4:
                nozzle_temps.append("0")
            while len(bed_temps) < 4:
                bed_temps.append(bed_temps[-1] if bed_temps else "60")
            while len(extruder_colors) < 4:
                extruder_colors.append("#FFFFFF")
            while len(material_types) < 4:
                material_types.append(material_types[-1] if material_types else "PLA")
            while len(profile_names) < 4:
                profile_names.append(profile_names[-1] if profile_names else "Snapmaker PLA")

        # Override colors if user specified custom colors per extruder.
        # Applied AFTER scatter so request.filament_colors (a positional 4-slot
        # array from the UI) patches the full positional extruder_colors array.
        if request.filament_colors:
            for idx, color in enumerate(request.filament_colors):
                if idx < len(extruder_colors):
                    extruder_colors[idx] = color

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
        # Auto-enable prime tower for multi-color copies.
        # Paint data files get SEMM + prime tower via build_slicer_config.
        need_prime_tower = request.enable_prime_tower
        if copies_count > 1 and extruder_count > 1 and not need_prime_tower:
            need_prime_tower = True
            job_logger.info("Auto-enabling prime tower for multi-color copies")

        if need_prime_tower:
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
            if request.wipe_tower_x is not None:
                overrides["wipe_tower_x"] = f"{float(request.wipe_tower_x):.3f}"
            if request.wipe_tower_y is not None:
                overrides["wipe_tower_y"] = f"{float(request.wipe_tower_y):.3f}"
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
                extruder_remap=extruder_remap or None,
                preserve_geometry=True,
                precomputed_is_bambu=model.is_bambu,
                precomputed_has_multi_assignments=model.has_multi_extruder_assignments,
                precomputed_has_layer_changes=model.has_layer_tool_changes,
                enable_flow_calibrate=request.enable_flow_calibrate if request.enable_flow_calibrate is not None else True,
                model=model,
            )
            three_mf_size_mb = embedded_3mf.stat().st_size / 1024 / 1024
            job_logger.info(f"Profile-embedded 3MF created: {embedded_3mf.name} ({three_mf_size_mb:.2f} MB)")
        except ProfileEmbedError as e:
            job_logger.error(f"Failed to embed profiles: {str(e)}")
            raise SlicingError(f"Profile embedding failed: {str(e)}")

        embedded_3mf = await _apply_object_transforms_if_needed(
            embedded_3mf,
            workspace,
            request.object_transforms,
            job_logger,
        )
        printer_profile = get_printer_profile("snapmaker_u1")
        if request.object_transforms:
            _enforce_transformed_bounds_or_raise(
                embedded_3mf,
                printer_profile,
                job_logger,
                baseline_file_path=(workspace / "embedded.3mf"),
            )

        # Slice with Orca (async to avoid blocking other API requests)
        _update_progress(job_id, 15, "Starting slicer")
        job_logger.info("Invoking Orca Slicer...")
        slicer = OrcaSlicer(printer_profile)
        scale_percent = float(request.scale_percent if request.scale_percent is not None else 100.0)
        scale_factor = scale_percent / 100.0
        scale_active = abs(scale_percent - 100.0) > 0.001
        layout_scale_active = scale_percent > 100.001

        # Progress callback maps slicer's 0-100% to our 20-85% range
        def _slicer_progress(pct, msg):
            if pct >= 100:
                return  # Skip slicer's "All done" — we have post-processing phases
            mapped = 20 + int(pct * 0.65)
            _update_progress(job_id, mapped, msg)

        source_for_slice = embedded_3mf
        if layout_scale_active:
            # Native --scale can miss component/matrix offsets in some files.
            # Pre-scale only layout offsets so spacing scales with the same factor.
            source_for_slice = workspace / "embedded_layout_scaled.3mf"
            await asyncio.to_thread(
                apply_layout_scale_to_3mf,
                embedded_3mf,
                source_for_slice,
                scale_percent,
            )
            job_logger.info("Applied pre-scale to assembly offsets for native --scale")

        # Apply copies if needed.
        if copies_count > 1:
            from copy_duplicator import apply_copies_to_3mf
            sliceable_3mf = workspace / "sliceable.3mf"
            copy_result = await asyncio.to_thread(
                apply_copies_to_3mf,
                source_for_slice,
                sliceable_3mf,
                copies_count,
                copies_spacing,
                scale_factor,
            )
            job_logger.info(f"Applied {copies_count} copies: {copy_result['cols']}x{copy_result['rows']} grid")
            if not copy_result.get("fits_bed", True):
                raise SlicingError(
                    f"{copies_count} copies at {scale_percent:.0f}% scale do not fit build plate. "
                    "Reduce copies or scale."
                )
        else:
            sliceable_3mf = source_for_slice

        result = await slicer.slice_3mf_async(
            sliceable_3mf,
            workspace,
            scale_factor=scale_factor,
            disable_arrange=bool(request.object_transforms),
            progress_callback=_slicer_progress,
            job_id=job_id,
        )

        if not result["success"] and scale_active:
            job_logger.warning("Native --scale failed; retrying with transform-based scaling")
            # Re-scale from the original embedded file to avoid double-applying
            # layout offsets when source_for_slice already has pre-scaled spacing.
            scaled_3mf = await _apply_scale_if_needed(embedded_3mf, workspace, scale_percent, job_logger)
            if copies_count > 1:
                from copy_duplicator import apply_copies_to_3mf
                fallback_sliceable_3mf = workspace / "sliceable_scaled_fallback.3mf"
                copy_result = await asyncio.to_thread(
                    apply_copies_to_3mf,
                    scaled_3mf,
                    fallback_sliceable_3mf,
                    copies_count,
                    copies_spacing,
                    1.0,
                )
                if not copy_result.get("fits_bed", True):
                    raise SlicingError(
                        f"{copies_count} copies at {scale_percent:.0f}% scale do not fit build plate. "
                        "Reduce copies or scale."
                    )
            else:
                fallback_sliceable_3mf = scaled_3mf
            result = await slicer.slice_3mf_async(
                fallback_sliceable_3mf,
                workspace,
                scale_factor=1.0,
                disable_arrange=bool(request.object_transforms),
                progress_callback=_slicer_progress,
                job_id=job_id,
            )

        if (
            not result["success"]
            and scale_active
            and scale_percent < 100.0
        ):
            job_logger.warning(
                "Scaled downslice failed; retrying once at 100% scale"
            )
            result = await slicer.slice_3mf_async(
                sliceable_3mf,
                workspace,
                scale_factor=1.0,
                disable_arrange=bool(request.object_transforms),
                progress_callback=_slicer_progress,
                job_id=job_id,
            )

        if not result["success"] and need_prime_tower and _is_wipe_tower_conflict(result):
            job_logger.warning(
                "Detected wipe-tower path conflict; retrying once with prime tower disabled"
            )
            retry_overrides = dict(overrides)
            retry_overrides["enable_prime_tower"] = "0"

            embedded_retry = workspace / "embedded_no_prime.3mf"
            await embedder.embed_profiles_async(
                source_3mf=source_3mf,
                output_3mf=embedded_retry,
                filament_settings=filament_settings,
                overrides=retry_overrides,
                requested_filament_count=extruder_count,
                extruder_remap=extruder_remap or None,
                preserve_geometry=True,
                precomputed_is_bambu=model.is_bambu,
                precomputed_has_multi_assignments=model.has_multi_extruder_assignments,
                precomputed_has_layer_changes=model.has_layer_tool_changes,
                enable_flow_calibrate=request.enable_flow_calibrate if request.enable_flow_calibrate is not None else True,
                model=model,
            )
            embedded_retry = await _apply_object_transforms_if_needed(
                embedded_retry,
                workspace,
                request.object_transforms,
                job_logger,
                suffix="no_prime",
            )
            if request.object_transforms:
                _enforce_transformed_bounds_or_raise(
                    embedded_retry,
                    printer_profile,
                    job_logger,
                    baseline_file_path=(workspace / "embedded_no_prime.3mf"),
                )

            retry_source = embedded_retry
            if layout_scale_active:
                retry_source = workspace / "embedded_no_prime_layout_scaled.3mf"
                await asyncio.to_thread(
                    apply_layout_scale_to_3mf,
                    embedded_retry,
                    retry_source,
                    scale_percent,
                )

            if copies_count > 1:
                from copy_duplicator import apply_copies_to_3mf
                retry_sliceable_3mf = workspace / "sliceable_no_prime.3mf"
                copy_result = await asyncio.to_thread(
                    apply_copies_to_3mf,
                    retry_source,
                    retry_sliceable_3mf,
                    copies_count,
                    copies_spacing,
                    scale_factor,
                )
                if not copy_result.get("fits_bed", True):
                    raise SlicingError(
                        f"{copies_count} copies at {scale_percent:.0f}% scale do not fit build plate. "
                        "Reduce copies or scale."
                    )
            else:
                retry_sliceable_3mf = retry_source

            result = await slicer.slice_3mf_async(
                retry_sliceable_3mf,
                workspace,
                scale_factor=scale_factor,
                disable_arrange=bool(request.object_transforms),
                progress_callback=_slicer_progress,
                job_id=job_id,
            )

        if not result["success"]:
            job_logger.error(f"Orca Slicer failed with exit code {result['exit_code']}")
            job_logger.error(f"stdout: {result['stdout']}")
            job_logger.error(f"stderr: {result['stderr']}")
            raise SlicingError(f"Orca Slicer failed: {result['stderr'][:200]}")

        _update_progress(job_id, 85, "Slicer finished")
        job_logger.info("Slicing completed successfully")
        job_logger.info(f"Orca stdout: {result['stdout'][:500]}")

        # Find generated G-code file (Orca produces plate_1.gcode)
        gcode_files = list(workspace.glob("plate_*.gcode"))
        if not gcode_files:
            job_logger.error("No G-code files generated")
            raise SlicingError("G-code file not generated by Orca")

        gcode_workspace_path = gcode_files[0]
        job_logger.info(f"Found G-code file: {gcode_workspace_path.name}")

        if len(filaments) > 1 and extruder_remap and has_overflow_extruders:
            # Pre-slice remap already collapsed >4 to 1-4 in the 3MF.
            # Post-slice remap only needs to fix compaction within 1-4.
            # Non-overflow remaps (e.g. E1,E2→E3,E4) are handled pre-slice
            # so OrcaSlicer generates correct tool numbers and is_extruder_used[].
            effective_extruders = sorted(set(extruder_remap.values()))
            target_tools = [ext - 1 for ext in effective_extruders]
            remap_result = slicer.remap_compacted_tools(gcode_workspace_path, target_tools)
            if remap_result.get("applied"):
                job_logger.info(f"Remapped compacted tools: {remap_result.get('map')}")
            else:
                job_logger.info(f"Tool remap skipped: {remap_result}")

        # Inject thumbnails from 3MF preview into G-code for printer display
        thumb_result = await asyncio.to_thread(
            inject_gcode_thumbnails, gcode_workspace_path, source_3mf
        )
        if thumb_result.get("injected"):
            job_logger.info(f"Injected thumbnails: {thumb_result['sizes']}")
        else:
            job_logger.info(f"Thumbnail injection skipped: {thumb_result.get('reason')}")

        # Parse G-code metadata (async to avoid blocking event loop)
        _update_progress(job_id, 88, "Parsing G-code metadata")
        job_logger.info("Parsing G-code metadata...")
        metadata = await asyncio.to_thread(slicer.parse_gcode_metadata, gcode_workspace_path)
        metadata["estimated_time_seconds"] = _clamp_int32(metadata.get("estimated_time_seconds")) or 0
        metadata["layer_count"] = _clamp_int32(metadata.get("layer_count"))
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
        _update_progress(job_id, 92, "Validating bounds")
        job_logger.info("Validating bounds against printer build volume...")
        try:
            slicer.validate_bounds(gcode_workspace_path)
            job_logger.info("Bounds validation passed")
        except Exception as e:
            job_logger.error(f"Bounds validation failed: {str(e)}")
            raise

        # Move G-code to final location
        _update_progress(job_id, 95, "Saving G-code")
        slices_dir = Path("/data/slices")
        slices_dir.mkdir(parents=True, exist_ok=True)
        final_gcode_path = slices_dir / f"{job_id}.gcode"

        shutil.copy(gcode_workspace_path, final_gcode_path)
        gcode_size = final_gcode_path.stat().st_size
        gcode_size_mb = gcode_size / 1024 / 1024
        job_logger.info(f"G-code saved: {final_gcode_path} ({gcode_size_mb:.2f} MB)")

        # Store full positional color array so viewer maps T0→color[0], etc.
        # After scatter, extruder_colors is already a 4-slot positional array
        # (e.g. assignments [2,3] → colors at indices 2,3, #FFFFFF elsewhere).
        # Previously we extracted only active positions, but that lost positional
        # info — viewer got 2 colors and labelled them E1/E2 instead of E3/E4.
        filament_colors_json = json.dumps(extruder_colors)
        filament_used_g_json = json.dumps(metadata.get('filament_used_g', []))
        async with pool.acquire() as conn:
            # Only mark completed if the job hasn't been cancelled in the meantime.
            # The cancel endpoint may have force-marked it as 'failed' while the
            # slicer was still running (race between cancel and completion).
            result_tag = await conn.execute(
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
                    filament_colors = $9,
                    filament_used_g = $10,
                    gcode_bounds_min_x = $11,
                    gcode_bounds_min_y = $12,
                    gcode_bounds_min_z = $13,
                    gcode_bounds_max_x = $14,
                    gcode_bounds_max_y = $15,
                    gcode_bounds_max_z = $16
                WHERE job_id = $1 AND status = 'processing'
                """,
                job_id,
                datetime.utcnow(),
                str(final_gcode_path),
                gcode_size,
                metadata['estimated_time_seconds'],
                metadata['filament_used_mm'],
                metadata.get('layer_count'),
                str(embedded_3mf),
                filament_colors_json,
                filament_used_g_json,
                metadata.get('min_x', 0.0),
                metadata.get('min_y', 0.0),
                metadata.get('min_z', 0.0),
                metadata.get('max_x', 0.0),
                metadata.get('max_y', 0.0),
                metadata.get('max_z', 0.0),
            )
            if result_tag == "UPDATE 0":
                job_logger.info(f"Job {job_id} was cancelled before completion could be recorded")
                raise SlicingCancelledError("Slicing cancelled by user")

        _update_progress(job_id, 100, "Complete")
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
        _clear_progress(job_id)
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

    except SlicingCancelledError:
        job_logger.info(f"Slicing cancelled by user: {job_id}")
        _clear_progress(job_id)
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE slicing_jobs SET status = 'failed', completed_at = $2, error_message = 'Cancelled'
                WHERE job_id = $1
                """,
                job_id, datetime.utcnow(),
            )
        raise HTTPException(status_code=499, detail="Slicing cancelled")

    except SlicingError as e:
        err_text = str(e)
        if len(filaments) > 1 and "segmentation fault" in err_text.lower():
            err_text = (
                "Multicolour slicing is unstable for this model in Snapmaker Orca v2.2.4 "
                "(slicer crash). Try single-filament slicing for now."
            )
        job_logger.error(f"Slicing failed: {err_text}")
        _clear_progress(job_id)
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
        low = err_text.lower()
        code = 500
        if (
            "unstable for this model" in low
            or "do not fit build plate" in low
            or "invalid object transforms" in low
            or "outside build volume" in low
            or "fully inside the print volume" in low
        ):
            code = 400
        raise HTTPException(status_code=code, detail=f"Slicing failed: {err_text}")

    except Exception as e:
        job_logger.error(f"Unexpected error: {str(e)}")
        _clear_progress(job_id)
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
    job_id = request.job_id or f"slice_plate_{uuid.uuid4().hex[:12]}"
    job_logger = setup_job_logging(job_id)
    _update_progress(job_id, 1, "Validating request")

    job_logger.info(f"Starting plate slicing job for upload {upload_id}, plate {request.plate_id}")
    job_logger.info(
        f"Request: filament_id={request.filament_id}, layer_height={request.layer_height}, "
        f"infill_density={request.infill_density}, wall_count={request.wall_count}, "
        f"infill_pattern={request.infill_pattern}, supports={request.supports}, "
        f"scale_percent={request.scale_percent}, "
        f"enable_prime_tower={request.enable_prime_tower}, "
        f"prime_volume={request.prime_volume}, "
        f"prime_tower_width={request.prime_tower_width}, "
        f"prime_tower_brim_width={request.prime_tower_brim_width}, "
        f"prime_tower_brim_chamfer={request.prime_tower_brim_chamfer}, "
        f"prime_tower_brim_chamfer_max_width={request.prime_tower_brim_chamfer_max_width}, "
        f"wipe_tower_x={request.wipe_tower_x}, wipe_tower_y={request.wipe_tower_y}"
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

        # Copies don't apply to plate-based slicing
        copies_count = 1

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

        # Parse 3MF model once — single source of truth for plates, detection, etc.
        model = await asyncio.to_thread(parse_threemf, source_3mf)
        if not model.is_multi_plate:
            job_logger.error(f"Upload {upload_id} is not a multi-plate file")
            raise HTTPException(status_code=400, detail="Not a multi-plate file - use /uploads/{id}/slice instead")

        # Validate requested plate exists.
        # UI sends build-item indices (from parse_multi_plate_3mf); for Bambu files
        # these differ from logical plater_ids, so map via item_to_plate first.
        target_plate = model.get_plate_for_item(request.plate_id) or model.get_plate(request.plate_id)
        if not target_plate:
            job_logger.error(f"Plate {request.plate_id} not found in file")
            raise HTTPException(status_code=404, detail=f"Plate {request.plate_id} not found")

        # Check if first item on plate is non-printable
        if target_plate.items and not target_plate.items[0].printable:
            job_logger.warning(f"Plate {request.plate_id} is marked as non-printable")

        target_object_id = target_plate.items[0].object_id if target_plate.items else "?"
        job_logger.info(f"Found plate {request.plate_id}: Object {target_object_id}")

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

        active_extruders = model.active_extruders
        if active_extruders:
            job_logger.info(f"Active assigned extruders: {active_extruders}")

        # Auto-expand single filament to match source file's required colour count.
        # Handles both multi-extruder (per-object assignment) and SEMM painted files
        # where detected_colors exceeds active_extruders.  Cap at 4 (U1 max).
        required_extruders = min(4, max(
            len(active_extruders) if active_extruders else 0,
            len(detected_colors),
        ))
        if required_extruders > 1 and len(filaments) < required_extruders:
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

        # For >4 source extruders, we must remap in the 3MF pre-slice
        has_overflow_extruders = any(s > 4 for s in extruder_remap) if extruder_remap else False

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
        _update_progress(job_id, 3, "Preparing workspace")

        # Create workspace directory
        workspace = Path(f"/cache/slicing/{job_id}")
        workspace.mkdir(parents=True, exist_ok=True)
        job_logger.info(f"Created workspace: {workspace}")

        embedded_3mf = workspace / "sliceable.3mf"

        # Embed profiles into source 3MF and slice only selected plate via CLI
        _update_progress(job_id, 5, "Embedding profiles")
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
        
        # Place filament settings into correct positional slots.
        # When extruder_assignments maps filaments to non-default positions
        # (e.g. filament_ids=[A,B] + assignments=[2,3]), scatter each
        # filament's properties into the assigned slot so temps/colors
        # align with physical extruder positions.
        if request.extruder_assignments:
            pos_nozzle = ["0"] * 4
            default_bed = bed_temps[-1] if bed_temps else "60"
            pos_bed = [default_bed] * 4
            pos_colors = ["#FFFFFF"] * 4
            default_mat = material_types[-1] if material_types else "PLA"
            pos_materials = [default_mat] * 4
            default_prof = profile_names[-1] if profile_names else "Snapmaker PLA"
            pos_profiles = [default_prof] * 4

            for i, pos in enumerate(request.extruder_assignments):
                if pos < 4:
                    if i < len(nozzle_temps):
                        pos_nozzle[pos] = nozzle_temps[i]
                    if i < len(bed_temps):
                        pos_bed[pos] = bed_temps[i]
                    if i < len(extruder_colors):
                        pos_colors[pos] = extruder_colors[i]
                    if i < len(material_types):
                        pos_materials[pos] = material_types[i]
                    if i < len(profile_names):
                        pos_profiles[pos] = profile_names[i]

            nozzle_temps = pos_nozzle
            bed_temps = pos_bed
            extruder_colors = pos_colors
            material_types = pos_materials
            profile_names = pos_profiles
            job_logger.info(f"Positioned filament settings to extruder slots: {sorted(set(request.extruder_assignments))}, nozzle_temps={nozzle_temps}")
        else:
            # No assignments — pad sequentially (unused nozzles get 0°C)
            while len(nozzle_temps) < 4:
                nozzle_temps.append("0")
            while len(bed_temps) < 4:
                bed_temps.append(bed_temps[-1] if bed_temps else "60")
            while len(extruder_colors) < 4:
                extruder_colors.append("#FFFFFF")
            while len(material_types) < 4:
                material_types.append(material_types[-1] if material_types else "PLA")
            while len(profile_names) < 4:
                profile_names.append(profile_names[-1] if profile_names else "Snapmaker PLA")

        # Override colors if user specified custom colors per extruder.
        # Applied AFTER scatter so request.filament_colors (a positional 4-slot
        # array from the UI) patches the full positional extruder_colors array.
        if request.filament_colors:
            for idx, color in enumerate(request.filament_colors):
                if idx < len(extruder_colors):
                    extruder_colors[idx] = color

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
        # Auto-enable prime tower for multi-color copies.
        # Paint data files get SEMM + prime tower via build_slicer_config.
        need_prime_tower = request.enable_prime_tower
        if copies_count > 1 and extruder_count > 1 and not need_prime_tower:
            need_prime_tower = True
            job_logger.info("Auto-enabling prime tower for multi-color copies")

        if need_prime_tower:
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
            if request.wipe_tower_x is not None:
                overrides["wipe_tower_x"] = f"{float(request.wipe_tower_x):.3f}"
            if request.wipe_tower_y is not None:
                overrides["wipe_tower_y"] = f"{float(request.wipe_tower_y):.3f}"
        else:
            overrides["enable_prime_tower"] = "0"

        if extruder_count > 1:
            overrides["machine_load_filament_time"] = "0"
            overrides["machine_unload_filament_time"] = "0"

        # The model's plate_id already maps to the Bambu plater_id (set during parse).
        # No separate lookup needed — target_plate.plate_id IS the effective plate ID.
        bambu_plate = target_plate.plate_id if model.is_bambu else None
        if model.is_bambu and bambu_plate != request.plate_id:
            job_logger.info(
                f"Bambu file — mapping plate {request.plate_id} "
                f"(object {target_object_id}) to Orca plate "
                f"{bambu_plate} (Bambu plater_id)"
            )

        try:
            await embedder.embed_profiles_async(
                source_3mf=source_3mf,
                output_3mf=embedded_3mf,
                filament_settings=filament_settings,
                overrides=overrides,
                requested_filament_count=extruder_count,
                extruder_remap=extruder_remap or None,
                precomputed_is_bambu=model.is_bambu,
                precomputed_has_multi_assignments=model.has_multi_extruder_assignments,
                precomputed_has_layer_changes=model.has_layer_tool_changes,
                enable_flow_calibrate=request.enable_flow_calibrate if request.enable_flow_calibrate is not None else True,
                bambu_plate_id=bambu_plate,
                model=model,
            )
            three_mf_size_mb = embedded_3mf.stat().st_size / 1024 / 1024
            job_logger.info(f"Profile-embedded 3MF created: {embedded_3mf.name} ({three_mf_size_mb:.2f} MB)")
        except ProfileEmbedError as e:
            job_logger.error(f"Failed to embed profiles: {str(e)}")
            raise SlicingError(f"Profile embedding failed: {str(e)}")

        # Expand user transforms to co-plate items via model (auto Bambu-aware).
        effective_transforms = request.object_transforms
        if effective_transforms:
            effective_transforms = apply_user_moves(model, effective_transforms)
            if len(effective_transforms) != len(request.object_transforms):
                job_logger.info(
                    f"Expanded {len(request.object_transforms)} transform(s) to "
                    f"{len(effective_transforms)} (co-plate items)"
                )

        embedded_3mf = await _apply_object_transforms_if_needed(
            embedded_3mf,
            workspace,
            effective_transforms,
            job_logger,
        )
        if effective_transforms:
            _enforce_transformed_bounds_or_raise(
                embedded_3mf,
                printer_profile,
                job_logger,
                plate_id=request.plate_id,
                baseline_file_path=(workspace / "sliceable.3mf"),
            )

        # Slice with Orca (async to avoid blocking other API requests)
        _update_progress(job_id, 15, "Starting slicer")
        job_logger.info("Invoking Orca Slicer...")
        slicer = OrcaSlicer(printer_profile)
        scale_percent = float(request.scale_percent if request.scale_percent is not None else 100.0)

        # Progress callback maps slicer's 0-100% to our 20-85% range
        def _slicer_progress(pct, msg):
            if pct >= 100:
                return  # Skip slicer's "All done" — we have post-processing phases
            mapped = 20 + int(pct * 0.65)
            _update_progress(job_id, mapped, msg)

        # The model's plate_id already maps to the correct Orca plate.
        effective_plate_id = target_plate.plate_id
        if effective_plate_id != request.plate_id:
            job_logger.info(
                f"Bambu file — using Orca plate {effective_plate_id} "
                f"(mapped from user plate {request.plate_id})"
            )

        scale_factor = scale_percent / 100.0
        scale_active = abs(scale_percent - 100.0) > 0.001
        layout_scale_active = scale_percent > 100.001
        source_for_slice = embedded_3mf
        if layout_scale_active:
            source_for_slice = workspace / "embedded_layout_scaled.3mf"
            await asyncio.to_thread(
                apply_layout_scale_to_3mf,
                embedded_3mf,
                source_for_slice,
                scale_percent,
            )
            job_logger.info("Applied pre-scale to assembly offsets for native --scale")

        result = await slicer.slice_3mf_async(
            source_for_slice,
            workspace,
            plate_index=effective_plate_id,
            scale_factor=scale_factor,
            disable_arrange=bool(request.object_transforms),
            progress_callback=_slicer_progress,
            job_id=job_id,
        )

        if not result["success"] and scale_active:
            job_logger.warning("Native --scale failed; retrying with transform-based scaling")
            # Re-scale from the original embedded file to avoid double-applying
            # layout offsets when source_for_slice already has pre-scaled spacing.
            scaled_3mf = await _apply_scale_if_needed(embedded_3mf, workspace, scale_percent, job_logger)
            result = await slicer.slice_3mf_async(
                scaled_3mf,
                workspace,
                plate_index=effective_plate_id,
                scale_factor=1.0,
                disable_arrange=bool(request.object_transforms),
                progress_callback=_slicer_progress,
                job_id=job_id,
            )

        if (
            not result["success"]
            and scale_active
            and scale_percent < 100.0
        ):
            job_logger.warning(
                "Scaled downslice failed; retrying once at 100% scale"
            )
            result = await slicer.slice_3mf_async(
                source_for_slice,
                workspace,
                plate_index=effective_plate_id,
                scale_factor=1.0,
                disable_arrange=bool(request.object_transforms),
                progress_callback=_slicer_progress,
                job_id=job_id,
            )

        if not result["success"] and need_prime_tower and _is_wipe_tower_conflict(result):
            job_logger.warning(
                "Detected wipe-tower path conflict; retrying once with prime tower disabled"
            )
            retry_overrides = dict(overrides)
            retry_overrides["enable_prime_tower"] = "0"

            embedded_retry = workspace / "embedded_no_prime.3mf"
            await embedder.embed_profiles_async(
                source_3mf=source_3mf,
                output_3mf=embedded_retry,
                filament_settings=filament_settings,
                overrides=retry_overrides,
                requested_filament_count=extruder_count,
                extruder_remap=extruder_remap or None,
                preserve_geometry=True,
                precomputed_is_bambu=model.is_bambu,
                precomputed_has_multi_assignments=model.has_multi_extruder_assignments,
                precomputed_has_layer_changes=model.has_layer_tool_changes,
                enable_flow_calibrate=request.enable_flow_calibrate if request.enable_flow_calibrate is not None else True,
                model=model,
            )
            embedded_retry = await _apply_object_transforms_if_needed(
                embedded_retry,
                workspace,
                effective_transforms,
                job_logger,
                suffix="no_prime",
            )
            if effective_transforms:
                _enforce_transformed_bounds_or_raise(
                    embedded_retry,
                    printer_profile,
                    job_logger,
                    plate_id=request.plate_id,
                    baseline_file_path=(workspace / "embedded_no_prime.3mf"),
                )
            retry_source = embedded_retry
            if layout_scale_active:
                retry_source = workspace / "embedded_no_prime_layout_scaled.3mf"
                await asyncio.to_thread(
                    apply_layout_scale_to_3mf,
                    embedded_retry,
                    retry_source,
                    scale_percent,
                )
            result = await slicer.slice_3mf_async(
                retry_source,
                workspace,
                plate_index=effective_plate_id,
                scale_factor=scale_factor,
                disable_arrange=bool(request.object_transforms),
                progress_callback=_slicer_progress,
                job_id=job_id,
            )

        if not result["success"]:
            job_logger.error(f"Orca Slicer failed with exit code {result['exit_code']}")
            job_logger.error(f"stdout: {result['stdout']}")
            job_logger.error(f"stderr: {result['stderr']}")
            raise SlicingError(f"Orca Slicer failed: {result['stderr'][:200]}")

        _update_progress(job_id, 85, "Slicer finished")
        job_logger.info("Slicing completed successfully")
        job_logger.info(f"Orca stdout: {result['stdout'][:500]}")

        # Find generated G-code file
        gcode_files = list(workspace.glob("plate_*.gcode"))
        if not gcode_files:
            job_logger.error("No G-code files generated")
            raise SlicingError("G-code file not generated by Orca")

        gcode_workspace_path = gcode_files[0]
        job_logger.info(f"Found G-code file: {gcode_workspace_path.name}")

        if len(filaments) > 1 and extruder_remap and has_overflow_extruders:
            # Pre-slice remap already collapsed >4 to 1-4 in the 3MF.
            # Post-slice remap only needs to fix compaction within 1-4.
            # Non-overflow remaps (e.g. E1,E2→E3,E4) are handled pre-slice
            # so OrcaSlicer generates correct tool numbers and is_extruder_used[].
            effective_extruders = sorted(set(extruder_remap.values()))
            target_tools = [ext - 1 for ext in effective_extruders]
            remap_result = slicer.remap_compacted_tools(gcode_workspace_path, target_tools)
            if remap_result.get("applied"):
                job_logger.info(f"Remapped compacted tools: {remap_result.get('map')}")
            else:
                job_logger.info(f"Tool remap skipped: {remap_result}")

        # Inject thumbnails from 3MF preview into G-code for printer display
        thumb_result = await asyncio.to_thread(
            inject_gcode_thumbnails, gcode_workspace_path, source_3mf,
            plate_id=request.plate_id,
        )
        if thumb_result.get("injected"):
            job_logger.info(f"Injected thumbnails: {thumb_result['sizes']}")
        else:
            job_logger.info(f"Thumbnail injection skipped: {thumb_result.get('reason')}")

        # Parse G-code metadata (async to avoid blocking event loop)
        _update_progress(job_id, 88, "Parsing G-code metadata")
        job_logger.info("Parsing G-code metadata...")
        metadata = await asyncio.to_thread(slicer.parse_gcode_metadata, gcode_workspace_path)
        metadata["estimated_time_seconds"] = _clamp_int32(metadata.get("estimated_time_seconds")) or 0
        metadata["layer_count"] = _clamp_int32(metadata.get("layer_count"))
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
        _update_progress(job_id, 92, "Validating bounds")
        job_logger.info("Validating bounds against printer build volume...")
        try:
            slicer.validate_bounds(gcode_workspace_path)
            job_logger.info("Bounds validation passed")
        except Exception as e:
            job_logger.error(f"Bounds validation failed: {str(e)}")
            raise

        # Move G-code to final location
        _update_progress(job_id, 95, "Saving G-code")
        slices_dir = Path("/data/slices")
        slices_dir.mkdir(parents=True, exist_ok=True)
        final_gcode_path = slices_dir / f"{job_id}.gcode"

        shutil.copy(gcode_workspace_path, final_gcode_path)
        gcode_size = final_gcode_path.stat().st_size
        gcode_size_mb = gcode_size / 1024 / 1024
        job_logger.info(f"G-code saved: {final_gcode_path} ({gcode_size_mb:.2f} MB)")

        # Store full positional color array (see full-file slice comment above)
        filament_colors_json = json.dumps(extruder_colors)
        filament_used_g_json = json.dumps(metadata.get('filament_used_g', []))
        async with pool.acquire() as conn:
            result_tag = await conn.execute(
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
                    filament_colors = $9,
                    filament_used_g = $10,
                    gcode_bounds_min_x = $11,
                    gcode_bounds_min_y = $12,
                    gcode_bounds_min_z = $13,
                    gcode_bounds_max_x = $14,
                    gcode_bounds_max_y = $15,
                    gcode_bounds_max_z = $16
                WHERE job_id = $1 AND status = 'processing'
                """,
                job_id,
                datetime.utcnow(),
                str(final_gcode_path),
                gcode_size,
                metadata['estimated_time_seconds'],
                metadata['filament_used_mm'],
                metadata.get('layer_count'),
                str(embedded_3mf),
                filament_colors_json,
                filament_used_g_json,
                metadata.get('min_x', 0.0),
                metadata.get('min_y', 0.0),
                metadata.get('min_z', 0.0),
                metadata.get('max_x', 0.0),
                metadata.get('max_y', 0.0),
                metadata.get('max_z', 0.0),
            )
            if result_tag == "UPDATE 0":
                job_logger.info(f"Job {job_id} was cancelled before completion could be recorded")
                raise SlicingCancelledError("Slicing cancelled by user")

        _update_progress(job_id, 100, "Complete")
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

        _clear_progress(job_id)
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

    except SlicingCancelledError:
        job_logger.info(f"Plate slicing cancelled by user: {job_id}")
        _clear_progress(job_id)
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE slicing_jobs SET status = 'failed', completed_at = $2, error_message = 'Cancelled'
                WHERE job_id = $1
                """,
                job_id, datetime.utcnow(),
            )
        raise HTTPException(status_code=499, detail="Slicing cancelled")

    except SlicingError as e:
        err_text = str(e)
        if len(filaments) > 1 and "segmentation fault" in err_text.lower():
            err_text = (
                "Multicolour slicing is unstable for this model in Snapmaker Orca v2.2.4 "
                "(slicer crash). Try single-filament slicing for now."
            )
        job_logger.error(f"Plate slicing failed: {err_text}")
        _clear_progress(job_id)
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
        low = err_text.lower()
        code = 500
        if (
            "unstable for this model" in low
            or "invalid object transforms" in low
            or "outside build volume" in low
            or "fully inside the print volume" in low
        ):
            code = 400
        raise HTTPException(status_code=code, detail=f"Plate slicing failed: {err_text}")

    except Exception as e:
        job_logger.error(f"Unexpected error: {str(e)}")
        _clear_progress(job_id)
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
                plate_colors = colors_per_plate.get(plate.plate_id, global_colors)
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
                plate_colors = colors_per_plate.get(plate.plate_id, global_colors)
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


@router.get("/uploads/{upload_id}/layout")
async def get_upload_layout(upload_id: int, plate_id: Optional[int] = Query(None, ge=1)):
    """Return editable top-level build-item layout metadata for M33 (move/rotate)."""
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        upload = await conn.fetchrow(
            """
            SELECT id, filename, file_path, is_multi_plate
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

    started = time.perf_counter()

    def _compute_layout():
        # For Bambu files, expand to all co-objects on the same Bambu plate
        # so the placement viewer shows the full plate group, not just one item.
        effective_plate_ids = None
        if plate_id is not None:
            bambu_meta = _load_bambu_plate_metadata(source_3mf)
            co_indices = _get_bambu_co_plate_indices(source_3mf, plate_id, preloaded=bambu_meta)
            if co_indices:
                effective_plate_ids = co_indices

        if effective_plate_ids:
            # Fetch all items and filter to the co-plate set
            all_items = list_build_items_3mf(source_3mf, plate_id=None)
            items = [it for it in all_items if int(it.get("build_item_index", 0)) in effective_plate_ids]
        else:
            items = list_build_items_3mf(source_3mf, plate_id=plate_id)

        printer_profile = get_printer_profile("snapmaker_u1")
        validator = PlateValidator(printer_profile)
        validation = validator.validate_3mf_bounds(source_3mf, plate_id=plate_id)
        bounds = validation.get("bounds")
        placement_frame = _derive_layout_placement_frame(
            items,
            source_3mf=source_3mf,
            is_multi_plate=bool(upload["is_multi_plate"]),
            plate_id=plate_id,
            bed_x=float(printer_profile.build_volume_x),
            bed_y=float(printer_profile.build_volume_y),
            validation_bounds=bounds if isinstance(bounds, dict) else None,
            bambu_co_plate_expanded=bool(effective_plate_ids),
        )
        return items, printer_profile, validation, bounds, placement_frame

    try:
        items, printer_profile, validation, bounds, placement_frame = await asyncio.to_thread(_compute_layout)

        return {
            "upload_id": upload_id,
            "filename": upload["filename"],
            "is_multi_plate": bool(upload["is_multi_plate"]),
            "selected_plate_id": plate_id,
            "build_volume": {
                "x": printer_profile.build_volume_x,
                "y": printer_profile.build_volume_y,
                "z": printer_profile.build_volume_z,
            },
            "validation": {
                "fits": validation.get("fits", False),
                "warnings": validation.get("warnings", []),
                "bounds": bounds,
            },
            "timing_ms": {
                "total": round((time.perf_counter() - started) * 1000, 1),
            },
            "placement_frame": placement_frame,
            "objects": items,
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load layout metadata: {str(e)}")


@router.get("/uploads/{upload_id}/geometry")
async def get_upload_geometry(
    upload_id: int,
    plate_id: Optional[int] = Query(None, ge=1),
    build_item_index: Optional[int] = Query(None, ge=1),
    include_modifiers: bool = Query(False),
    lod: str = Query("placement_low"),
):
    """Return per-build-item local mesh geometry for the placement viewer (M33/M36 shared)."""
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        upload = await conn.fetchrow(
            """
            SELECT id, filename, file_path, is_multi_plate
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

    started = time.perf_counter()
    max_triangles = 10000
    lod_key = str(lod or "placement_low").strip().lower()
    if lod_key in ("low", "placement_low", "preview"):
        max_triangles = 5000
    elif lod_key in ("high", "placement_high", "detail"):
        max_triangles = 15000
    elif lod_key in ("full",):
        max_triangles = 50000

    # For Bambu files, expand to all co-objects on the same Bambu plate
    # so the placement viewer shows the full plate group geometry.
    effective_plate_ids = None
    if plate_id is not None and build_item_index is None:
        bambu_meta = await asyncio.to_thread(_load_bambu_plate_metadata, source_3mf)
        co_indices = _get_bambu_co_plate_indices(source_3mf, plate_id, preloaded=bambu_meta)
        if co_indices:
            effective_plate_ids = co_indices

    try:
        geom = await asyncio.to_thread(
            list_build_item_geometry_3mf,
            source_3mf,
            plate_id=plate_id if effective_plate_ids is None else None,
            plate_ids=effective_plate_ids,
            build_item_index=build_item_index,
            max_triangles_per_object=max_triangles,
            include_modifiers=include_modifiers,
        )
        return {
            "upload_id": upload_id,
            "filename": upload["filename"],
            "is_multi_plate": bool(upload["is_multi_plate"]),
            "selected_plate_id": plate_id,
            "selected_build_item_index": build_item_index,
            "include_modifiers": bool(include_modifiers),
            "lod": lod_key,
            "max_triangles_per_object": max_triangles,
            "timing_ms": {
                "total": round((time.perf_counter() - started) * 1000, 1),
            },
            **geom,
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load geometry: {str(e)}")


# In-memory preview cache: (upload_id, plate_id_or_"best") → (image_bytes, media_type)
# Small footprint (thumbnails are typically <100 KB each).
_preview_cache: Dict[Tuple[int, str], Tuple[bytes, str]] = {}


def _get_cached_preview(upload_id: int, plate_key: str, source_3mf: Path) -> Optional[Tuple[bytes, str]]:
    """Return cached (image_bytes, media_type) or extract from ZIP and cache."""
    cache_key = (upload_id, plate_key)
    if cache_key in _preview_cache:
        return _preview_cache[cache_key]

    # Single ZIP open: index + extract in one shot
    try:
        with zipfile.ZipFile(source_3mf, "r") as zf:
            names = zf.namelist()
            image_names = [
                n for n in names
                if n.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))
                and "/metadata/" in f"/{n.lower()}"
            ]

            if plate_key == "best":
                if not image_names:
                    return None
                def _score(path: str):
                    p = path.lower()
                    for i, kw in enumerate(["thumbnail", "preview", "cover", "top", "plate", "pick"]):
                        if kw in p:
                            return (i, len(p))
                    return (9, len(p))
                internal_path = sorted(image_names, key=_score)[0]
            else:
                pid = int(plate_key)
                preview_map: Dict[int, str] = {}
                for img_name in image_names:
                    lower = img_name.lower()
                    match = re.search(r"(?:plate|top|pick|thumbnail|preview|cover)[_\-]?(\d+)", lower)
                    if not match:
                        match = re.search(r"[_\-/](\d+)\.(?:png|jpg|jpeg|webp)$", lower)
                    if match:
                        img_pid = int(match.group(1))
                        if img_pid not in preview_map:
                            preview_map[img_pid] = img_name

                internal_path = preview_map.get(pid)
                if not internal_path and pid == 1:
                    # Fallback to best generic preview for plate 1
                    if image_names:
                        def _score(path: str):
                            p = path.lower()
                            for i, kw in enumerate(["thumbnail", "preview", "cover", "top", "plate", "pick"]):
                                if kw in p:
                                    return (i, len(p))
                            return (9, len(p))
                        internal_path = sorted(image_names, key=_score)[0]

                if not internal_path:
                    return None

            image_bytes = zf.read(internal_path)
            media_type = _guess_image_media_type(internal_path)
            _preview_cache[cache_key] = (image_bytes, media_type)
            return (image_bytes, media_type)
    except Exception:
        return None


@router.get("/uploads/{upload_id}/plates/{plate_id}/preview")
async def get_upload_plate_preview(upload_id: int, plate_id: int):
    """Return embedded preview image for a specific plate when available."""
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        upload = await conn.fetchrow(
            "SELECT id, file_path FROM uploads WHERE id = $1",
            upload_id,
        )
        if not upload:
            raise HTTPException(status_code=404, detail="Upload not found")

    source_3mf = Path(upload["file_path"])
    if not source_3mf.exists():
        raise HTTPException(status_code=404, detail="Source 3MF file not found")

    result = _get_cached_preview(upload_id, str(plate_id), source_3mf)
    if not result:
        raise HTTPException(status_code=404, detail="Plate preview not available")

    return Response(content=result[0], media_type=result[1])


@router.get("/uploads/{upload_id}/preview")
async def get_upload_preview(upload_id: int):
    """Return best embedded upload preview image (Explorer-style thumbnail)."""
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        upload = await conn.fetchrow(
            "SELECT id, file_path FROM uploads WHERE id = $1",
            upload_id,
        )
        if not upload:
            raise HTTPException(status_code=404, detail="Upload not found")

    source_3mf = Path(upload["file_path"])
    if not source_3mf.exists():
        raise HTTPException(status_code=404, detail="Source 3MF file not found")

    result = _get_cached_preview(upload_id, "best", source_3mf)
    if not result:
        raise HTTPException(status_code=404, detail="Upload preview not available")

    return Response(content=result[0], media_type=result[1])


@router.get("/jobs/{job_id}")
async def get_slicing_job(job_id: str):
    """Get slicing job status and results."""
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        job = await conn.fetchrow(
            """
            SELECT j.job_id, j.upload_id, j.status, j.started_at, j.completed_at,
                   j.gcode_path, j.gcode_size, j.estimated_time_seconds, j.filament_used_mm,
                   j.layer_count, j.filament_colors, j.filament_used_g, j.error_message,
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

    filament_used_g = []
    if job["filament_used_g"]:
        try:
            filament_used_g = json.loads(job["filament_used_g"])
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

    # Add real-time progress for in-progress jobs
    if job["status"] == "processing":
        prog = _get_progress(job["job_id"])
        result["progress"] = prog["progress"]
        result["progress_message"] = prog["message"]
    elif job["status"] == "completed":
        result["progress"] = 100
        result["progress_message"] = "Complete"
    elif job["status"] == "failed":
        result["progress"] = 0
        result["progress_message"] = job["error_message"] or "Failed"

    # Add metadata if job is completed
    if job["status"] == "completed":
        result["metadata"] = {
            "estimated_time_seconds": job["estimated_time_seconds"],
            "filament_used_mm": job["filament_used_mm"],
            "filament_used_g": filament_used_g,
            "layer_count": job["layer_count"]
        }

    return result


@router.post("/jobs/{job_id}/cancel")
async def cancel_slicing_job(job_id: str):
    """Cancel a running slicing job by killing the OrcaSlicer process."""
    killed = cancel_slice_job(job_id)
    if killed:
        return {"cancelled": True, "job_id": job_id}
    # Process not found — it may have already finished or never started.
    # Mark the job as failed/cancelled in the DB so the poll picks it up.
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status FROM slicing_jobs WHERE job_id = $1", job_id
        )
        if row and row["status"] == "processing":
            await conn.execute(
                """
                UPDATE slicing_jobs SET status = 'failed', completed_at = $2, error_message = 'Cancelled'
                WHERE job_id = $1
                """,
                job_id, datetime.utcnow(),
            )
            _clear_progress(job_id)
            return {"cancelled": True, "job_id": job_id}
    return {"cancelled": False, "job_id": job_id}


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


@router.get("/jobs/{job_id}/gcode/preview-image")
async def preview_gcode_image(
    job_id: str,
    size: int = Query(800, ge=200, le=2000),
):
    """Render a server-side 2D top-down PNG preview of the G-code.

    Used for large files (>50 MB) where client-side 3D rendering is too slow.
    Returns a PNG image that can be displayed directly in an <img> tag.
    """
    from gcode_image_renderer import render_gcode_image
    import io

    pool = get_pg_pool()
    async with pool.acquire() as conn:
        job = await conn.fetchrow(
            "SELECT gcode_path, gcode_size, status, filament_colors "
            "FROM slicing_jobs WHERE job_id = $1",
            job_id,
        )

        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        if job["status"] != "completed":
            raise HTTPException(status_code=400, detail="Job not completed")

        gcode_path = Path(job["gcode_path"])
        if not gcode_path.exists():
            raise HTTPException(status_code=404, detail="G-code file not found")

    # Parse filament colors from DB
    filament_colors = None
    if job["filament_colors"]:
        try:
            filament_colors = json.loads(job["filament_colors"])
        except (json.JSONDecodeError, ValueError):
            pass

    # Render image in thread pool (CPU-bound)
    img = await asyncio.to_thread(
        render_gcode_image,
        gcode_path,
        image_size=size,
        filament_colors=filament_colors,
    )

    # Encode to PNG
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    buf.seek(0)

    return Response(
        content=buf.getvalue(),
        media_type="image/png",
        headers={
            "Cache-Control": "public, max-age=86400",
        },
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
                   estimated_time_seconds, filament_used_mm,
                   gcode_bounds_min_x, gcode_bounds_min_y, gcode_bounds_min_z,
                   gcode_bounds_max_x, gcode_bounds_max_y, gcode_bounds_max_z
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

    # Use cached bounds from DB if available, else fall back to file scan (legacy jobs)
    if job["gcode_bounds_max_x"] is not None:
        bounds = {
            "min_x": job["gcode_bounds_min_x"] or 0.0,
            "min_y": job["gcode_bounds_min_y"] or 0.0,
            "min_z": job["gcode_bounds_min_z"] or 0.0,
            "max_x": job["gcode_bounds_max_x"] or 0.0,
            "max_y": job["gcode_bounds_max_y"] or 0.0,
            "max_z": job["gcode_bounds_max_z"] or 0.0,
        }
    else:
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

    try:
        with open(gcode_path, 'r') as f:
            for line in f:
                line = line.strip()

                # Skip comments and non-move commands
                if not line or line.startswith(';') or not line.startswith('G1'):
                    continue

                # Parse coordinates
                parts = dict(_RE_GCODE_COORD.findall(line))

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
    last_e = 0.0
    has_last_e = False
    relative_extrusion = False
    layer_moves = []

    pattern = _RE_GCODE_FIELDS
    layer_comment_re = _RE_LAYER_CHANGE
    layer_number_re = _RE_LAYER_NUMBER

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

                # Extrusion mode
                if line.startswith("M82"):
                    relative_extrusion = False
                    continue
                if line.startswith("M83"):
                    relative_extrusion = True
                    continue

                # Extruder position reset
                if line.startswith("G92 "):
                    parts = dict(pattern.findall(line))
                    if 'E' in parts:
                        try:
                            last_e = float(parts['E'])
                            has_last_e = True
                        except ValueError:
                            pass
                    continue

                # Parse motion commands (must track G0/G2/G3 too to avoid stale XY
                # causing fake long extrusion bridges in the viewer layer parser).
                if line.startswith("G0 ") or line.startswith("G1 ") or line.startswith("G2 ") or line.startswith("G3 "):
                    parts = dict(pattern.findall(line))

                    # Get coordinates, use last known if not specified
                    x = float(parts['X']) if 'X' in parts else last_x
                    y = float(parts['Y']) if 'Y' in parts else last_y
                    z = float(parts['Z']) if 'Z' in parts else current_z
                    e = parts.get('E')

                    if z != current_z:
                        current_z = z

                    is_arc = line.startswith("G2 ") or line.startswith("G3 ")
                    # Only record XY moves (ignore Z-only moves)
                    if x != last_x or y != last_y:
                        is_extrude = False
                        if (line.startswith("G1 ") or is_arc) and e is not None:
                            try:
                                e_value = float(e)
                                if relative_extrusion:
                                    # In relative mode, only positive E deposits material.
                                    is_extrude = e_value > 1e-6
                                else:
                                    # In absolute mode, compare against last known E.
                                    if has_last_e:
                                        is_extrude = e_value > (last_e + 1e-6)
                                    else:
                                        is_extrude = False
                            except ValueError:
                                is_extrude = False
                        # The lightweight layer API currently returns straight line segments.
                        # Rendering G2/G3 arcs as one straight endpoint chord creates very
                        # misleading long lines (especially in wipe/travel paths). Track arc
                        # endpoints for parser state, but skip emitting them until we add
                        # proper arc tessellation in this endpoint.
                        if not is_arc:
                            layer_moves.append({
                                "type": "extrude" if is_extrude else "travel",
                                "x1": last_x,
                                "y1": last_y,
                                "x2": x,
                                "y2": y
                            })

                    if e is not None:
                        try:
                            last_e = float(e)
                            has_last_e = True
                        except ValueError:
                            pass
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
                sj.filament_used_g,
                sj.filament_colors,
                sj.layer_count,
                sj.started_at,
                sj.completed_at
            FROM slicing_jobs sj
            JOIN uploads u ON sj.upload_id = u.id
            ORDER BY sj.completed_at DESC NULLS LAST
            LIMIT $1 OFFSET $2
        """, limit, offset)

        job_list = []
        for job in jobs:
            filament_colors = []
            if job["filament_colors"]:
                try:
                    filament_colors = json.loads(job["filament_colors"])
                except (json.JSONDecodeError, ValueError):
                    pass
            filament_used_g = []
            if job["filament_used_g"]:
                try:
                    filament_used_g = json.loads(job["filament_used_g"])
                except (json.JSONDecodeError, ValueError):
                    pass
            job_list.append({
                "job_id": job["job_id"],
                "upload_id": job["upload_id"],
                "filename": job["filename"],
                "status": job["status"],
                "gcode_size": job["gcode_size"] or 0,
                "estimated_time_seconds": job["estimated_time_seconds"] or 0,
                "filament_used_mm": job["filament_used_mm"] or 0,
                "filament_used_g": filament_used_g,
                "filament_colors": filament_colors,
                "layer_count": job["layer_count"] or 0,
                "started_at": job["started_at"].isoformat() if job["started_at"] else None,
                "completed_at": job["completed_at"].isoformat() if job["completed_at"] else None
            })

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
