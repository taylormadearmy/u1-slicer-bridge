"""Normalized 3MF model representation and parser.

Provides a single-pass parser that builds a rich internal model from a 3MF file,
normalizing the plate abstraction (Bambu groups multiple build items per plate),
detecting all features, and computing bounds — all in one ZIP open.

This replaces scattered detection functions across profile_embedder.py and
routes_slice.py with a single source of truth.
"""

from __future__ import annotations

import json
import logging
import re
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from multi_plate_parser import (
    _apply_affine_to_bounds_3x4,
    _parse_3mf_transform_values,
    _scan_object_bounds,
    _scan_vertex_bounds_from_element,
    _transform_3x4_to_4x4,
    _estimate_rotation_z_deg_from_3x4,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 3MF XML namespace constants
# ---------------------------------------------------------------------------
NS = {
    "m": "http://schemas.microsoft.com/3dmanufacturing/core/2015/02",
    "p": "http://schemas.microsoft.com/3dmanufacturing/production/2015/06",
}

# Marker files that indicate a Bambu Studio origin
_BAMBU_MARKERS = {
    "Metadata/model_settings.config",
    "Metadata/slice_info.config",
    "Metadata/filament_sequence.json",
}

# Foreign metadata to strip when emitting
FOREIGN_METADATA_FILES = {
    "Metadata/project_settings.config",
    "Metadata/slice_info.config",
    "Metadata/cut_information.xml",
    "Metadata/filament_sequence.json",
    "Metadata/Slic3r_PE.config",
    "Metadata/Slic3r_PE_model.config",
}


# ---------------------------------------------------------------------------
# Source slicer constants
# ---------------------------------------------------------------------------
class SourceSlicer:
    BAMBU = "bambu"
    ORCASLICER = "orcaslicer"
    PRUSASLICER = "prusaslicer"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Geometry types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Bounds3D:
    """Axis-aligned bounding box."""
    min_x: float
    min_y: float
    min_z: float
    max_x: float
    max_y: float
    max_z: float

    @property
    def size(self) -> Tuple[float, float, float]:
        return (self.max_x - self.min_x, self.max_y - self.min_y, self.max_z - self.min_z)

    @property
    def center_xy(self) -> Tuple[float, float]:
        return ((self.min_x + self.max_x) / 2.0, (self.min_y + self.max_y) / 2.0)

    def fits_volume(self, vol_x: float, vol_y: float, vol_z: float, tol: float = 0.5) -> bool:
        w, d, h = self.size
        return w <= vol_x + tol and d <= vol_y + tol and h <= vol_z + tol

    def shifted(self, dx: float, dy: float, dz: float = 0.0) -> Bounds3D:
        return Bounds3D(
            self.min_x + dx, self.min_y + dy, self.min_z + dz,
            self.max_x + dx, self.max_y + dy, self.max_z + dz,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "min": [self.min_x, self.min_y, self.min_z],
            "max": [self.max_x, self.max_y, self.max_z],
            "size": list(self.size),
        }

    @classmethod
    def from_minmax(cls, bmin: List[float], bmax: List[float]) -> Bounds3D:
        return cls(bmin[0], bmin[1], bmin[2], bmax[0], bmax[1], bmax[2])

    @classmethod
    def union(cls, *bounds: Bounds3D) -> Bounds3D:
        if not bounds:
            raise ValueError("Cannot union empty bounds")
        return cls(
            min(b.min_x for b in bounds), min(b.min_y for b in bounds), min(b.min_z for b in bounds),
            max(b.max_x for b in bounds), max(b.max_y for b in bounds), max(b.max_z for b in bounds),
        )


# ---------------------------------------------------------------------------
# Model data classes
# ---------------------------------------------------------------------------

@dataclass
class ThreeMFObject:
    """A single <object> from 3MF resources."""
    object_id: str
    name: str
    extruder: int = 0          # 1-based, 0 = not assigned
    local_bounds: Optional[Bounds3D] = None
    has_paint_data: bool = False
    has_components: bool = False
    obj_type: str = "model"    # "model", "other" (modifier), etc.


@dataclass
class BuildItem:
    """A single <item> from the 3MF <build> section."""
    index: int                 # 1-based position in <build>
    object_id: str
    object: Optional[ThreeMFObject] = None
    transform_3x4: List[float] = field(default_factory=lambda: [
        1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0
    ])
    printable: bool = True
    world_bounds: Optional[Bounds3D] = None
    # Bambu assemble_item transform (separate from build item transform)
    assemble_transform: Optional[List[float]] = None
    assemble_world_bounds: Optional[Bounds3D] = None

    @property
    def translation(self) -> Tuple[float, float, float]:
        return (self.transform_3x4[9], self.transform_3x4[10], self.transform_3x4[11])

    @property
    def rotation_z_deg(self) -> float:
        return _estimate_rotation_z_deg_from_3x4(self.transform_3x4)

    def to_dict(self) -> Dict[str, Any]:
        tx, ty, tz = self.translation
        d: Dict[str, Any] = {
            "build_item_index": self.index,
            "plate_id": self.index,  # Compat with existing API (overridden by plate mapping)
            "object_id": self.object_id,
            "name": self.object.name if self.object else f"Object {self.object_id}",
            "printable": self.printable,
            "transform_3x4": self.transform_3x4,
            "transform": _transform_3x4_to_4x4(self.transform_3x4),
            "translation": [tx, ty, tz],
            "rotation_z_deg": self.rotation_z_deg,
            "local_bounds": self.object.local_bounds.to_dict() if self.object and self.object.local_bounds else None,
            "world_bounds": self.world_bounds.to_dict() if self.world_bounds else None,
            "assemble_translation": None,
            "assemble_world_bounds": None,
        }
        if self.assemble_transform is not None:
            d["assemble_translation"] = [
                self.assemble_transform[9],
                self.assemble_transform[10],
                self.assemble_transform[11],
            ]
        if self.assemble_world_bounds is not None:
            d["assemble_world_bounds"] = self.assemble_world_bounds.to_dict()
        return d


@dataclass
class Plate:
    """A logical plate grouping one or more BuildItems.

    For non-Bambu files: each build item is its own plate (1:1).
    For Bambu files: items are grouped by plater_id from model_settings.config.
    """
    plate_id: int              # For Bambu = plater_id, for non-Bambu = item index
    name: str = ""
    items: List[BuildItem] = field(default_factory=list)
    assemble_transforms: Dict[str, List[float]] = field(default_factory=dict)

    @property
    def world_bounds(self) -> Optional[Bounds3D]:
        bounds = [it.world_bounds for it in self.items if it.world_bounds is not None]
        if not bounds:
            return None
        return Bounds3D.union(*bounds)

    @property
    def is_printable(self) -> bool:
        return any(it.printable for it in self.items)

    @property
    def primary_item(self) -> Optional[BuildItem]:
        for it in self.items:
            if it.printable:
                return it
        return self.items[0] if self.items else None

    @property
    def object_ids(self) -> List[str]:
        return [it.object_id for it in self.items]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "plate_id": self.plate_id,
            "name": self.name,
            "printable": self.is_printable,
            "item_count": len(self.items),
            "items": [it.to_dict() for it in self.items],
        }


@dataclass
class ThreeMFModel:
    """Complete parsed representation of a 3MF file.

    Built once by parse_threemf(), then passed through the pipeline.
    This is the single source of truth for all 3MF processing.
    """
    source_path: Path
    source_slicer: str = SourceSlicer.UNKNOWN

    # Core structure
    objects: Dict[str, ThreeMFObject] = field(default_factory=dict)
    all_items: List[BuildItem] = field(default_factory=list)
    plates: List[Plate] = field(default_factory=list)
    item_to_plate: Dict[int, int] = field(default_factory=dict)  # item.index → plate.plate_id

    # Color/extruder detection
    detected_colors: List[str] = field(default_factory=list)
    assigned_extruder_count: int = 1
    has_paint_data: bool = False
    has_layer_tool_changes: bool = False
    has_multi_extruder_assignments: bool = False
    _all_extruder_indices: set = field(default_factory=set)  # All unique extruder values from model_settings

    # Source config
    source_config: Optional[Dict[str, Any]] = None
    source_bed_center: Optional[Tuple[float, float]] = None

    # Coordinate system
    coord_is_packed: bool = False
    packed_grid_step: Optional[Tuple[Optional[float], Optional[float]]] = None

    # Raw metadata bytes (for emit stage to use)
    model_settings_xml: Optional[bytes] = None
    custom_gcode_xml: Optional[bytes] = None

    # Layer tool changes by plate
    layer_tool_changes: Dict[int, List[Dict[str, Any]]] = field(default_factory=dict)

    # --- Derived properties ---

    @property
    def is_multi_plate(self) -> bool:
        return len(self.plates) > 1

    @property
    def is_bambu(self) -> bool:
        return self.source_slicer == SourceSlicer.BAMBU

    @property
    def needs_preserve(self) -> bool:
        """Whether the original 3MF structure must be preserved (not trimesh rebuilt).

        True when ANY of:
        - Multi-extruder assignments (per-object)
        - Layer tool changes (MultiAsSingle)
        - Paint data (SEMM mode)
        - Multi-plate structure (trimesh merges all plates)
        """
        return (
            self.has_multi_extruder_assignments
            or self.has_layer_tool_changes
            or self.has_paint_data
            or (self.is_bambu and self.is_multi_plate)
        )

    @property
    def is_multicolor(self) -> bool:
        return (
            self.has_multi_extruder_assignments
            or self.has_layer_tool_changes
            or self.has_paint_data
            or len(self.detected_colors) > 1
        )

    @property
    def active_extruders(self) -> List[int]:
        """Sorted unique assigned extruder indices (1-based).

        Matches the format of extract_3mf_metadata_batch()["active_extruders"].
        Uses ALL extruder values found in model_settings.config (including
        per-part/volume assignments), not just object-level ones.
        """
        if self._all_extruder_indices:
            return sorted(self._all_extruder_indices)
        # Fallback: scan objects (for non-Bambu files)
        extruders: set = set()
        for obj in self.objects.values():
            if obj.extruder > 0:
                extruders.add(obj.extruder)
        return sorted(extruders)

    @property
    def item_count(self) -> int:
        return len(self.all_items)

    def get_plate(self, plate_id: int) -> Optional[Plate]:
        for p in self.plates:
            if p.plate_id == plate_id:
                return p
        return None

    def get_plate_for_item(self, item_index: int) -> Optional[Plate]:
        pid = self.item_to_plate.get(item_index)
        if pid is not None:
            return self.get_plate(pid)
        return None

    def get_co_plate_items(self, item_index: int) -> List[BuildItem]:
        """Return all items on the same plate as the given item (including itself)."""
        plate = self.get_plate_for_item(item_index)
        if plate is None:
            return []
        return plate.items

    def get_item(self, item_index: int) -> Optional[BuildItem]:
        for it in self.all_items:
            if it.index == item_index:
                return it
        return None

    def get_item_by_object_id(self, object_id: str) -> Optional[BuildItem]:
        for it in self.all_items:
            if it.object_id == object_id:
                return it
        return None


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def parse_threemf(source_path: Path) -> ThreeMFModel:
    """Parse a 3MF file into a complete ThreeMFModel.

    Opens the ZIP once and extracts all needed metadata:
    - Source slicer detection
    - Build items and objects with bounds
    - Plate grouping (Bambu-aware: groups by plater_id)
    - Feature detection (paint data, multi-extruder, layer tool changes)
    - Source config and coordinate system
    """
    model = ThreeMFModel(source_path=source_path)

    try:
        with zipfile.ZipFile(source_path, "r") as zf:
            namelist = set(zf.namelist())

            # ----------------------------------------------------------
            # 1. Detect source slicer
            # ----------------------------------------------------------
            model.source_slicer = _detect_source_slicer(zf, namelist)

            # ----------------------------------------------------------
            # 2. Read raw metadata files into memory
            # ----------------------------------------------------------
            model_settings_raw = None
            if "Metadata/model_settings.config" in namelist:
                model.model_settings_xml = zf.read("Metadata/model_settings.config")
                model_settings_raw = model.model_settings_xml.decode("utf-8", errors="ignore")

            if "Metadata/custom_gcode_per_layer.xml" in namelist:
                model.custom_gcode_xml = zf.read("Metadata/custom_gcode_per_layer.xml")

            source_config_raw = None
            if "Metadata/project_settings.config" in namelist:
                try:
                    source_config_raw = json.loads(zf.read("Metadata/project_settings.config"))
                    model.source_config = source_config_raw
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass

            # ----------------------------------------------------------
            # 3. Parse model_settings.config for Bambu metadata
            # ----------------------------------------------------------
            bambu_plate_defs: Dict[int, List[str]] = {}  # plater_id → [object_id, ...]
            bambu_plate_names: Dict[int, str] = {}
            bambu_object_names: Dict[str, str] = {}
            bambu_extruder_map: Dict[str, int] = {}  # object_id → extruder (1-based)
            bambu_assemble_by_object: Dict[str, List[float]] = {}

            all_extruder_indices: set = set()
            if model_settings_raw:
                _parse_model_settings(
                    model_settings_raw,
                    bambu_plate_defs,
                    bambu_plate_names,
                    bambu_object_names,
                    bambu_extruder_map,
                    bambu_assemble_by_object,
                    all_extruder_indices,
                )
            # Also scan PrusaSlicer Slic3r_PE_model.config for per-object
            # extruder assignments (key="extruder" type="object")
            if "Metadata/Slic3r_PE_model.config" in namelist:
                try:
                    pe_model_raw = zf.read("Metadata/Slic3r_PE_model.config").decode("utf-8", errors="ignore")
                    pe_root = ET.fromstring(pe_model_raw)
                    for meta in pe_root.findall('.//metadata'):
                        if meta.get('key') == 'extruder' and meta.get('type') == 'object':
                            raw_val = (meta.get('value') or '').strip()
                            if raw_val:
                                try:
                                    idx = int(raw_val)
                                    if idx > 0:
                                        all_extruder_indices.add(idx)
                                except ValueError:
                                    pass
                except (ET.ParseError, Exception):
                    pass

            model._all_extruder_indices = all_extruder_indices

            # ----------------------------------------------------------
            # 4. Detect multi-extruder assignments
            # ----------------------------------------------------------
            extruder_values = set(bambu_extruder_map.values())
            model.has_multi_extruder_assignments = len(extruder_values) > 1 or len(all_extruder_indices) > 1
            model.assigned_extruder_count = max(
                max(extruder_values) if extruder_values else 1,
                max(all_extruder_indices) if all_extruder_indices else 1,
            )

            # ----------------------------------------------------------
            # 5. Detect layer tool changes
            # ----------------------------------------------------------
            if model.custom_gcode_xml:
                model.has_layer_tool_changes, model.layer_tool_changes = (
                    _detect_layer_tool_changes(model.custom_gcode_xml)
                )

            # ----------------------------------------------------------
            # 6. Detect paint data (chunked scan)
            # ----------------------------------------------------------
            model.has_paint_data = _scan_paint_data(zf, namelist)

            # ----------------------------------------------------------
            # 7. Parse 3D/3dmodel.model — objects + build items
            # ----------------------------------------------------------
            main_xml = zf.read("3D/3dmodel.model")
            root = ET.fromstring(main_xml)

            # Parse objects from resources
            object_names_3mf: Dict[str, str] = {}
            object_elems: Dict[str, Any] = {}
            resources = root.find("m:resources", NS)
            if resources is not None:
                _parse_resources(zf, resources, object_names_3mf, object_elems)

            # Build ThreeMFObject for each resource object
            for oid, elem in object_elems.items():
                obj_type = (elem.get("type") or "model").strip().lower()
                has_components = elem.find("m:components", NS) is not None
                name = (
                    bambu_object_names.get(oid)
                    or object_names_3mf.get(oid)
                    or f"Object {oid}"
                )
                extruder = bambu_extruder_map.get(oid, 0)

                # Compute local bounds
                local_bounds = None
                raw_bounds = _scan_object_bounds(zf, elem, NS)
                if raw_bounds:
                    bmin, bmax = raw_bounds
                    local_bounds = Bounds3D.from_minmax(bmin, bmax)

                model.objects[oid] = ThreeMFObject(
                    object_id=oid,
                    name=name,
                    extruder=extruder,
                    local_bounds=local_bounds,
                    has_paint_data=model.has_paint_data,  # File-level flag
                    has_components=has_components,
                    obj_type=obj_type,
                )

            # Parse build items
            build = root.find("m:build", NS)
            if build is not None:
                items = build.findall("m:item", NS)
                for i, item_elem in enumerate(items):
                    idx = i + 1
                    object_id = item_elem.get("objectid") or str(idx)
                    printable = item_elem.get("printable", "1") != "0"
                    t3 = _parse_3mf_transform_values(item_elem.get("transform", ""))

                    obj = model.objects.get(object_id)
                    world_bounds = None
                    if obj and obj.local_bounds:
                        bmin = [obj.local_bounds.min_x, obj.local_bounds.min_y, obj.local_bounds.min_z]
                        bmax = [obj.local_bounds.max_x, obj.local_bounds.max_y, obj.local_bounds.max_z]
                        tbmin, tbmax = _apply_affine_to_bounds_3x4(bmin, bmax, t3)
                        world_bounds = Bounds3D.from_minmax(tbmin, tbmax)

                    # Assemble transform (Bambu)
                    assemble_t = bambu_assemble_by_object.get(str(object_id))
                    assemble_wb = None
                    if assemble_t and obj and obj.local_bounds:
                        bmin = [obj.local_bounds.min_x, obj.local_bounds.min_y, obj.local_bounds.min_z]
                        bmax = [obj.local_bounds.max_x, obj.local_bounds.max_y, obj.local_bounds.max_z]
                        abmin, abmax = _apply_affine_to_bounds_3x4(bmin, bmax, assemble_t)
                        assemble_wb = Bounds3D.from_minmax(abmin, abmax)

                    build_item = BuildItem(
                        index=idx,
                        object_id=object_id,
                        object=obj,
                        transform_3x4=t3,
                        printable=printable,
                        world_bounds=world_bounds,
                        assemble_transform=assemble_t,
                        assemble_world_bounds=assemble_wb,
                    )
                    model.all_items.append(build_item)

            # ----------------------------------------------------------
            # 8. Group items into plates
            # ----------------------------------------------------------
            _group_items_into_plates(
                model, bambu_plate_defs, bambu_plate_names,
                bambu_object_names, object_names_3mf,
                bambu_assemble_by_object,
            )

            # ----------------------------------------------------------
            # 9. Detect colors from source config
            # ----------------------------------------------------------
            model.detected_colors = _detect_colors(zf, namelist, source_config_raw)

            # ----------------------------------------------------------
            # 10. Parse coordinate system
            # ----------------------------------------------------------
            if source_config_raw:
                model.source_bed_center = _parse_bed_center(source_config_raw)

            model.coord_is_packed, model.packed_grid_step = _detect_packed_coords(model)

    except zipfile.BadZipFile:
        raise ValueError("Invalid .3mf file: not a valid ZIP archive")
    except ET.ParseError:
        raise ValueError("Invalid .3mf file: malformed XML")
    except KeyError as e:
        if "3D/3dmodel.model" in str(e):
            raise ValueError("Invalid .3mf file: missing 3D/3dmodel.model")
        raise

    logger.info(
        f"Parsed {source_path.name}: slicer={model.source_slicer}, "
        f"items={len(model.all_items)}, plates={len(model.plates)}, "
        f"multicolor={model.is_multicolor}, needs_preserve={model.needs_preserve}, "
        f"packed={model.coord_is_packed}"
    )
    return model


# ---------------------------------------------------------------------------
# Parser helper functions
# ---------------------------------------------------------------------------

def _detect_source_slicer(zf: zipfile.ZipFile, namelist: set) -> str:
    """Detect which slicer produced this 3MF file."""
    has_bambu_markers = bool(_BAMBU_MARKERS & namelist)
    has_prusa_markers = (
        "Metadata/Slic3r_PE.config" in namelist
        or "Metadata/Slic3r_PE_model.config" in namelist
    )

    if has_bambu_markers:
        return SourceSlicer.BAMBU
    if has_prusa_markers:
        return SourceSlicer.PRUSASLICER

    # Check project_settings.config for slicer name
    if "Metadata/project_settings.config" in namelist:
        try:
            config = json.loads(zf.read("Metadata/project_settings.config"))
            for key in ("printer_settings_id", "print_settings_id"):
                val = str(config.get(key, "")).lower()
                if "bambu" in val or "bbl" in val:
                    return SourceSlicer.BAMBU
                if "orcaslicer" in val:
                    return SourceSlicer.ORCASLICER
        except Exception:
            pass

    return SourceSlicer.UNKNOWN


def _parse_model_settings(
    raw_xml: str,
    plate_defs: Dict[int, List[str]],
    plate_names: Dict[int, str],
    object_names: Dict[str, str],
    extruder_map: Dict[str, int],
    assemble_by_object: Dict[str, List[float]],
    all_extruder_indices: Optional[set] = None,
) -> None:
    """Parse Bambu model_settings.config into multiple output dicts."""
    try:
        ms_root = ET.fromstring(raw_xml)
    except ET.ParseError:
        return

    # Collect ALL extruder values from anywhere in the tree (matches old
    # extract_3mf_metadata_batch behavior — captures per-part/volume assignments)
    if all_extruder_indices is not None:
        for meta in ms_root.findall('.//metadata'):
            if meta.get('key') == 'extruder':
                raw_val = (meta.get('value') or '').strip()
                if raw_val:
                    try:
                        idx = int(raw_val)
                        if idx > 0:
                            all_extruder_indices.add(idx)
                    except ValueError:
                        pass

    # Parse plate definitions: <plate> → <model_instance> → object_id
    for plate_elem in ms_root.findall("plate"):
        pid_meta = plate_elem.find("metadata[@key='plater_id']")
        pname_meta = plate_elem.find("metadata[@key='plater_name']")
        if pid_meta is None:
            continue
        try:
            pid = int(pid_meta.get("value", "0"))
        except ValueError:
            continue
        if pid <= 0:
            continue

        if pname_meta is not None:
            pname = (pname_meta.get("value") or "").strip()
            if pname:
                plate_names[pid] = pname

        # Collect object_ids on this plate
        # Bambu stores object_id as nested <metadata key="object_id" value="..."/>
        obj_ids = []
        for mi in plate_elem.findall("model_instance"):
            # Try attribute first
            oid = mi.get("object_id")
            if not oid:
                # Try nested metadata element (Bambu format)
                oid_meta = mi.find("metadata[@key='object_id']")
                if oid_meta is not None:
                    oid = oid_meta.get("value")
            if oid:
                obj_ids.append(oid)
        if obj_ids:
            plate_defs[pid] = obj_ids

    # Parse object metadata (names + extruder assignments)
    for obj_elem in ms_root.findall("object"):
        oid = obj_elem.get("id")
        if not oid:
            continue

        # Object name
        name_meta = obj_elem.find("metadata[@key='name']")
        if name_meta is not None:
            oname = (name_meta.get("value") or "").strip()
            if oname:
                object_names[oid] = oname

        # Extruder assignment
        ext_meta = obj_elem.find("metadata[@key='extruder']")
        if ext_meta is not None:
            try:
                extruder_map[oid] = int(ext_meta.get("value", "0"))
            except ValueError:
                pass

        # Also check nested part extruder assignments
        for part in obj_elem.findall(".//part"):
            part_ext = part.find("metadata[@key='extruder']")
            if part_ext is not None:
                try:
                    val = int(part_ext.get("value", "0"))
                    # Track any non-zero extruder even from parts
                    # (the oid key ensures we capture the object-level)
                    if val > 0 and oid not in extruder_map:
                        extruder_map[oid] = val
                except ValueError:
                    pass

    # Parse assemble_item transforms
    tag_re = re.compile(r"<assemble_item\b(?P<tag>[^>]*)/?>", re.IGNORECASE | re.DOTALL)
    transform_re = re.compile(r"\btransform=(['\"])(.*?)\1", re.IGNORECASE | re.DOTALL)
    object_re = re.compile(r"\bobject_id=(['\"])(.*?)\1", re.IGNORECASE | re.DOTALL)
    duplicates: set = set()

    for m in tag_re.finditer(raw_xml):
        tag = m.group("tag") or ""
        mo = object_re.search(tag)
        mt = transform_re.search(tag)
        if not mo or not mt:
            continue
        object_id = str(mo.group(2))
        parts = (mt.group(2) or "").strip().split()
        if len(parts) != 12:
            continue
        try:
            vals = [float(v) for v in parts]
        except ValueError:
            continue
        if object_id in assemble_by_object:
            duplicates.add(object_id)
        else:
            assemble_by_object[object_id] = vals
    for oid in duplicates:
        assemble_by_object.pop(oid, None)


def _detect_layer_tool_changes(
    custom_gcode_xml: bytes,
) -> Tuple[bool, Dict[int, List[Dict[str, Any]]]]:
    """Detect layer tool changes from custom_gcode_per_layer.xml."""
    result: Dict[int, List[Dict[str, Any]]] = {}
    found = False
    try:
        root = ET.fromstring(custom_gcode_xml)
        for plate_el in root.findall("plate"):
            plate_id = 0
            info = plate_el.find("plate_info")
            if info is not None:
                try:
                    plate_id = int(info.get("id", "0"))
                except ValueError:
                    pass
            changes = []
            for layer in plate_el.findall("layer"):
                if layer.get("type") != "2":
                    continue
                found = True
                try:
                    z = float(layer.get("top_z", "0"))
                except ValueError:
                    z = 0.0
                try:
                    ext = int(layer.get("extruder", "1"))
                except ValueError:
                    ext = 1
                color = layer.get("color", "")
                changes.append({"z": z, "extruder": ext, "color": color})
            if changes and plate_id > 0:
                result[plate_id] = changes
    except ET.ParseError:
        pass
    return found, result


def _scan_paint_data(zf: zipfile.ZipFile, namelist: set) -> bool:
    """Chunked scan for paint data in .model files.

    Bambu uses ``paint_color`` attributes; PrusaSlicer uses
    ``mmu_segmentation`` attributes on ``<triangle>`` elements.
    """
    MAX_SCAN = 32 * 1024 * 1024
    CHUNK = 1024 * 1024
    needles = (b"paint_color", b"mmu_segmentation")
    for name in namelist:
        if not name.endswith(".model"):
            continue
        try:
            scanned = 0
            with zf.open(name) as f:
                while scanned < MAX_SCAN:
                    chunk = f.read(CHUNK)
                    if not chunk:
                        break
                    for needle in needles:
                        if needle in chunk:
                            return True
                    scanned += len(chunk)
        except Exception:
            continue
    return False


def _parse_resources(
    zf: zipfile.ZipFile,
    resources: Any,
    object_names: Dict[str, str],
    object_elems: Dict[str, Any],
) -> None:
    """Parse <resources> section to build object name and element maps."""
    p_ns_uri = NS["p"]
    for obj in resources.findall("m:object", NS):
        oid = obj.get("id")
        if not oid:
            continue
        object_elems[oid] = obj
        obj_name = (obj.get("name") or "").strip()
        if obj_name:
            object_names[oid] = obj_name
        else:
            # Resolve name from component references (Bambu multi-file format)
            components = obj.find("m:components", NS)
            if components is not None:
                for comp in components.findall("m:component", NS):
                    p_path = comp.get(f"{{{p_ns_uri}}}path")
                    if not p_path:
                        continue
                    ref_path = p_path.lstrip("/")
                    try:
                        ref_xml = zf.read(ref_path)
                        ref_root = ET.fromstring(ref_xml)
                        ref_resources = ref_root.find("m:resources", NS)
                        if ref_resources is not None:
                            for ref_obj in ref_resources.findall("m:object", NS):
                                ref_name = (ref_obj.get("name") or "").strip()
                                if ref_name:
                                    object_names[oid] = ref_name
                                    break
                    except Exception:
                        pass
                    if oid not in object_names:
                        stem = Path(p_path).stem
                        if stem:
                            object_names[oid] = stem
                    break  # First component is enough for name


def _group_items_into_plates(
    model: ThreeMFModel,
    bambu_plate_defs: Dict[int, List[str]],
    bambu_plate_names: Dict[int, str],
    bambu_object_names: Dict[str, str],
    object_names_3mf: Dict[str, str],
    bambu_assemble_by_object: Dict[str, List[float]],
) -> None:
    """Group build items into logical plates.

    For Bambu files with plate definitions: groups by plater_id.
    For everything else: each build item is its own plate (1:1).
    """
    if model.is_bambu and bambu_plate_defs:
        # Build object_id → item index map
        oid_to_item: Dict[str, BuildItem] = {}
        for it in model.all_items:
            oid_to_item[it.object_id] = it

        # Group by Bambu plate definitions
        assigned_items: set = set()
        for plater_id in sorted(bambu_plate_defs.keys()):
            obj_ids = bambu_plate_defs[plater_id]
            plate_items: List[BuildItem] = []
            plate_assemble: Dict[str, List[float]] = {}
            for oid in obj_ids:
                it = oid_to_item.get(oid)
                if it is not None:
                    plate_items.append(it)
                    assigned_items.add(it.index)
                at = bambu_assemble_by_object.get(oid)
                if at is not None:
                    plate_assemble[oid] = at

            if not plate_items:
                continue

            name = (
                bambu_plate_names.get(plater_id)
                or (bambu_object_names.get(plate_items[0].object_id) if plate_items else None)
                or (object_names_3mf.get(plate_items[0].object_id) if plate_items else None)
                or f"Plate {plater_id}"
            )
            plate = Plate(
                plate_id=plater_id,
                name=name,
                items=plate_items,
                assemble_transforms=plate_assemble,
            )
            model.plates.append(plate)
            for it in plate_items:
                model.item_to_plate[it.index] = plater_id

        # Handle unassigned items (items not in any Bambu plate definition)
        for it in model.all_items:
            if it.index not in assigned_items:
                # Create a synthetic plate for orphaned items
                synth_id = max(p.plate_id for p in model.plates) + 1 if model.plates else it.index
                name = (
                    bambu_object_names.get(it.object_id)
                    or object_names_3mf.get(it.object_id)
                    or f"Plate {synth_id}"
                )
                plate = Plate(
                    plate_id=synth_id,
                    name=name,
                    items=[it],
                    assemble_transforms={},
                )
                at = bambu_assemble_by_object.get(it.object_id)
                if at is not None:
                    plate.assemble_transforms[it.object_id] = at
                model.plates.append(plate)
                model.item_to_plate[it.index] = synth_id
    else:
        # Non-Bambu or no plate definitions: 1:1 item→plate
        for it in model.all_items:
            name = (
                bambu_object_names.get(it.object_id)
                or object_names_3mf.get(it.object_id)
                or (it.object.name if it.object else None)
                or f"Plate {it.index}"
            )
            at_map: Dict[str, List[float]] = {}
            at = bambu_assemble_by_object.get(it.object_id)
            if at is not None:
                at_map[it.object_id] = at
            plate = Plate(
                plate_id=it.index,
                name=name,
                items=[it],
                assemble_transforms=at_map,
            )
            model.plates.append(plate)
            model.item_to_plate[it.index] = it.index


def _detect_colors(
    zf: zipfile.ZipFile,
    namelist: set,
    source_config: Optional[Dict[str, Any]],
) -> List[str]:
    """Detect filament colors from multiple sources."""
    colors: List[str] = []

    # Source 1: filament_sequence.json (Bambu)
    if "Metadata/filament_sequence.json" in namelist:
        try:
            seq = json.loads(zf.read("Metadata/filament_sequence.json"))
            if "filament_info" in seq:
                for fi in seq["filament_info"]:
                    color = fi.get("color", "#FFFFFF")
                    if not color.startswith("#"):
                        color = "#" + color
                    colors.append(color)
                if colors:
                    return colors
        except Exception:
            pass

    # Source 2: project_settings.config
    if source_config:
        fc = source_config.get("filament_colour", [])
        # For painted files, return all filament colours (including white)
        # since paint data uses per-triangle filament indices.
        if isinstance(fc, list) and len(fc) > 1:
            if _scan_paint_data(zf, namelist):
                active = [c for c in fc if c]
                if active:
                    return active

        # Non-paint path: return non-white filament colours
        if isinstance(fc, list) and fc:
            for c in fc:
                c = str(c).strip()
                if c and c != "#FFFFFF" and c != "#ffffff":
                    if not c.startswith("#"):
                        c = "#" + c
                    colors.append(c)
            if colors:
                return colors

        # Try extruder_colour
        ec = source_config.get("extruder_colour", [])
        if isinstance(ec, list) and ec:
            for c in ec:
                c = str(c).strip()
                if c and c != "#FFFFFF" and c != "#ffffff":
                    if not c.startswith("#"):
                        c = "#" + c
                    colors.append(c)
            if colors:
                return colors

    # Source 3: PrusaSlicer — check per-object extruder assignments in
    # Slic3r_PE_model.config, then map to extruder_colour from
    # Slic3r_PE.config.  extruder_colour alone is just the printer's
    # tool-head colours, not per-model assignments.
    if "Metadata/Slic3r_PE.config" in namelist:
        try:
            pe_extruder_colours: List[str] = []
            pe_data = zf.read("Metadata/Slic3r_PE.config").decode("utf-8", errors="replace")
            for line in pe_data.splitlines():
                stripped = line.lstrip("; ").strip()
                if stripped.startswith("extruder_colour"):
                    _, _, value = stripped.partition("=")
                    value = value.strip()
                    if value:
                        pe_extruder_colours = [c.strip() for c in value.split(";") if c.strip()]
                    break

            # Check for paint data (mmu_segmentation) or per-object
            # extruder assignments — either makes the file multicolor.
            # During slicing, mmu_segmentation is converted to paint_color.
            if pe_extruder_colours:
                if _scan_paint_data(zf, namelist):
                    return pe_extruder_colours

                if "Metadata/Slic3r_PE_model.config" in namelist:
                    try:
                        model_cfg = zf.read("Metadata/Slic3r_PE_model.config").decode("utf-8", errors="replace")
                        model_cfg_root = ET.fromstring(model_cfg)
                        assigned: set = set()
                        for meta in model_cfg_root.findall('.//metadata'):
                            if meta.get('key') == 'extruder' and meta.get('type') == 'object':
                                raw = (meta.get('value') or '').strip()
                                if raw:
                                    try:
                                        assigned.add(int(raw))
                                    except ValueError:
                                        pass
                        if len(assigned) > 1:
                            result = []
                            for ext in sorted(assigned):
                                idx = ext - 1
                                if 0 <= idx < len(pe_extruder_colours):
                                    c = pe_extruder_colours[idx]
                                    if c and c not in result:
                                        result.append(c)
                            if result:
                                return result
                    except Exception:
                        pass
        except Exception:
            pass

    return colors


def _parse_bed_center(config: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    """Parse printable_area from config and return center coordinates."""
    area = config.get("printable_area")
    if not isinstance(area, list) or not area:
        return None
    try:
        xs, ys = [], []
        for pt in area:
            parts = str(pt).split("x")
            if len(parts) == 2:
                xs.append(float(parts[0]))
                ys.append(float(parts[1]))
        if xs and ys:
            return ((min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0)
    except (ValueError, TypeError):
        pass
    return None


def _detect_packed_coords(model: ThreeMFModel) -> Tuple[bool, Optional[Tuple[Optional[float], Optional[float]]]]:
    """Detect if build items use Bambu packed grid coordinates."""
    if not model.all_items or len(model.all_items) <= 1:
        return False, None

    # Check if any translation is far beyond a reasonable bed size (>370mm)
    BED_THRESHOLD = 370.0
    has_packed = False
    for it in model.all_items:
        tx, ty, _ = it.translation
        if abs(tx) > BED_THRESHOLD or abs(ty) > BED_THRESHOLD:
            has_packed = True
            break

    if not has_packed:
        return False, None

    # Infer grid step from translation deltas
    translations_x = sorted(set(it.translation[0] for it in model.all_items))
    translations_y = sorted(set(it.translation[1] for it in model.all_items))

    def _infer_step(vals: List[float], bed_size: float = 270.0) -> Optional[float]:
        if len(vals) < 2:
            return None
        deltas = [vals[i + 1] - vals[i] for i in range(len(vals) - 1)]
        # Filter to deltas that are roughly bed-sized (>90% of bed)
        big_deltas = [d for d in deltas if d > bed_size * 0.9]
        if big_deltas:
            return sum(big_deltas) / len(big_deltas)
        return None

    step_x = _infer_step(translations_x)
    step_y = _infer_step(translations_y)

    return True, (step_x, step_y)


# ---------------------------------------------------------------------------
# Transform helpers (used by routes_slice.py)
# ---------------------------------------------------------------------------

def apply_user_moves(
    model: ThreeMFModel,
    object_transforms: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Expand user transforms to include co-plate items.

    Replaces _expand_transforms_for_bambu_plate(). Uses model.get_co_plate_items()
    which is Bambu-aware by construction — no file-type branching needed.

    If item A is moved, and items B, C are on the same plate,
    B and C automatically receive the same translation delta.
    """
    if not object_transforms:
        return []

    existing = {int(t.get("build_item_index", 0)) for t in object_transforms}
    expanded = list(object_transforms)

    for tx in list(object_transforms):
        idx = int(tx.get("build_item_index", 0))
        co_items = model.get_co_plate_items(idx)
        if len(co_items) <= 1:
            continue
        for co_item in co_items:
            if co_item.index in existing:
                continue
            # Replicate the same delta to co-plate items
            expanded.append({
                "build_item_index": co_item.index,
                "object_id": co_item.object_id,
                "translate_x_mm": tx.get("translate_x_mm", 0),
                "translate_y_mm": tx.get("translate_y_mm", 0),
                "rotate_z_deg": tx.get("rotate_z_deg", 0),
            })
            existing.add(co_item.index)
            logger.debug(
                f"Expanded transform to co-plate item {co_item.index} "
                f"(object {co_item.object_id})"
            )

    if len(expanded) > len(object_transforms):
        logger.info(
            f"Expanded {len(object_transforms)} transforms to {len(expanded)} "
            f"(co-plate items included)"
        )

    return expanded


def compute_layout_frame(
    model: ThreeMFModel,
    plate_id: Optional[int],
    bed_x: float = 270.0,
    bed_y: float = 270.0,
) -> Dict[str, Any]:
    """Compute placement frame for UI viewer.

    Replaces _derive_layout_placement_frame() with its 4-strategy cascade.
    Uses model.coord_is_packed and model.plates to determine mapping directly.
    """
    frame: Dict[str, Any] = {
        "version": 1,
        "canonical": True,
        "confidence": "exact",
        "mapping": "direct",
        "offset_xy": [0.0, 0.0],
        "capabilities": {"object_edit": True},
        "objects": [],
    }

    # Determine which items to include
    if plate_id is not None:
        plate = model.get_plate(plate_id)
        if plate is None:
            return frame
        target_items = plate.items
    else:
        target_items = model.all_items

    if not target_items:
        return frame

    # Compute display offset based on coordinate system
    offset_x, offset_y = 0.0, 0.0

    if not model.is_multi_plate:
        # Single plate: detect origin (bed-center vs bed-corner)
        offset_x, offset_y = _detect_display_offset(model, target_items, bed_x, bed_y)
        frame["mapping"] = "direct"
        frame["confidence"] = "exact"
    elif model.coord_is_packed and plate_id is not None:
        # Selected plate from packed grid: use plate translation as offset
        plate = model.get_plate(plate_id)
        if plate and plate.items:
            # Find a reference item's assemble transform for this plate
            ref_item = plate.primary_item
            if ref_item and ref_item.assemble_transform:
                at = ref_item.assemble_transform
                offset_x = bed_x / 2.0 - at[9]
                offset_y = bed_y / 2.0 - at[10]
            else:
                # Fall back to build item translation
                ref_tx, ref_ty, _ = ref_item.translation if ref_item else (0, 0, 0)
                offset_x = bed_x / 2.0 - ref_tx
                offset_y = bed_y / 2.0 - ref_ty
        frame["mapping"] = "bambu_plate_translation_offset"
        frame["confidence"] = "exact"
    elif model.coord_is_packed:
        # All plates overview from packed grid: approximate fold
        frame["mapping"] = "bambu_packed_grid_fold"
        frame["confidence"] = "approximate"
        frame["capabilities"]["object_edit"] = False
    else:
        # Multi-plate, non-packed: center all items
        offset_x, offset_y = _detect_display_offset(model, target_items, bed_x, bed_y)
        frame["mapping"] = "centered"
        if not plate_id:
            frame["confidence"] = "approximate"
            frame["capabilities"]["object_edit"] = False

    frame["offset_xy"] = [offset_x, offset_y]

    # Build per-item entries
    for it in target_items:
        base_x, base_y, base_z = it.translation
        if it.assemble_transform:
            base_x = it.assemble_transform[9]
            base_y = it.assemble_transform[10]
            # Keep z from build transform
        ui_x = base_x + offset_x
        ui_y = base_y + offset_y

        frame["objects"].append({
            "build_item_index": it.index,
            "object_id": it.object_id,
            "ui_base_pose": {
                "x": ui_x,
                "y": ui_y,
                "z": base_z,
                "rotation_z_deg": it.rotation_z_deg,
            },
        })

    return frame


def _detect_display_offset(
    model: ThreeMFModel,
    items: List[BuildItem],
    bed_x: float,
    bed_y: float,
) -> Tuple[float, float]:
    """Detect whether coordinates are bed-center or bed-corner and return offset."""
    if not items:
        return 0.0, 0.0

    # Collect all item translations
    txs = [it.translation[0] for it in items if it.printable]
    tys = [it.translation[1] for it in items if it.printable]
    if not txs:
        return 0.0, 0.0

    min_tx, max_tx = min(txs), max(txs)
    min_ty, max_ty = min(tys), max(tys)

    # Test bed-corner interpretation: coords should fit [0, bed_x]
    corner_fits = (
        min_tx >= -10 and max_tx <= bed_x + 10
        and min_ty >= -10 and max_ty <= bed_y + 10
    )

    # Test bed-center interpretation: coords + bed/2 should fit [0, bed_x]
    center_fits = (
        min_tx + bed_x / 2 >= -10 and max_tx + bed_x / 2 <= bed_x + 10
        and min_ty + bed_y / 2 >= -10 and max_ty + bed_y / 2 <= bed_y + 10
    )

    if corner_fits and not center_fits:
        return 0.0, 0.0  # Bed-corner origin, no offset needed
    elif center_fits:
        return bed_x / 2.0, bed_y / 2.0  # Bed-center origin, shift to corner
    else:
        # Neither fits cleanly; default to bed-center (most common)
        return bed_x / 2.0, bed_y / 2.0
