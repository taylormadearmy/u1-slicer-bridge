/**
 * 3D G-code viewer using gcode-preview + Three.js
 * Alpine.js component wrapping the gcode-preview library
 *
 * IMPORTANT: Three.js objects (preview, camera, vectors) must NOT be stored
 * as Alpine component properties. Alpine wraps properties in Proxies, but
 * Three.js objects have non-configurable properties (modelViewMatrix, etc.)
 * that break under Proxy wrapping. All Three.js state lives in closure
 * variables instead.
 *
 * Performance strategy for large G-code files:
 *   < 8 MB  — tube rendering (best quality, ~2-5s)
 *   8–50 MB — line rendering (good quality, ~5-15s)
 *   > 50 MB — server-side 2D image preview (instant display)
 * A Web Worker handles download + text cleaning off the main thread.
 */

function gcodeViewer(initialJobId, initialFilamentColors = null) {
    // Three.js objects live here — outside Alpine's reactive scope
    let preview = null;
    let initialCameraPos = null;
    let initialControlsTarget = null;
    let resizeHandler = null;

    // Size thresholds (bytes)
    const TUBE_THRESHOLD = 8 * 1024 * 1024;       // Below: tube rendering
    const IMAGE_PREVIEW_THRESHOLD = 50 * 1024 * 1024; // Above: server-side PNG

    return {
        // Only plain JS values in Alpine reactive state
        jobId: initialJobId || null,
        filamentColors: initialFilamentColors || [],
        gcodeSize: 0,
        currentLayer: 0,
        totalLayers: 0,
        serverLayerCount: 0,
        showTravel: false,
        loading: false,
        loadingMessage: 'Initializing...',
        error: null,
        imageMode: false,       // true when showing server-rendered PNG
        previewImageUrl: null,  // URL for the PNG image
        imgZoom: 1,             // Current zoom level for image mode
        imgPanX: 0,             // Pan offset X (px)
        imgPanY: 0,             // Pan offset Y (px)

        /**
         * Alpine.js lifecycle hook — runs on mount
         */
        async init() {
            if (!this.jobId) {
                console.warn('Viewer mounted without job ID');
                this.error = 'No job ID provided';
                return;
            }

            // Fetch job metadata (filament colors, layer count, gcode_size)
            try {
                const job = await api.getJobStatus(this.jobId);
                if (!this.filamentColors || this.filamentColors.length === 0) {
                    this.filamentColors = job.filament_colors || [];
                }
                this.serverLayerCount = job.metadata?.layer_count || 0;
                this.gcodeSize = job.gcode_size || 0;
            } catch (e) {
                console.warn('Could not fetch job metadata:', e);
            }

            // Wait for next frame to ensure DOM has laid out
            await new Promise(resolve => requestAnimationFrame(resolve));

            // Large files: server-side PNG image preview
            if (this.gcodeSize > IMAGE_PREVIEW_THRESHOLD) {
                await this.loadImagePreview();
            } else {
                await this.loadViewer();
            }
        },

        /**
         * Load a server-rendered 2D preview image for large files.
         * Fast — the server renders the image, browser just displays it.
         */
        async loadImagePreview() {
            this.imageMode = true;
            this.loading = true;
            this.loadingMessage = 'Generating preview image...';

            try {
                const url = `/api/jobs/${this.jobId}/gcode/preview-image?size=800`;
                const response = await fetch(url);
                if (!response.ok) {
                    throw new Error(`Server returned ${response.status}`);
                }

                const blob = await response.blob();
                this.previewImageUrl = URL.createObjectURL(blob);
                this.totalLayers = this.serverLayerCount || 0;
                this.currentLayer = this.totalLayers > 0 ? this.totalLayers - 1 : 0;
                this.loading = false;

                const mb = (this.gcodeSize / 1024 / 1024).toFixed(0);
                console.log(`Large G-code (${mb} MB) — using server-side image preview`);
            } catch (err) {
                console.error('Failed to load preview image:', err);
                this.error = `Failed to generate preview: ${err.message}`;
                this.loading = false;
            }
        },

        /**
         * Initialize the 3D preview
         */
        async loadViewer() {
            const canvas = this.$refs.canvas;
            if (!canvas) {
                this.error = 'Canvas element not found';
                return;
            }

            // Wait for container to have stable dimensions
            const container = canvas.parentElement;
            let attempts = 0;
            let lastWidth = 0;
            let stableCount = 0;

            while (attempts < 30) {
                const currentWidth = container.clientWidth;
                if (currentWidth >= 100) {
                    if (currentWidth === lastWidth) {
                        stableCount++;
                        if (stableCount >= 2) break;
                    } else {
                        stableCount = 0;
                    }
                }
                lastWidth = currentWidth;
                await new Promise(resolve => requestAnimationFrame(resolve));
                attempts++;
            }

            if (container.clientWidth < 100) {
                this.error = 'Failed to initialize canvas';
                return;
            }

            // Check WebGL support
            const testCanvas = document.createElement('canvas');
            const gl = testCanvas.getContext('webgl') || testCanvas.getContext('experimental-webgl');
            if (!gl) {
                this.error = 'G-code preview requires hardware acceleration (WebGL). To fix in Chrome: Settings → System → enable "Use graphics acceleration when available" → Relaunch.';
                return;
            }

            try {
                this.loading = true;
                this.loadingMessage = 'Initializing 3D viewer...';

                // Build color array for tools — plain strings, escaped from Alpine proxy
                const colors = this.filamentColors.length > 0
                    ? [...this.filamentColors].map(c => String(c))
                    : ['#3b82f6']; // Default blue

                // Pick rendering mode based on G-code size.
                // Default to line rendering (safe) if size is unknown.
                const useTubes = this.gcodeSize > 0 && this.gcodeSize <= TUBE_THRESHOLD;

                if (!useTubes && this.gcodeSize > 0) {
                    console.log(`Medium G-code (${(this.gcodeSize/1024/1024).toFixed(1)} MB) — using line rendering`);
                }

                // Initialize gcode-preview with tool colors array.
                // disableGradient: the default gradient replaces lightness (0.1–0.8)
                // which turns black filament into gray/white (S=0 means no hue preserved).
                preview = GCodePreview.init({
                    canvas: canvas,
                    buildVolume: { x: 270, y: 270, z: 270 },
                    initialCameraPosition: [0, 400, 350],
                    extrusionColor: colors,
                    backgroundColor: '#1a1a1a',
                    renderTravel: false,
                    disableGradient: true,
                    renderTubes: useTubes,
                    extrusionWidth: 0.45,
                });

                // Improve tube lighting for better contrast between adjacent lines.
                // The library's defaults (ambient + overhead point) give flat, uniform
                // illumination. Adding a low-angle directional light creates shadows
                // between toolpaths, making individual lines distinguishable.
                if (preview.scene) {
                    // Replace library's default lights (ambient 0.3π + point π)
                    // with balanced setup that creates contrast without washing out
                    preview.scene.children
                        .filter(c => c.isAmbientLight || c.isPointLight)
                        .forEach(c => preview.scene.remove(c));
                    preview.scene.add(new THREE.AmbientLight(0xffffff, 0.4));
                    const dirLight = new THREE.DirectionalLight(0xffffff, 1.2);
                    dirLight.position.set(-200, 150, 300); // Low angle from front-left
                    preview.scene.add(dirLight);
                }

                // Match OrcaSlicer mouse controls: left=rotate, middle/right=pan, scroll=zoom
                if (preview.controls) {
                    preview.controls.mouseButtons = {
                        LEFT: THREE.MOUSE.ROTATE,
                        MIDDLE: THREE.MOUSE.PAN,
                        RIGHT: THREE.MOUSE.PAN,
                    };
                }

                // Store initial camera state for reset (in closure)
                initialCameraPos = preview.camera.position.clone();
                initialControlsTarget = preview.controls
                    ? preview.controls.target.clone()
                    : new THREE.Vector3(135, 0, 135);

                // Stop the library's animation loop during loading.
                // gcode-preview runs requestAnimationFrame continuously, re-rendering
                // on every frame even while we're adding geometry chunks. This wastes
                // CPU/GPU and shows ugly partial renders behind the loading overlay.
                preview.cancelAnimation();

                // Download G-code (full file for <50MB)
                const downloadUrl = `/api/jobs/${this.jobId}/download`;

                // Download and clean G-code via Web Worker (off main thread),
                // then feed chunks to gcode-preview on the main thread.
                const chunks = await this._downloadViaWorker(downloadUrl);

                // Feed chunks to gcode-preview cooperatively.
                // Use requestAnimationFrame between chunks so the browser can
                // paint progress updates and stay responsive.
                for (let i = 0; i < chunks.length; i++) {
                    preview.processGCode(chunks[i]);
                    const pct = Math.min(100, Math.round((i + 1) / chunks.length * 100));
                    this.loadingMessage = `Building preview... ${pct}%`;
                    await new Promise(r => requestAnimationFrame(r));
                }

                // Slider uses preview.layers.length for 1:1 rendering control.
                // Server layer count is only used for the display label.
                this.totalLayers = preview.layers.length;

                if (this.totalLayers === 0) {
                    throw new Error('No layers found in G-code');
                }

                // Show all layers, then do a single render and restart animation loop
                this.currentLayer = this.totalLayers - 1;
                preview.endLayer = this.totalLayers;
                this.loadingMessage = 'Rendering...';
                await new Promise(r => setTimeout(r, 0));
                preview.render();
                preview.animate(); // Restart animation loop for interactive controls

                this.loading = false;
                const renderMode = useTubes ? 'tubes' : 'lines';
                console.log(`3D viewer initialized: ${this.totalLayers} layers, ${colors.length} tool color(s), ${renderMode}`);

                // Handle window resize
                resizeHandler = () => {
                    if (!preview) return;
                    const c = this.$refs.canvas;
                    if (!c) return;
                    const cont = c.parentElement;
                    preview.resize(cont.clientWidth, cont.clientHeight);
                };
                window.addEventListener('resize', resizeHandler);

            } catch (err) {
                console.error('Failed to initialize viewer:', err);
                this.error = `Failed to load G-code preview: ${err.message}`;
                this.loading = false;
            }
        },

        /**
         * Download and process G-code in a Web Worker.
         * Returns an array of line-array chunks ready for processGCode().
         * Falls back to synchronous fetch if Worker is unavailable.
         */
        async _downloadViaWorker(url) {
            // Fallback for environments without Worker support
            if (typeof Worker === 'undefined') {
                return this._downloadSynchronous(url);
            }

            const chunks = [];
            const self = this;

            return new Promise((resolve, reject) => {
                const worker = new Worker('viewer-worker.js');

                worker.onmessage = (e) => {
                    const msg = e.data;
                    switch (msg.type) {
                        case 'progress':
                            if (msg.phase === 'download') {
                                if (msg.percent >= 0) {
                                    self.loadingMessage = `Downloading G-code... ${msg.percent}%`;
                                } else {
                                    self.loadingMessage = `Downloading G-code... ${msg.receivedMB} MB`;
                                }
                            } else {
                                self.loadingMessage = 'Processing G-code...';
                            }
                            break;
                        case 'chunk':
                            chunks.push(msg.lines);
                            break;
                        case 'done': {
                            worker.terminate();
                            const info = msg.decimationFactor > 1
                                ? ` (decimated 1/${msg.decimationFactor})`
                                : '';
                            console.log(`Worker delivered ${msg.totalLines} lines in ${chunks.length} chunks${info}`);
                            resolve(chunks);
                            break;
                        }
                        case 'error':
                            worker.terminate();
                            reject(new Error(msg.message));
                            break;
                    }
                };

                worker.onerror = (err) => {
                    worker.terminate();
                    // Worker failed to load — fall back to synchronous
                    console.warn('Worker failed, falling back to synchronous:', err.message);
                    self._downloadSynchronous(url).then(resolve, reject);
                };

                worker.postMessage({ type: 'start', url });
            });
        },

        /**
         * Synchronous fallback: download + clean on main thread.
         * Used when Web Workers are unavailable.
         */
        async _downloadSynchronous(url) {
            this.loadingMessage = 'Downloading G-code...';
            await new Promise(r => setTimeout(r, 0));

            const response = await fetch(url);
            if (!response.ok) {
                throw new Error(`Failed to download G-code: HTTP ${response.status}`);
            }

            let text = await response.text();
            this.loadingMessage = 'Processing G-code...';
            await new Promise(r => setTimeout(r, 0));

            text = text.replace(/^(T[A-Z_]{2,}\b.*)/gm, '; $1');
            const lines = text.split('\n');
            text = null;

            // Split into chunks of 10K lines
            const CHUNK = 10000;
            const chunks = [];
            for (let i = 0; i < lines.length; i += CHUNK) {
                chunks.push(lines.slice(i, i + CHUNK));
            }
            return chunks;
        },

        /**
         * Display text for layer counter.
         * Uses server layer count (from OrcaSlicer) when available for accurate display,
         * while slider internally uses preview layer count for correct rendering.
         */
        displayLayerText() {
            if (this.imageMode) {
                // Image mode: just show server layer count
                return this.serverLayerCount > 0 ? `${this.serverLayerCount} layers` : '';
            }
            const displayTotal = this.serverLayerCount > 0 ? this.serverLayerCount : this.totalLayers;
            if (this.serverLayerCount > 0 && this.totalLayers > 0 && this.serverLayerCount !== this.totalLayers) {
                const displayLayer = Math.max(1, Math.round((this.currentLayer + 1) / this.totalLayers * this.serverLayerCount));
                return `${displayLayer} / ${this.serverLayerCount}`;
            }
            return `${this.currentLayer + 1} / ${this.totalLayers}`;
        },

        /**
         * Layer slider change — show layers 1 through (n+1)
         */
        onLayerChange(newLayer) {
            this.currentLayer = newLayer;
            if (!preview) return;
            preview.endLayer = newLayer + 1;
            preview.render();
        },

        /**
         * Navigate to next layer
         */
        nextLayer() {
            if (this.currentLayer < this.totalLayers - 1) {
                this.currentLayer++;
                this.onLayerChange(this.currentLayer);
            }
        },

        /**
         * Navigate to previous layer
         */
        previousLayer() {
            if (this.currentLayer > 0) {
                this.currentLayer--;
                this.onLayerChange(this.currentLayer);
            }
        },

        /**
         * Toggle travel moves visibility
         */
        toggleTravel() {
            if (!preview) return;
            preview.renderTravel = this.showTravel;
            preview.render();
        },

        /**
         * Zoom in by moving camera closer to target
         */
        zoomIn() {
            if (!preview || !preview.controls) return;
            const camera = preview.camera;
            const target = preview.controls.target;
            const dir = new THREE.Vector3().subVectors(target, camera.position).normalize();
            camera.position.addScaledVector(dir, camera.position.distanceTo(target) * 0.2);
            preview.controls.update();
        },

        /**
         * Zoom out by moving camera away from target
         */
        zoomOut() {
            if (!preview || !preview.controls) return;
            const camera = preview.camera;
            const target = preview.controls.target;
            const dir = new THREE.Vector3().subVectors(camera.position, target).normalize();
            camera.position.addScaledVector(dir, camera.position.distanceTo(target) * 0.25);
            preview.controls.update();
        },

        /**
         * Reset camera to initial position
         */
        resetView() {
            if (!preview) return;
            if (initialCameraPos) {
                preview.camera.position.copy(initialCameraPos);
            }
            if (preview.controls && initialControlsTarget) {
                preview.controls.target.copy(initialControlsTarget);
                preview.controls.update();
            }
        },

        /**
         * Image mode: zoom in
         */
        imgZoomIn() {
            this.imgZoom = Math.min(this.imgZoom * 1.3, 10);
        },

        /**
         * Image mode: zoom out
         */
        imgZoomOut() {
            this.imgZoom = Math.max(this.imgZoom / 1.3, 0.5);
        },

        /**
         * Image mode: reset zoom and pan
         */
        imgResetView() {
            this.imgZoom = 1;
            this.imgPanX = 0;
            this.imgPanY = 0;
        },

        /**
         * Image mode: handle mouse wheel zoom (called from @wheel handler)
         */
        imgHandleWheel(event) {
            event.preventDefault();
            const delta = event.deltaY > 0 ? 1 / 1.15 : 1.15;
            const newZoom = Math.min(Math.max(this.imgZoom * delta, 0.5), 10);

            // Zoom toward cursor position
            const rect = event.currentTarget.getBoundingClientRect();
            const cx = event.clientX - rect.left - rect.width / 2;
            const cy = event.clientY - rect.top - rect.height / 2;
            const factor = newZoom / this.imgZoom;
            this.imgPanX = cx - factor * (cx - this.imgPanX);
            this.imgPanY = cy - factor * (cy - this.imgPanY);
            this.imgZoom = newZoom;
        },

        /**
         * Image mode: start drag pan (called from @mousedown / @touchstart)
         */
        imgStartPan(event) {
            const container = event.currentTarget;
            const startX = (event.touches ? event.touches[0].clientX : event.clientX) - this.imgPanX;
            const startY = (event.touches ? event.touches[0].clientY : event.clientY) - this.imgPanY;
            const self = this;

            function onMove(e) {
                const px = (e.touches ? e.touches[0].clientX : e.clientX) - startX;
                const py = (e.touches ? e.touches[0].clientY : e.clientY) - startY;
                self.imgPanX = px;
                self.imgPanY = py;
            }

            function onUp() {
                document.removeEventListener('mousemove', onMove);
                document.removeEventListener('mouseup', onUp);
                document.removeEventListener('touchmove', onMove);
                document.removeEventListener('touchend', onUp);
            }

            document.addEventListener('mousemove', onMove);
            document.addEventListener('mouseup', onUp);
            document.addEventListener('touchmove', onMove, { passive: true });
            document.addEventListener('touchend', onUp);
        },

        /**
         * Cleanup when component is destroyed
         */
        destroy() {
            if (resizeHandler) {
                window.removeEventListener('resize', resizeHandler);
                resizeHandler = null;
            }
            if (preview) {
                preview.dispose();
                preview = null;
            }
            if (this.previewImageUrl) {
                URL.revokeObjectURL(this.previewImageUrl);
                this.previewImageUrl = null;
            }
            initialCameraPos = null;
            initialControlsTarget = null;
        }
    };
}
