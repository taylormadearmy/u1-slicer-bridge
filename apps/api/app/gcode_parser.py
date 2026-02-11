"""G-code metadata extraction from Orca Slicer output."""

from pathlib import Path
from typing import Dict, Optional
from dataclasses import dataclass
import re


@dataclass
class GCodeMetadata:
    estimated_time_seconds: int
    filament_used_mm: float
    layer_count: Optional[int]
    max_x: float
    max_y: float
    max_z: float


def parse_time_to_seconds(time_str: str) -> int:
    """Convert Orca time format '1h 23m 45s' to seconds."""
    total_seconds = 0

    # Extract hours
    hours_match = re.search(r'(\d+)h', time_str)
    if hours_match:
        total_seconds += int(hours_match.group(1)) * 3600

    # Extract minutes
    minutes_match = re.search(r'(\d+)m', time_str)
    if minutes_match:
        total_seconds += int(minutes_match.group(1)) * 60

    # Extract seconds
    seconds_match = re.search(r'(\d+)s', time_str)
    if seconds_match:
        total_seconds += int(seconds_match.group(1))

    return total_seconds


def parse_orca_metadata(gcode_path: Path) -> GCodeMetadata:
    """Parse Orca Slicer comments for metadata.

    Orca embeds metadata in comments like:
    ; estimated printing time (normal mode) = 1h 23m 45s
    ; filament used [mm] = 1234.5
    ; total layer number: 100 (in header)

    Note: Time and filament metadata appears at the END of the file,
    while layer count appears in the header at the beginning.
    """
    estimated_time_seconds = 0
    filament_used_mm = 0.0
    layer_count = None

    # Read first 100 lines for header metadata (layer count)
    with open(gcode_path, 'r') as f:
        for i, line in enumerate(f):
            if i >= 100:
                break

            line = line.strip()

            # Extract layer count (appears as "total layer number: 100")
            if 'total layer number' in line.lower():
                layers_match = re.search(r':\s*(\d+)', line)
                if layers_match:
                    layer_count = int(layers_match.group(1))

    # Read last 1000 lines for footer metadata (time, filament)
    # Orca Slicer puts summary metadata near the end, before CONFIG_BLOCK
    with open(gcode_path, 'r') as f:
        lines = f.readlines()
        footer_lines = lines[-1000:] if len(lines) > 1000 else lines

        for line in footer_lines:
            line = line.strip()

            # Extract estimated time
            if 'estimated printing time' in line.lower() and 'normal mode' in line.lower():
                time_match = re.search(r'=\s*(.+)$', line)
                if time_match:
                    estimated_time_seconds = parse_time_to_seconds(time_match.group(1))

            # Extract filament usage (specifically [mm] units)
            elif 'filament used' in line.lower() and '[mm]' in line.lower():
                filament_match = re.search(r'=\s*([\d.]+)', line)
                if filament_match:
                    filament_used_mm = float(filament_match.group(1))

    # Extract movement bounds
    bounds = extract_movement_bounds(gcode_path)

    return GCodeMetadata(
        estimated_time_seconds=estimated_time_seconds,
        filament_used_mm=filament_used_mm,
        layer_count=layer_count,
        max_x=bounds['max_x'],
        max_y=bounds['max_y'],
        max_z=bounds['max_z']
    )


def extract_movement_bounds(gcode_path: Path) -> Dict[str, float]:
    """Parse G1/G0 commands to find actual print bounds.

    Tracks min/max X, Y, Z from movement commands.
    """
    min_x = min_y = min_z = float('inf')
    max_x = max_y = max_z = float('-inf')

    current_x = current_y = current_z = 0.0

    with open(gcode_path, 'r') as f:
        for line in f:
            line = line.strip()

            # Only process G0/G1 movement commands
            if not (line.startswith('G0 ') or line.startswith('G1 ')):
                continue

            # Extract X coordinate
            x_match = re.search(r'X([\d.-]+)', line)
            if x_match:
                current_x = float(x_match.group(1))
                min_x = min(min_x, current_x)
                max_x = max(max_x, current_x)

            # Extract Y coordinate
            y_match = re.search(r'Y([\d.-]+)', line)
            if y_match:
                current_y = float(y_match.group(1))
                min_y = min(min_y, current_y)
                max_y = max(max_y, current_y)

            # Extract Z coordinate
            z_match = re.search(r'Z([\d.-]+)', line)
            if z_match:
                current_z = float(z_match.group(1))
                min_z = min(min_z, current_z)
                max_z = max(max_z, current_z)

    # Handle case where no movements found
    if max_x == float('-inf'):
        max_x = max_y = max_z = 0.0

    return {
        'min_x': min_x if min_x != float('inf') else 0.0,
        'min_y': min_y if min_y != float('inf') else 0.0,
        'min_z': min_z if min_z != float('inf') else 0.0,
        'max_x': max_x,
        'max_y': max_y,
        'max_z': max_z
    }
