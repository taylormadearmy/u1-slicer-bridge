# M31 — Multiple Copies on Build Plate

## Overview
Allow users to place multiple copies of a single object on the build plate before slicing. Uses the existing 3MF `<build><item>` mechanism — copies are just additional items referencing the same object geometry with different transform matrices.

## Key Insight
OrcaSlicer already handles multiple `<item>` elements natively. Adding copies means inserting extra `<item>` elements in the 3MF `<build>` section, each with a calculated position offset. No slicer changes needed.

## 3MF Structure

```xml
<build>
  <!-- Original object -->
  <item objectid="1" transform="1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1" printable="1" />
  <!-- Copy 2, offset 60mm in X -->
  <item objectid="1" transform="1 0 0 60 0 1 0 0 0 0 1 0 0 0 0 1" printable="1" />
</build>
```

Translation lives in transform positions [3, 7, 11] (column-major) or [9, 10, 11] (row-major, how our parser reads it). Same `objectid` = same geometry, different position.

## Implementation Plan

### Phase 1: Backend — Grid Layout Engine
**New file**: `apps/api/app/copy_duplicator.py`

```python
def calculate_grid_positions(
    object_bounds: Tuple[float, float, float, float],  # min_x, min_y, max_x, max_y
    copies: int,
    spacing: float = 5.0,  # mm gap between copies
    bed_size: float = 270.0,
) -> List[Tuple[float, float]]:
    """Auto-arrange N copies in a grid centered on the bed."""
    # Calculate object dimensions
    # Determine grid rows/cols (prefer square-ish: ceil(sqrt(N)) columns)
    # Center grid on bed
    # Return list of (x_offset, y_offset) for each copy
```

```python
def apply_copies_to_3mf(
    source_path: Path,
    output_path: Path,
    copies: int,
    spacing: float = 5.0,
) -> dict:
    """Duplicate items in 3MF build section with grid-spaced transforms."""
    # 1. Parse source 3MF, find <build> section
    # 2. Get first printable <item>'s objectid and original transform
    # 3. Calculate object bounds from mesh vertices
    # 4. Generate grid positions for N copies
    # 5. Create N <item> elements with adjusted transforms
    # 6. Remove original items, insert new grid
    # 7. Write modified 3MF
    # 8. Validate bounds fit on bed
```

### Phase 2: API Endpoint
**In**: `apps/api/app/main.py`

```
POST /api/uploads/{upload_id}/copies
Body: { "copies": 4, "spacing": 5.0 }
Response: {
    "copies_applied": 4,
    "grid": "2x2",
    "fits_bed": true,
    "max_copies": 12,  // estimate for this object
    "plates": [...]     // updated plate info
}
```

Flow:
1. Validate upload exists and has exactly 1 printable object (multi-object not supported initially)
2. Call `apply_copies_to_3mf()` — writes modified 3MF alongside original
3. Re-validate bounds with `PlateValidator`
4. Return updated plate metadata

Also add: `DELETE /api/uploads/{upload_id}/copies` to revert to original single copy.

### Phase 3: Frontend UI
**In configure step** (`apps/web/index.html` + `app.js`):

Add copies control after plate selection, before filament config:

```
┌──────────────────────────────────┐
│  Copies: [1] [2] [4] [6] [9]    │   ← Quick-select buttons
│  Custom: [___] copies            │   ← Or type a number
│  Spacing: [5] mm                 │
│  Grid: 3×3  |  Fits: ✓          │   ← Live feedback
│  Max for this object: ~12        │
└──────────────────────────────────┘
```

State additions:
```javascript
copyCount: 1,
copySpacing: 5,
copyGridInfo: null,     // { rows, cols, fits, max_copies }
copyApplying: false,
```

Methods:
- `applyCopies()` — POST to API, update plate info
- `resetCopies()` — DELETE to API, revert to 1 copy
- `estimateMaxCopies()` — client-side estimate from object bounds

### Phase 4: Testing
- Unit test: grid layout calculations (1, 2, 4, 9, 16 copies)
- Integration test: upload calib-cube → apply 4 copies → slice → verify G-code has 4 objects
- Bounds test: try too many copies → get proper error
- Reset test: apply copies → reset → verify single copy

## Constraints
- **Single-object files only** (initially) — multi-plate/multi-object copies are complex
- **No rotation** in v1 — all copies same orientation (rotation support later)
- **270×270mm bed** — max copies depends on object size + spacing
- **Multicolor files** — extruder assignments preserved (same objectid = same colors)
- **Bambu files** — apply copies AFTER trimesh rebuild, not before

## Estimated Complexity
- Backend: ~150 lines (grid engine + 3MF manipulation)
- API: ~60 lines (endpoint + validation)
- Frontend: ~80 lines (UI + state + API calls)
- Tests: ~100 lines
- **Total: ~400 lines, Medium complexity**
