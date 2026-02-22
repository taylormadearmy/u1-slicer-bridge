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

logger = logging.getLogger(__name__)


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
                transform_str = item.get("transform", "1 0 0 0 1 0 0 0 1 0 0 0 1")
                transform_values = [float(x) for x in transform_str.split()]
                
                # Convert 3x4 matrix (12 values) to 4x4 matrix (16 values)
                if len(transform_values) == 12:
                    # 3x4 matrix in row-major: [m00,m01,m02,m03, m10,m11,m12,m13, m20,m21,m22,m23]
                    # e.g., [1,0,0,135, 0,1,0,135, 0,0,1,10]
                    # This should become a 4x4 with identity bottom row
                    transform_values = transform_values + [0.0, 0.0, 0.0, 1.0]
                elif len(transform_values) != 16:
                    logger.warning(f"Invalid transform matrix for plate {i+1} (got {len(transform_values)} values), using identity")
                    transform_values = [1.0,0.0,0.0,0.0, 0.0,1.0,0.0,0.0, 0.0,0.0,1.0,0.0, 0.0,0.0,0.0,1.0]
                
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
        comp_transform = component.get("transform", "")
        tx, ty, tz = 0.0, 0.0, 0.0
        if comp_transform:
            vals = comp_transform.split()
            if len(vals) >= 12:
                tx, ty, tz = float(vals[9]), float(vals[10]), float(vals[11])

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
                # Apply component transform offset to bounds
                for i, offset in enumerate([tx, ty, tz]):
                    if bmin[i] + offset < combined_min[i]: combined_min[i] = bmin[i] + offset
                    if bmax[i] + offset > combined_max[i]: combined_max[i] = bmax[i] + offset
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

            # Apply item transform (translation component)
            transform_str = item.get("transform", "1 0 0 0 1 0 0 0 1 0 0 0")
            transform_values = [float(x) for x in transform_str.split()]
            if len(transform_values) >= 12:
                tx, ty, tz = transform_values[9], transform_values[10], transform_values[11]
            else:
                tx = ty = tz = 0.0

            for i, (lo, hi, t) in enumerate(zip(bmin, bmax, [tx, ty, tz])):
                val_min = lo + t
                val_max = hi + t
                if val_min < global_min[i]: global_min[i] = val_min
                if val_max > global_max[i]: global_max[i] = val_max
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
