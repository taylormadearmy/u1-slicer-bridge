"""Inject thumbnail images into G-code files for printer display.

Extracts preview images from 3MF files and embeds them as base64-encoded
PNG thumbnails in the G-code header, using the standard PrusaSlicer/OrcaSlicer
format that Moonraker/Klipper can parse and display on the printer's screen.

Format:
    ; thumbnail begin WxH LEN
    ; <base64 data, 78 chars per line>
    ; thumbnail end
"""

import base64
import io
import logging
import re
import zipfile
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from PIL import Image

logger = logging.getLogger(__name__)

# Thumbnail sizes to embed (must match printer profile's "thumbnails" setting)
THUMBNAIL_SIZES = [(48, 48), (300, 300)]

# Max base64 characters per G-code comment line (excluding "; " prefix)
MAX_B64_LINE_LENGTH = 76


def _extract_best_preview(
    source_3mf: Path,
    plate_id: Optional[int] = None,
) -> Optional[bytes]:
    """Extract the best preview image from a 3MF file.

    Args:
        source_3mf: Path to the 3MF file.
        plate_id: Optional plate ID for plate-specific preview.

    Returns:
        Raw image bytes (PNG/JPG/WebP) or None if no preview found.
    """
    try:
        with zipfile.ZipFile(source_3mf, "r") as zf:
            image_paths = [
                n for n in zf.namelist()
                if n.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))
                and "metadata" in n.lower()
            ]
            if not image_paths:
                return None

            # Try plate-specific preview first
            if plate_id is not None:
                for path in image_paths:
                    lower = path.lower()
                    # Match patterns like plate_1.png, plate1.png, top_1.png
                    match = re.search(
                        r"(?:plate|top|pick|thumbnail|preview|cover)[_\-]?(\d+)",
                        lower,
                    )
                    if match and int(match.group(1)) == plate_id:
                        return zf.read(path)

            # Fall back to best generic preview (scoring by keyword priority)
            def _score(path: str) -> Tuple[int, int]:
                p = path.lower()
                for i, kw in enumerate(
                    ["thumbnail", "preview", "cover", "top", "plate", "pick"]
                ):
                    if kw in p:
                        return (i, len(p))
                return (9, len(p))

            best_path = sorted(image_paths, key=_score)[0]
            return zf.read(best_path)
    except Exception as e:
        logger.warning(f"Failed to extract preview from 3MF: {e}")
        return None


def _encode_thumbnail(image_bytes: bytes, width: int, height: int) -> Optional[str]:
    """Resize image and encode as base64 PNG thumbnail block.

    Returns G-code comment block string or None on failure.
    """
    try:
        img = Image.open(io.BytesIO(image_bytes))
        # Convert to RGB (strip alpha channel for consistent PNG encoding)
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGB")
        img = img.resize((width, height), Image.LANCZOS)

        # Encode as PNG
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        png_bytes = buf.getvalue()

        # Base64 encode
        b64_data = base64.b64encode(png_bytes).decode("ascii")
        b64_len = len(b64_data)

        # Build G-code comment block
        lines = [f"; thumbnail begin {width}x{height} {b64_len}"]
        for i in range(0, b64_len, MAX_B64_LINE_LENGTH):
            lines.append(f"; {b64_data[i:i + MAX_B64_LINE_LENGTH]}")
        lines.append("; thumbnail end")
        lines.append(";")

        return "\n".join(lines)
    except Exception as e:
        logger.warning(f"Failed to encode {width}x{height} thumbnail: {e}")
        return None


def inject_gcode_thumbnails(
    gcode_path: Path,
    source_3mf: Path,
    plate_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Extract preview from 3MF and inject as thumbnails into G-code.

    Modifies the G-code file in-place by prepending thumbnail blocks.

    Args:
        gcode_path: Path to the G-code file to modify.
        source_3mf: Path to the source 3MF file for preview extraction.
        plate_id: Optional plate ID for plate-specific preview.

    Returns:
        Dict with injection result: {"injected": bool, "sizes": [...], "reason": str}
    """
    # Extract preview image from 3MF
    preview_bytes = _extract_best_preview(source_3mf, plate_id=plate_id)
    if not preview_bytes:
        return {"injected": False, "reason": "no_preview_in_3mf"}

    # Generate thumbnail blocks for each size
    blocks: List[str] = []
    sizes_added: List[str] = []
    for w, h in THUMBNAIL_SIZES:
        block = _encode_thumbnail(preview_bytes, w, h)
        if block:
            blocks.append(block)
            sizes_added.append(f"{w}x{h}")

    if not blocks:
        return {"injected": False, "reason": "encoding_failed"}

    # Insert thumbnail blocks after HEADER_BLOCK_END to avoid pushing
    # header metadata past the gcode_parser's first-100-lines scan window.
    # Moonraker's thumbnail extractor searches the entire file, so placement
    # after the header is fine.
    try:
        thumbnail_data = "\n".join(blocks) + "\n"
        with open(gcode_path, "r", errors="ignore") as f:
            lines = f.readlines()

        # Find HEADER_BLOCK_END and insert after it
        insert_idx = None
        for i, line in enumerate(lines):
            if "HEADER_BLOCK_END" in line:
                insert_idx = i + 1
                break

        if insert_idx is not None:
            lines.insert(insert_idx, "\n" + thumbnail_data)
        else:
            # No header block found — prepend to file
            lines.insert(0, thumbnail_data)

        with open(gcode_path, "w") as f:
            f.writelines(lines)

        logger.info(
            f"Injected {len(sizes_added)} thumbnails into {gcode_path.name}: "
            f"{', '.join(sizes_added)}"
        )
        return {"injected": True, "sizes": sizes_added}
    except Exception as e:
        logger.error(f"Failed to inject thumbnails: {e}")
        return {"injected": False, "reason": str(e)}
