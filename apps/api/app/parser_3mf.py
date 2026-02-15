import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import List, Dict, Any
import json


class Object3MF:
    """Represents a 3D object extracted from a .3mf file."""

    def __init__(self, object_id: str, name: str, vertices: int = 0, triangles: int = 0):
        self.object_id = object_id
        self.name = name
        self.vertices = vertices
        self.triangles = triangles

    def to_dict(self) -> Dict[str, Any]:
        return {
            "object_id": self.object_id,
            "name": self.name,
            "vertices": self.vertices,
            "triangles": self.triangles,
        }


def parse_3mf(file_path: Path) -> List[Object3MF]:
    """
    Parse a .3mf file and extract object metadata.

    .3mf is a ZIP archive containing XML files that describe 3D models.
    Main model file is at 3D/3dmodel.model

    Supports both:
    - Inline mesh data in main model
    - Component references to external object files (MakerWorld format)
    """
    objects = []

    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            # Read the main model file
            model_xml = zf.read("3D/3dmodel.model")
            root = ET.fromstring(model_xml)

            # 3MF uses namespaces
            ns = {
                "m": "http://schemas.microsoft.com/3dmanufacturing/core/2015/02",
                "p": "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"
            }

            # Find all object elements in main model
            resources = root.find("m:resources", ns)
            if resources is not None:
                for obj in resources.findall("m:object", ns):
                    object_id = obj.get("id", "unknown")
                    name = obj.get("name", f"Object_{object_id}")
                    vertices_count = 0
                    triangles_count = 0

                    # Check if object has inline mesh data
                    mesh = obj.find("m:mesh", ns)
                    if mesh is not None:
                        # Inline mesh data
                        vertices_elem = mesh.find("m:vertices", ns)
                        if vertices_elem is not None:
                            vertices_count = len(vertices_elem.findall("m:vertex", ns))

                        triangles_elem = mesh.find("m:triangles", ns)
                        if triangles_elem is not None:
                            triangles_count = len(triangles_elem.findall("m:triangle", ns))
                    else:
                        # Check for component references (external object files)
                        components = obj.find("m:components", ns)
                        if components is not None:
                            for component in components.findall("m:component", ns):
                                # Get path to external object file using full namespace URI
                                p_ns = ns["p"]
                                ref_path = component.get(f"{{{p_ns}}}path")
                                ref_object_id = component.get("objectid")

                                if not ref_path or not ref_object_id:
                                    continue

                                # Load referenced object file
                                ref_path_clean = ref_path.lstrip("/")

                                try:
                                    ref_xml = zf.read(ref_path_clean)
                                    ref_root = ET.fromstring(ref_xml)

                                    # Find the referenced object in the external file
                                    ref_resources = ref_root.find("m:resources", ns)
                                    if ref_resources is not None:
                                        # Iterate through objects to find matching ID
                                        for ref_obj in ref_resources.findall("m:object", ns):
                                            if ref_obj.get("id") == ref_object_id:
                                                ref_mesh = ref_obj.find("m:mesh", ns)
                                                if ref_mesh is not None:
                                                    vertices_elem = ref_mesh.find("m:vertices", ns)
                                                    if vertices_elem is not None:
                                                        vertices_count = len(vertices_elem.findall("m:vertex", ns))

                                                    triangles_elem = ref_mesh.find("m:triangles", ns)
                                                    if triangles_elem is not None:
                                                        triangles_count = len(triangles_elem.findall("m:triangle", ns))

                                                    # Use the referenced object's ID and name
                                                    # (Trimesh loads the actual mesh objects, not containers)
                                                    object_id = ref_object_id
                                                    ref_name = ref_obj.get("name")
                                                    if ref_name:
                                                        name = ref_name
                                                break  # Found the object, stop searching
                                except (KeyError, ET.ParseError):
                                    # Referenced file not found or malformed, skip
                                    continue

                    # Only include objects with actual geometry
                    if vertices_count > 0:
                        objects.append(
                            Object3MF(
                                object_id=object_id,
                                name=name,
                                vertices=vertices_count,
                                triangles=triangles_count,
                            )
                        )

    except zipfile.BadZipFile:
        raise ValueError("Invalid .3mf file: not a valid ZIP archive")
    except ET.ParseError:
        raise ValueError("Invalid .3mf file: malformed XML")
    except KeyError:
        raise ValueError("Invalid .3mf file: missing 3D/3dmodel.model")

    return objects


def _extract_assigned_extruders(file_path: Path) -> List[int]:
    """Return sorted unique assigned extruder indices (1-based) from model_settings."""
    assigned: List[int] = []
    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            if "Metadata/model_settings.config" not in zf.namelist():
                return []

            root = ET.fromstring(zf.read("Metadata/model_settings.config"))
            for meta in root.findall(".//metadata"):
                if meta.get("key") != "extruder":
                    continue
                raw = (meta.get("value") or "").strip()
                if not raw:
                    continue
                try:
                    idx = int(raw)
                except ValueError:
                    continue
                if idx > 0 and idx not in assigned:
                    assigned.append(idx)
    except Exception:
        return []

    assigned.sort()
    return assigned


def detect_active_extruders_from_3mf(file_path: Path) -> List[int]:
    """Public helper for active extruder assignments used by models."""
    return _extract_assigned_extruders(file_path)


def detect_filament_count_from_3mf(file_path: Path) -> int:
    """Return the number of filament slots defined in project_settings.config.

    For single-extruder-multi-material (painted) files this may exceed the
    number of object-level extruder assignments because colour painting uses
    per-triangle filament indices rather than per-object assignments.
    """
    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            if "Metadata/project_settings.config" not in zf.namelist():
                return 0
            settings = json.loads(zf.read("Metadata/project_settings.config"))
            filament_colours = settings.get("filament_colour", [])
            if isinstance(filament_colours, list):
                return len(filament_colours)
    except Exception:
        pass
    return 0


def detect_colors_from_3mf(file_path: Path) -> List[str]:
    """
    Detect colors from a .3mf file.
    
    Looks for:
    - Metadata/filament_sequence.json (BambuStudio)
    - Metadata/project_settings.config (extruder_colour, filament_colour)
    - Metadata/extruder_colour in model metadata
    
    Returns list of unique color hex codes (e.g., ["#FF0000", "#00FF00"])
    """
    colors = []
    
    try:
        assigned_extruders = _extract_assigned_extruders(file_path)
        with zipfile.ZipFile(file_path, "r") as zf:
            # Try to read filament_sequence.json (BambuStudio format)
            try:
                seq_data = zf.read("Metadata/filament_sequence.json")
                seq = json.loads(seq_data)
                
                # Filament sequence contains color info
                # Format: {"filament_info": [{"color": "#FF0000", ...}, ...]}
                if "filament_info" in seq:
                    for filament in seq["filament_info"]:
                        color = filament.get("color", "#FFFFFF")
                        # Convert to hex if needed (Bambu may use different formats)
                        if not color.startswith("#"):
                            color = "#" + color
                        colors.append(color)
                        
                # Also check plate sequences
                for plate_name, plate_data in seq.items():
                    if isinstance(plate_data, dict) and "sequence" in plate_data:
                        for filament in plate_data["sequence"]:
                            color = filament.get("color", "#FFFFFF")
                            if not color.startswith("#"):
                                color = "#" + color
                            colors.append(color)
                            
            except KeyError:
                # filament_sequence.json not found, try other methods
                pass
            
            # Try to read from project_settings.config
            try:
                settings_data = zf.read("Metadata/project_settings.config")
                settings = json.loads(settings_data)

                # Detect single-extruder multi-material (painted) files.
                # These have one object-level extruder assignment but use
                # multiple filament colours via per-triangle paint_color.
                filament_colors = settings.get("filament_colour", [])
                is_semm = str(settings.get("single_extruder_multi_material", "0")) == "1"

                if is_semm and isinstance(filament_colors, list) and len(filament_colors) > 1:
                    # Return all defined filament colours for painted files
                    active_colors = [c for c in filament_colors if c]
                    if active_colors:
                        return active_colors

                # Prefer colors tied to actually assigned extruders when available.
                if assigned_extruders:
                    active_colors = []
                    if isinstance(filament_colors, list):
                        for ext in assigned_extruders:
                            idx = ext - 1
                            if 0 <= idx < len(filament_colors):
                                c = filament_colors[idx]
                                if c and c not in active_colors:
                                    active_colors.append(c)
                    if active_colors:
                        return active_colors
                
                # Check extruder_colour
                extruder_colors = settings.get("extruder_colour", [])
                if isinstance(extruder_colors, list):
                    for color in extruder_colors:
                        if color and color not in colors:
                            colors.append(color)
                
                # Check filament_colour  
                filament_colors = settings.get("filament_colour", [])
                if isinstance(filament_colors, list):
                    for color in filament_colors:
                        if color and color not in colors:
                            colors.append(color)
                            
            except (KeyError, ValueError):
                pass
            
            # Try to read from model metadata
            try:
                model_xml = zf.read("3D/3dmodel.model")
                root = ET.fromstring(model_xml)
                
                ns = {
                    "m": "http://schemas.microsoft.com/3dmanufacturing/core/2015/02",
                    "p": "http://schemas.microsoft.com/3dmanufacturing/production/2015/06",
                    "b": "http://schema.bambulab.com/bambustudio/2023/03"
                }
                
                # Look for metadata element
                metadata = root.find("m:metadata", ns)
                if metadata is not None:
                    # Check for extruder_colour in metadata
                    for meta in metadata.findall("m:metadataproperty", ns):
                        name = meta.get("name", "")
                        if name == "extruder_colour" or name == "b:extruder_colour":
                            value = meta.get("value", "")
                            if value:
                                # May be JSON array: ["#FF0000", "#00FF00"]
                                try:
                                    extruder_colors = json.loads(value)
                                    if isinstance(extruder_colors, list):
                                        for c in extruder_colors:
                                            if c not in colors:
                                                colors.append(c)
                                except:
                                    pass
                                    
            except (KeyError, ET.ParseError):
                pass
                
    except zipfile.BadZipFile:
        pass
    
    # Remove duplicates while preserving order
    seen = set()
    unique_colors = []
    for c in colors:
        if c not in seen:
            seen.add(c)
            unique_colors.append(c)
    
    return unique_colors
