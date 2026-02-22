# M33 — Vertical Layer Slider

## Overview
Move the G-code preview layer slider from its current horizontal position below the viewer to a vertical slider on the right side. This matches the convention in OrcaSlicer, PrusaSlicer, and other slicers where the layer scrubber runs vertically alongside the 3D view.

## Current State
- Horizontal `<input type="range">` below the 3D canvas (index.html ~line 938)
- Controls `preview.endLayer` via `onLayerChange()` in viewer.js
- Additional controls: layer number display, prev/next buttons, travel toggle, zoom buttons
- Canvas is in a `relative` container with overlay elements

## Current Layout
```
┌──────────────────────────────────┐
│                                  │
│         3D G-code Viewer         │
│            (Canvas)              │
│                                  │
│                    [+][-] zoom   │
└──────────────────────────────────┘
  ◄═══════════════●═══════════════►   ← Horizontal slider
  Layer 42 of 150    [◄] [►]
  □ Show travel moves
```

## Target Layout
```
┌──────────────────────────────┬──┐
│                              │▲ │
│                              │║ │
│      3D G-code Viewer        │║ │
│         (Canvas)             │● │  ← Vertical slider
│                              │║ │
│                              │║ │
│                [+][-] zoom   │▼ │
└──────────────────────────────┴──┘
  Layer 42 / 150    [◄] [►]
  □ Show travel moves
```

## Implementation Plan

### Phase 1: HTML Restructure
Change the viewer container from vertical stack to horizontal flex:

```html
<!-- Before: vertical stack -->
<div class="space-y-3">
    <div class="relative"> <!-- canvas container -->
        <canvas id="gcodeCanvas"></canvas>
    </div>
    <div> <!-- layer slider (horizontal) --> </div>
    <div> <!-- layer info + buttons --> </div>
</div>

<!-- After: horizontal flex with vertical slider -->
<div class="flex flex-col gap-2">
    <div class="flex gap-0">
        <!-- Canvas container (grows to fill) -->
        <div class="relative flex-1">
            <canvas id="gcodeCanvas"></canvas>
        </div>
        <!-- Vertical slider rail (fixed width) -->
        <div class="flex flex-col items-center w-8 py-2">
            <span class="text-xs text-gray-400" x-text="totalLayers - 1">150</span>
            <input type="range"
                   x-model.number="currentLayer"
                   @input="onLayerChange(currentLayer)"
                   min="0"
                   :max="totalLayers - 1"
                   class="vertical-slider flex-1">
            <span class="text-xs text-gray-400">0</span>
        </div>
    </div>
    <!-- Layer info bar (below, full width) -->
    <div class="flex items-center gap-3 text-sm">
        <span>Layer <span x-text="currentLayer">0</span> / <span x-text="totalLayers">0</span></span>
        <button @click="previousLayer()">◄</button>
        <button @click="nextLayer()">►</button>
        <label><input type="checkbox" x-model="showTravel"> Travel</label>
    </div>
</div>
```

### Phase 2: CSS for Vertical Range Input
Vertical range inputs need special CSS. Two approaches:

**Option A: CSS Transform (simpler)**
```css
.vertical-slider {
    writing-mode: vertical-lr;
    direction: rtl;           /* High values at top */
    -webkit-appearance: slider-vertical;
    width: 20px;
    height: 100%;
}
```

**Option B: CSS `writing-mode` + custom styling**
```css
.vertical-slider {
    writing-mode: vertical-lr;
    direction: rtl;
    appearance: none;
    width: 6px;
    height: 100%;
    background: #e5e7eb;
    border-radius: 3px;
    cursor: pointer;
}

.vertical-slider::-webkit-slider-thumb {
    appearance: none;
    width: 16px;
    height: 16px;
    background: #3b82f6;
    border-radius: 50%;
    cursor: grab;
}

.vertical-slider::-moz-range-thumb {
    width: 16px;
    height: 16px;
    background: #3b82f6;
    border-radius: 50%;
    border: none;
    cursor: grab;
}
```

**Recommendation**: Option A for browser compatibility; test on Chrome, Firefox, Safari.

### Phase 3: Canvas Resize Handling
The canvas currently fills the full container width. With the vertical slider alongside, it needs to share horizontal space:

```javascript
// In viewer.js resizeCanvas()
resizeCanvas() {
    const container = canvas.parentElement;
    const w = container.clientWidth;
    const h = container.clientHeight || 500;
    canvas.width = w;
    canvas.height = h;
    if (preview) preview.resize();
}
```

No code change needed — the canvas parent (`flex-1`) will naturally shrink to accommodate the slider's `w-8` (32px). Just verify `resizeCanvas()` is triggered by the layout change.

### Phase 4: Layer Count Labels
Add top/bottom labels showing layer range:
- **Top**: max layer number (e.g., "150")
- **Bottom**: "0"
- **Alongside thumb**: current layer tooltip (optional, nice-to-have)

### Phase 5: Touch Support
Vertical sliders can be awkward on mobile. Consider:
- Larger touch target (thumb: 24px instead of 16px on mobile)
- Swipe gesture on the canvas to change layers (optional)
- Keep layer prev/next buttons visible for accessibility

## Constraints
- **Browser compatibility**: `appearance: slider-vertical` is non-standard; `writing-mode` approach works more broadly
- **RTL direction**: needed so dragging UP increases layer number (natural mapping: top=high layer)
- **Slider height**: must match canvas height for intuitive correspondence
- **Responsive**: on very narrow screens, may need to revert to horizontal or collapse
- **No JS changes to viewer.js** — `onLayerChange()`, `preview.endLayer`, `preview.render()` all stay the same

## Estimated Complexity
- HTML restructure: ~40 lines changed
- CSS additions: ~30 lines
- Responsive adjustments: ~20 lines
- Testing: visual verification across browsers
- **Total: ~90 lines, Low complexity**
- **Mostly a CSS/HTML task** — no API or backend changes needed
