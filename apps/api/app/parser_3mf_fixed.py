import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import List, Dict, Any


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

                                                    # Use the referenced object's name if available
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
