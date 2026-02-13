"""Plate bounds validation for 3MF files.

Validates that the entire plate layout fits within the printer's build volume.
Now supports multi-plate 3MF files - validates individual plates.
"""

import trimesh
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional
from config import PrinterProfile
from multi_plate_parser import parse_multi_plate_3mf, get_plate_bounds


logger = logging.getLogger(__name__)


class PlateValidationError(Exception):
    """Raised when plate validation fails."""
    pass


class PlateValidator:
    """Validates 3MF plate layouts against printer build volume."""

    def __init__(self, printer_profile: PrinterProfile):
        """Initialize validator with printer profile.

        Args:
            printer_profile: PrinterProfile with build volume limits
        """
        self.printer = printer_profile
        logger.info(f"PlateValidator initialized for {printer_profile.name}")

    def validate_3mf_bounds(self, file_path: Path, plate_id: Optional[int] = None) -> Dict[str, Any]:
        """Load 3MF and calculate bounding box for specific plate or combined scene.

        Args:
            file_path: Path to .3mf file
            plate_id: Specific plate ID to validate (None for all plates combined)

        Returns:
            Dictionary containing:
            - bounds: {min: [x,y,z], max: [x,y,z], size: [w,d,h]}
            - warnings: List of warning messages
            - fits: Boolean indicating if plate fits in build volume
            - plates: List of plate information (if multi-plate)
            - is_multi_plate: Boolean indicating multi-plate file
            - validated_plate: Which plate was validated (if specified)

        Raises:
            PlateValidationError: If 3MF cannot be loaded
        """
        try:
            logger.info(f"Validating plate bounds for {file_path.name}" + 
                       (f", plate {plate_id}" if plate_id else ""))

            # Check if this is a multi-plate file
            plates, is_multi_plate = parse_multi_plate_3mf(file_path)
            
            # Get bounds information
            bounds_info = get_plate_bounds(file_path, plate_id)
            bounds = bounds_info['bounds']
            
            # Calculate dimensions
            width = float(bounds['size'][0])
            depth = float(bounds['size'][1])
            height = float(bounds['size'][2])

            if is_multi_plate and plate_id:
                logger.info(f"Plate {plate_id} dimensions: {width:.1f}x{depth:.1f}x{height:.1f}mm")
            else:
                logger.info(f"Combined scene dimensions: {width:.1f}x{depth:.1f}x{height:.1f}mm")

            # Check against printer build volume limits
            warnings = self._check_build_volume(width, depth, height)

            # Check for objects below bed (Z < 0)
            if bounds['min'][2] < -0.001:  # Tolerance for floating point
                warnings.append(
                    f"Warning: Objects extend below bed (Z_min = {bounds['min'][2]:.1f}mm). "
                    "This may cause printing issues."
                )

            result = {
                "bounds": bounds,
                "warnings": warnings,
                "fits": len(warnings) == 0,
                "is_multi_plate": is_multi_plate,
                "plates": bounds_info.get("plates", [])
            }

            if plate_id:
                result["validated_plate"] = plate_id

            # Add multi-plate specific warnings
            if is_multi_plate:
                if plate_id:
                    plate_info = next((p for p in plates if p.plate_id == plate_id), None)
                    if plate_info and not plate_info.printable:
                        warnings.append(f"Plate {plate_id} is marked as non-printable")
                else:
                    # Multi-plate file validating combined bounds
                    warnings.append(
                        f"Multi-plate file with {len(plates)} plates. "
                        "Individual plates may fit even if combined bounds exceed build volume."
                    )

            if warnings:
                logger.warning(f"Plate validation warnings: {'; '.join(warnings)}")
            else:
                logger.info("Plate fits within build volume")

            return result

        except Exception as e:
            logger.error(f"Failed to validate plate bounds: {str(e)}")
            raise PlateValidationError(f"Could not validate plate: {str(e)}") from e

    def _check_build_volume(self, width: float, depth: float, height: float) -> List[str]:
        """Check dimensions against build volume and generate warnings.

        Args:
            width: X dimension in mm
            depth: Y dimension in mm
            height: Z dimension in mm

        Returns:
            List of warning messages (empty if all dimensions OK)
        """
        warnings = []

        # Check X (width)
        if width > self.printer.build_volume_x:
            warnings.append(
                f"Width exceeds build volume: {width:.1f}mm > {self.printer.build_volume_x:.1f}mm (X-axis)"
            )

        # Check Y (depth)
        if depth > self.printer.build_volume_y:
            warnings.append(
                f"Depth exceeds build volume: {depth:.1f}mm > {self.printer.build_volume_y:.1f}mm (Y-axis)"
            )

        # Check Z (height)
        if height > self.printer.build_volume_z:
            warnings.append(
                f"Height exceeds build volume: {height:.1f}mm > {self.printer.build_volume_z:.1f}mm (Z-axis)"
            )

        return warnings
