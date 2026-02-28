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
                        # Inline mesh data — count with iter() to avoid building huge lists
                        vertices_elem = mesh.find("m:vertices", ns)
                        if vertices_elem is not None:
                            vertices_count = sum(1 for _ in vertices_elem.iter(f"{{{ns['m']}}}vertex"))

                        triangles_elem = mesh.find("m:triangles", ns)
                        if triangles_elem is not None:
                            triangles_count = sum(1 for _ in triangles_elem.iter(f"{{{ns['m']}}}triangle"))
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
                                                        vertices_count = sum(1 for _ in vertices_elem.iter(f"{{{ns['m']}}}vertex"))

                                                    triangles_elem = ref_mesh.find("m:triangles", ns)
                                                    if triangles_elem is not None:
                                                        triangles_count = sum(1 for _ in triangles_elem.iter(f"{{{ns['m']}}}triangle"))

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
    """Check whether any .model file in the archive contains paint data.

    Bambu uses ``paint_color`` attributes; PrusaSlicer uses
    ``mmu_segmentation`` attributes on ``<triangle>`` elements.
    Scans in chunks, capped at 32 MB per file.
    """
    MAX_SCAN = 32 * 1024 * 1024  # 32 MB
    CHUNK = 1024 * 1024          # 1 MB reads
    needles = (b"paint_color", b"mmu_segmentation")
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
                    for needle in needles:
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

                # Detect painted multicolor files.  These use per-triangle
                # paint_color / mmu_segmentation attributes.  During slicing,
                # mmu_segmentation is converted to paint_color for OrcaSlicer.
                filament_colors = settings.get("filament_colour", [])

                if isinstance(filament_colors, list) and len(filament_colors) > 1:
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

            # Try PrusaSlicer format: check Slic3r_PE_model.config for
            # per-object extruder assignments, then map to extruder_colour
            # from Slic3r_PE.config.  extruder_colour alone is just the
            # printer's tool-head colours (not per-model assignments).
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
                    if _has_paint_data(zf):
                        return pe_extruder_colours

                    try:
                        model_cfg = zf.read("Metadata/Slic3r_PE_model.config").decode("utf-8", errors="replace")
                        model_cfg_root = ET.fromstring(model_cfg)
                        assigned_extruders: set = set()
                        for meta in model_cfg_root.findall('.//metadata'):
                            if meta.get('key') == 'extruder' and meta.get('type') == 'object':
                                raw = (meta.get('value') or '').strip()
                                if raw:
                                    try:
                                        assigned_extruders.add(int(raw))
                                    except ValueError:
                                        pass
                        if len(assigned_extruders) > 1:
                            return _extruders_to_colors(assigned_extruders, pe_extruder_colours)
                    except (KeyError, ET.ParseError):
                        pass
            except KeyError:
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


def _extruders_to_colors(ext_indices: set, filament_colors: List[str]) -> List[str]:
    """Map a set of 1-based extruder indices to color hex strings."""
    colors: List[str] = []
    for ext in sorted(ext_indices):
        idx = ext - 1
        if 0 <= idx < len(filament_colors):
            c = filament_colors[idx]
            if c and c not in colors:
                colors.append(c)
    return colors


def detect_colors_per_plate(file_path: Path) -> Dict[int, List[str]]:
    """Detect colors used by each plate in a multi-plate 3MF.

    Returns a dict mapping plate_id (1-based) to a list of color hex codes.
    Empty dict if per-plate detection is not possible.

    Uses three sources and merges them:
    - Source 1 (layer tool changes): Correct for filament-swap plates
      (MultiAsSingle mode) but may overcount for H2D plates.
    - Source 2 (model_settings per-object/part extruders): Correct for
      H2D plates with part-level extruder assignments but misses
      filament-swap plates.
    - Source 3 (plate_N.json wipe tower): If a plate's cached slice
      metadata contains a wipe_tower, the plate is multi-color even when
      Sources 1+2 miss it (e.g. Bambu model_settings inconsistency).

    Strategy: prefer Source 2 when it finds >=2 colors (H2D), otherwise
    prefer Source 1 (filament swap), otherwise fall back to Source 2.
    Source 3 upgrades any single-color result to the first N filament
    colours (N = number of extruders).
    """
    result: Dict[int, List[str]] = {}
    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            # Get filament colour palette from project_settings
            filament_colors: List[str] = []
            num_extruders = 2
            try:
                settings = json.loads(zf.read("Metadata/project_settings.config"))
                filament_colors = settings.get("filament_colour", [])
                if not isinstance(filament_colors, list):
                    filament_colors = []
                ext_colours = settings.get("extruder_colour", [])
                if isinstance(ext_colours, list) and len(ext_colours) >= 2:
                    num_extruders = len(ext_colours)
            except (KeyError, ValueError):
                pass

            if not filament_colors:
                return result

            # Source 1: Layer tool changes (per-plate extruder indices)
            source1: Dict[int, List[str]] = {}
            layer_changes = detect_layer_tool_changes(file_path)
            if layer_changes:
                for plate_id, changes in layer_changes.items():
                    ext_indices = {1}
                    for ch in changes:
                        ext_indices.add(ch.get("extruder", 1))
                    plate_colors = _extruders_to_colors(ext_indices, filament_colors)
                    if plate_colors:
                        source1[plate_id] = plate_colors

            # Source 2: Per-object/part extruder from model_settings.config
            source2: Dict[int, List[str]] = {}
            try:
                if "Metadata/model_settings.config" in zf.namelist():
                    ms_root = ET.fromstring(zf.read("Metadata/model_settings.config"))
                    obj_extruders: Dict[str, set] = {}
                    for obj_elem in ms_root.findall("object"):
                        oid = obj_elem.get("id")
                        if not oid:
                            continue
                        exts: set = set()
                        ext_meta = obj_elem.find("metadata[@key='extruder']")
                        if ext_meta is not None:
                            try:
                                exts.add(int(ext_meta.get("value", "1")))
                            except ValueError:
                                pass
                        for part in obj_elem.findall("part"):
                            part_ext = part.find("metadata[@key='extruder']")
                            if part_ext is not None:
                                try:
                                    exts.add(int(part_ext.get("value", "1")))
                                except ValueError:
                                    pass
                        if exts:
                            obj_extruders[oid] = exts

                    model_xml = zf.read("3D/3dmodel.model")
                    root = ET.fromstring(model_xml)
                    mns = {"m": "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"}
                    build = root.find("m:build", mns)
                    if build is not None:
                        for i, item in enumerate(build.findall("m:item", mns)):
                            plate_id = i + 1
                            obj_id = item.get("objectid", "")
                            exts = obj_extruders.get(obj_id, {1})
                            plate_colors = _extruders_to_colors(exts, filament_colors)
                            if plate_colors:
                                source2[plate_id] = plate_colors
            except Exception:
                pass

            # Source 3: Wipe tower presence in plate_N.json (Bambu slice cache).
            # A wipe tower means the plate was sliced as multi-color,
            # even if model_settings extruder data is inconsistent.
            wipe_tower_plates: set = set()
            for name in zf.namelist():
                if not name.startswith("Metadata/plate_") or not name.endswith(".json"):
                    continue
                try:
                    plate_data = json.loads(zf.read(name))
                    bbox_objects = plate_data.get("bbox_objects", [])
                    has_wipe = any(o.get("name") == "wipe_tower" for o in bbox_objects)
                    if has_wipe:
                        # Extract plate number from filename (plate_2.json -> 2)
                        num_str = name.split("plate_")[1].split(".")[0]
                        wipe_tower_plates.add(int(num_str))
                except Exception:
                    pass

            # Merge: collect all plate IDs from all sources
            all_plate_ids = set(source1.keys()) | set(source2.keys()) | wipe_tower_plates
            for pid in all_plate_ids:
                s1 = source1.get(pid, [])
                s2 = source2.get(pid, [])
                # Prefer Source 2 when it finds multi-color (H2D with
                # explicit part-level extruder assignments).  Otherwise
                # prefer Source 1 (filament-swap via tool changes).
                if len(s2) >= 2:
                    result[pid] = s2
                elif len(s1) >= 2:
                    result[pid] = s1
                elif s2:
                    result[pid] = s2
                elif s1:
                    result[pid] = s1

                # Source 3: if wipe tower detected but we only found 1 color,
                # upgrade to the first N filament colors (N = num extruders).
                if pid in wipe_tower_plates and len(result.get(pid, [])) < 2:
                    multi = _extruders_to_colors(set(range(1, num_extruders + 1)), filament_colors)
                    if len(multi) >= 2:
                        result[pid] = multi

    except Exception:
        pass
    return result


def detect_print_settings(file_path: Path) -> Dict[str, Any]:
    """Extract print settings from a 3MF's project_settings.config.

    Returns a dict with normalised values.  Empty dict when nothing found.
    Keys use OrcaSlicer config names so they can round-trip straight through
    the override pipeline.
    """
    settings: Dict[str, Any] = {}

    def _as_bool(val) -> bool:
        return str(val).strip() in ("1", "true", "True")

    def _as_int(val):
        try:
            return int(float(str(val)))
        except (ValueError, TypeError):
            return None

    def _as_float(val, decimals=2):
        try:
            return round(float(str(val)), decimals)
        except (ValueError, TypeError):
            return None

    def _first_element(val):
        """Return first element if list/array, else the value itself."""
        if isinstance(val, list) and val:
            return val[0]
        return val

    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            if "Metadata/project_settings.config" not in zf.namelist():
                return settings
            config = json.loads(zf.read("Metadata/project_settings.config"))

            # --- support ---
            raw = config.get("enable_support")
            if raw is not None:
                settings["enable_support"] = _as_bool(raw)

            raw = config.get("support_type")
            if raw is not None and str(raw).strip():
                settings["support_type"] = str(raw).strip()

            raw = config.get("support_threshold_angle")
            if raw is not None:
                v = _as_int(raw)
                if v is not None:
                    settings["support_threshold_angle"] = v

            # --- brim ---
            raw = config.get("brim_type")
            if raw is not None and str(raw).strip():
                settings["brim_type"] = str(raw).strip()

            raw = config.get("brim_width")
            if raw is not None:
                v = _as_float(raw)
                if v is not None:
                    settings["brim_width"] = v

            raw = config.get("brim_object_gap")
            if raw is not None:
                v = _as_float(raw)
                if v is not None:
                    settings["brim_object_gap"] = v

            # --- skirt ---
            raw = config.get("skirt_loops")
            if raw is not None:
                v = _as_int(raw)
                if v is not None:
                    settings["skirt_loops"] = v

            raw = config.get("skirt_distance")
            if raw is not None:
                v = _as_float(raw)
                if v is not None:
                    settings["skirt_distance"] = v

            raw = config.get("skirt_height")
            if raw is not None:
                v = _as_int(raw)
                if v is not None:
                    settings["skirt_height"] = v

            # --- wall / infill / layer ---
            raw = config.get("wall_loops")
            if raw is not None:
                v = _as_int(raw)
                if v is not None:
                    settings["wall_loops"] = v

            raw = config.get("sparse_infill_density")
            if raw is not None:
                v = _as_int(str(raw).replace("%", ""))
                if v is not None:
                    settings["sparse_infill_density"] = v

            raw = config.get("sparse_infill_pattern")
            if raw is not None and str(raw).strip():
                settings["sparse_infill_pattern"] = str(raw).strip()

            raw = config.get("layer_height")
            if raw is not None:
                v = _as_float(raw)
                if v is not None:
                    settings["layer_height"] = v

            # --- prime tower ---
            raw = config.get("enable_prime_tower")
            if raw is not None:
                settings["enable_prime_tower"] = _as_bool(raw)

            raw = config.get("prime_tower_width")
            if raw is not None:
                v = _as_int(raw)
                if v is not None:
                    settings["prime_tower_width"] = v

            raw = config.get("prime_tower_brim_width")
            if raw is not None:
                v = _as_int(raw)
                if v is not None and v >= 0:
                    settings["prime_tower_brim_width"] = v

            raw = config.get("filament_prime_volume")
            if raw is not None:
                v = _as_int(_first_element(raw))
                if v is not None:
                    settings["prime_volume"] = v

            # --- temperature / bed ---
            raw = config.get("nozzle_temperature")
            if raw is not None:
                v = _as_int(_first_element(raw))
                if v is not None:
                    settings["nozzle_temperature"] = v

            raw = config.get("bed_temperature")
            if raw is not None:
                v = _as_int(_first_element(raw))
                if v is not None:
                    settings["bed_temperature"] = v

            raw = config.get("curr_bed_type")
            if raw is not None and str(raw).strip():
                settings["curr_bed_type"] = str(raw).strip()

    except Exception:
        pass
    return settings


def extract_3mf_metadata_batch(file_path: Path) -> Dict[str, Any]:
    """Single-pass metadata extraction from a 3MF file.

    Opens the ZIP once and extracts all metadata used by the slice pipeline,
    avoiding multiple independent ZIP opens.

    Returns dict with:
        active_extruders: sorted unique assigned extruder indices (1-based)
        is_bambu: whether this is a Bambu Studio file
        has_multi_extruder_assignments: whether model has >1 extruder assignment
        has_layer_tool_changes: whether custom_gcode_per_layer.xml has ToolChange entries
    """
    result: Dict[str, Any] = {
        "active_extruders": [],
        "is_bambu": False,
        "has_multi_extruder_assignments": False,
        "has_layer_tool_changes": False,
    }

    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            names = set(zf.namelist())

            # Bambu detection (same check as ProfileEmbedder._is_bambu_file)
            bambu_markers = {
                'Metadata/model_settings.config',
                'Metadata/slice_info.config',
                'Metadata/filament_sequence.json'
            }
            result["is_bambu"] = bool(bambu_markers & names)

            # Extruder assignments from model_settings.config
            if 'Metadata/model_settings.config' in names:
                root = ET.fromstring(zf.read('Metadata/model_settings.config'))
                extruders = set()
                for meta in root.findall('.//metadata'):
                    if meta.get('key') == 'extruder':
                        raw = (meta.get('value') or '').strip()
                        if raw:
                            try:
                                idx = int(raw)
                                if idx > 0:
                                    extruders.add(idx)
                            except ValueError:
                                pass
                result["active_extruders"] = sorted(extruders)
                result["has_multi_extruder_assignments"] = len(extruders) > 1

            # Layer tool changes from custom_gcode_per_layer.xml
            if 'Metadata/custom_gcode_per_layer.xml' in names:
                root = ET.fromstring(zf.read('Metadata/custom_gcode_per_layer.xml'))
                for layer in root.findall('.//layer'):
                    if layer.get('type') == '2':
                        result["has_layer_tool_changes"] = True
                        break
    except Exception:
        pass

    return result


import re as _re_module  # for preview asset indexing


def extract_upload_metadata(file_path: Path) -> Dict[str, Any]:
    """Single-pass extraction of ALL upload-processing metadata from a 3MF.

    Opens the ZIP once and gathers everything that _process_3mf_sync needs:
    colors, per-plate colors, print settings, preview asset index, and the
    Bambu Z-offset artifact flag.  Replaces 6+ separate function calls that
    each opened the ZIP independently.

    Returns dict with:
        detected_colors: List[str]          — hex color codes
        colors_per_plate: Dict[int, List[str]]
        print_settings: Dict[str, Any]
        preview_assets: {"by_plate": {...}, "best": str|None}
        has_bambu_z_offset: bool            — source_offset_z present in model_settings
    """
    result: Dict[str, Any] = {
        "detected_colors": [],
        "colors_per_plate": {},
        "print_settings": {},
        "preview_assets": {"by_plate": {}, "best": None},
        "has_bambu_z_offset": False,
    }

    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            names = set(zf.namelist())

            # ── Shared data loaded once ────────────────────────────
            project_settings = None
            if "Metadata/project_settings.config" in names:
                try:
                    project_settings = json.loads(zf.read("Metadata/project_settings.config"))
                except (ValueError, KeyError):
                    pass

            model_settings_root = None
            has_source_offset_z = False
            if "Metadata/model_settings.config" in names:
                try:
                    model_settings_root = ET.fromstring(zf.read("Metadata/model_settings.config"))
                    has_source_offset_z = any(
                        m.get("key") == "source_offset_z"
                        for m in model_settings_root.findall(".//metadata")
                    )
                except ET.ParseError:
                    pass

            result["has_bambu_z_offset"] = (
                has_source_offset_z
                and project_settings is not None
            )

            # ── Assigned extruders (from model_settings) ──────────
            assigned_extruders: List[int] = []
            if model_settings_root is not None:
                exts_set: set = set()
                for meta in model_settings_root.findall('.//metadata'):
                    if meta.get('key') == 'extruder':
                        raw = (meta.get('value') or '').strip()
                        if raw:
                            try:
                                idx = int(raw)
                                if idx > 0:
                                    exts_set.add(idx)
                            except ValueError:
                                pass
                assigned_extruders = sorted(exts_set)

            # ── Layer tool changes ────────────────────────────────
            layer_changes: Dict[int, List[Dict[str, Any]]] = {}
            if 'Metadata/custom_gcode_per_layer.xml' in names:
                try:
                    cg_root = ET.fromstring(zf.read('Metadata/custom_gcode_per_layer.xml'))
                    for plate_el in cg_root.findall("plate"):
                        p_id = 0
                        info = plate_el.find("plate_info")
                        if info is not None:
                            try:
                                p_id = int(info.get("id", "0"))
                            except ValueError:
                                pass
                        changes: List[Dict[str, Any]] = []
                        for layer in plate_el.findall("layer"):
                            if layer.get("type") != "2":
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
                        if changes and p_id > 0:
                            layer_changes[p_id] = changes
                except ET.ParseError:
                    pass

            # ── Paint data detection (chunked scan) ───────────────
            has_paint = False
            MAX_SCAN = 32 * 1024 * 1024
            CHUNK = 1024 * 1024
            needles = (b"paint_color", b"mmu_segmentation")
            for name in names:
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
                                    has_paint = True
                                    break
                            if has_paint:
                                break
                            scanned += len(chunk)
                except Exception:
                    pass
                if has_paint:
                    break

            # ── Filament colours from project_settings ────────────
            filament_colors: List[str] = []
            if project_settings:
                fc = project_settings.get("filament_colour", [])
                if isinstance(fc, list):
                    filament_colors = fc

            # ── Detect colors (consolidated from detect_colors_from_3mf) ──
            detected_colors: List[str] = []

            # Try filament_sequence.json first
            if 'Metadata/filament_sequence.json' in names:
                try:
                    seq = json.loads(zf.read('Metadata/filament_sequence.json'))
                    if "filament_info" in seq:
                        for filament in seq["filament_info"]:
                            color = filament.get("color", "#FFFFFF")
                            if not color.startswith("#"):
                                color = "#" + color
                            detected_colors.append(color)
                    for plate_name, plate_data in seq.items():
                        if isinstance(plate_data, dict) and "sequence" in plate_data:
                            for filament in plate_data["sequence"]:
                                color = filament.get("color", "#FFFFFF")
                                if not color.startswith("#"):
                                    color = "#" + color
                                detected_colors.append(color)
                except (KeyError, ValueError):
                    pass

            # Paint data → return all filament colors
            if filament_colors and len(filament_colors) > 1 and has_paint:
                active = [c for c in filament_colors if c]
                if active:
                    detected_colors = active

            # Layer tool changes → map used extruders to colors
            elif layer_changes and filament_colors:
                ext_indices: set = {1}
                for changes in layer_changes.values():
                    for ch in changes:
                        ext_indices.add(ch.get("extruder", 1))
                active = _extruders_to_colors(ext_indices, filament_colors)
                if len(active) > 1:
                    detected_colors = active

            # Assigned extruders → map to colors
            elif assigned_extruders and filament_colors:
                active = []
                for ext in assigned_extruders:
                    idx = ext - 1
                    if 0 <= idx < len(filament_colors):
                        c = filament_colors[idx]
                        if c and c not in active:
                            active.append(c)
                if active:
                    detected_colors = active

            # Fallback: extruder_colour / filament_colour from project_settings
            if not detected_colors and project_settings:
                for key in ("extruder_colour", "filament_colour"):
                    vals = project_settings.get(key, [])
                    if isinstance(vals, list):
                        for c in vals:
                            if c and c not in detected_colors:
                                detected_colors.append(c)

            # PrusaSlicer fallback
            if not detected_colors:
                try:
                    pe_data = zf.read("Metadata/Slic3r_PE.config").decode("utf-8", errors="replace")
                    pe_extruder_colours: List[str] = []
                    for line in pe_data.splitlines():
                        stripped = line.lstrip("; ").strip()
                        if stripped.startswith("extruder_colour"):
                            _, _, value = stripped.partition("=")
                            value = value.strip()
                            if value:
                                pe_extruder_colours = [c.strip() for c in value.split(";") if c.strip()]
                            break
                    if pe_extruder_colours:
                        if has_paint:
                            detected_colors = pe_extruder_colours
                        else:
                            try:
                                model_cfg = zf.read("Metadata/Slic3r_PE_model.config").decode("utf-8", errors="replace")
                                model_cfg_root = ET.fromstring(model_cfg)
                                pe_assigned: set = set()
                                for meta in model_cfg_root.findall('.//metadata'):
                                    if meta.get('key') == 'extruder' and meta.get('type') == 'object':
                                        raw = (meta.get('value') or '').strip()
                                        if raw:
                                            try:
                                                pe_assigned.add(int(raw))
                                            except ValueError:
                                                pass
                                if len(pe_assigned) > 1:
                                    detected_colors = _extruders_to_colors(pe_assigned, pe_extruder_colours)
                            except (KeyError, ET.ParseError):
                                pass
                except KeyError:
                    pass

            # Deduplicate
            seen: set = set()
            unique: List[str] = []
            for c in detected_colors:
                if c not in seen:
                    seen.add(c)
                    unique.append(c)
            result["detected_colors"] = unique

            # ── Per-plate colors (consolidated from detect_colors_per_plate) ──
            colors_per_plate: Dict[int, List[str]] = {}
            if filament_colors:
                num_extruders = 2
                if project_settings:
                    ext_colours = project_settings.get("extruder_colour", [])
                    if isinstance(ext_colours, list) and len(ext_colours) >= 2:
                        num_extruders = len(ext_colours)

                # Source 1: Layer tool changes
                source1: Dict[int, List[str]] = {}
                if layer_changes:
                    for pid, changes in layer_changes.items():
                        ext_indices = {1}
                        for ch in changes:
                            ext_indices.add(ch.get("extruder", 1))
                        plate_colors = _extruders_to_colors(ext_indices, filament_colors)
                        if plate_colors:
                            source1[pid] = plate_colors

                # Source 2: Per-object/part extruder from model_settings
                source2: Dict[int, List[str]] = {}
                if model_settings_root is not None:
                    obj_extruders: Dict[str, set] = {}
                    for obj_elem in model_settings_root.findall("object"):
                        oid = obj_elem.get("id")
                        if not oid:
                            continue
                        exts: set = set()
                        ext_meta = obj_elem.find("metadata[@key='extruder']")
                        if ext_meta is not None:
                            try:
                                exts.add(int(ext_meta.get("value", "1")))
                            except ValueError:
                                pass
                        for part in obj_elem.findall("part"):
                            part_ext = part.find("metadata[@key='extruder']")
                            if part_ext is not None:
                                try:
                                    exts.add(int(part_ext.get("value", "1")))
                                except ValueError:
                                    pass
                        if exts:
                            obj_extruders[oid] = exts

                    # Map objects to plates via build items
                    try:
                        model_xml = zf.read("3D/3dmodel.model")
                        root = ET.fromstring(model_xml)
                        mns = {"m": "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"}
                        build = root.find("m:build", mns)
                        if build is not None:
                            for i, item in enumerate(build.findall("m:item", mns)):
                                pid = i + 1
                                obj_id = item.get("objectid", "")
                                exts = obj_extruders.get(obj_id, {1})
                                plate_colors = _extruders_to_colors(exts, filament_colors)
                                if plate_colors:
                                    source2[pid] = plate_colors
                    except (KeyError, ET.ParseError):
                        pass

                # Source 3: Wipe tower in plate_N.json
                wipe_tower_plates: set = set()
                for name in names:
                    if not name.startswith("Metadata/plate_") or not name.endswith(".json"):
                        continue
                    try:
                        plate_data = json.loads(zf.read(name))
                        bbox_objects = plate_data.get("bbox_objects", [])
                        has_wipe = any(o.get("name") == "wipe_tower" for o in bbox_objects)
                        if has_wipe:
                            num_str = name.split("plate_")[1].split(".")[0]
                            wipe_tower_plates.add(int(num_str))
                    except Exception:
                        pass

                # Merge sources
                all_plate_ids = set(source1.keys()) | set(source2.keys()) | wipe_tower_plates
                for pid in all_plate_ids:
                    s1 = source1.get(pid, [])
                    s2 = source2.get(pid, [])
                    if len(s2) >= 2:
                        colors_per_plate[pid] = s2
                    elif len(s1) >= 2:
                        colors_per_plate[pid] = s1
                    elif s2:
                        colors_per_plate[pid] = s2
                    elif s1:
                        colors_per_plate[pid] = s1

                    if pid in wipe_tower_plates and len(colors_per_plate.get(pid, [])) < 2:
                        multi = _extruders_to_colors(set(range(1, num_extruders + 1)), filament_colors)
                        if len(multi) >= 2:
                            colors_per_plate[pid] = multi

            result["colors_per_plate"] = colors_per_plate

            # ── Print settings (consolidated from detect_print_settings) ──
            if project_settings:
                settings = _extract_print_settings_from_config(project_settings)
                result["print_settings"] = settings

            # ── Preview assets (consolidated from _index_preview_assets) ──
            preview_map: Dict[int, str] = {}
            best_preview = None
            image_names = [
                n for n in names
                if n.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))
                and "/metadata/" in f"/{n.lower()}"
            ]
            for img_name in image_names:
                lower = img_name.lower()
                match = _re_module.search(r"(?:plate|top|pick|thumbnail|preview|cover)[_\-]?(\d+)", lower)
                if not match:
                    match = _re_module.search(r"[_\-/](\d+)\.(?:png|jpg|jpeg|webp)$", lower)
                if not match:
                    continue
                pid = int(match.group(1))
                if pid not in preview_map:
                    preview_map[pid] = img_name

            if image_names:
                def _score(path: str):
                    p = path.lower()
                    for i, kw in enumerate(["thumbnail", "preview", "cover", "top", "plate", "pick"]):
                        if kw in p:
                            return (i, len(p))
                    return (9, len(p))
                best_preview = sorted(image_names, key=_score)[0]

            result["preview_assets"] = {"by_plate": preview_map, "best": best_preview}

    except Exception:
        pass

    return result


def _extract_print_settings_from_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """Extract print settings from a parsed project_settings.config dict.

    Pure function — no file I/O.  Shared by both extract_upload_metadata()
    and the standalone detect_print_settings().
    """
    settings: Dict[str, Any] = {}

    def _as_bool(val) -> bool:
        return str(val).strip() in ("1", "true", "True")

    def _as_int(val):
        try:
            return int(float(str(val)))
        except (ValueError, TypeError):
            return None

    def _as_float(val, decimals=2):
        try:
            return round(float(str(val)), decimals)
        except (ValueError, TypeError):
            return None

    def _first_element(val):
        if isinstance(val, list) and val:
            return val[0]
        return val

    # support
    raw = config.get("enable_support")
    if raw is not None:
        settings["enable_support"] = _as_bool(raw)
    raw = config.get("support_type")
    if raw is not None and str(raw).strip():
        settings["support_type"] = str(raw).strip()
    raw = config.get("support_threshold_angle")
    if raw is not None:
        v = _as_int(raw)
        if v is not None:
            settings["support_threshold_angle"] = v

    # brim
    raw = config.get("brim_type")
    if raw is not None and str(raw).strip():
        settings["brim_type"] = str(raw).strip()
    raw = config.get("brim_width")
    if raw is not None:
        v = _as_float(raw)
        if v is not None:
            settings["brim_width"] = v
    raw = config.get("brim_object_gap")
    if raw is not None:
        v = _as_float(raw)
        if v is not None:
            settings["brim_object_gap"] = v

    # skirt
    raw = config.get("skirt_loops")
    if raw is not None:
        v = _as_int(raw)
        if v is not None:
            settings["skirt_loops"] = v
    raw = config.get("skirt_distance")
    if raw is not None:
        v = _as_float(raw)
        if v is not None:
            settings["skirt_distance"] = v
    raw = config.get("skirt_height")
    if raw is not None:
        v = _as_int(raw)
        if v is not None:
            settings["skirt_height"] = v

    # wall / infill / layer
    raw = config.get("wall_loops")
    if raw is not None:
        v = _as_int(raw)
        if v is not None:
            settings["wall_loops"] = v
    raw = config.get("sparse_infill_density")
    if raw is not None:
        v = _as_int(str(raw).replace("%", ""))
        if v is not None:
            settings["sparse_infill_density"] = v
    raw = config.get("sparse_infill_pattern")
    if raw is not None and str(raw).strip():
        settings["sparse_infill_pattern"] = str(raw).strip()
    raw = config.get("layer_height")
    if raw is not None:
        v = _as_float(raw)
        if v is not None:
            settings["layer_height"] = v

    # prime tower
    raw = config.get("enable_prime_tower")
    if raw is not None:
        settings["enable_prime_tower"] = _as_bool(raw)
    raw = config.get("prime_tower_width")
    if raw is not None:
        v = _as_int(raw)
        if v is not None:
            settings["prime_tower_width"] = v
    raw = config.get("prime_tower_brim_width")
    if raw is not None:
        v = _as_int(raw)
        if v is not None and v >= 0:
            settings["prime_tower_brim_width"] = v
    raw = config.get("filament_prime_volume")
    if raw is not None:
        v = _as_int(_first_element(raw))
        if v is not None:
            settings["prime_volume"] = v

    # temperature / bed
    raw = config.get("nozzle_temperature")
    if raw is not None:
        v = _as_int(_first_element(raw))
        if v is not None:
            settings["nozzle_temperature"] = v
    raw = config.get("bed_temperature")
    if raw is not None:
        v = _as_int(_first_element(raw))
        if v is not None:
            settings["bed_temperature"] = v
    raw = config.get("curr_bed_type")
    if raw is not None and str(raw).strip():
        settings["curr_bed_type"] = str(raw).strip()

    return settings
