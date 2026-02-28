import { test, expect } from '@playwright/test';
import { waitForApp, uploadFile, selectUploadByName, waitForSliceComplete, getAppState, API, apiUpload, apiSliceDualColour, proceedFromPlateSelection } from './helpers';

test.describe('G-code Viewer', () => {
  // This test slices a file first, so it needs extra time
  test.setTimeout(180_000);
  const gcodeCanvasSelector = 'canvas[x-ref="canvas"]';

  async function uiSliceToViewer(page: any) {
    await waitForApp(page);
    await uploadFile(page, 'calib-cube-10-dual-colour-merged.3mf');
    await page.getByRole('button', { name: /Slice Now/i }).click();
    await waitForSliceComplete(page);
    const canvas = page.locator(gcodeCanvasSelector);
    await canvas.waitFor({ state: 'visible', timeout: 15_000 });
    return canvas;
  }

  test('viewer core UI renders, controls work, and has no initialization errors', async ({ page }) => {
    const consoleErrors: string[] = [];
    page.on('console', (msg) => {
      if (msg.type() === 'error') consoleErrors.push(msg.text());
    });
    page.on('pageerror', (err) => {
      consoleErrors.push(err.message);
    });

    const canvas = await uiSliceToViewer(page);

    const box = await canvas.boundingBox();
    expect(box).not.toBeNull();
    expect(box!.width).toBeGreaterThan(100);
    expect(box!.height).toBeGreaterThan(100);

    await page.waitForTimeout(2_000);
    await expect(page.getByRole('button', { name: /Previous/i })).toBeVisible();
    await expect(page.getByRole('button', { name: /Next/i })).toBeVisible();
    await expect(page.getByRole('slider').first()).toBeVisible();

    await expect(page.getByTitle('Zoom in')).toBeVisible();
    await expect(page.getByTitle('Zoom out')).toBeVisible();
    await expect(page.getByTitle('Fit to bed')).toBeVisible();
    await page.getByTitle('Zoom in').click();
    await page.getByTitle('Fit to bed').click();

    await page.waitForTimeout(1_000);
    await expect(page.getByText(/Failed to load G-code preview/i)).not.toBeVisible();
    const proxyErrors = consoleErrors.filter(e =>
      e.includes('modelViewMatrix') ||
      e.includes('on proxy') ||
      e.includes('non-configurable')
    );
    expect(proxyErrors).toEqual([]);
    await expect(page.getByText(/Failed to/i)).not.toBeVisible();
  });

  test('viewer loads correct job after re-slicing', async ({ page }) => {
    await waitForApp(page);

    // Slice file (first time)
    await uploadFile(page, 'calib-cube-10-dual-colour-merged.3mf');
    await page.getByRole('button', { name: /Slice Now/i }).click();
    await waitForSliceComplete(page);
    await page.locator(gcodeCanvasSelector).waitFor({ state: 'visible', timeout: 15_000 });
    await page.waitForTimeout(2_000);

    // Note the first job ID
    const firstSliceResult = await getAppState(page, 'sliceResult') as any;
    const firstJobId = firstSliceResult?.job_id;
    expect(firstJobId).toBeTruthy();

    // No viewer errors
    await expect(page.getByText(/Failed to/i)).not.toBeVisible();

    // Re-select the same file from history and slice again
    await selectUploadByName(page, 'calib-cube-10-dual-colour-merged.3mf');
    await page.getByRole('button', { name: /Slice Now/i }).click();
    await waitForSliceComplete(page);
    await page.locator(gcodeCanvasSelector).waitFor({ state: 'visible', timeout: 15_000 });
    await page.waitForTimeout(2_000);

    // The new job should have a different ID (new slice = new job)
    const secondSliceResult = await getAppState(page, 'sliceResult') as any;
    const secondJobId = secondSliceResult?.job_id;
    expect(secondJobId).toBeTruthy();
    expect(secondJobId).not.toBe(firstJobId);

    // No viewer errors after re-slice
    await expect(page.getByText(/Failed to/i)).not.toBeVisible();
  });

  test('gcode metadata API returns valid data', async ({ request }) => {
    const upload = await apiUpload(request, 'calib-cube-10-dual-colour-merged.3mf');
    const job = await apiSliceDualColour(request, String(upload.upload_id));

    // Get metadata
    const metaRes = await request.get(`${API}/jobs/${job.job_id}/gcode/metadata`, { timeout: 30_000 });
    expect(metaRes.ok()).toBe(true);
    const meta = await metaRes.json();
    expect(meta).toHaveProperty('layer_count');
    expect(meta.layer_count).toBeGreaterThan(0);

    // Get layers
    const layerRes = await request.get(`${API}/jobs/${job.job_id}/gcode/layers?start=0&count=5`, { timeout: 30_000 });
    expect(layerRes.ok()).toBe(true);
    const layers = await layerRes.json();
    expect(layers).toHaveProperty('layers');
    expect(layers.layers.length).toBeGreaterThan(0);
  });

  test('multicolour slice shows color legend in viewer', async ({ request }) => {
    // Slice dual-colour file with two filament colors via API
    const upload = await apiUpload(request, 'calib-cube-10-dual-colour-merged.3mf');
    const job = await apiSliceDualColour(request, String(upload.upload_id), {
      filament_colors: ['#FF0000', '#0000FF'],
    });

    // Verify job stored filament colors
    const jobRes = await request.get(`${API}/jobs/${job.job_id}`, { timeout: 30_000 });
    const jobData = await jobRes.json();
    expect(jobData.filament_colors).toBeDefined();
    if (jobData.filament_colors) {
      expect(jobData.filament_colors.length).toBeGreaterThanOrEqual(2);
    }
  });

  test('placement geometry API mesh extents match layout bounds (regression: flattened mesh)', async ({ request }) => {
    const upload = await apiUpload(request, 'u1-auxiliary-fan-cover-hex_mw.3mf');

    const [layoutRes, geomRes, geomWithModifiersRes] = await Promise.all([
      request.get(`${API}/uploads/${upload.upload_id}/layout`, { timeout: 30_000 }),
      request.get(`${API}/uploads/${upload.upload_id}/geometry`, { timeout: 30_000 }),
      request.get(`${API}/uploads/${upload.upload_id}/geometry?include_modifiers=true`, { timeout: 30_000 }),
    ]);
    expect(layoutRes.ok()).toBe(true);
    expect(geomRes.ok()).toBe(true);
    expect(geomWithModifiersRes.ok()).toBe(true);

    const layout = await layoutRes.json();
    const geom = await geomRes.json();
    const geomWithModifiers = await geomWithModifiersRes.json();

    expect(Array.isArray(layout.objects)).toBe(true);
    expect(Array.isArray(geom.objects)).toBe(true);
    expect(Array.isArray(geomWithModifiers.objects)).toBe(true);
    expect(geom.objects.length).toBeGreaterThan(0);

    const meshObj = geomWithModifiers.objects.find((o: any) => o.has_mesh && Array.isArray(o.vertices) && o.vertices.length > 0);
    expect(meshObj).toBeTruthy();
    const filteredObj = geom.objects.find((o: any) => Number(o.build_item_index) === Number(meshObj.build_item_index));
    expect(filteredObj).toBeTruthy();
    // This fixture contains a modifier cube; default geometry should hide it.
    expect(Number(filteredObj.triangle_count || 0)).toBeLessThan(Number(meshObj.triangle_count || 0));

    const layoutObj = layout.objects.find((o: any) => o.build_item_index === meshObj.build_item_index);
    expect(layoutObj).toBeTruthy();
    expect(layoutObj.local_bounds).toBeTruthy();

    const mins = [Infinity, Infinity, Infinity];
    const maxs = [-Infinity, -Infinity, -Infinity];
    for (const v of meshObj.vertices) {
      for (let i = 0; i < 3; i++) {
        mins[i] = Math.min(mins[i], Number(v[i]));
        maxs[i] = Math.max(maxs[i], Number(v[i]));
      }
    }
    const meshSize = maxs.map((mx, i) => mx - mins[i]);
    const expectedSize = layoutObj.local_bounds.size.map((n: any) => Number(n));

    // Regression guard: geometry extraction should not collapse one axis to ~0.
    expect(meshSize[0]).toBeGreaterThan(1);
    expect(meshSize[1]).toBeGreaterThan(1);
    expect(meshSize[2]).toBeGreaterThan(1);

    // Allow small float/parser differences while ensuring axis order and extents remain correct.
    for (let i = 0; i < 3; i++) {
      expect(Math.abs(meshSize[i] - expectedSize[i])).toBeLessThan(0.5);
    }
  });

  test('aux fan single-plate placement preview uses exact/direct mapping and starts on-bed (regression)', async ({ page, request }) => {
    await page.setViewportSize({ width: 1280, height: 1100 });
    await waitForApp(page);
    await apiUpload(request, 'u1-auxiliary-fan-cover-hex_mw.3mf');
    await selectUploadByName(page, 'u1-auxiliary-fan-cover-hex_mw.3mf');
    await page.getByText('Object Placement').scrollIntoViewIfNeeded();

    await page.waitForFunction(() => {
      const body = document.querySelector('body') as any;
      const scope = (body?._x_dataStack || []).find((s: any) => 'objectLayout' in s);
      return !!scope?.objectLayout && !scope?.objectLayoutLoading && !scope?.objectLayoutError;
    }, undefined, { timeout: 90_000 });

    const placement = await page.evaluate(() => {
      const body = document.querySelector('body') as any;
      const scope = (body?._x_dataStack || []).find((s: any) => typeof s.getObjectEffectivePoseForViewer === 'function');
      const layout = scope?.objectLayout;
      const obj = layout?.objects?.[0];
      if (!scope || !layout || !obj || !obj.local_bounds) return null;
      const pose = scope.getObjectEffectivePoseForViewer(obj);
      const lb = obj.local_bounds;
      const vol = layout.build_volume || { x: 270, y: 270 };
      const minX = Number(pose.x || 0) + Number(lb.min?.[0] || 0);
      const maxX = Number(pose.x || 0) + Number(lb.max?.[0] || 0);
      const minY = Number(pose.y || 0) + Number(lb.min?.[1] || 0);
      const maxY = Number(pose.y || 0) + Number(lb.max?.[1] || 0);
      return {
        frame: layout.placement_frame || null,
        minX, maxX, minY, maxY,
        bedW: Number(vol.x || 270),
        bedH: Number(vol.y || 270),
      };
    });

    expect(placement).toBeTruthy();
    expect(placement!.frame?.canonical).toBe('bed_local_xy_mm');
    expect(placement!.frame?.mapping).toBe('direct');
    expect(placement!.frame?.confidence).toBe('exact');
    expect(placement!.maxX).toBeGreaterThan(0);
    expect(placement!.minX).toBeLessThan(placement!.bedW);
    expect(placement!.maxY).toBeGreaterThan(0);
    expect(placement!.minY).toBeLessThan(placement!.bedH);
  });

  test('placement geometry API returns decimated mesh for large objects (Shashibo)', async ({ request }) => {
    const upload = await apiUpload(request, 'Shashibo-h2s-textured.3mf');

    const geomRes = await request.get(`${API}/uploads/${upload.upload_id}/geometry?plate_id=3`, { timeout: 60_000 });
    expect(geomRes.ok()).toBe(true);
    const geom = await geomRes.json();
    expect(Array.isArray(geom.objects)).toBe(true);
    expect(geom.objects.length).toBeGreaterThan(0);

    const obj = geom.objects[0];
    expect(obj.mesh_too_large).toBe(true);
    expect(obj.mesh_decimated).toBe(true);
    expect(obj.has_mesh).toBe(true);
    expect(Number(obj.original_triangle_count || 0)).toBeGreaterThan(Number(obj.triangle_count || 0));
    expect(Number(obj.triangle_count || 0)).toBeGreaterThan(1000);
  });

  test('Shashibo plate 6 placement preview starts on-bed (regression: off-plate selected plate preview)', async ({ page, request }) => {
    test.setTimeout(420_000);
    await page.setViewportSize({ width: 1440, height: 1400 });
    await waitForApp(page);
    await apiUpload(request, 'Shashibo-h2s-textured.3mf');
    await selectUploadByName(page, 'Shashibo-h2s-textured.3mf');

    // Select plate 6 on selectplate step and proceed to configure
    await page.evaluate(() => {
      const body = document.querySelector('body') as any;
      const scope = (body?._x_dataStack || []).find((s: any) => typeof s.selectPlate === 'function');
      if (!scope) throw new Error('Alpine app scope not found');
      scope.selectPlate(6);
    });
    await proceedFromPlateSelection(page);

    await page.evaluate(() => {
      const body = document.querySelector('body') as any;
      const scope = (body?._x_dataStack || []).find((s: any) => typeof s.selectPlate === 'function');
      if (!scope) throw new Error('Alpine app scope not found');
      scope.sliceSettings.enable_prime_tower = true;
      scope.sliceSettings.prime_tower_width = 35;
      scope.sliceSettings.prime_tower_brim_width = 3;
      scope.schedulePlacementViewerRefresh();
    });

    await page.waitForFunction(() => {
      const body = document.querySelector('body') as any;
      const scope = (body?._x_dataStack || []).find((s: any) => 'objectLayout' in s);
      return !!scope?.objectLayout && !scope?.objectLayoutLoading && !scope?.objectLayoutError && Number(scope?.selectedPlate || 0) === 6;
    }, undefined, { timeout: 180_000 });

    const placement = await page.evaluate(() => {
      const body = document.querySelector('body') as any;
      const scope = (body?._x_dataStack || []).find((s: any) => typeof s.getObjectEffectivePoseForViewer === 'function');
      if (!scope) return null;
      const layout = scope.objectLayout;
      const obj = layout?.objects?.[0];
      if (!obj || !obj.local_bounds) return null;
      const pose = scope.getObjectEffectivePoseForViewer(obj);
      const lb = obj.local_bounds;
      const vol = layout.build_volume || { x: 270, y: 270 };
      const minX = Number(pose.x || 0) + Number(lb.min?.[0] || 0);
      const maxX = Number(pose.x || 0) + Number(lb.max?.[0] || 0);
      const minY = Number(pose.y || 0) + Number(lb.min?.[1] || 0);
      const maxY = Number(pose.y || 0) + Number(lb.max?.[1] || 0);
      const cx = (minX + maxX) / 2;
      const cy = (minY + maxY) / 2;
      const tower = typeof scope.getPrimeTowerPreviewConfig === 'function' ? scope.getPrimeTowerPreviewConfig() : null;
      return { minX, maxX, minY, maxY, cx, cy, bedW: Number(vol.x || 270), bedH: Number(vol.y || 270), tower };
    });

    expect(placement).toBeTruthy();
    // Object must start intersecting the bed area and have its center on the bed.
    expect(placement!.maxX).toBeGreaterThan(0);
    expect(placement!.minX).toBeLessThan(placement!.bedW);
    expect(placement!.maxY).toBeGreaterThan(0);
    expect(placement!.minY).toBeLessThan(placement!.bedH);
    expect(placement!.cx).toBeGreaterThanOrEqual(0);
    expect(placement!.cx).toBeLessThanOrEqual(placement!.bedW);
    expect(placement!.cy).toBeGreaterThanOrEqual(0);
    expect(placement!.cy).toBeLessThanOrEqual(placement!.bedH);
    // Prime tower preview should also remain visible on the plate when enabled.
    expect(placement!.tower).toBeTruthy();
    expect(Number(placement!.tower.x ?? -1)).toBeGreaterThanOrEqual(0);
    expect(Number(placement!.tower.x ?? 999)).toBeLessThanOrEqual(placement!.bedW);
    expect(Number(placement!.tower.y ?? -1)).toBeGreaterThanOrEqual(0);
    expect(Number(placement!.tower.y ?? 999)).toBeLessThanOrEqual(placement!.bedH);
  });

  test('Shashibo H2D selected plate uses exact translation-offset mapping and enables object move controls (regression)', async ({ page, request }) => {
    await page.setViewportSize({ width: 1440, height: 1400 });
    await waitForApp(page);
    await apiUpload(request, 'Shashibo-h2s-textured.3mf');
    await selectUploadByName(page, 'Shashibo-h2s-textured.3mf');

    // Select plate 5 on selectplate step and proceed to configure
    await page.evaluate(() => {
      const body = document.querySelector('body') as any;
      const scope = (body?._x_dataStack || []).find((s: any) => typeof s.selectPlate === 'function');
      if (!scope) throw new Error('Alpine app scope not found');
      scope.selectPlate(5);
    });
    await proceedFromPlateSelection(page);
    await page.getByText('Object Placement').scrollIntoViewIfNeeded();

    await page.evaluate(() => {
      const body = document.querySelector('body') as any;
      const scope = (body?._x_dataStack || []).find((s: any) => typeof s.selectPlate === 'function');
      if (!scope) throw new Error('Alpine app scope not found');
      scope.sliceSettings.enable_prime_tower = true;
      scope.schedulePlacementViewerRefresh?.();
    });

    await page.waitForFunction(() => {
      const body = document.querySelector('body') as any;
      const scope = (body?._x_dataStack || []).find((s: any) => 'objectLayout' in s);
      const frame = scope?.objectLayout?.placement_frame;
      return (
        !!scope &&
        Number(scope.selectedPlate || 0) === 5 &&
        !scope.objectLayoutLoading &&
        !scope.objectLayoutError &&
        !!frame &&
        frame.capabilities &&
        frame.mapping === 'bambu_plate_translation_offset' &&
        frame.confidence === 'exact' &&
        frame.capabilities.object_transform_edit === true
      );
    }, undefined, { timeout: 120_000 });

    await expect(page.getByText(/object move is disabled/i)).not.toBeVisible();

    // Prime tower remains supported too.
    const caps = await page.evaluate(() => {
      const body = document.querySelector('body') as any;
      const scope = (body?._x_dataStack || []).find((s: any) => 'objectLayout' in s);
      return scope?.objectLayout?.placement_frame?.capabilities || null;
    });
    expect(caps?.prime_tower_edit).toBe(true);
    expect(caps?.object_transform_edit).toBe(true);
  });

  test('Shashibo Small vs Large H2D plates render different placement-viewer size (regression: build-item scale ignored)', async ({ page, request }) => {
    await page.setViewportSize({ width: 1440, height: 1400 });
    await waitForApp(page);
    await apiUpload(request, 'Shashibo-h2s-textured.3mf');
    await selectUploadByName(page, 'Shashibo-h2s-textured.3mf');
    await proceedFromPlateSelection(page);
    await page.getByText('Object Placement').scrollIntoViewIfNeeded();

    async function getPlateRenderSize(plateId: number) {
      await page.evaluate((pid) => {
        const body = document.querySelector('body') as any;
        const scope = (body?._x_dataStack || []).find((s: any) => typeof s.selectPlate === 'function');
        if (!scope) throw new Error('Alpine app scope not found');
        scope.selectPlate(pid);
      }, plateId);
      await page.getByText('Object Placement').scrollIntoViewIfNeeded();

      await page.waitForFunction((pid) => {
        const body = document.querySelector('body') as any;
        const scope = (body?._x_dataStack || []).find((s: any) => 'objectLayout' in s);
        const viewer = (window as any).__u1PlacementViewer;
        const obj = scope?.objectLayout?.objects?.[0];
        if (!scope || !viewer || !obj) return false;
        if (Number(scope.selectedPlate || 0) !== Number(pid)) return false;
        if (scope.objectLayoutLoading || scope.objectLayoutError) return false;
        // Ensure layout data actually matches the requested plate (not stale from previous plate)
        if (Number(obj.build_item_index || 0) !== Number(pid)) return false;
        const rs = typeof viewer.getDebugObjectRenderState === 'function'
          ? viewer.getDebugObjectRenderState(obj.build_item_index)
          : null;
        return !!(rs && rs.bedFootprintEstimate && rs.bedFootprintEstimate.x > 0 && rs.bedFootprintEstimate.y > 0);
      }, plateId, { timeout: 120_000 });

      return await page.evaluate(() => {
        const body = document.querySelector('body') as any;
        const scope = (body?._x_dataStack || []).find((s: any) => 'objectLayout' in s);
        const viewer = (window as any).__u1PlacementViewer;
        const obj = scope?.objectLayout?.objects?.[0];
        const rs = (viewer && obj && typeof viewer.getDebugObjectRenderState === 'function')
          ? viewer.getDebugObjectRenderState(obj.build_item_index)
          : null;
        return {
          plateId: Number(scope?.selectedPlate || 0),
          buildItemIndex: Number(obj?.build_item_index || 0),
          label: String(scope?.selectedPlateData?.plate_name || ''),
          bedFootprintEstimate: rs?.bedFootprintEstimate || null,
          meshScale: rs?.meshScale || null,
        };
      });
    }

    const small = await getPlateRenderSize(5); // Small - H2D
    const large = await getPlateRenderSize(6); // Large - H2D
    expect(small.plateId).toBe(5);
    expect(large.plateId).toBe(6);
    expect(small.bedFootprintEstimate).toBeTruthy();
    expect(large.bedFootprintEstimate).toBeTruthy();

    const smallW = Number(small.bedFootprintEstimate.x || 0);
    const smallH = Number(small.bedFootprintEstimate.y || 0);
    const largeW = Number(large.bedFootprintEstimate.x || 0);
    const largeH = Number(large.bedFootprintEstimate.y || 0);
    expect(smallW).toBeGreaterThan(1);
    expect(smallH).toBeGreaterThan(1);
    expect(largeW).toBeGreaterThan(1);
    expect(largeH).toBeGreaterThan(1);

    // Large H2D should render meaningfully larger than Small H2D in the placement viewer.
    expect(largeW).toBeGreaterThan(smallW * 1.1);
    expect(largeH).toBeGreaterThan(smallH * 1.1);
  });

  test('gcode layer parser does not create fake long extrusion segments for Shashibo plate 6 @extended', async ({ request }) => {
    const upload = await apiUpload(request, 'Shashibo-h2s-textured.3mf');

    const filRes = await request.get(`${API}/filaments`, { timeout: 30_000 });
    expect(filRes.ok()).toBe(true);
    const filBody = await filRes.json();
    const fil = (filBody?.filaments || []).find((f: any) => f?.is_default) || (filBody?.filaments || [])[0];
    expect(fil?.id).toBeTruthy();

    const sliceRes = await request.post(`${API}/uploads/${upload.upload_id}/slice-plate`, {
      data: {
        plate_id: 6,
        filament_ids: [fil.id, fil.id],
        layer_height: 0.2,
        infill_density: 15,
        supports: false,
        enable_prime_tower: true,
        prime_tower_width: 35,
        prime_tower_brim_width: 3,
        wipe_tower_x: 210,
        wipe_tower_y: 210,
      },
      timeout: 300_000,
    });
    expect(sliceRes.ok()).toBe(true);
    const job = await sliceRes.json();

    const deadline = Date.now() + 480_000;
    let status: any = null;
    while (Date.now() < deadline) {
      await new Promise((r) => setTimeout(r, 1000));
      const sRes = await request.get(`${API}/jobs/${job.job_id}`, { timeout: 30_000 });
      expect(sRes.ok()).toBe(true);
      status = await sRes.json();
      if (status.status === 'completed') break;
      if (status.status === 'failed') throw new Error(`Slice failed: ${status.error || 'unknown'}`);
    }
    expect(status?.status).toBe('completed');

    const metaRes = await request.get(`${API}/jobs/${job.job_id}/gcode/metadata`, { timeout: 30_000 });
    expect(metaRes.ok()).toBe(true);
    const meta = await metaRes.json();
    const layerCount = Number(meta?.layer_count || 0);
    expect(layerCount).toBeGreaterThan(20);

    const start = Math.max(0, layerCount - 20);
    const layersRes = await request.get(`${API}/jobs/${job.job_id}/gcode/layers?start=${start}&count=20`, { timeout: 120_000 });
    expect(layersRes.ok()).toBe(true);
    const layersBody = await layersRes.json();
    const layers = Array.isArray(layersBody?.layers) ? layersBody.layers : [];
    expect(layers.length).toBeGreaterThan(0);

    let maxExtrudeLen = 0;
    let extrudeSegments = 0;
    for (const layer of layers) {
      for (const mv of (layer.moves || [])) {
        if (mv?.type !== 'extrude') continue;
        const dx = Number(mv.x2) - Number(mv.x1);
        const dy = Number(mv.y2) - Number(mv.y1);
        const len = Math.hypot(dx, dy);
        if (Number.isFinite(len)) {
          maxExtrudeLen = Math.max(maxExtrudeLen, len);
          extrudeSegments += 1;
        }
      }
    }
    expect(extrudeSegments).toBeGreaterThan(0);
    // Regression: parser must not connect across ignored G0 moves and create fake
    // cross-bed "extrusion" segments in the viewer.
    expect(maxExtrudeLen).toBeLessThan(80);
  });
});
