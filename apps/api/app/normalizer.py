"""3D object normalization for print bed placement."""

import trimesh
import numpy as np
from pathlib import Path
from typing import Dict, Any, Optional, Tuple
from config import PrinterProfile
import logging


class NormalizationError(Exception):
    """Raised when object cannot be normalized."""
    pass


class Normalizer:
    def __init__(self, printer_profile: PrinterProfile, logger: Optional[logging.Logger] = None):
        self.printer = printer_profile
        self.logger = logger or logging.getLogger(__name__)

    def load_object_from_3mf(self, file_path: Path, object_id: str) -> trimesh.Trimesh:
        """Load single object from .3mf by object ID.

        Handles both consecutive and non-consecutive object IDs by using
        trimesh's geometry dictionary keys instead of positional indexing.
        """
        self.logger.info(f"Loading object_id='{object_id}' (type={type(object_id).__name__}) from {file_path.name}")
        scene = trimesh.load(str(file_path), file_type='3mf')

        # Find mesh matching object_id
        if isinstance(scene, trimesh.Scene):
            # Multiple objects - scene.geometry is a dict with mesh names as keys
            available_keys = list(scene.geometry.keys())
            self.logger.info(f"Available geometry keys: {available_keys}")

            # Try direct key lookup first (trimesh may use object ID as key)
            if object_id in scene.geometry:
                self.logger.info(f"Found exact match for '{object_id}'")
                return scene.geometry[object_id]

            # Try with common prefixes that trimesh might add
            for prefix in ['', 'mesh_', 'object_']:
                key = f"{prefix}{object_id}"
                if key in scene.geometry:
                    return scene.geometry[key]

            # Fallback: Build ID-to-index mapping from sorted geometry keys
            # This handles cases where trimesh uses sequential naming
            geometry_items = sorted(scene.geometry.items())
            if object_id.isdigit():
                obj_id_int = int(object_id)
                # Try to find by matching the Nth item for ID N
                # Account for potential gaps in numbering
                for idx, (key, mesh) in enumerate(geometry_items):
                    # Extract numeric part from key if possible
                    key_num = ''.join(filter(str.isdigit, key))
                    if key_num and int(key_num) == obj_id_int:
                        return mesh
                    # Fallback: use 1-indexed position for backward compatibility
                    if idx == obj_id_int - 1:
                        return mesh

            # If still not found, list available keys for debugging
            available_keys = list(scene.geometry.keys())
            self.logger.error(f"Object {object_id} not found. Available keys: {available_keys}")
            raise ValueError(f"Object {object_id} not found in scene (available: {available_keys})")
        else:
            # Single mesh
            return scene

    def calculate_bounds(self, mesh: trimesh.Trimesh) -> Dict[str, Tuple[float, float]]:
        """Calculate bounding box in mm."""
        bounds = mesh.bounds  # [[min_x, min_y, min_z], [max_x, max_y, max_z]]
        return {
            "x": (float(bounds[0][0]), float(bounds[1][0])),
            "y": (float(bounds[0][1]), float(bounds[1][1])),
            "z": (float(bounds[0][2]), float(bounds[1][2])),
        }

    def validate_bounds(self, bounds: Dict[str, Tuple[float, float]], object_name: str):
        """Check if object fits in build volume."""
        width_x = bounds["x"][1] - bounds["x"][0]
        width_y = bounds["y"][1] - bounds["y"][0]
        height_z = bounds["z"][1] - bounds["z"][0]

        errors = []
        if width_x > self.printer.build_volume_x:
            errors.append(f"width {width_x:.1f}mm > {self.printer.build_volume_x:.1f}mm max (X axis)")
        if width_y > self.printer.build_volume_y:
            errors.append(f"depth {width_y:.1f}mm > {self.printer.build_volume_y:.1f}mm max (Y axis)")
        if height_z > self.printer.build_volume_z:
            errors.append(f"height {height_z:.1f}mm > {self.printer.build_volume_z:.1f}mm max (Z axis)")

        if errors:
            raise NormalizationError(
                f"Object '{object_name}' exceeds build envelope: {'; '.join(errors)}"
            )

    def normalize_to_bed(self, mesh: trimesh.Trimesh) -> Tuple[trimesh.Trimesh, Dict[str, Any]]:
        """
        Translate object so Z_min = 0 (sits on bed).
        Returns normalized mesh and transform metadata.
        """
        original_bounds = self.calculate_bounds(mesh)
        z_min = original_bounds["z"][0]

        transform_data = {"translate_z": 0.0}

        if abs(z_min) > 0.001:  # Only translate if not already at Z=0
            translation = np.array([0, 0, -z_min])
            mesh.apply_translation(translation)
            transform_data["translate_z"] = float(-z_min)
            self.logger.info(f"Translated object by Z={-z_min:.3f}mm to bed")

        normalized_bounds = self.calculate_bounds(mesh)
        return mesh, {
            "original_bounds": original_bounds,
            "normalized_bounds": normalized_bounds,
            "transform": transform_data,
        }

    def normalize_object(
        self,
        source_3mf: Path,
        object_id: str,
        object_name: str,
        output_path: Path
    ) -> Dict[str, Any]:
        """
        Main normalization workflow for single object.
        Returns metadata dict.
        """
        self.logger.info(f"Normalizing object {object_id} ({object_name})")

        # Load mesh
        mesh = self.load_object_from_3mf(source_3mf, object_id)

        # Check original bounds
        original_bounds = self.calculate_bounds(mesh)
        self.logger.info(f"Original bounds: {original_bounds}")

        # Normalize to bed
        mesh, transform_info = self.normalize_to_bed(mesh)

        # Validate fits in build volume
        self.validate_bounds(transform_info["normalized_bounds"], object_name)

        # Export to STL
        mesh.export(str(output_path), file_type='stl')
        self.logger.info(f"Exported to {output_path}")

        return {
            "object_id": object_id,
            "name": object_name,
            "stl_file": output_path.name,
            **transform_info
        }
