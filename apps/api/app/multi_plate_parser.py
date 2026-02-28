"""Multi-plate 3MF parser to detect and extract individual plates.

BambuStudio/OrcaSlicer multi-plate 3MF files contain multiple <item> elements
in the <build> section, each representing a separate plate with its own
transform matrix positioning.
"""

import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
import logging
import math
import re

logger = logging.getLogger(__name__)


def _read_bambu_assemble_transforms_by_object_id_from_zip(zf: zipfile.ZipFile) -> Dict[str, List[float]]:
    """Best-effort parse of Metadata/model_settings.config assemble_item transforms keyed by object_id."""
    try:
        if "Metadata/model_settings.config" not in zf.namelist():
            return {}
        raw = zf.read("Metadata/model_settings.config").decode("utf-8", errors="ignore")
    except Exception:
        return {}

    by_object: Dict[str, List[float]] = {}
    duplicates: set[str] = set()
    tag_re = re.compile(r"<assemble_item\b(?P<tag>[^>]*)/?>", re.IGNORECASE | re.DOTALL)
    transform_re = re.compile(r"\btransform=(['\"])(.*?)\1", re.IGNORECASE | re.DOTALL)
    object_re = re.compile(r"\bobject_id=(['\"])(.*?)\1", re.IGNORECASE | re.DOTALL)
    for m in tag_re.finditer(raw):
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
        if object_id in by_object:
            duplicates.add(object_id)
        else:
            by_object[object_id] = vals
    for oid in duplicates:
        by_object.pop(oid, None)
    return by_object


def _parse_3mf_transform_values(transform_str: str, default_identity: bool = True) -> List[float]:
    """Parse a 3MF transform into 3x4 row-major [3x3 | tx ty tz]."""
    try:
        values = [float(x) for x in (transform_str or "").split()]
    except ValueError:
        values = []

    if len(values) == 12:
        return values

    if len(values) == 16:
        return [
            values[0], values[1], values[2],
            values[4], values[5], values[6],
            values[8], values[9], values[10],
            values[12], values[13], values[14],
        ]

    if default_identity:
        # 3MF 3x4 row-major affine: [m00 m01 m02 m10 m11 m12 m20 m21 m22 tx ty tz]
        return [1.0, 0.0, 0.0,
                0.0, 1.0, 0.0,
                0.0, 0.0, 1.0,
                0.0, 0.0, 0.0]
    return []


def _transform_point_3x4(point: List[float], t: List[float]) -> List[float]:
    x, y, z = point
    return [
        t[0] * x + t[1] * y + t[2] * z + t[9],
        t[3] * x + t[4] * y + t[5] * z + t[10],
        t[6] * x + t[7] * y + t[8] * z + t[11],
    ]


def _compose_affine_3x4(a: List[float], b: List[float]) -> List[float]:
    """Return a∘b (apply b, then a) for 3MF 3x4 affine matrices."""
    return [
        a[0] * b[0] + a[1] * b[3] + a[2] * b[6],
        a[0] * b[1] + a[1] * b[4] + a[2] * b[7],
        a[0] * b[2] + a[1] * b[5] + a[2] * b[8],
        a[3] * b[0] + a[4] * b[3] + a[5] * b[6],
        a[3] * b[1] + a[4] * b[4] + a[5] * b[7],
        a[3] * b[2] + a[4] * b[5] + a[5] * b[8],
        a[6] * b[0] + a[7] * b[3] + a[8] * b[6],
        a[6] * b[1] + a[7] * b[4] + a[8] * b[7],
        a[6] * b[2] + a[7] * b[5] + a[8] * b[8],
        a[0] * b[9] + a[1] * b[10] + a[2] * b[11] + a[9],
        a[3] * b[9] + a[4] * b[10] + a[5] * b[11] + a[10],
        a[6] * b[9] + a[7] * b[10] + a[8] * b[11] + a[11],
    ]


def _apply_affine_to_bounds_3x4(bmin: List[float], bmax: List[float], t: List[float]) -> Tuple[List[float], List[float]]:
    """Transform an AABB by a 3x4 affine transform and return enclosing AABB."""
    corners = []
    for x in (bmin[0], bmax[0]):
        for y in (bmin[1], bmax[1]):
            for z in (bmin[2], bmax[2]):
                corners.append(_transform_point_3x4([x, y, z], t))

    out_min = [min(p[i] for p in corners) for i in range(3)]
    out_max = [max(p[i] for p in corners) for i in range(3)]
    return out_min, out_max


def _transform_3x4_to_4x4(transform_values: List[float]) -> List[float]:
    if len(transform_values) != 12:
        return [1.0,0.0,0.0,0.0, 0.0,1.0,0.0,0.0, 0.0,0.0,1.0,0.0, 0.0,0.0,0.0,1.0]
    # Existing repo convention appends identity bottom row after 12 values.
    return transform_values + [0.0, 0.0, 0.0, 1.0]


def _estimate_rotation_z_deg_from_3x4(t: List[float]) -> float:
    """Estimate planar Z rotation from affine matrix (best-effort)."""
    try:
        angle = math.degrees(math.atan2(t[3], t[0]))
        if abs(angle) < 1e-9:
            return 0.0
        return angle
    except Exception:
        return 0.0


class PlateInfo:
    """Represents a single plate in a multi-plate 3MF file."""
    
    def __init__(self, plate_id: int, object_id: str, transform: List[float],
                 printable: bool = True, plate_name: Optional[str] = None):
        self.plate_id = plate_id
        self.object_id = object_id
        self.transform = transform  # 4x4 transform matrix (16 elements)
        self.printable = printable
        self.plate_name = plate_name or f"Plate {plate_id}"
        
    def get_translation(self) -> Tuple[float, float, float]:
        """Extract translation from transform matrix."""
        # Transform is 4x4 matrix in row-major order: [m00,m01,m02,m03,m10,m11,m12,m13,m20,m21,m22,m23,m30,m31,m32,m33]
        # For 3MF converted from 3x4 format, translation is in: [m20,m21,m22] (positions 9,10,11)
        return (self.transform[9], self.transform[10], self.transform[11])
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON response."""
        tx, ty, tz = self.get_translation()
        return {
            "plate_id": self.plate_id,
            "object_id": self.object_id,
            "plate_name": self.plate_name,
            "printable": self.printable,
            "translation": [tx, ty, tz],
            "transform": self.transform
        }


def parse_multi_plate_3mf(file_path: Path) -> Tuple[List[PlateInfo], bool]:
    """
    Parse a 3MF file and detect if it contains multiple plates.
    
    Args:
        file_path: Path to .3mf file
        
    Returns:
        Tuple of (plates_list, is_multi_plate)
        - plates_list: List of PlateInfo objects (empty if single plate)
        - is_multi_plate: True if multiple plates detected
    """
    plates = []
    
    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            # Read the main model file
            model_xml = zf.read("3D/3dmodel.model")
            root = ET.fromstring(model_xml)
            
            # 3MF namespaces
            ns = {
                "m": "http://schemas.microsoft.com/3dmanufacturing/core/2015/02",
                "p": "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"
            }
            
            # --- Bambu metadata: plate names and object names ---
            # model_settings.config has <plate> elements with plater_name
            # and <object> elements with name metadata.
            bambu_plate_names: Dict[int, str] = {}  # plater_id -> plater_name
            bambu_object_names: Dict[str, str] = {}  # object id -> name
            try:
                if "Metadata/model_settings.config" in zf.namelist():
                    ms_root = ET.fromstring(zf.read("Metadata/model_settings.config"))
                    for plate_elem in ms_root.findall("plate"):
                        pid_meta = plate_elem.find("metadata[@key='plater_id']")
                        pname_meta = plate_elem.find("metadata[@key='plater_name']")
                        if pid_meta is not None and pname_meta is not None:
                            pid = int(pid_meta.get("value", "0"))
                            pname = (pname_meta.get("value") or "").strip()
                            if pid and pname:
                                bambu_plate_names[pid] = pname
                    for obj_elem in ms_root.findall("object"):
                        oid = obj_elem.get("id")
                        name_meta = obj_elem.find("metadata[@key='name']")
                        if oid and name_meta is not None:
                            oname = (name_meta.get("value") or "").strip()
                            if oname:
                                bambu_object_names[oid] = oname
                    if bambu_plate_names:
                        logger.info(f"Bambu plate names: {bambu_plate_names}")
                    if bambu_object_names:
                        logger.info(f"Bambu object names: {bambu_object_names}")
            except Exception as e:
                logger.debug(f"Could not parse model_settings.config: {e}")

            # Build object ID -> name map from 3MF resources (fallback)
            object_names: Dict[str, str] = {}
            resources = root.find("m:resources", ns)
            if resources is not None:
                p_ns_uri = ns["p"]
                for obj in resources.findall("m:object", ns):
                    obj_id = obj.get("id")
                    obj_name = (obj.get("name") or "").strip()
                    if obj_id and obj_name:
                        object_names[obj_id] = obj_name
                    elif obj_id:
                        # Bambu exports: container objects have no name attr;
                        # resolve from component p:path sub-model references.
                        components = obj.find("m:components", ns)
                        if components is not None:
                            for comp in components.findall("m:component", ns):
                                p_path = comp.get(f"{{{p_ns_uri}}}path")
                                if not p_path:
                                    continue
                                ref_path = p_path.lstrip("/")
                                try:
                                    ref_xml = zf.read(ref_path)
                                    ref_root = ET.fromstring(ref_xml)
                                    ref_resources = ref_root.find("m:resources", ns)
                                    if ref_resources is not None:
                                        for ref_obj in ref_resources.findall("m:object", ns):
                                            ref_name = (ref_obj.get("name") or "").strip()
                                            if ref_name:
                                                object_names[obj_id] = ref_name
                                                break
                                except Exception:
                                    pass
                                if obj_id not in object_names:
                                    stem = Path(p_path).stem
                                    if stem:
                                        object_names[obj_id] = stem
                                break  # first component is enough

            # Find build section which contains plate items
            build = root.find("m:build", ns)
            if build is None:
                logger.info("No build section found - single plate file")
                return [], False
                
            # Extract all item elements from build section
            items = build.findall("m:item", ns)
            if len(items) <= 1:
                logger.info(f"Single plate file ({len(items)} item found)")
                return [], False
                
            logger.info(f"Multi-plate file detected: {len(items)} plates")
            
            # Parse each item as a plate
            for i, item in enumerate(items):
                object_id_attr = item.get("objectid")
                object_id = object_id_attr if object_id_attr is not None else str(i + 1)
                printable_str = item.get("printable", "1")
                printable = printable_str != "0"
                
                # Parse transform matrix (default to identity if not present)
                parsed_3x4 = _parse_3mf_transform_values(item.get("transform", ""))
                transform_values = _transform_3x4_to_4x4(parsed_3x4)
                
                # Name priority: Bambu plate name > Bambu object name > 3MF object name > "Plate N"
                plate_num = i + 1
                resolved_name = (
                    bambu_plate_names.get(plate_num)
                    or bambu_object_names.get(object_id)
                    or object_names.get(object_id)
                    or f"Plate {plate_num}"
                )
                plate = PlateInfo(
                    plate_id=plate_num,
                    object_id=object_id,
                    transform=transform_values,
                    printable=printable,
                    plate_name=resolved_name
                )
                plates.append(plate)
                
                tx, ty, tz = plate.get_translation()
                logger.info(f"Plate {i+1}: Object {object_id} at ({tx:.1f}, {ty:.1f}, {tz:.1f})")
                
    except zipfile.BadZipFile:
        raise ValueError("Invalid .3mf file: not a valid ZIP archive")
    except ET.ParseError:
        raise ValueError("Invalid .3mf file: malformed XML")
    except KeyError:
        raise ValueError("Invalid .3mf file: missing 3D/3dmodel.model")
        
    return plates, len(plates) > 1


def list_build_items_3mf(file_path: Path, plate_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """Return editable top-level build items for a 3MF file.

    M33 foundation uses top-level build items as transform targets.
    """
    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            model_xml = zf.read("3D/3dmodel.model")
            root = ET.fromstring(model_xml)
            ns = {
                "m": "http://schemas.microsoft.com/3dmanufacturing/core/2015/02",
                "p": "http://schemas.microsoft.com/3dmanufacturing/production/2015/06",
            }

            # Best-effort object names from model resources
            object_names: Dict[str, str] = {}
            object_elems: Dict[str, Any] = {}
            object_bounds_cache: Dict[str, Optional[Tuple[List[float], List[float]]]] = {}
            resources = root.find("m:resources", ns)
            if resources is not None:
                for obj in resources.findall("m:object", ns):
                    oid = obj.get("id")
                    if not oid:
                        continue
                    object_elems[oid] = obj
                    object_names[oid] = (obj.get("name") or f"Object {oid}").strip() or f"Object {oid}"

            bambu_assemble_by_object = _read_bambu_assemble_transforms_by_object_id_from_zip(zf)

            build = root.find("m:build", ns)
            if build is None:
                return []

            items = build.findall("m:item", ns)
            if plate_id is not None and (plate_id < 1 or plate_id > len(items)):
                raise ValueError(f"Plate {plate_id} not found (file has {len(items)} items)")

            target_indices = [plate_id] if plate_id is not None else list(range(1, len(items) + 1))
            results: List[Dict[str, Any]] = []

            for idx in target_indices:
                item = items[idx - 1]
                object_id = item.get("objectid") or str(idx)
                printable = (item.get("printable", "1") != "0")
                t3 = _parse_3mf_transform_values(item.get("transform", ""))
                tx, ty, tz = t3[9], t3[10], t3[11]
                local_bounds_dict = None
                world_bounds_dict = None
                assemble_translation = None
                assemble_world_bounds_dict = None
                if object_id in object_elems:
                    if object_id not in object_bounds_cache:
                        object_bounds_cache[object_id] = _scan_object_bounds(zf, object_elems[object_id], ns)
                    cached_bounds = object_bounds_cache.get(object_id)
                    if cached_bounds is not None:
                        bmin, bmax = cached_bounds
                        tbmin, tbmax = _apply_affine_to_bounds_3x4(bmin, bmax, t3)
                        local_bounds_dict = {
                            "min": [float(bmin[0]), float(bmin[1]), float(bmin[2])],
                            "max": [float(bmax[0]), float(bmax[1]), float(bmax[2])],
                            "size": [
                                float(bmax[0] - bmin[0]),
                                float(bmax[1] - bmin[1]),
                                float(bmax[2] - bmin[2]),
                            ],
                        }
                        world_bounds_dict = {
                            "min": [float(tbmin[0]), float(tbmin[1]), float(tbmin[2])],
                            "max": [float(tbmax[0]), float(tbmax[1]), float(tbmax[2])],
                            "size": [
                                float(tbmax[0] - tbmin[0]),
                                float(tbmax[1] - tbmin[1]),
                                float(tbmax[2] - tbmin[2]),
                            ],
                        }
                        at3 = bambu_assemble_by_object.get(str(object_id))
                        if at3 is not None:
                            assemble_translation = [float(at3[9]), float(at3[10]), float(at3[11])]
                            abmin, abmax = _apply_affine_to_bounds_3x4(bmin, bmax, at3)
                            assemble_world_bounds_dict = {
                                "min": [float(abmin[0]), float(abmin[1]), float(abmin[2])],
                                "max": [float(abmax[0]), float(abmax[1]), float(abmax[2])],
                                "size": [
                                    float(abmax[0] - abmin[0]),
                                    float(abmax[1] - abmin[1]),
                                    float(abmax[2] - abmin[2]),
                                ],
                            }
                results.append({
                    "build_item_index": idx,
                    "plate_id": idx,  # build-item index is the "plate" index in the current parser model
                    "object_id": object_id,
                    "name": object_names.get(object_id, f"Object {object_id}"),
                    "printable": printable,
                    "transform_3x4": t3,
                    "transform": _transform_3x4_to_4x4(t3),
                    "translation": [tx, ty, tz],
                    "assemble_translation": assemble_translation,
                    "rotation_z_deg": _estimate_rotation_z_deg_from_3x4(t3),
                    "local_bounds": local_bounds_dict,
                    "world_bounds": world_bounds_dict,
                    "assemble_world_bounds": assemble_world_bounds_dict,
                })

            return results
    except zipfile.BadZipFile:
        raise ValueError("Invalid .3mf file: not a valid ZIP archive")
    except ET.ParseError:
        raise ValueError("Invalid .3mf file: malformed XML")
    except KeyError:
        raise ValueError("Invalid .3mf file: missing 3D/3dmodel.model")


def _collect_object_mesh_geometry(
    zf: zipfile.ZipFile,
    resources_by_model: Dict[str, Dict[str, Any]],
    model_path: str,
    object_id: str,
    ns: Dict[str, str],
    transform_3x4: Optional[List[float]] = None,
    depth: int = 0,
    include_modifiers: bool = True,
) -> Tuple[List[List[float]], List[List[int]]]:
    """Recursively collect object mesh triangles in local object space."""
    if depth > 12:
        raise ValueError("3MF component nesting too deep")

    obj_map = resources_by_model.get(model_path) or {}
    obj_elem = obj_map.get(str(object_id))
    if obj_elem is None:
        return [], []

    obj_type = (obj_elem.get("type") or "model").strip().lower()
    if not include_modifiers and obj_type != "model":
        # Hide modifier/support/other helper meshes from placement viewer by default.
        return [], []

    t = transform_3x4 or _parse_3mf_transform_values("", default_identity=True)
    vertices_out: List[List[float]] = []
    triangles_out: List[List[int]] = []

    mesh = obj_elem.find("m:mesh", ns)
    if mesh is not None:
        vertices_elem = mesh.find("m:vertices", ns)
        triangles_elem = mesh.find("m:triangles", ns)
        local_vertices: List[List[float]] = []
        if vertices_elem is not None:
            for v in vertices_elem.findall("m:vertex", ns):
                pt = [float(v.get("x", "0")), float(v.get("y", "0")), float(v.get("z", "0"))]
                local_vertices.append(_transform_point_3x4(pt, t))
        if triangles_elem is None or not local_vertices:
            return local_vertices, []

        base_index = 0
        vertices_out.extend(local_vertices)
        for tri in triangles_elem.findall("m:triangle", ns):
            try:
                v1 = int(tri.get("v1", "0"))
                v2 = int(tri.get("v2", "0"))
                v3 = int(tri.get("v3", "0"))
            except ValueError:
                continue
            if v1 < 0 or v2 < 0 or v3 < 0:
                continue
            if v1 >= len(local_vertices) or v2 >= len(local_vertices) or v3 >= len(local_vertices):
                continue
            triangles_out.append([base_index + v1, base_index + v2, base_index + v3])
        return vertices_out, triangles_out

    components = obj_elem.find("m:components", ns)
    if components is None:
        return [], []

    p_ns = ns.get("p")
    for comp in components.findall("m:component", ns):
        ref_object_id = comp.get("objectid")
        if not ref_object_id:
            continue
        ref_path_attr = comp.get(f"{{{p_ns}}}path") if p_ns else None
        ref_model_path = model_path
        if ref_path_attr:
            ref_model_path = ref_path_attr.lstrip("/")
        comp_t = _parse_3mf_transform_values(comp.get("transform", ""))
        child_t = _compose_affine_3x4(t, comp_t)

        child_vertices, child_triangles = _collect_object_mesh_geometry(
            zf,
            resources_by_model,
            ref_model_path,
            ref_object_id,
            ns,
            transform_3x4=child_t,
            depth=depth + 1,
            include_modifiers=include_modifiers,
        )
        if not child_vertices:
            continue
        base_index = len(vertices_out)
        vertices_out.extend(child_vertices)
        for tri in child_triangles:
            triangles_out.append([base_index + tri[0], base_index + tri[1], base_index + tri[2]])

    return vertices_out, triangles_out


def list_build_item_geometry_3mf(
    file_path: Path,
    plate_id: Optional[int] = None,
    plate_ids: Optional[List[int]] = None,
    build_item_index: Optional[int] = None,
    max_triangles_per_object: int = 20000,
    include_modifiers: bool = True,
) -> Dict[str, Any]:
    """Return per-build-item local mesh geometry for the placement viewer."""
    try:
        def decimate_mesh(
            vertices_in: List[List[float]],
            triangles_in: List[List[int]],
            max_triangles: int,
        ) -> Tuple[List[List[float]], List[List[int]], bool]:
            if max_triangles <= 0 or len(triangles_in) <= max_triangles:
                return vertices_in, triangles_in, False

            step = max(1, int(math.ceil(len(triangles_in) / float(max_triangles))))
            sampled = triangles_in[::step]
            if len(sampled) > max_triangles:
                sampled = sampled[:max_triangles]

            used: Dict[int, int] = {}
            vertices_out: List[List[float]] = []
            triangles_out: List[List[int]] = []
            for tri in sampled:
                remapped: List[int] = []
                for old_idx in tri:
                    if old_idx not in used:
                        used[old_idx] = len(vertices_out)
                        vertices_out.append(vertices_in[old_idx])
                    remapped.append(used[old_idx])
                triangles_out.append(remapped)
            return vertices_out, triangles_out, True

        with zipfile.ZipFile(file_path, "r") as zf:
            ns = {
                "m": "http://schemas.microsoft.com/3dmanufacturing/core/2015/02",
                "p": "http://schemas.microsoft.com/3dmanufacturing/production/2015/06",
            }

            resources_by_model: Dict[str, Dict[str, Any]] = {}

            def ensure_resources(model_path: str) -> Dict[str, Any]:
                if model_path in resources_by_model:
                    return resources_by_model[model_path]
                xml_bytes = zf.read(model_path)
                root = ET.fromstring(xml_bytes)
                resources = root.find("m:resources", ns)
                obj_map: Dict[str, Any] = {}
                if resources is not None:
                    for obj in resources.findall("m:object", ns):
                        oid = obj.get("id")
                        if oid:
                            obj_map[oid] = obj
                resources_by_model[model_path] = obj_map
                return obj_map

            main_model_path = "3D/3dmodel.model"
            main_xml = zf.read(main_model_path)
            root = ET.fromstring(main_xml)
            ensure_resources(main_model_path)
            for name in zf.namelist():
                if not name.lower().endswith(".model") or name == main_model_path:
                    continue
                try:
                    ensure_resources(name)
                except Exception:
                    continue

            build = root.find("m:build", ns)
            if build is None:
                return {"objects": [], "max_triangles_per_object": max_triangles_per_object}
            items = build.findall("m:item", ns)
            if plate_id is not None and (plate_id < 1 or plate_id > len(items)):
                raise ValueError(f"Plate {plate_id} not found (file has {len(items)} items)")
            if build_item_index is not None and (build_item_index < 1 or build_item_index > len(items)):
                raise ValueError(f"Build item {build_item_index} not found (file has {len(items)} items)")

            if plate_ids is not None:
                target_indices = [pid for pid in plate_ids if 1 <= pid <= len(items)]
            elif plate_id is not None:
                target_indices = [plate_id]
            else:
                target_indices = list(range(1, len(items) + 1))
            if build_item_index is not None:
                if build_item_index not in target_indices:
                    raise ValueError(
                        f"Build item {build_item_index} is not available in selected scope"
                    )
                target_indices = [build_item_index]
            out_objects: List[Dict[str, Any]] = []

            # Preload any externally referenced model files from top-level objects on demand.
            for idx in target_indices:
                item = items[idx - 1]
                object_id = item.get("objectid")
                if not object_id:
                    continue

                # Apply the build item's rotation+scale (but NOT translation) to
                # geometry vertices so the viewer displays the correct orientation.
                # Translation is handled separately via ui_base_pose in the layout API.
                item_t = _parse_3mf_transform_values(item.get("transform", ""))
                rotscale_t = list(item_t)
                rotscale_t[9] = 0.0   # zero out translation X
                rotscale_t[10] = 0.0  # zero out translation Y
                rotscale_t[11] = 0.0  # zero out translation Z

                try:
                    vertices, triangles = _collect_object_mesh_geometry(
                        zf,
                        resources_by_model,
                        main_model_path,
                        object_id,
                        ns,
                        transform_3x4=rotscale_t,
                        include_modifiers=include_modifiers,
                    )
                except KeyError:
                    vertices, triangles = [], []
                except ET.ParseError:
                    vertices, triangles = [], []

                original_vertex_count = len(vertices)
                original_triangle_count = len(triangles)
                vertices, triangles, decimated = decimate_mesh(vertices, triangles, max_triangles_per_object)
                too_large = original_triangle_count > max_triangles_per_object

                out_objects.append({
                    "build_item_index": idx,
                    "object_id": object_id,
                    "has_mesh": bool(vertices and triangles),
                    "mesh_too_large": too_large,
                    "mesh_decimated": bool(decimated),
                    "vertex_count": len(vertices),
                    "triangle_count": len(triangles),
                    "original_vertex_count": original_vertex_count,
                    "original_triangle_count": original_triangle_count,
                    "vertices": vertices,
                    "triangles": triangles,
                    "include_modifiers": bool(include_modifiers),
                })

            return {
                "objects": out_objects,
                "max_triangles_per_object": max_triangles_per_object,
            }
    except zipfile.BadZipFile:
        raise ValueError("Invalid .3mf file: not a valid ZIP archive")
    except ET.ParseError:
        raise ValueError("Invalid .3mf file: malformed XML")
    except KeyError:
        raise ValueError("Invalid .3mf file: missing 3D/3dmodel.model")


def extract_plate_objects(file_path: Path, target_plate_id: int) -> List[Dict[str, Any]]:
    """
    Extract object information for a specific plate from a multi-plate 3MF.
    
    Args:
        file_path: Path to .3mf file
        target_plate_id: Which plate to extract (1-based)
        
    Returns:
        List of object dictionaries for the specified plate
    """
    plates, is_multi_plate = parse_multi_plate_3mf(file_path)
    
    if not is_multi_plate:
        # Single plate file - use existing parser
        from parser_3mf import parse_3mf
        objects = parse_3mf(file_path)
        return [obj.to_dict() for obj in objects]
    
    # Find the target plate
    target_plate = None
    for plate in plates:
        if plate.plate_id == target_plate_id:
            target_plate = plate
            break
            
    if not target_plate:
        raise ValueError(f"Plate {target_plate_id} not found in file")
    
    # Load the specific object for this plate
    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            model_xml = zf.read("3D/3dmodel.model")
            root = ET.fromstring(model_xml)
            
            ns = {
                "m": "http://schemas.microsoft.com/3dmanufacturing/core/2015/02",
                "p": "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"
            }
            
            # Find the object in resources
            resources = root.find("m:resources", ns)
            if resources is None:
                return []
                
            target_object_id = target_plate.object_id
            objects_info = []
            
            for obj in resources.findall("m:object", ns):
                if obj.get("id") == target_object_id:
                    # Check if this object has inline mesh or component references
                    name = obj.get("name", f"Plate_{target_plate_id}")
                    vertices_count = 0
                    triangles_count = 0
                    
                    # Inline mesh data
                    mesh = obj.find("m:mesh", ns)
                    if mesh is not None:
                        vertices_elem = mesh.find("m:vertices", ns)
                        if vertices_elem is not None:
                            vertices_count = len(vertices_elem.findall("m:vertex", ns))
                        
                        triangles_elem = mesh.find("m:triangles", ns)
                        if triangles_elem is not None:
                            triangles_count = len(triangles_elem.findall("m:triangle", ns))
                    
                    # Component references (external files)
                    else:
                        components = obj.find("m:components", ns)
                        if components is not None:
                            for component in components.findall("m:component", ns):
                                p_ns = ns["p"]
                                ref_path = component.get(f"{{{p_ns}}}path")
                                ref_object_id = component.get("objectid")
                                
                                if ref_path and ref_object_id:
                                    try:
                                        ref_path_clean = ref_path.lstrip("/")
                                        ref_xml = zf.read(ref_path_clean)
                                        ref_root = ET.fromstring(ref_xml)
                                        
                                        ref_resources = ref_root.find("m:resources", ns)
                                        if ref_resources is not None:
                                            for ref_obj in ref_resources.findall("m:object", ns):
                                                if ref_obj.get("id") == ref_object_id:
                                                    ref_mesh = ref_obj.find("m:mesh", ns)
                                                    if ref_mesh is not None:
                                                        vertices_elem = ref_mesh.find("m:vertices", ns)
                                                        if vertices_elem is not None:
                                                            vertices_count += len(vertices_elem.findall("m:vertex", ns))
                                                        
                                                        triangles_elem = ref_mesh.find("m:triangles", ns)
                                                        if triangles_elem is not None:
                                                            triangles_count += len(triangles_elem.findall("m:triangle", ns))
                                                    
                                                    ref_name = ref_obj.get("name")
                                                    if ref_name:
                                                        name = ref_name
                                                    break
                                    except (KeyError, ET.ParseError):
                                        continue
                    
                    if vertices_count > 0:
                        objects_info.append({
                            "object_id": target_object_id,
                            "name": name,
                            "vertices": vertices_count,
                            "triangles": triangles_count,
                            "plate_id": target_plate_id
                        })
                    break
                    
    except Exception as e:
        logger.error(f"Failed to extract plate {target_plate_id}: {str(e)}")
        raise ValueError(f"Could not extract plate {target_plate_id}: {str(e)}")
        
    return objects_info


def _scan_vertex_bounds_from_element(mesh_elem, ns: Dict[str, str]) -> Optional[Tuple[List[float], List[float]]]:
    """Scan vertex elements to find min/max XYZ without building full mesh.

    Returns (min_xyz, max_xyz) or None if no vertices found.
    """
    vertices_elem = mesh_elem.find("m:vertices", ns)
    if vertices_elem is None:
        return None

    min_x = min_y = min_z = float('inf')
    max_x = max_y = max_z = float('-inf')
    count = 0

    for vertex in vertices_elem.iter(f"{{{ns['m']}}}vertex"):
        x = float(vertex.get("x"))
        y = float(vertex.get("y"))
        z = float(vertex.get("z"))
        if x < min_x: min_x = x
        if x > max_x: max_x = x
        if y < min_y: min_y = y
        if y > max_y: max_y = y
        if z < min_z: min_z = z
        if z > max_z: max_z = z
        count += 1

    if count == 0:
        return None

    return [min_x, min_y, min_z], [max_x, max_y, max_z]


def _scan_object_bounds(zf: zipfile.ZipFile, obj_elem, ns: Dict[str, str]) -> Optional[Tuple[List[float], List[float]]]:
    """Get bounds for a single object (inline mesh or component references).

    Returns (min_xyz, max_xyz) or None.
    """
    # Inline mesh
    mesh = obj_elem.find("m:mesh", ns)
    if mesh is not None:
        return _scan_vertex_bounds_from_element(mesh, ns)

    # Component references (external files)
    components = obj_elem.find("m:components", ns)
    if components is None:
        return None

    p_ns = ns["p"]
    combined_min = [float('inf')] * 3
    combined_max = [float('-inf')] * 3
    found = False

    for component in components.findall("m:component", ns):
        ref_path = component.get(f"{{{p_ns}}}path")
        ref_object_id = component.get("objectid")
        if not ref_object_id:
            continue

        # Parse component transform (offset applied to this component's geometry)
        comp_t = _parse_3mf_transform_values(component.get("transform", ""))

        ref_mesh_elem = None
        if ref_path:
            # External sub-model file
            try:
                ref_xml = zf.read(ref_path.lstrip("/"))
                ref_root = ET.fromstring(ref_xml)
                ref_resources = ref_root.find("m:resources", ns)
                if ref_resources is None:
                    continue
                for ref_obj in ref_resources.findall("m:object", ns):
                    if ref_obj.get("id") == ref_object_id:
                        ref_mesh_elem = ref_obj.find("m:mesh", ns)
                        break
            except (KeyError, ET.ParseError):
                continue
        else:
            # Local component reference (same model file)
            parent = obj_elem.getparent() if hasattr(obj_elem, 'getparent') else None
            # Walk siblings in resources to find the referenced object
            if parent is not None:
                for sibling in parent.findall("m:object", ns):
                    if sibling.get("id") == ref_object_id:
                        ref_mesh_elem = sibling.find("m:mesh", ns)
                        break

        if ref_mesh_elem is not None:
            result = _scan_vertex_bounds_from_element(ref_mesh_elem, ns)
            if result:
                bmin, bmax = result
                tbmin, tbmax = _apply_affine_to_bounds_3x4(bmin, bmax, comp_t)
                for i in range(3):
                    if tbmin[i] < combined_min[i]: combined_min[i] = tbmin[i]
                    if tbmax[i] > combined_max[i]: combined_max[i] = tbmax[i]
                found = True

    return (combined_min, combined_max) if found else None


def _calculate_xml_bounds(file_path: Path,
                          plates: Optional[List[PlateInfo]] = None,
                          plate_id: Optional[int] = None) -> Dict[str, Any]:
    """Calculate bounds from XML vertex data without trimesh.

    Opens the ZIP once and scans vertex coordinates directly.
    """
    if plates is None:
        plates_parsed, is_multi_plate = parse_multi_plate_3mf(file_path)
    else:
        plates_parsed = plates
        is_multi_plate = len(plates_parsed) > 1

    ns = {
        "m": "http://schemas.microsoft.com/3dmanufacturing/core/2015/02",
        "p": "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"
    }

    with zipfile.ZipFile(file_path, "r") as zf:
        model_xml = zf.read("3D/3dmodel.model")
        root = ET.fromstring(model_xml)

        resources = root.find("m:resources", ns)
        if resources is None:
            raise ValueError("3MF missing resources section")

        # Build object_id -> element map
        obj_map: Dict[str, Any] = {}
        for obj in resources.findall("m:object", ns):
            oid = obj.get("id")
            if oid:
                obj_map[oid] = obj

        build = root.find("m:build", ns)
        items = build.findall("m:item", ns) if build is not None else []

        # Determine which items to scan
        if plate_id is not None:
            # Single plate
            if plate_id < 1 or plate_id > len(items):
                raise ValueError(f"Plate {plate_id} not found (file has {len(items)} items)")
            target_items = [(plate_id - 1, items[plate_id - 1])]
        else:
            # All items
            target_items = list(enumerate(items))

        global_min = [float('inf')] * 3
        global_max = [float('-inf')] * 3
        found = False

        for idx, item in target_items:
            obj_id = item.get("objectid")
            if not obj_id or obj_id not in obj_map:
                continue

            bounds = _scan_object_bounds(zf, obj_map[obj_id], ns)
            if bounds is None:
                continue

            bmin, bmax = bounds

            item_t = _parse_3mf_transform_values(item.get("transform", ""))
            tbmin, tbmax = _apply_affine_to_bounds_3x4(bmin, bmax, item_t)

            for i in range(3):
                if tbmin[i] < global_min[i]: global_min[i] = tbmin[i]
                if tbmax[i] > global_max[i]: global_max[i] = tbmax[i]
            found = True

    if not found:
        global_min = [0.0, 0.0, 0.0]
        global_max = [0.0, 0.0, 0.0]

    size = [global_max[i] - global_min[i] for i in range(3)]

    result: Dict[str, Any] = {
        "is_multi_plate": is_multi_plate,
        "bounds": {
            "min": global_min,
            "max": global_max,
            "size": size
        }
    }

    if plate_id is not None:
        target_plate = next((p for p in plates_parsed if p.plate_id == plate_id), None)
        result["plates"] = [target_plate.to_dict()] if target_plate else []
        result["plate_id"] = plate_id
    else:
        result["plates"] = [p.to_dict() for p in plates_parsed] if is_multi_plate else []

    return result


def calculate_all_bounds(file_path: Path,
                         plates: List[PlateInfo]) -> Dict[str, Any]:
    """Single-pass bounds computation for all plates + combined.

    Opens the ZIP once, scans vertex bounds for every build item, and returns
    both per-plate and combined bounds in a single pass.  Also returns the
    number of geometry-bearing objects found (replaces separate parse_3mf call).

    Returns:
        {
            "combined": {"min": [...], "max": [...], "size": [...]},
            "per_plate": {
                plate_id: {"min": [...], "max": [...], "size": [...]},
                ...
            },
            "objects_count": int,
        }
    """
    is_multi_plate = len(plates) > 1

    ns = {
        "m": "http://schemas.microsoft.com/3dmanufacturing/core/2015/02",
        "p": "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"
    }

    with zipfile.ZipFile(file_path, "r") as zf:
        model_xml = zf.read("3D/3dmodel.model")
        root = ET.fromstring(model_xml)

        resources = root.find("m:resources", ns)
        if resources is None:
            raise ValueError("3MF missing resources section")

        # Build object_id -> element map
        obj_map: Dict[str, Any] = {}
        for obj in resources.findall("m:object", ns):
            oid = obj.get("id")
            if oid:
                obj_map[oid] = obj

        build = root.find("m:build", ns)
        items = build.findall("m:item", ns) if build is not None else []

        # Scan all items in one pass, accumulating per-plate bounds
        combined_min = [float('inf')] * 3
        combined_max = [float('-inf')] * 3
        per_plate_bounds: Dict[int, Dict[str, list]] = {}
        objects_count = 0

        for idx, item in enumerate(items):
            plate_id = idx + 1
            obj_id = item.get("objectid")
            if not obj_id or obj_id not in obj_map:
                continue

            bounds = _scan_object_bounds(zf, obj_map[obj_id], ns)
            if bounds is None:
                continue

            objects_count += 1
            bmin, bmax = bounds
            item_t = _parse_3mf_transform_values(item.get("transform", ""))
            tbmin, tbmax = _apply_affine_to_bounds_3x4(bmin, bmax, item_t)

            # Update combined bounds
            for i in range(3):
                if tbmin[i] < combined_min[i]:
                    combined_min[i] = tbmin[i]
                if tbmax[i] > combined_max[i]:
                    combined_max[i] = tbmax[i]

            # Store per-plate bounds
            per_plate_bounds[plate_id] = {
                "min": list(tbmin),
                "max": list(tbmax),
                "size": [tbmax[i] - tbmin[i] for i in range(3)],
            }

    if not per_plate_bounds:
        combined_min = [0.0, 0.0, 0.0]
        combined_max = [0.0, 0.0, 0.0]

    return {
        "combined": {
            "min": combined_min,
            "max": combined_max,
            "size": [combined_max[i] - combined_min[i] for i in range(3)],
        },
        "per_plate": per_plate_bounds,
        "objects_count": objects_count,
    }


def get_plate_bounds(file_path: Path,
                     plate_id: Optional[int] = None,
                     plates: Optional[List[PlateInfo]] = None) -> Dict[str, Any]:
    """Calculate bounds for a specific plate or all plates combined.

    Uses fast XML vertex scanning — no trimesh required.

    Args:
        file_path: Path to .3mf file
        plate_id: Specific plate ID to check (None for all plates combined)
        plates: Pre-parsed plate list (avoids redundant ZIP opens)

    Returns:
        Dictionary with bounds information
    """
    return _calculate_xml_bounds(file_path, plates=plates, plate_id=plate_id)


def extract_plate_to_3mf(source_3mf: Path, target_plate_id: int, output_3mf: Path) -> Path:
    """
    Extract a single plate from a multi-plate 3MF and save as new 3MF.
    
    This function:
    1. Parses the original 3MF to identify the target plate
    2. Loads the plate's geometry using trimesh
    3. Applies inverse transform to center objects at origin
    4. Writes a clean single-plate 3MF
    
    Args:
        source_3mf: Path to original multi-plate 3MF
        target_plate_id: Which plate to extract (1-based)
        output_3mf: Path where new single-plate 3MF should be saved
        
    Returns:
        Path to the new 3MF file
        
    Raises:
        ValueError: If plate not found or extraction fails
    """
    import shutil
    
    logger.info(f"Extracting plate {target_plate_id} from {source_3mf.name}")
    
    # Parse to get plate info
    plates, is_multi_plate = parse_multi_plate_3mf(source_3mf)
    
    if not is_multi_plate:
        # Single plate file - just copy it
        logger.info("Single plate file - copying directly")
        shutil.copy2(source_3mf, output_3mf)
        return output_3mf
    
    # Find target plate
    target_plate = None
    for plate in plates:
        if plate.plate_id == target_plate_id:
            target_plate = plate
            break
    
    if not target_plate:
        raise ValueError(f"Plate {target_plate_id} not found in file")
    
    # Keep original metadata/resources and only narrow <build> to the target plate.
    # This preserves Bambu assignment semantics better than trimesh re-export.
    try:
        with zipfile.ZipFile(source_3mf, "r") as src_zf:
            model_xml = src_zf.read("3D/3dmodel.model")
            root = ET.fromstring(model_xml)

            ns = {"m": "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"}
            build = root.find("m:build", ns)
            if build is None:
                raise ValueError("3MF missing build section")

            items = build.findall("m:item", ns)
            if target_plate_id < 1 or target_plate_id > len(items):
                raise ValueError(f"Plate {target_plate_id} out of range (1-{len(items)})")

            # Keep all build items to preserve internal multi-plate metadata links,
            # but mark non-target plates as non-printable.
            for idx, item in enumerate(items, start=1):
                item.set("printable", "1" if idx == target_plate_id else "0")

            updated_model = ET.tostring(root, encoding="utf-8", xml_declaration=True)

            with zipfile.ZipFile(output_3mf, "w", zipfile.ZIP_DEFLATED) as dst_zf:
                for info in src_zf.infolist():
                    if info.filename == "3D/3dmodel.model":
                        dst_zf.writestr(info.filename, updated_model)
                    else:
                        dst_zf.writestr(info, src_zf.read(info.filename))

        logger.info(f"Successfully extracted plate {target_plate_id} with metadata preserved")
        return output_3mf

    except Exception as e:
        logger.error(f"Failed to extract plate: {str(e)}")
        raise ValueError(f"Failed to extract plate {target_plate_id}: {str(e)}") from e
