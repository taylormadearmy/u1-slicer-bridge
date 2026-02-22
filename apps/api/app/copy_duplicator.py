"""Duplicate objects on the build plate by adding extra <item> elements to the 3MF.

OrcaSlicer natively handles multiple <item> elements referencing the same object —
each item gets its own transform matrix defining position on the bed. This module
calculates a grid layout and writes the modified 3MF.
"""

import math
import shutil
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import logging

logger = logging.getLogger(__name__)

# Snapmaker U1 bed dimensions (printable area)
BED_SIZE_X = 270.0
BED_SIZE_Y = 270.0


def calculate_grid_layout(
    obj_width: float,
    obj_depth: float,
    copies: int,
    spacing: float = 5.0,
) -> List[Tuple[float, float]]:
    """Calculate grid positions for N copies centered on the bed.

    Returns list of (center_x, center_y) positions in mm.
    """
    if copies < 1:
        raise ValueError("copies must be >= 1")
    if copies == 1:
        return [(BED_SIZE_X / 2, BED_SIZE_Y / 2)]

    # Determine grid dimensions (prefer wider grids)
    cols = math.ceil(math.sqrt(copies))
    rows = math.ceil(copies / cols)

    cell_w = obj_width + spacing
    cell_h = obj_depth + spacing
    total_w = cols * cell_w - spacing
    total_h = rows * cell_h - spacing

    # Starting offset to center the grid on the bed
    start_x = (BED_SIZE_X - total_w) / 2 + obj_width / 2
    start_y = (BED_SIZE_Y - total_h) / 2 + obj_depth / 2

    positions = []
    for i in range(copies):
        col = i % cols
        row = i // cols
        cx = start_x + col * cell_w
        cy = start_y + row * cell_h
        positions.append((cx, cy))

    return positions


def estimate_max_copies(
    obj_width: float,
    obj_depth: float,
    spacing: float = 5.0,
) -> int:
    """Estimate maximum copies that fit on the bed."""
    if obj_width <= 0 or obj_depth <= 0:
        return 1
    cols = max(1, int((BED_SIZE_X + spacing) / (obj_width + spacing)))
    rows = max(1, int((BED_SIZE_Y + spacing) / (obj_depth + spacing)))
    return cols * rows


def grid_fits_bed(
    obj_width: float,
    obj_depth: float,
    copies: int,
    spacing: float = 5.0,
) -> bool:
    """Check if a grid of copies fits within the bed."""
    positions = calculate_grid_layout(obj_width, obj_depth, copies, spacing)
    for cx, cy in positions:
        if (cx - obj_width / 2) < -0.5 or (cx + obj_width / 2) > BED_SIZE_X + 0.5:
            return False
        if (cy - obj_depth / 2) < -0.5 or (cy + obj_depth / 2) > BED_SIZE_Y + 0.5:
            return False
    return True


def get_object_dimensions(file_path: Path) -> Tuple[float, float, float]:
    """Get the bounding box dimensions (width, depth, height) of the first printable object.

    Scans mesh vertices directly from the 3MF XML.
    """
    ns = {
        "m": "http://schemas.microsoft.com/3dmanufacturing/core/2015/02",
        "p": "http://schemas.microsoft.com/3dmanufacturing/production/2015/06",
    }

    with zipfile.ZipFile(file_path, "r") as zf:
        model_xml = zf.read("3D/3dmodel.model")
        root = ET.fromstring(model_xml)

        resources = root.find("m:resources", ns)
        if resources is None:
            raise ValueError("3MF missing resources section")

        build = root.find("m:build", ns)
        items = build.findall("m:item", ns) if build is not None else []

        # Find the first printable item's object
        for item in items:
            if item.get("printable", "1") == "0":
                continue
            obj_id = item.get("objectid")
            if not obj_id:
                continue

            for obj in resources.findall("m:object", ns):
                if obj.get("id") != obj_id:
                    continue
                mesh = obj.find("m:mesh", ns)
                if mesh is None:
                    # Check component references
                    components = obj.find("m:components", ns)
                    if components is None:
                        continue
                    # Scan component files for bounds
                    from multi_plate_parser import _scan_object_bounds
                    result = _scan_object_bounds(zf, obj, ns)
                    if result:
                        bmin, bmax = result
                        return (bmax[0] - bmin[0], bmax[1] - bmin[1], bmax[2] - bmin[2])
                    continue

                vertices = mesh.find("m:vertices", ns)
                if vertices is None:
                    continue

                min_xyz = [float('inf')] * 3
                max_xyz = [float('-inf')] * 3
                for v in vertices.iter(f"{{{ns['m']}}}vertex"):
                    x, y, z = float(v.get("x")), float(v.get("y")), float(v.get("z"))
                    for i, val in enumerate([x, y, z]):
                        if val < min_xyz[i]:
                            min_xyz[i] = val
                        if val > max_xyz[i]:
                            max_xyz[i] = val

                return (
                    max_xyz[0] - min_xyz[0],
                    max_xyz[1] - min_xyz[1],
                    max_xyz[2] - min_xyz[2],
                )

    raise ValueError("No printable objects with mesh data found in 3MF")


def _patch_model_settings(
    settings_xml: str,
    object_id: str,
    positions: List[Tuple[float, float]],
    tz: float,
) -> str:
    """Patch model_settings.config to add instance entries for each copy.

    OrcaSlicer requires each build <item> to have a corresponding
    <model_instance> in <plate> and <assemble_item> in <assemble>.
    Missing entries cause segfaults.
    """
    try:
        root = ET.fromstring(settings_xml)
    except ET.ParseError:
        return settings_xml  # Return unchanged if we can't parse

    # Find or create <plate> element
    plate = root.find("plate")
    if plate is None:
        plate = ET.SubElement(root, "plate")
        ET.SubElement(plate, "metadata", key="plater_id", value="1")
        ET.SubElement(plate, "metadata", key="plater_name", value="")

    # Remove existing model_instance entries (we'll recreate them)
    for mi in list(plate.findall("model_instance")):
        plate.remove(mi)

    # Find or create <assemble> element
    assemble = root.find("assemble")
    if assemble is None:
        assemble = ET.SubElement(root, "assemble")

    # Remove existing assemble_item entries
    for ai in list(assemble.findall("assemble_item")):
        assemble.remove(ai)

    # Add entries for each copy
    for idx, (cx, cy) in enumerate(positions):
        # <model_instance> in <plate>
        mi = ET.SubElement(plate, "model_instance")
        ET.SubElement(mi, "metadata", key="object_id", value=str(object_id))
        ET.SubElement(mi, "metadata", key="instance_id", value=str(idx))
        ET.SubElement(mi, "metadata", key="identify_id", value=str(231 + idx))

        # <assemble_item>
        transform = f"1 0 0 0 1 0 0 0 1 {cx:.4f} {cy:.4f} {tz:.4f}"
        ET.SubElement(
            assemble,
            "assemble_item",
            object_id=str(object_id),
            instance_id=str(idx),
            transform=transform,
            offset="0 0 0",
        )

    return ET.tostring(root, encoding="unicode", xml_declaration=True)


def apply_copies_to_3mf(
    source_path: Path,
    output_path: Path,
    copies: int,
    spacing: float = 5.0,
    layout_scale_factor: float = 1.0,
) -> Dict:
    """Create a modified 3MF with multiple copies arranged in a grid.

    The original file is not modified. A new file is written to output_path.

    Args:
        source_path: Original 3MF file
        output_path: Where to write the modified 3MF
        copies: Number of copies (including the original)
        spacing: Gap between copies in mm
        layout_scale_factor: Scale multiplier used for grid layout dimensions and gap.
            This is needed when slicer-side scaling is applied after copy placement.

    Returns:
        Dict with grid info: cols, rows, fits_bed, positions, max_copies
    """
    if copies < 1:
        raise ValueError("copies must be >= 1")
    if layout_scale_factor <= 0:
        raise ValueError("layout_scale_factor must be > 0")

    # Get object dimensions for layout calculation
    obj_w, obj_d, obj_h = get_object_dimensions(source_path)
    effective_obj_w = obj_w * layout_scale_factor
    effective_obj_d = obj_d * layout_scale_factor
    effective_spacing = spacing * layout_scale_factor
    logger.info(
        f"Object dimensions: {obj_w:.1f} x {obj_d:.1f} x {obj_h:.1f} mm "
        f"(layout scale {layout_scale_factor:.3f} -> {effective_obj_w:.1f} x {effective_obj_d:.1f}, "
        f"spacing {effective_spacing:.1f}mm)"
    )

    if copies == 1:
        # Just copy the file as-is
        shutil.copy2(source_path, output_path)
        return {
            "copies": 1,
            "cols": 1,
            "rows": 1,
            "fits_bed": True,
            "max_copies": estimate_max_copies(effective_obj_w, effective_obj_d, effective_spacing),
            "object_dimensions": [round(obj_w, 1), round(obj_d, 1), round(obj_h, 1)],
        }

    positions = calculate_grid_layout(effective_obj_w, effective_obj_d, copies, effective_spacing)
    fits = grid_fits_bed(effective_obj_w, effective_obj_d, copies, effective_spacing)
    cols = math.ceil(math.sqrt(copies))
    rows = math.ceil(copies / cols)

    ns = {
        "m": "http://schemas.microsoft.com/3dmanufacturing/core/2015/02",
        "p": "http://schemas.microsoft.com/3dmanufacturing/production/2015/06",
    }

    # Read source 3MF and modify the model XML
    with zipfile.ZipFile(source_path, "r") as zf_in:
        model_xml = zf_in.read("3D/3dmodel.model")
        root = ET.fromstring(model_xml)

        # Register namespaces to preserve them on write
        ET.register_namespace("", ns["m"])
        ET.register_namespace("p", ns["p"])

        build = root.find("m:build", ns)
        if build is None:
            raise ValueError("3MF missing build section")

        items = build.findall("m:item", ns)
        if not items:
            raise ValueError("3MF build section has no items")

        # Find first printable item to use as template
        template_item = None
        for item in items:
            if item.get("printable", "1") != "0":
                template_item = item
                break
        if template_item is None:
            template_item = items[0]

        template_obj_id = template_item.get("objectid")

        # Get original Z offset from the template (preserve Z positioning)
        orig_transform_str = template_item.get("transform", "1 0 0 0 1 0 0 0 1 0 0 0")
        orig_values = [float(x) for x in orig_transform_str.split()]
        if len(orig_values) >= 12:
            orig_tz = orig_values[11]
        else:
            orig_tz = 0.0

        # Remove all existing items
        for item in list(build):
            build.remove(item)

        # Add new items for each copy position
        for cx, cy in positions:
            # 3MF transform: 3x3 rotation (identity) then translation (tx ty tz)
            # Format: m00 m01 m02 m10 m11 m12 m20 m21 m22 m30 m31 m32
            transform_str = f"1 0 0 0 1 0 0 0 1 {cx:.4f} {cy:.4f} {orig_tz:.4f}"
            new_item = ET.SubElement(build, f"{{{ns['m']}}}item")
            new_item.set("objectid", template_obj_id)
            new_item.set("transform", transform_str)
            new_item.set("printable", "1")

        # Write modified 3MF
        modified_xml = ET.tostring(root, encoding="unicode", xml_declaration=True)

        # Patch model_settings.config to add instance entries for each copy.
        # OrcaSlicer expects <plate>/<model_instance> and <assemble>/<assemble_item>
        # entries matching each build <item>, or it segfaults.
        patched_settings = None
        if "Metadata/model_settings.config" in zf_in.namelist():
            patched_settings = _patch_model_settings(
                zf_in.read("Metadata/model_settings.config").decode("utf-8"),
                template_obj_id,
                positions,
                orig_tz,
            )

        with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf_out:
            for entry in zf_in.namelist():
                if entry == "3D/3dmodel.model":
                    zf_out.writestr(entry, modified_xml)
                elif entry == "Metadata/model_settings.config" and patched_settings:
                    zf_out.writestr(entry, patched_settings)
                else:
                    zf_out.writestr(entry, zf_in.read(entry))

    logger.info(f"Created {copies} copies in {cols}x{rows} grid (fits={fits})")

    return {
        "copies": copies,
        "cols": cols,
        "rows": rows,
        "fits_bed": fits,
        "max_copies": estimate_max_copies(effective_obj_w, effective_obj_d, effective_spacing),
        "object_dimensions": [round(obj_w, 1), round(obj_d, 1), round(obj_h, 1)],
    }
