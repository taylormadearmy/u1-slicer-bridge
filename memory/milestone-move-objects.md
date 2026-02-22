# M32 — Move Objects on Build Plate

## Overview
Add a 2D top-down build plate view where users can drag objects to reposition them before slicing. Modifies the `<item transform="...">` in the 3MF to update object positions.

## Current State
- API already returns `translation` and `transform` per plate from `GET /uploads/{id}/plates`
- `PlateInfo.get_translation()` extracts X/Y/Z from the transform matrix
- `PlateValidator` already validates bounds after transforms
- **No 2D plate visualization exists** — only small thumbnail previews

## Implementation Plan

### Phase 1: 2D Plate Canvas
**New section in configure step** — a `<canvas>` element showing top-down bed view.

```
┌─────────────────────────────────────┐
│            Build Plate              │
│  ┌─────────────────────────────┐    │
│  │  270mm × 270mm              │    │
│  │                             │    │
│  │    ┌────┐      ┌────┐      │    │
│  │    │Obj1│      │Obj2│      │    │
│  │    │    │      │    │      │    │
│  │    └────┘      └────┘      │    │
│  │                             │    │
│  └─────────────────────────────┘    │
│  [Reset Positions] [Center All]     │
└─────────────────────────────────────┘
```

Implementation approach: **HTML5 Canvas 2D** (lightweight, no library needed).

```javascript
// Plate layout renderer
function drawPlateLayout(canvas, plates, bedWidth, bedHeight) {
    const ctx = canvas.getContext('2d');
    const scale = canvas.width / bedWidth;  // px per mm

    // Draw bed outline (grid lines optional)
    ctx.strokeRect(0, 0, bedWidth * scale, bedHeight * scale);

    // Draw each object as a colored rectangle
    for (const plate of plates) {
        const [x, y] = plate.translation;
        const [w, h] = plate.dimensions;  // Need to add to API
        ctx.fillStyle = plate.color || '#4A90D9';
        ctx.fillRect(
            (x - w/2) * scale,
            (y - h/2) * scale,
            w * scale,
            h * scale
        );
        ctx.fillText(plate.plate_name || `Object ${plate.plate_id}`, ...);
    }
}
```

### Phase 2: Drag-and-Drop Interaction
Mouse/touch events on the canvas:

```javascript
// State
dragging: null,         // plate_id being dragged
dragOffset: [0, 0],     // mouse offset from object center
platePositions: {},     // { plate_id: [x, y] } — local edits before save

// Events
onCanvasMouseDown(e) → find clicked object → set dragging
onCanvasMouseMove(e) → update position → redraw → live validate bounds
onCanvasMouseUp(e)   → finalize position → clear dragging
```

Bounds checking during drag:
- Green outline: object fits on bed
- Red outline: object exceeds bed boundary
- Snap to bed edge if dragged past boundary (optional)

### Phase 3: API Endpoint
**New endpoint**: `PUT /api/uploads/{upload_id}/reposition`

```json
// Request
{
    "positions": [
        { "plate_id": 1, "x": 100.0, "y": 135.0 },
        { "plate_id": 2, "x": 200.0, "y": 135.0 }
    ]
}

// Response
{
    "success": true,
    "plates": [...],  // Updated plate info with new transforms
    "validation": {
        "all_fit": true,
        "warnings": []
    }
}
```

Backend logic:
1. Parse 3MF, find `<build><item>` elements
2. For each repositioned plate, update transform matrix translation values
3. Preserve rotation/scale from original transform (only modify translation)
4. Write modified 3MF
5. Re-validate all bounds
6. Return updated plate data

### Phase 4: Object Dimensions API
Current API returns `translation` but NOT object dimensions. Need to add:

```python
# In multi_plate_parser.py, extend PlateInfo
def get_dimensions(self) -> Tuple[float, float, float]:
    """Return object width, depth, height in mm."""
    # Already calculated in _calculate_xml_bounds()
    return (max_x - min_x, max_y - min_y, max_z - min_z)
```

Update `GET /uploads/{id}/plates` response to include:
```json
{
    "plate_id": 1,
    "translation": [135.0, 135.0, 10.0],
    "dimensions": [40.0, 30.0, 20.0],  // NEW: width, depth, height in mm
    ...
}
```

### Phase 5: Frontend Integration
Add to configure step UI:
- "Arrange" button/tab that opens the plate layout canvas
- Canvas replaces plate thumbnail list when active
- Save/Reset buttons
- Position readout showing X, Y coordinates

State additions:
```javascript
showPlateLayout: false,
platePositions: {},       // User edits: { plate_id: [x, y] }
positionsModified: false,
positionsSaving: false,
```

### Phase 6: Testing
- Unit: transform matrix update preserves rotation components
- Integration: upload → reposition → slice → verify G-code object positions changed
- UI: drag object → verify position updates → save → verify API called
- Edge cases: drag off bed → clamp to boundary, overlapping objects warning

## Constraints
- **Single-plate files**: straightforward — one object to move
- **Multi-plate files**: each plate's object can be moved independently
- **Copies (M31)**: if copies exist, either move all as a group or individually
- **Bambu files**: use `effective_plate_id = 1` for all items
- **No Z-axis movement** — only X/Y repositioning (Z stays at bed level)
- **No rotation in v1** — only translation (rotation adds significant UI complexity)
- **Canvas sizing**: responsive, maintain aspect ratio of 270:270 bed

## Dependency
- Benefits from M31 (Multiple Copies) — can rearrange copies after placement
- Independent of other milestones

## Estimated Complexity
- 2D Canvas renderer: ~120 lines
- Drag-and-drop logic: ~80 lines
- API endpoint + 3MF modification: ~100 lines
- Object dimensions API: ~30 lines
- Frontend state/UI: ~80 lines
- Tests: ~100 lines
- **Total: ~500 lines, Medium-High complexity**
