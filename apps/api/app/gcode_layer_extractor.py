"""
Extract layer-by-layer geometry from G-code files for preview rendering.

This module parses G-code and extracts movement data grouped by layers (Z-height).
It distinguishes between extrusion moves (printing) and travel moves (non-printing).
"""

import re
from pathlib import Path
from typing import List, Dict, Optional
import logging

logger = logging.getLogger(__name__)


class LayerExtractor:
    """Parse G-code and extract per-layer movement data."""

    # G-code command patterns
    G0_G1_PATTERN = re.compile(r'G[01]\s+')
    COORD_PATTERN = re.compile(r'([XYZE])([-+]?\d*\.?\d+)')

    def __init__(self):
        """Initialize extractor with default state."""
        self.reset_state()

    def reset_state(self):
        """Reset parser state for new file."""
        # Current position
        self.x = 0.0
        self.y = 0.0
        self.z = 0.0
        self.e = 0.0

        # Positioning mode
        self.absolute_positioning = True
        self.absolute_extrusion = False

        # Layer tracking
        self.current_layer_num = 0
        self.current_z = 0.0
        self.layers = {}  # {layer_num: {"z_height": float, "moves": []}}

        # Move decimation for preview (only keep every Nth move to reduce data size)
        self.move_counter = 0
        self.decimation_factor = 100  # Keep 1 out of every 100 moves

    def extract_layers(
        self,
        gcode_path: Path,
        start: int = 0,
        count: int = 20
    ) -> Dict:
        """
        Extract layer-by-layer geometry from G-code file.

        Args:
            gcode_path: Path to G-code file
            start: Starting layer number (0-indexed)
            count: Number of layers to extract

        Returns:
            {
                "total_layers": int,
                "start_layer": int,
                "layer_count": int,
                "layers": [
                    {
                        "layer_num": 0,
                        "z_height": 0.2,
                        "moves": [
                            {"type": "travel", "x1": 100.0, "y1": 100.0, "x2": 105.0, "y2": 100.0},
                            {"type": "extrude", "x1": 105.0, "y1": 100.0, "x2": 110.0, "y2": 100.0}
                        ]
                    }
                ]
            }
        """
        logger.info(f"Extracting layers {start} to {start + count - 1} from {gcode_path}")

        if not gcode_path.exists():
            raise FileNotFoundError(f"G-code file not found: {gcode_path}")

        self.reset_state()
        end = start + count

        # Parse G-code file
        with open(gcode_path, 'r') as f:
            for line in f:
                line = line.strip()

                # Skip comments and empty lines
                if not line or line.startswith(';'):
                    continue

                # Remove inline comments
                if ';' in line:
                    line = line[:line.index(';')].strip()

                # Process command
                self._process_command(line)

        # Get total layer count
        total_layers = max(self.layers.keys()) + 1 if self.layers else 0

        # Extract requested range
        result_layers = []
        for layer_num in range(start, min(end, total_layers)):
            if layer_num in self.layers:
                result_layers.append({
                    "layer_num": layer_num,
                    "z_height": self.layers[layer_num]["z_height"],
                    "moves": self.layers[layer_num]["moves"]
                })

        logger.info(f"Extracted {len(result_layers)} layers (total: {total_layers})")

        return {
            "total_layers": total_layers,
            "start_layer": start,
            "layer_count": len(result_layers),
            "layers": result_layers
        }

    def _process_command(self, line: str):
        """Process a single G-code command."""
        # Handle positioning mode changes
        if line.startswith('G90'):
            self.absolute_positioning = True
            return
        elif line.startswith('G91'):
            self.absolute_positioning = False
            return
        elif line.startswith('M82'):
            self.absolute_extrusion = True
            return
        elif line.startswith('M83'):
            self.absolute_extrusion = False
            return

        # Handle position reset (G92)
        if line.startswith('G92'):
            self._handle_position_reset(line)
            return

        # Handle movement commands (G0, G1)
        if self.G0_G1_PATTERN.match(line):
            self._handle_move(line)

    def _handle_position_reset(self, line: str):
        """Handle G92 position reset command."""
        coords = dict(self.COORD_PATTERN.findall(line))

        if 'X' in coords:
            self.x = float(coords['X'])
        if 'Y' in coords:
            self.y = float(coords['Y'])
        if 'Z' in coords:
            self.z = float(coords['Z'])
        if 'E' in coords:
            self.e = float(coords['E'])

    def _handle_move(self, line: str):
        """Handle G0/G1 movement command."""
        # Extract coordinates
        coords = dict(self.COORD_PATTERN.findall(line))

        # Store previous position
        prev_x = self.x
        prev_y = self.y
        prev_z = self.z
        prev_e = self.e

        # Update position
        if 'X' in coords:
            if self.absolute_positioning:
                self.x = float(coords['X'])
            else:
                self.x += float(coords['X'])

        if 'Y' in coords:
            if self.absolute_positioning:
                self.y = float(coords['Y'])
            else:
                self.y += float(coords['Y'])

        if 'Z' in coords:
            if self.absolute_positioning:
                self.z = float(coords['Z'])
            else:
                self.z += float(coords['Z'])

            # Z change indicates new layer
            if abs(self.z - self.current_z) > 0.001:  # Threshold for float comparison
                self.current_z = self.z
                self.current_layer_num = len([z for z in self.layers.values() if z["z_height"] <= self.z])

        if 'E' in coords:
            if self.absolute_extrusion:
                self.e = float(coords['E'])
            else:
                self.e += float(coords['E'])

        # Determine move type (extrusion vs travel)
        is_extrusion = self.e > prev_e

        # Skip moves with no XY change (pure Z-hop or retraction)
        if abs(self.x - prev_x) < 0.001 and abs(self.y - prev_y) < 0.001:
            return

        # Ensure layer exists
        if self.current_layer_num not in self.layers:
            self.layers[self.current_layer_num] = {
                "z_height": round(self.current_z, 3),
                "moves": []
            }

        # Increment move counter
        self.move_counter += 1

        # Only keep every Nth move for preview (decimation to reduce data size)
        # Always keep the first and last moves of each type for continuity
        layer_moves = self.layers[self.current_layer_num]["moves"]
        should_keep = (
            self.move_counter % self.decimation_factor == 0 or  # Every Nth move
            len(layer_moves) == 0  # First move
        )

        if should_keep:
            # Add move to current layer
            move = {
                "type": "extrude" if is_extrusion else "travel",
                "x1": round(prev_x, 2),
                "y1": round(prev_y, 2),
                "x2": round(self.x, 2),
                "y2": round(self.y, 2)
            }

            layer_moves.append(move)

    def get_total_layers(self, gcode_path: Path) -> int:
        """
        Quickly scan G-code file to get total layer count.

        This reads the header comments where Orca Slicer writes layer count.
        Falls back to full parsing if not found in header.
        """
        try:
            with open(gcode_path, 'r') as f:
                # Read first 100 lines to find layer count comment
                for i, line in enumerate(f):
                    if i >= 100:
                        break

                    # Orca format: "; total layer number: 100"
                    if 'total layer number' in line.lower():
                        match = re.search(r'(\d+)', line)
                        if match:
                            return int(match.group(1))

            # Fallback: parse entire file (slower)
            logger.warning(f"Layer count not found in header, parsing entire file: {gcode_path}")
            result = self.extract_layers(gcode_path, start=0, count=999999)
            return result["total_layers"]

        except Exception as e:
            logger.error(f"Failed to get total layers: {e}")
            return 0
