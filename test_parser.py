#!/usr/bin/env python3
"""Test parser with cube.3mf to debug component reference handling."""

import sys
sys.path.insert(0, 'apps/api/app')

from pathlib import Path
from parser_3mf import parse_3mf

test_file = Path("../cube.3mf")

print(f"Testing parser with: {test_file}")
print(f"File exists: {test_file.exists()}")
print(f"File size: {test_file.stat().st_size} bytes")
print()

try:
    objects = parse_3mf(test_file)
    print(f"✓ Parser succeeded!")
    print(f"Found {len(objects)} object(s):")
    for obj in objects:
        print(f"  - {obj.name} (id={obj.object_id}): {obj.vertices} vertices, {obj.triangles} triangles")
except Exception as e:
    print(f"✗ Parser failed: {type(e).__name__}: {e}")
    import traceback
    traceback.print_exc()
