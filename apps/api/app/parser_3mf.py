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


def detect_layer_tool_changes(file_path: Path) -> Dict[int, List[Dict[str, Any]]]:
    """Detect per-plate layer-based tool changes from custom_gcode_per_layer.xml.

    Bambu's ``MultiAsSingle`` mode stores mid-print filament swaps as
    ``<layer type="2" .../>`` entries (type 2 = ToolChange in OrcaSlicer).
    OrcaSlicer emits real T-commands for these, so on the U1 they become
    dual-extruder prints.

    Returns a dict mapping plate_id (1-based) to a list of tool-change dicts::

        {1: [{"z": 13.6, "extruder": 2, "color": "#2850E0"}]}

    Returns an empty dict when no tool changes are found.
    """
    result: Dict[int, List[Dict[str, Any]]] = {}
    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            if "Metadata/custom_gcode_per_layer.xml" not in zf.namelist():
                return result
            root = ET.fromstring(zf.read("Metadata/custom_gcode_per_layer.xml"))
            for plate_el in root.findall("plate"):
                plate_id = 0
                info = plate_el.find("plate_info")
                if info is not None:
                    try:
                        plate_id = int(info.get("id", "0"))
                    except ValueError:
                        pass
                changes: List[Dict[str, Any]] = []
                for layer in plate_el.findall("layer"):
                    ltype = layer.get("type", "")
                    # type="2" = ToolChange
                    if ltype != "2":
                        continue
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
    except Exception:
        pass
    return result


def _has_paint_data(zf: zipfile.ZipFile) -> bool:
    """Check whether any .model file in the archive contains paint_color attributes.

    Scans model files in chunks.  Paint data lives on ``<triangle>`` elements
    which appear *after* ``<vertices>`` — so for large meshes the marker can be
    several MB into the file.  We cap the scan at 32 MB per file to stay fast.
    """
    MAX_SCAN = 32 * 1024 * 1024  # 32 MB
    CHUNK = 1024 * 1024          # 1 MB reads
    needle = b"paint_color"
    for name in zf.namelist():
        if not name.endswith(".model"):
            continue
        try:
            scanned = 0
            with zf.open(name) as f:
                while scanned < MAX_SCAN:
                    chunk = f.read(CHUNK)
                    if not chunk:
                        break
                    if needle in chunk:
                        return True
                    scanned += len(chunk)
        except Exception:
            continue
    return False


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
                # IMPORTANT: single_extruder_multi_material alone is not enough —
                # it may just be the machine AMS config.  We also require actual
                # paint_color data in the mesh to confirm painting is used.
                filament_colors = settings.get("filament_colour", [])
                is_semm = str(settings.get("single_extruder_multi_material", "0")) == "1"

                if is_semm and isinstance(filament_colors, list) and len(filament_colors) > 1:
                    if _has_paint_data(zf):
                        active_colors = [c for c in filament_colors if c]
                        if active_colors:
                            return active_colors

                # Detect layer-based tool changes (MultiAsSingle dual-colour).
                # These specify extruder indices that map into filament_colour.
                layer_changes = detect_layer_tool_changes(file_path)
                if layer_changes and isinstance(filament_colors, list):
                    # Collect all extruder indices referenced across all plates
                    ext_indices: set = {1}  # base extruder is always 1
                    for changes in layer_changes.values():
                        for ch in changes:
                            ext_indices.add(ch.get("extruder", 1))
                    active_colors = []
                    for ext in sorted(ext_indices):
                        idx = ext - 1
                        if 0 <= idx < len(filament_colors):
                            c = filament_colors[idx]
                            if c and c not in active_colors:
                                active_colors.append(c)
                    if len(active_colors) > 1:
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
                                except (json.JSONDecodeError, ValueError):
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


def detect_colors_per_plate(file_path: Path) -> Dict[int, List[str]]:
    """Detect colors used by each plate in a multi-plate 3MF.

    Returns a dict mapping plate_id (1-based) to a list of color hex codes.
    Empty dict if per-plate detection is not possible.
    """
    result: Dict[int, List[str]] = {}
    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            # Get filament colour palette from project_settings
            filament_colors: List[str] = []
            try:
                settings = json.loads(zf.read("Metadata/project_settings.config"))
                filament_colors = settings.get("filament_colour", [])
                if not isinstance(filament_colors, list):
                    filament_colors = []
            except (KeyError, ValueError):
                pass

            if not filament_colors:
                return result

            # Source 1: Layer tool changes (per-plate extruder indices)
            layer_changes = detect_layer_tool_changes(file_path)
            if layer_changes:
                for plate_id, changes in layer_changes.items():
                    ext_indices = {1}
                    for ch in changes:
                        ext_indices.add(ch.get("extruder", 1))
                    plate_colors = []
                    for ext in sorted(ext_indices):
                        idx = ext - 1
                        if 0 <= idx < len(filament_colors):
                            c = filament_colors[idx]
                            if c and c not in plate_colors:
                                plate_colors.append(c)
                    if plate_colors:
                        result[plate_id] = plate_colors

            # Source 2: Per-object extruder from model_settings.config
            # Each <object id="N"> has <metadata key="extruder" value="M"/>
            # Build items map plate_id → object_id, so we can get per-plate extruder.
            if not result:
                try:
                    if "Metadata/model_settings.config" in zf.namelist():
                        ms_root = ET.fromstring(zf.read("Metadata/model_settings.config"))
                        obj_extruders: Dict[str, set] = {}
                        for obj_elem in ms_root.findall("object"):
                            oid = obj_elem.get("id")
                            if not oid:
                                continue
                            exts: set = set()
                            # Object-level extruder
                            ext_meta = obj_elem.find("metadata[@key='extruder']")
                            if ext_meta is not None:
                                try:
                                    exts.add(int(ext_meta.get("value", "1")))
                                except ValueError:
                                    pass
                            # Part-level extruders
                            for part in obj_elem.findall("part"):
                                part_ext = part.find("metadata[@key='extruder']")
                                if part_ext is not None:
                                    try:
                                        exts.add(int(part_ext.get("value", "1")))
                                    except ValueError:
                                        pass
                            if exts:
                                obj_extruders[oid] = exts

                        # Map plates to objects via 3MF build items
                        model_xml = zf.read("3D/3dmodel.model")
                        root = ET.fromstring(model_xml)
                        mns = {"m": "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"}
                        build = root.find("m:build", mns)
                        if build is not None:
                            for i, item in enumerate(build.findall("m:item", mns)):
                                plate_id = i + 1
                                obj_id = item.get("objectid", "")
                                exts = obj_extruders.get(obj_id, set())
                                if exts:
                                    plate_colors = []
                                    for ext in sorted(exts):
                                        idx = ext - 1
                                        if 0 <= idx < len(filament_colors):
                                            c = filament_colors[idx]
                                            if c and c not in plate_colors:
                                                plate_colors.append(c)
                                    if plate_colors:
                                        result[plate_id] = plate_colors
                except Exception:
                    pass

    except Exception:
        pass
    return result


def detect_print_settings(file_path: Path) -> Dict[str, Any]:
    """Extract support/brim print settings from a 3MF's project_settings.config.

    Returns a dict with normalised values.  Empty dict when nothing found.
    Keys use OrcaSlicer config names so they can round-trip straight through
    the override pipeline.
    """
    settings: Dict[str, Any] = {}
    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            if "Metadata/project_settings.config" not in zf.namelist():
                return settings
            config = json.loads(zf.read("Metadata/project_settings.config"))

            # --- support ---
            raw = config.get("enable_support")
            if raw is not None:
                settings["enable_support"] = str(raw).strip() in ("1", "true", "True")

            raw = config.get("support_type")
            if raw is not None and str(raw).strip():
                settings["support_type"] = str(raw).strip()

            raw = config.get("support_threshold_angle")
            if raw is not None:
                try:
                    settings["support_threshold_angle"] = int(float(str(raw)))
                except (ValueError, TypeError):
                    pass

            # --- brim ---
            raw = config.get("brim_type")
            if raw is not None and str(raw).strip():
                settings["brim_type"] = str(raw).strip()

            raw = config.get("brim_width")
            if raw is not None:
                try:
                    settings["brim_width"] = round(float(str(raw)), 2)
                except (ValueError, TypeError):
                    pass

            raw = config.get("brim_object_gap")
            if raw is not None:
                try:
                    settings["brim_object_gap"] = round(float(str(raw)), 2)
                except (ValueError, TypeError):
                    pass

    except Exception:
        pass
    return settings
