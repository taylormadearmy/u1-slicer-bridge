"""G-code metadata extraction from Orca Slicer output."""

from pathlib import Path
from typing import Dict, List, Optional
from dataclasses import dataclass, field
import re


# Module-level compiled regex patterns (avoid recompilation per call)
_RE_COORD = re.compile(r'([XYZ])([\d.-]+)')
_RE_GCODE_FIELDS = re.compile(r'([GXYZEF])([\d.-]+)')
_RE_LAYER_CHANGE = re.compile(r'^;\s*(LAYER_CHANGE|CHANGE_LAYER)\b', re.IGNORECASE)
_RE_LAYER_NUMBER = re.compile(r'^;\s*LAYER\s*:\s*(\d+)\b', re.IGNORECASE)


@dataclass
class GCodeMetadata:
    estimated_time_seconds: int
    filament_used_mm: float
    layer_count: Optional[int]
    min_x: float
    min_y: float
    min_z: float
    max_x: float
    max_y: float
    max_z: float
    filament_used_g: List[float] = field(default_factory=list)


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
    """Parse Orca Slicer comments for metadata in a single pass.

    Reads the file once: extracts header metadata (first 100 lines),
    tracks movement bounds from G0/G1 lines throughout, and captures
    footer metadata (last 1000 lines).
    """
    estimated_time_seconds = 0
    filament_used_mm = 0.0
    filament_used_g: List[float] = []
    layer_count = None

    min_x = min_y = min_z = float('inf')
    max_x = max_y = max_z = float('-inf')

    # Ring buffer for last 1000 lines (footer metadata)
    footer_buf: List[str] = []
    FOOTER_SIZE = 1000
    line_num = 0

    with open(gcode_path, 'r') as f:
        for line in f:
            stripped = line.strip()
            line_num += 1

            # ── Header metadata (first 100 lines) ────────
            if line_num <= 100:
                if 'total layer number' in stripped.lower():
                    layers_match = re.search(r':\s*(\d+)', stripped)
                    if layers_match:
                        layer_count = int(layers_match.group(1))

                parsed_time = _parse_time_from_line(stripped)
                if parsed_time is not None:
                    estimated_time_seconds = max(estimated_time_seconds, parsed_time)

            # ── Movement bounds (G0/G1 lines throughout) ──
            if stripped.startswith('G0 ') or stripped.startswith('G1 '):
                x_match = re.search(r'X([\d.-]+)', stripped)
                if x_match:
                    x = float(x_match.group(1))
                    if x < min_x: min_x = x
                    if x > max_x: max_x = x

                y_match = re.search(r'Y([\d.-]+)', stripped)
                if y_match:
                    y = float(y_match.group(1))
                    if y < min_y: min_y = y
                    if y > max_y: max_y = y

                z_match = re.search(r'Z([\d.-]+)', stripped)
                if z_match:
                    z = float(z_match.group(1))
                    if z < min_z: min_z = z
                    if z > max_z: max_z = z

            # ── Footer ring buffer ────────────────────────
            footer_buf.append(stripped)
            if len(footer_buf) > FOOTER_SIZE:
                footer_buf.pop(0)

    # ── Extract footer metadata ───────────────────────────
    for stripped in footer_buf:
        parsed_time = _parse_time_from_line(stripped)
        if parsed_time is not None:
            estimated_time_seconds = max(estimated_time_seconds, parsed_time)

        elif 'filament used' in stripped.lower() and '[mm]' in stripped.lower():
            mm_match = re.search(r'=\s*(.+)$', stripped)
            if mm_match:
                try:
                    values = [float(v.strip()) for v in mm_match.group(1).split(',') if v.strip()]
                    filament_used_mm = sum(values)
                except ValueError:
                    pass

        elif 'filament used' in stripped.lower() and '[g]' in stripped.lower() and 'total' not in stripped.lower():
            g_match = re.search(r'=\s*(.+)$', stripped)
            if g_match:
                try:
                    filament_used_g = [float(v.strip()) for v in g_match.group(1).split(',') if v.strip()]
                except ValueError:
                    pass

    # Handle case where no movements found
    if max_x == float('-inf'):
        min_x = min_y = min_z = 0.0
        max_x = max_y = max_z = 0.0

    return GCodeMetadata(
        estimated_time_seconds=estimated_time_seconds,
        filament_used_mm=filament_used_mm,
        layer_count=layer_count,
        filament_used_g=filament_used_g,
        min_x=min_x if min_x != float('inf') else 0.0,
        min_y=min_y if min_y != float('inf') else 0.0,
        min_z=min_z if min_z != float('inf') else 0.0,
        max_x=max_x,
        max_y=max_y,
        max_z=max_z
    )


def _parse_time_from_line(line: str) -> Optional[int]:
    """Extract estimated time from a G-code comment line."""
    lowered = line.lower()

    # Newer Orca/Snapmaker summaries
    total_est_match = re.search(r'total\s+estimated\s+time\s*:\s*([^;]+)', lowered)
    if total_est_match:
        return parse_time_to_seconds(total_est_match.group(1).strip())

    model_time_match = re.search(r'model\s+printing\s+time\s*:\s*([^;]+)', lowered)
    if model_time_match:
        return parse_time_to_seconds(model_time_match.group(1).strip())

    # Older Orca summary style
    if 'estimated printing time' in lowered and 'normal mode' in lowered:
        value_match = re.search(r'=\s*(.+)$', line)
        if value_match:
            return parse_time_to_seconds(value_match.group(1).strip())

    return None


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
