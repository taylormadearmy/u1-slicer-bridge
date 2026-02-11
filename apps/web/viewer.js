/**
 * Simple 2D G-code layer viewer using Canvas 2D
 * Mobile-first Alpine.js component
 */

function gcodeViewer(initialJobId) {
    return {
        // Canvas and rendering
        canvas: null,
        ctx: null,
        jobId: initialJobId || null,

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
            await this.loadViewer();
        },

        /**
         * Load viewer data
         */
        async loadViewer() {
            console.log('Initializing viewer with job ID:', this.jobId);
            this.canvas = this.$refs.canvas;
            this.ctx = this.canvas.getContext('2d');

            // Set canvas size
            this.resizeCanvas();
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

                this.renderLayer(0);
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
            if (this.currentLayer >= 0) {
                this.renderLayer(this.currentLayer);
            }
        },

        /**
         * Calculate scale to fit 200mm bed in canvas
         */
        calculateScale() {
            const container = this.canvas.parentElement;

            // Snapmaker U1 bed is 200x200mm
            const bedSize = 200;
            const bedScale = Math.min(container.clientWidth, container.clientHeight) * 0.85 / bedSize;

            this.scale = bedScale;
            this.offsetX = (container.clientWidth - bedSize * bedScale) / 2;
            this.offsetY = (container.clientHeight - bedSize * bedScale) / 2;
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
         */
        drawBuildPlate() {
            const container = this.canvas.parentElement;

            // Snapmaker U1 bed size is 200x200mm
            const bedSize = 200;
            const bedScale = Math.min(container.clientWidth, container.clientHeight) * 0.85 / bedSize;
            const bedWidth = bedSize * bedScale;
            const bedHeight = bedSize * bedScale;
            const bedX = (container.clientWidth - bedWidth) / 2;
            const bedY = (container.clientHeight - bedHeight) / 2;

            // Draw bed background
            this.ctx.fillStyle = '#2a2a2a';
            this.ctx.fillRect(bedX, bedY, bedWidth, bedHeight);

            // Draw grid lines every 10mm
            this.ctx.strokeStyle = '#3a3a3a';
            this.ctx.lineWidth = 1;
            const gridSpacing = 10 * bedScale;

            for (let i = 1; i < bedSize / 10; i++) {
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

            // Draw bed outline
            this.ctx.strokeStyle = '#666';
            this.ctx.lineWidth = 2;
            this.ctx.strokeRect(bedX, bedY, bedWidth, bedHeight);

            // Draw bed dimensions
            this.ctx.fillStyle = '#999';
            this.ctx.font = '12px system-ui';
            this.ctx.textAlign = 'center';
            this.ctx.fillText('200mm', bedX + bedWidth / 2, bedY - 5);

            this.ctx.save();
            this.ctx.translate(bedX - 5, bedY + bedHeight / 2);
            this.ctx.rotate(-Math.PI / 2);
            this.ctx.fillText('200mm', 0, 0);
            this.ctx.restore();

            // Draw origin indicator
            this.ctx.fillStyle = '#f00';
            this.ctx.beginPath();
            this.ctx.arc(bedX, bedY + bedHeight, 4, 0, Math.PI * 2);
            this.ctx.fill();
            this.ctx.fillStyle = '#999';
            this.ctx.font = '10px system-ui';
            this.ctx.textAlign = 'left';
            this.ctx.fillText('(0,0)', bedX + 6, bedY + bedHeight - 2);
        },

        /**
         * Draw print bounds and dimensions
         */
        drawPrintBounds() {
            // Draw print area outline
            const printWidth = this.bounds.max_x - this.bounds.min_x;
            const printHeight = this.bounds.max_y - this.bounds.min_y;

            this.ctx.strokeStyle = '#f59e0b';
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
            this.ctx.fillStyle = '#f59e0b';
            this.ctx.font = 'bold 11px system-ui';
            this.ctx.textAlign = 'center';

            // Width dimension (bottom)
            const dimY = this.toCanvasY(this.bounds.min_y) + 15;
            this.ctx.fillText(
                `${printWidth.toFixed(1)}mm`,
                this.toCanvasX(this.bounds.min_x + printWidth / 2),
                dimY
            );

            // Height dimension (right)
            this.ctx.save();
            const dimX = this.toCanvasX(this.bounds.max_x) + 15;
            this.ctx.translate(dimX, this.toCanvasY(this.bounds.max_y - printHeight / 2));
            this.ctx.rotate(-Math.PI / 2);
            this.ctx.fillText(`${printHeight.toFixed(1)}mm`, 0, 0);
            this.ctx.restore();
        },

        /**
         * Draw extrusion move (printing)
         */
        drawExtrusion(move) {
            this.ctx.strokeStyle = '#3b82f6';  // Blue
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
            this.ctx.fillStyle = 'rgba(0, 0, 0, 0.7)';
            this.ctx.fillRect(10, 10, 180, 50);

            this.ctx.fillStyle = '#fff';
            this.ctx.font = '14px system-ui';
            this.ctx.fillText(`Layer ${layer.layer_num + 1} / ${this.totalLayers}`, 20, 30);
            this.ctx.fillText(`Height: ${layer.z_height.toFixed(2)} mm`, 20, 50);
        },

        /**
         * Convert G-code X coordinate to canvas X
         * Maps from bed coordinate (0-200mm) to canvas pixels
         */
        toCanvasX(x) {
            return this.offsetX + x * this.scale;
        },

        /**
         * Convert G-code Y coordinate to canvas Y (flip Y axis)
         * Maps from bed coordinate (0-200mm) to canvas pixels
         * Y-axis is flipped (0 at bottom in G-code, top in canvas)
         */
        toCanvasY(y) {
            const container = this.canvas.parentElement;
            const bedSize = 200;
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
