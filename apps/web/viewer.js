/**
 * Simple 2D G-code layer viewer using Canvas 2D
 * Mobile-first Alpine.js component
 */

function gcodeViewer(initialJobId, initialFilamentColors = null) {
    return {
        // Canvas and rendering
        canvas: null,
        ctx: null,
        jobId: initialJobId || null,
        filamentColors: initialFilamentColors || [],

        // Layer data
        currentLayer: 0,
        totalLayers: 0,
        layerCache: new Map(),
        bounds: null,

        // View settings
        scale: 1,
        offsetX: 0,
        offsetY: 0,
        showTravel: false,

        // Loading state
        loading: false,
        error: null,

        /**
         * Alpine.js lifecycle hook - runs automatically on mount
         */
        async init() {
            if (!this.jobId) {
                console.warn('Viewer mounted without job ID');
                this.error = 'No job ID provided';
                return;
            }

            // Fetch job to get filament colors if not provided
            if (!this.filamentColors || this.filamentColors.length === 0) {
                try {
                    const job = await api.getJobStatus(this.jobId);
                    this.filamentColors = job.filament_colors || [];
                    console.log('Loaded filament colors:', this.filamentColors);
                } catch (e) {
                    console.warn('Could not fetch filament colors:', e);
                }
            }

            // Wait for next frame to ensure DOM has laid out
            await new Promise(resolve => requestAnimationFrame(resolve));
            await this.loadViewer();
        },

        /**
         * Load viewer data
         */
        async loadViewer() {
            console.log('Initializing viewer with job ID:', this.jobId);
            this.canvas = this.$refs.canvas;
            this.ctx = this.canvas.getContext('2d');

            // Wait for container to have valid dimensions
            // We need at least 100px width to render properly
            const container = this.canvas.parentElement;
            let attempts = 0;
            let lastWidth = 0;
            let stableCount = 0;

            // Wait for container to have a reasonable size AND be stable
            while (attempts < 30) {  // Max 30 frames (~500ms at 60fps)
                const currentWidth = container.clientWidth;

                // Need at least 100px width
                if (currentWidth >= 100) {
                    // Check if size is stable (same for 2 consecutive frames)
                    if (currentWidth === lastWidth) {
                        stableCount++;
                        if (stableCount >= 2) {
                            console.log(`Container ready: ${currentWidth}x${container.clientHeight}`);
                            break;
                        }
                    } else {
                        stableCount = 0;
                    }
                }

                lastWidth = currentWidth;
                await new Promise(resolve => requestAnimationFrame(resolve));
                attempts++;
            }

            if (container.clientWidth < 100) {
                console.error(`Container dimensions too small: ${container.clientWidth}x${container.clientHeight}`);
                this.error = 'Failed to initialize canvas';
                return;
            }

            // Set canvas size
            console.log('Setting initial canvas size...');
            this.resizeCanvas();
            console.log(`Canvas initialized: ${this.canvas.width}x${this.canvas.height} (display: ${this.canvas.style.width} x ${this.canvas.style.height})`);
            window.addEventListener('resize', () => this.resizeCanvas());

            try {
                // Fetch metadata
                console.log('Fetching G-code metadata...');
                const metadata = await api.getGCodeMetadata(this.jobId);
                console.log('Received metadata:', metadata);

                // Validate metadata
                if (!metadata || !metadata.layer_count || !metadata.bounds) {
                    throw new Error(`Invalid metadata: layer_count=${metadata?.layer_count}, bounds=${!!metadata?.bounds}`);
                }

                this.totalLayers = metadata.layer_count;
                this.bounds = metadata.bounds;

                // Calculate scale to fit bed bounds
                this.calculateScale();

                // Load first batch of layers
                console.log('Loading layers 0-20...');
                await this.loadLayers(0, 20);
                console.log('Layers loaded, cache size:', this.layerCache.size);

                // Render first layer if it exists
                if (this.layerCache.has(0)) {
                    console.log('Rendering layer 0...');
                    this.renderLayer(0);
                } else {
                    console.error('Layer 0 not loaded after loadLayers call');
                }
            } catch (err) {
                console.error('Failed to initialize viewer:', err);
                this.error = `Failed to load G-code preview: ${err.message}`;
            }
        },

        /**
         * Resize canvas to fit container
         */
        resizeCanvas() {
            const container = this.canvas.parentElement;
            const dpr = window.devicePixelRatio || 1;

            // Set display size
            this.canvas.style.width = container.clientWidth + 'px';
            this.canvas.style.height = container.clientHeight + 'px';

            // Set actual size in pixels (for retina displays)
            this.canvas.width = container.clientWidth * dpr;
            this.canvas.height = container.clientHeight * dpr;

            // Scale context to match
            this.ctx.scale(dpr, dpr);

            this.calculateScale();
            // Only render if bounds exist and layer is cached (data loaded)
            if (this.bounds && this.layerCache.has(this.currentLayer)) {
                this.renderLayer(this.currentLayer);
            }
        },

        /**
         * Calculate scale to fit bed in canvas (always show full 270mm bed)
         */
        calculateScale() {
            const container = this.canvas.parentElement;
            const bedSize = 270;

            // Always scale to show the full bed (0-270mm)
            this.scale = Math.min(container.clientWidth, container.clientHeight) * 0.85 / bedSize;

            // Center the bed in the canvas
            const scaledBed = bedSize * this.scale;
            this.offsetX = (container.clientWidth - scaledBed) / 2;
            this.offsetY = (container.clientHeight - scaledBed) / 2;
        },

        /**
         * Load layer data from API
         */
        async loadLayers(start, count) {
            this.loading = true;
            try {
                const data = await api.getGCodeLayers(this.jobId, start, count);
                console.log(`Received ${data.layers?.length || 0} layers (requested ${start}-${start+count})`);

                if (!data || !data.layers || data.layers.length === 0) {
                    throw new Error(`No layer data returned for range ${start}-${start+count}`);
                }

                data.layers.forEach(layer => {
                    this.layerCache.set(layer.layer_num, layer);
                });
            } catch (err) {
                console.error('Failed to load layers:', err);
                this.error = `Failed to load layer data: ${err.message}`;
            } finally {
                this.loading = false;
            }
        },

        /**
         * Render a single layer
         */
        renderLayer(layerNum) {
            const layer = this.layerCache.get(layerNum);
            if (!layer) {
                console.warn(`Layer ${layerNum} not in cache`);
                return;
            }

            const container = this.canvas.parentElement;

            // Clear canvas
            this.ctx.fillStyle = '#1a1a1a';
            this.ctx.fillRect(0, 0, container.clientWidth, container.clientHeight);

            // Draw build plate and grid
            this.drawBuildPlate();

            // Draw moves
            layer.moves.forEach(move => {
                if (move.type === 'extrude') {
                    this.drawExtrusion(move);
                } else if (move.type === 'travel' && this.showTravel) {
                    this.drawTravel(move);
                }
            });

            // Draw print bounds and dimensions
            this.drawPrintBounds();

            // Draw layer info overlay
            this.drawLayerInfo(layer);
        },

        /**
         * Draw build plate with grid and dimensions
         * Shows the U1's 270x270mm bed as a reference outline
         */
        drawBuildPlate() {
            // Draw U1 bed outline at (0,0) to (270,270) in G-code coordinates
            const bedX = this.toCanvasX(0);
            const bedY = this.toCanvasY(270);  // Top-left corner (Y is flipped)
            const bedWidth = 270 * this.scale;
            const bedHeight = 270 * this.scale;

            // Draw grid lines every 27mm (10% of bed size for reference)
            this.ctx.strokeStyle = '#3a3a3a';
            this.ctx.lineWidth = 1;
            const gridSpacing = 27 * this.scale;

            for (let i = 1; i < 10; i++) {
                // Vertical lines
                this.ctx.beginPath();
                this.ctx.moveTo(bedX + i * gridSpacing, bedY);
                this.ctx.lineTo(bedX + i * gridSpacing, bedY + bedHeight);
                this.ctx.stroke();

                // Horizontal lines
                this.ctx.beginPath();
                this.ctx.moveTo(bedX, bedY + i * gridSpacing);
                this.ctx.lineTo(bedX + bedWidth, bedY + i * gridSpacing);
                this.ctx.stroke();
            }

            // Draw bed outline (U1 270x270mm bed)
            this.ctx.strokeStyle = '#666';
            this.ctx.lineWidth = 2;
            this.ctx.strokeRect(bedX, bedY, bedWidth, bedHeight);

            // Draw bed label
            this.ctx.fillStyle = '#999';
            this.ctx.font = '11px system-ui';
            this.ctx.textAlign = 'center';
            this.ctx.fillText('U1 Bed (270×270mm)', bedX + bedWidth / 2, bedY - 5);

            // Draw origin indicator
            this.ctx.fillStyle = '#f00';
            this.ctx.beginPath();
            this.ctx.arc(this.toCanvasX(0), this.toCanvasY(0), 4, 0, Math.PI * 2);
            this.ctx.fill();
            this.ctx.fillStyle = '#999';
            this.ctx.font = '10px system-ui';
            this.ctx.textAlign = 'left';
            this.ctx.fillText('(0,0)', this.toCanvasX(0) + 6, this.toCanvasY(0) + 4);

            // Draw X axis indicator
            this.ctx.strokeStyle = '#f00';
            this.ctx.lineWidth = 2;
            this.ctx.beginPath();
            this.ctx.moveTo(this.toCanvasX(0), this.toCanvasY(0));
            this.ctx.lineTo(this.toCanvasX(30), this.toCanvasY(0));
            this.ctx.stroke();
            // Arrow head
            this.ctx.beginPath();
            this.ctx.moveTo(this.toCanvasX(30), this.toCanvasY(0));
            this.ctx.lineTo(this.toCanvasX(26), this.toCanvasY(-3));
            this.ctx.lineTo(this.toCanvasX(26), this.toCanvasY(3));
            this.ctx.closePath();
            this.ctx.fillStyle = '#f00';
            this.ctx.fill();
            this.ctx.fillStyle = '#f00';
            this.ctx.font = 'bold 12px system-ui';
            this.ctx.textAlign = 'left';
            this.ctx.fillText('X+', this.toCanvasX(32), this.toCanvasY(0) + 4);

            // Draw Y axis indicator
            this.ctx.strokeStyle = '#0f0';
            this.ctx.lineWidth = 2;
            this.ctx.beginPath();
            this.ctx.moveTo(this.toCanvasX(0), this.toCanvasY(0));
            this.ctx.lineTo(this.toCanvasX(0), this.toCanvasY(30));
            this.ctx.stroke();
            // Arrow head
            this.ctx.beginPath();
            this.ctx.moveTo(this.toCanvasX(0), this.toCanvasY(30));
            this.ctx.lineTo(this.toCanvasX(-3), this.toCanvasY(26));
            this.ctx.lineTo(this.toCanvasX(3), this.toCanvasY(26));
            this.ctx.closePath();
            this.ctx.fillStyle = '#0f0';
            this.ctx.fill();
            this.ctx.fillStyle = '#0f0';
            this.ctx.font = 'bold 12px system-ui';
            this.ctx.textAlign = 'left';
            this.ctx.fillText('Y+', this.toCanvasX(3), this.toCanvasY(35) + 4);
        },

        /**
         * Draw print bounds and dimensions
         */
        drawPrintBounds() {
            if (!this.bounds) return;

            // Draw print area outline
            const printWidth = this.bounds.max_x - this.bounds.min_x;
            const printHeight = this.bounds.max_y - this.bounds.min_y;

            // Use red if print exceeds U1 bed size, orange otherwise
            const exceedsBed = printWidth > 270 || printHeight > 270;
            this.ctx.strokeStyle = exceedsBed ? '#dc2626' : '#f59e0b';
            this.ctx.lineWidth = 2;
            this.ctx.setLineDash([5, 5]);
            this.ctx.strokeRect(
                this.toCanvasX(this.bounds.min_x),
                this.toCanvasY(this.bounds.max_y),
                printWidth * this.scale,
                printHeight * this.scale
            );
            this.ctx.setLineDash([]);

            // Draw print dimensions
            this.ctx.fillStyle = exceedsBed ? '#dc2626' : '#f59e0b';
            this.ctx.font = 'bold 12px system-ui';
            this.ctx.textAlign = 'center';

            // Width dimension (bottom)
            const dimY = this.toCanvasY(this.bounds.min_y) + 15;
            this.ctx.fillText(
                `Print: ${printWidth.toFixed(1)}mm`,
                this.toCanvasX(this.bounds.min_x + printWidth / 2),
                dimY
            );

            // Height dimension (right)
            this.ctx.save();
            const dimX = this.toCanvasX(this.bounds.max_x) + 25;
            this.ctx.translate(dimX, this.toCanvasY(this.bounds.max_y - printHeight / 2));
            this.ctx.rotate(-Math.PI / 2);
            this.ctx.fillText(`Print: ${printHeight.toFixed(1)}mm`, 0, 0);
            this.ctx.restore();

            // Warning if exceeds bed
            if (exceedsBed) {
                this.ctx.fillStyle = '#dc2626';
                this.ctx.font = 'bold 14px system-ui';
                this.ctx.textAlign = 'center';
                const container = this.canvas.parentElement;
                this.ctx.fillText('⚠️ Print exceeds U1 bed size!', container.clientWidth / 2, 30);
            }
        },

        /**
         * Draw extrusion move (printing)
         */
        drawExtrusion(move) {
            // Use filament color if available, otherwise default blue
            const color = this.filamentColors && this.filamentColors.length > 0 
                ? this.filamentColors[0]  // Use first filament color
                : '#3b82f6';
            this.ctx.strokeStyle = color;
            this.ctx.lineWidth = 2;
            this.ctx.beginPath();
            this.ctx.moveTo(
                this.toCanvasX(move.x1),
                this.toCanvasY(move.y1)
            );
            this.ctx.lineTo(
                this.toCanvasX(move.x2),
                this.toCanvasY(move.y2)
            );
            this.ctx.stroke();
        },

        /**
         * Draw travel move (non-printing)
         */
        drawTravel(move) {
            this.ctx.strokeStyle = '#666';  // Gray
            this.ctx.lineWidth = 1;
            this.ctx.setLineDash([5, 5]);
            this.ctx.beginPath();
            this.ctx.moveTo(
                this.toCanvasX(move.x1),
                this.toCanvasY(move.y1)
            );
            this.ctx.lineTo(
                this.toCanvasX(move.x2),
                this.toCanvasY(move.y2)
            );
            this.ctx.stroke();
            this.ctx.setLineDash([]);
        },

        /**
         * Draw layer info overlay
         */
        drawLayerInfo(layer) {
            // Position in bottom-left to avoid cropping and overlap with warnings
            const container = this.canvas.parentElement;
            const boxX = 10;
            const boxY = container.clientHeight - 65;
            const boxWidth = 200;
            const boxHeight = 55;

            // Semi-transparent background
            this.ctx.fillStyle = 'rgba(0, 0, 0, 0.75)';
            this.ctx.fillRect(boxX, boxY, boxWidth, boxHeight);

            // White text
            this.ctx.fillStyle = '#fff';
            this.ctx.font = 'bold 14px system-ui';
            this.ctx.textAlign = 'left';
            this.ctx.fillText(`Layer ${layer.layer_num + 1} / ${this.totalLayers}`, boxX + 10, boxY + 22);

            this.ctx.font = '12px system-ui';
            this.ctx.fillText(`Height: ${layer.z_height.toFixed(2)} mm`, boxX + 10, boxY + 42);
        },

        /**
         * Convert G-code X coordinate to canvas X
         * Maps from bed coordinate (0-270mm) to canvas pixels
         */
        toCanvasX(x) {
            return this.offsetX + x * this.scale;
        },

        /**
         * Convert G-code Y coordinate to canvas Y (flip Y axis)
         * Maps from bed coordinate (0-270mm) to canvas pixels
         * Y-axis is flipped (0 at bottom in G-code, top in canvas)
         */
        toCanvasY(y) {
            const container = this.canvas.parentElement;
            const bedSize = 270;
            return container.clientHeight - (this.offsetY + y * this.scale);
        },

        /**
         * Handle layer slider change
         */
        async onLayerChange(newLayer) {
            this.currentLayer = newLayer;

            // Check if we need to load more layers
            if (!this.layerCache.has(newLayer)) {
                const batchStart = Math.floor(newLayer / 20) * 20;
                await this.loadLayers(batchStart, 20);
            }

            this.renderLayer(newLayer);
        },

        /**
         * Navigate to next layer
         */
        nextLayer() {
            if (this.currentLayer < this.totalLayers - 1) {
                this.onLayerChange(this.currentLayer + 1);
            }
        },

        /**
         * Navigate to previous layer
         */
        previousLayer() {
            if (this.currentLayer > 0) {
                this.onLayerChange(this.currentLayer - 1);
            }
        },

        /**
         * Toggle travel moves display
         */
        toggleTravel() {
            this.showTravel = !this.showTravel;
            this.renderLayer(this.currentLayer);
        }
    };
}
