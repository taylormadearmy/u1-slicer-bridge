"""Multi-plate 3MF parser to detect and extract individual plates.

BambuStudio/OrcaSlicer multi-plate 3MF files contain multiple <item> elements
in the <build> section, each representing a separate plate with its own
transform matrix positioning.
"""

import zipfile
import xml.etree.ElementTree as ET
import trimesh
import numpy as np
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
import logging

logger = logging.getLogger(__name__)


class PlateInfo:
    """Represents a single plate in a multi-plate 3MF file."""
    
    def __init__(self, plate_id: int, object_id: str, transform: List[float], 
                 printable: bool = True):
        self.plate_id = plate_id
        self.object_id = object_id
        self.transform = transform  # 4x4 transform matrix (16 elements)
        self.printable = printable
        
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
                object_id = item.get("objectid")
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
                    transform_values = transform_values + [0, 0, 0, 1]
                elif len(transform_values) != 16:
                    logger.warning(f"Invalid transform matrix for plate {i+1} (got {len(transform_values)} values), using identity")
                    transform_values = [1,0,0,0, 0,1,0,0, 0,0,1,0, 0,0,0,1]
                
                plate = PlateInfo(
                    plate_id=i + 1,  # 1-based plate numbering
                    object_id=object_id,
                    transform=transform_values,
                    printable=printable
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


def get_plate_bounds(file_path: Path, plate_id: Optional[int] = None) -> Dict[str, Any]:
    """
    Calculate bounds for a specific plate or all plates combined.
    
    Args:
        file_path: Path to .3mf file
        plate_id: Specific plate ID to check (None for all plates combined)
        
    Returns:
        Dictionary with bounds information
    """
    plates, is_multi_plate = parse_multi_plate_3mf(file_path)
    
    if not is_multi_plate:
        # Single plate file - load entire scene
        scene = trimesh.load(str(file_path), file_type='3mf')
        bounds = scene.bounds
        return {
            "plates": [],
            "is_multi_plate": False,
            "bounds": {
                "min": bounds[0].tolist(),
                "max": bounds[1].tolist(),
                "size": (bounds[1] - bounds[0]).tolist()
            }
        }
    
    # Multi-plate file
    if plate_id is None:
        # Combined bounds of all plates
        scene = trimesh.load(str(file_path), file_type='3mf')
        bounds = scene.bounds
        return {
            "plates": [p.to_dict() for p in plates],
            "is_multi_plate": True,
            "bounds": {
                "min": bounds[0].tolist(),
                "max": bounds[1].tolist(),
                "size": (bounds[1] - bounds[0]).tolist()
            }
        }
    else:
        # Bounds for specific plate - extract only this plate's geometry
        try:
            # Find the target plate info
            target_plate = None
            for p in plates:
                if p.plate_id == plate_id:
                    target_plate = p
                    break
            
            # Extract the specific object for this plate
            plate_mesh = _extract_plate_mesh(file_path, target_plate)
                    
            if not target_plate:
                raise ValueError(f"Plate {plate_id} not found")
            
            if plate_mesh is None:
                # Fallback to translation-based estimation
                logger.warning(f"Could not extract mesh for plate {plate_id}, using translation estimate")
                tx, ty, tz = target_plate.get_translation()
                # Use a reasonable default size for small objects
                plate_min = [tx - 40, ty - 15, tz]
                plate_max = [tx + 40, ty + 15, tz + 20]
            else:
                # Raw mesh bounds are local coordinates relative to the object's origin
                raw_bounds = plate_mesh.bounds
                
                # Get the plate's translation from the transform
                tx, ty, tz = target_plate.get_translation()
                
                # Add translation to get world-space bounds
                plate_min = [raw_bounds[0][0] + tx, raw_bounds[0][1] + ty, raw_bounds[0][2] + tz]
                plate_max = [raw_bounds[1][0] + tx, raw_bounds[1][1] + ty, raw_bounds[1][2] + tz]
            
            return {
                "plates": [target_plate.to_dict()],
                "is_multi_plate": True,
                "plate_id": plate_id,
                "bounds": {
                    "min": plate_min,
                    "max": plate_max,
                    "size": [plate_max[0] - plate_min[0], plate_max[1] - plate_min[1], plate_max[2] - plate_min[2]]
                }
            }
            
        except Exception as e:
            logger.error(f"Failed to calculate plate {plate_id} bounds: {str(e)}")
            raise ValueError(f"Could not calculate plate bounds: {str(e)}")


def _extract_plate_mesh(file_path: Path, plate: PlateInfo) -> Optional[trimesh.Trimesh]:
    """
    Extract the mesh geometry for a specific plate from a multi-plate 3MF file.
    
    Args:
        file_path: Path to .3mf file
        plate: PlateInfo object for the target plate
        
    Returns:
        trimesh.Trimesh object for the plate, or None if extraction fails
    """
    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            # Read the main model file to find the object
            model_xml = zf.read("3D/3dmodel.model")
            root = ET.fromstring(model_xml)
            
            ns = {
                "m": "http://schemas.microsoft.com/3dmanufacturing/core/2015/02",
                "p": "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"
            }
            
            # Find the object in resources
            resources = root.find("m:resources", ns)
            if resources is None:
                logger.warning("No resources section found in 3MF")
                return None
                
            # Look for the target object
            target_object_id = plate.object_id
            
            for obj in resources.findall("m:object", ns):
                if obj.get("id") == target_object_id:
                    # Check if this object has inline mesh
                    mesh = obj.find("m:mesh", ns)
                    if mesh is not None:
                        return _parse_inline_mesh(mesh, ns)
                    
                    # Check if this object has component references
                    components = obj.find("m:components", ns)
                    if components is not None:
                        return _parse_components(zf, components, ns)
                    
                    logger.warning(f"Object {target_object_id} has no mesh or components")
                    return None
                    
            logger.warning(f"Object {target_object_id} not found in resources")
            return None
            
    except Exception as e:
        logger.error(f"Failed to extract plate mesh: {str(e)}")
        return None


def _parse_inline_mesh(mesh_elem, ns: Dict[str, str]) -> Optional[trimesh.Trimesh]:
    """Parse inline mesh data from a 3MF object."""
    try:
        vertices_elem = mesh_elem.find("m:vertices", ns)
        triangles_elem = mesh_elem.find("m:triangles", ns)
        
        if vertices_elem is None or triangles_elem is None:
            return None
            
        # Extract vertices
        vertices = []
        for vertex in vertices_elem.findall("m:vertex", ns):
            x = float(vertex.get("x"))
            y = float(vertex.get("y"))
            z = float(vertex.get("z"))
            vertices.append([x, y, z])
            
        # Extract triangles (face indices)
        faces = []
        for triangle in triangles_elem.findall("m:triangle", ns):
            v1 = int(triangle.get("v1"))
            v2 = int(triangle.get("v2"))
            v3 = int(triangle.get("v3"))
            faces.append([v1, v2, v3])
            
        if len(vertices) == 0 or len(faces) == 0:
            return None
            
        # Create trimesh object
        mesh = trimesh.Trimesh(vertices=vertices, faces=faces)
        return mesh
        
    except Exception as e:
        logger.error(f"Failed to parse inline mesh: {str(e)}")
        return None


def _parse_components(zf: zipfile.ZipFile, components_elem, ns: Dict[str, str]) -> Optional[trimesh.Trimesh]:
    """Parse component references from a 3MF object."""
    try:
        meshes = []
        p_ns = ns["p"]
        
        for component in components_elem.findall("m:component", ns):
            ref_path = component.get(f"{{{p_ns}}}path")
            ref_object_id = component.get("objectid")
            
            if ref_path and ref_object_id:
                try:
                    # Clean the path and read the referenced model file
                    ref_path_clean = ref_path.lstrip("/")
                    ref_xml = zf.read(ref_path_clean)
                    ref_root = ET.fromstring(ref_xml)
                    
                    # Find the referenced object
                    ref_resources = ref_root.find("m:resources", ns)
                    if ref_resources is not None:
                        for ref_obj in ref_resources.findall("m:object", ns):
                            if ref_obj.get("id") == ref_object_id:
                                ref_mesh = ref_obj.find("m:mesh", ns)
                                if ref_mesh is not None:
                                    mesh = _parse_inline_mesh(ref_mesh, ns)
                                    if mesh is not None:
                                        meshes.append(mesh)
                                break
                                
                except (KeyError, ET.ParseError) as e:
                    logger.warning(f"Failed to load component {ref_path}: {str(e)}")
                    continue
        
        # Combine all component meshes
        if meshes:
            if len(meshes) == 1:
                return meshes[0]
            else:
                # Merge multiple meshes
                combined = meshes[0]
                for mesh in meshes[1:]:
                    combined = combined + mesh
                return combined
                
        return None
        
    except Exception as e:
        logger.error(f"Failed to parse components: {str(e)}")
        return None


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
