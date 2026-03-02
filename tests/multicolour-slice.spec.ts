import { test, expect } from '@playwright/test';
import { API, apiUpload, getDefaultFilament, waitForJobComplete, apiSliceDualColour } from './helpers';

test.describe('Multicolour Slicing End-to-End (M11)', () => {
  test.setTimeout(180_000);

  test('dual-colour file slices with two filament_ids', async ({ request }) => {
    const upload = await apiUpload(request, 'calib-cube-10-dual-colour-merged.3mf');
    expect(upload.detected_colors?.length).toBeGreaterThanOrEqual(2);
    const job = await apiSliceDualColour(request, String(upload.upload_id));
    expect(job.status).toBe('completed');
    expect(job.metadata.layer_count).toBeGreaterThan(0);
  });

  test('dual-colour file stores filament_colors in job', async ({ request }) => {
    const upload = await apiUpload(request, 'calib-cube-10-dual-colour-merged.3mf');
    const job = await apiSliceDualColour(request, String(upload.upload_id), {
      filament_colors: ['#FF0000', '#0000FF'],
    });
    expect(job.status).toBe('completed');

    // Fetch job and check filament_colors
    const jobRes = await request.get(`${API}/jobs/${job.job_id}`, { timeout: 30_000 });
    const jobData = await jobRes.json();
    expect(jobData).toHaveProperty('filament_colors');
    if (jobData.filament_colors) {
      expect(jobData.filament_colors.length).toBeGreaterThanOrEqual(2);
    }
  });

  test('multicolour slice with prime tower succeeds', async ({ request }) => {
    const upload = await apiUpload(request, 'calib-cube-10-dual-colour-merged.3mf');
    const job = await apiSliceDualColour(request, String(upload.upload_id), {
      enable_prime_tower: true,
      prime_tower_width: 40,
    });
    expect(job.status).toBe('completed');
  });

  test('single filament_id auto-expands for dual-colour file (segfault fix)', async ({ request }) => {
    // Previously this caused an OrcaSlicer segfault because model_settings.config
    // referenced extruder 2 but project_settings.config only defined 1 extruder.
    // The backend now auto-expands the filament list to match active extruder count.
    const upload = await apiUpload(request, 'calib-cube-10-dual-colour-merged.3mf');
    expect(upload.detected_colors?.length).toBeGreaterThanOrEqual(2);

    const fil = await getDefaultFilament(request);

    const res = await request.post(`${API}/uploads/${upload.upload_id}/slice`, {
      data: {
        filament_id: fil.id,  // Single filament on a dual-colour file
        layer_height: 0.2,
        infill_density: 15,
        supports: false,
      },
      timeout: 120_000,
    });
    expect(res.ok()).toBe(true);
    const job = await waitForJobComplete(request, await res.json());
    expect(job.status).toBe('completed');
    expect(job.metadata.layer_count).toBeGreaterThan(0);
  });

  test('>4 filament_ids rejected with 400', async ({ request }) => {
    const upload = await apiUpload(request, 'calib-cube-10-dual-colour-merged.3mf');
    const fil = await getDefaultFilament(request);

    const res = await request.post(`${API}/uploads/${upload.upload_id}/slice`, {
      data: {
        filament_ids: [fil.id, fil.id, fil.id, fil.id, fil.id],
        layer_height: 0.2,
        infill_density: 15,
        supports: false,
      },
      timeout: 30_000,
    });
    expect(res.status()).toBe(400);
    const body = await res.json();
    expect(body.detail).toContain('4 extruders');
  });

  test('slice request without filament_id or filament_ids returns 400', async ({ request }) => {
    const upload = await apiUpload(request, 'calib-cube-10-dual-colour-merged.3mf');

    const res = await request.post(`${API}/uploads/${upload.upload_id}/slice`, {
      data: {
        layer_height: 0.2,
        infill_density: 15,
        supports: false,
      },
      timeout: 30_000,
    });
    expect(res.status()).toBe(400);
    const body = await res.json();
    expect(body.detail).toContain('filament_id');
  });

  test('extruder_assignments [2,3] produces G-code with T2/T3 tools, not T0/T1 (regression)', async ({ request }) => {
    // Regression: when mapping dual-colour to E3+E4 (assignments [2,3]),
    // the slicer used to generate T0/T1 and post-process remap. This caused
    // start G-code to prime E1+E2 instead of E3+E4 because is_extruder_used[]
    // was wrong. Fix: pre-slice remap in the 3MF so OrcaSlicer knows the
    // correct extruder positions natively.
    const upload = await apiUpload(request, 'calib-cube-10-dual-colour-merged.3mf');
    const fil = await getDefaultFilament(request);

    // Positional array: [E1, E2, E3, E4] — E1/E2 are gap-fillers
    const res = await request.post(`${API}/uploads/${upload.upload_id}/slice`, {
      data: {
        filament_ids: [fil.id, fil.id, fil.id, fil.id],
        extruder_assignments: [2, 3],
        layer_height: 0.2,
        infill_density: 15,
        supports: false,
      },
      timeout: 120_000,
    });
    expect(res.ok()).toBe(true);
    const job = await waitForJobComplete(request, await res.json());
    expect(job.status).toBe('completed');

    // Download G-code and verify tool usage
    const dlRes = await request.get(`${API}/jobs/${job.job_id}/download`, { timeout: 120_000 });
    expect(dlRes.ok()).toBe(true);
    const gcode = await dlRes.text();

    // Must use T2 and T3 (E3 and E4, 0-indexed)
    expect(gcode).toContain('T2');
    expect(gcode).toContain('T3');

    // Must NOT have T0 or T1 tool changes in the print body
    // (T0/T1 may appear in comments, so check for actual tool change commands)
    const toolChangeLines = gcode.split('\n').filter(
      (line) => /^T[0-3]\s*$/.test(line.trim())
    );
    const tools = new Set(toolChangeLines.map((l) => l.trim()));
    expect(tools.has('T0')).toBe(false);
    expect(tools.has('T1')).toBe(false);
  });

  test('extruder_assignments [2,3] primes E3+E4, not E1+E2 (regression)', async ({ request }) => {
    // Regression: start G-code SM_PRINT_AUTO_FEED and SM_PRINT_START_LINE
    // must target the assigned extruders (E3=INDEX 2, E4=INDEX 3), not the
    // default E1/E2. This verifies that is_extruder_used[] is correct.
    const upload = await apiUpload(request, 'calib-cube-10-dual-colour-merged.3mf');
    const fil = await getDefaultFilament(request);

    const res = await request.post(`${API}/uploads/${upload.upload_id}/slice`, {
      data: {
        filament_ids: [fil.id, fil.id, fil.id, fil.id],
        extruder_assignments: [2, 3],
        layer_height: 0.2,
        infill_density: 15,
        supports: false,
      },
      timeout: 120_000,
    });
    expect(res.ok()).toBe(true);
    const job = await waitForJobComplete(request, await res.json());
    expect(job.status).toBe('completed');

    const dlRes = await request.get(`${API}/jobs/${job.job_id}/download`, { timeout: 120_000 });
    expect(dlRes.ok()).toBe(true);
    const gcode = await dlRes.text();

    // Filter to executable lines only (OrcaSlicer appends the raw template
    // as a "; machine_start_gcode = ..." comment which contains all indices).
    const execLines = gcode.split('\n').filter((l) => !l.trimStart().startsWith(';'));
    const execGcode = execLines.join('\n');

    // Start line macros must target E3 and E4 (INDEX=2 and INDEX=3)
    expect(execGcode).toMatch(/SM_PRINT_START_LINE\s+INDEX=2/);
    expect(execGcode).toMatch(/SM_PRINT_START_LINE\s+INDEX=3/);

    // Must NOT prime E1 or E2
    expect(execGcode).not.toMatch(/SM_PRINT_START_LINE\s+INDEX=0/);
    expect(execGcode).not.toMatch(/SM_PRINT_START_LINE\s+INDEX=1/);

    // Auto-feed must also target E3 and E4 only
    expect(execGcode).toMatch(/SM_PRINT_AUTO_FEED\s+EXTRUDER=2/);
    expect(execGcode).toMatch(/SM_PRINT_AUTO_FEED\s+EXTRUDER=3/);
    expect(execGcode).not.toMatch(/SM_PRINT_AUTO_FEED\s+EXTRUDER=0/);
    expect(execGcode).not.toMatch(/SM_PRINT_AUTO_FEED\s+EXTRUDER=1/);
  });

  test('assignments [2,3] stores full positional filament_colors in job (viewer regression)', async ({ request }) => {
    // Regression: filament_colors stored only active-position colors (2 entries)
    // losing positional info. Viewer got ['red','blue'] and labelled E1/E2 instead
    // of E3/E4. Fix: store full 4-slot positional array with #FFFFFF for unused.
    const upload = await apiUpload(request, 'calib-cube-10-dual-colour-merged.3mf');
    const fil = await getDefaultFilament(request);

    const res = await request.post(`${API}/uploads/${upload.upload_id}/slice`, {
      data: {
        filament_ids: [fil.id, fil.id, fil.id, fil.id],
        extruder_assignments: [2, 3],
        filament_colors: ['#FFFFFF', '#FFFFFF', '#FF0000', '#0000FF'],
        layer_height: 0.2,
        infill_density: 15,
        supports: false,
      },
      timeout: 120_000,
    });
    expect(res.ok()).toBe(true);
    const job = await waitForJobComplete(request, await res.json());
    expect(job.status).toBe('completed');

    // Fetch job and verify filament_colors is a full positional array
    const jobRes = await request.get(`${API}/jobs/${job.job_id}`, { timeout: 30_000 });
    const jobData = await jobRes.json();
    expect(jobData.filament_colors).toBeDefined();
    // Must have at least 4 entries (full positional array, not just 2)
    expect(jobData.filament_colors.length).toBeGreaterThanOrEqual(4);
    // E3 (index 2) and E4 (index 3) must have the assigned colors
    expect(jobData.filament_colors[2]).toBe('#FF0000');
    expect(jobData.filament_colors[3]).toBe('#0000FF');
    // E1/E2 (indices 0,1) should be #FFFFFF (unused placeholder)
    expect(jobData.filament_colors[0]).toBe('#FFFFFF');
    expect(jobData.filament_colors[1]).toBe('#FFFFFF');
  });

  test('sparse filament_ids [A,B] + assignments [2,3] heats E3+E4, not all-zero (API regression)', async ({ request }) => {
    // Regression: when API consumer sends only 2 filament_ids (not 4 gap-fillers)
    // with extruder_assignments [2,3], the sequential temp array [T_A, T_B, "0", "0"]
    // was padded then zeroed — resulting in all nozzles at 0°C because the active
    // slots (2,3) held padding zeros. Fix: scatter temps into assigned positions.
    const upload = await apiUpload(request, 'calib-cube-10-dual-colour-merged.3mf');
    const fil = await getDefaultFilament(request);

    // Only 2 filament_ids — no gap-fillers, direct API usage
    const res = await request.post(`${API}/uploads/${upload.upload_id}/slice`, {
      data: {
        filament_ids: [fil.id, fil.id],
        extruder_assignments: [2, 3],
        layer_height: 0.2,
        infill_density: 15,
        supports: false,
      },
      timeout: 120_000,
    });
    expect(res.ok()).toBe(true);
    const job = await waitForJobComplete(request, await res.json());
    expect(job.status).toBe('completed');

    // Download G-code and verify nozzle temps are non-zero for active extruders
    const dlRes = await request.get(`${API}/jobs/${job.job_id}/download`, { timeout: 120_000 });
    expect(dlRes.ok()).toBe(true);
    const gcode = await dlRes.text();

    // Verify correct tool numbers (T2/T3, not T0/T1)
    const toolChangeLines = gcode.split('\n').filter(
      (line) => /^T[0-3]\s*$/.test(line.trim())
    );
    const tools = new Set(toolChangeLines.map((l) => l.trim()));
    expect(tools.has('T2')).toBe(true);
    expect(tools.has('T3')).toBe(true);
    expect(tools.has('T0')).toBe(false);
    expect(tools.has('T1')).toBe(false);

    // Verify nozzle temperature commands exist with non-zero values.
    // M104/M109 set nozzle temp; active extruders must have real temps.
    const tempLines = gcode.split('\n').filter(
      (line) => /^M10[49]\s/.test(line.trim()) && !/^;/.test(line.trim())
    );
    const temps = tempLines.map((l) => {
      const m = l.match(/S(\d+)/);
      return m ? parseInt(m[1], 10) : 0;
    });
    // At least some temp commands must be non-zero (filament nozzle temp)
    const nonZeroTemps = temps.filter((t) => t > 0);
    expect(nonZeroTemps.length).toBeGreaterThan(0);
  });
});
