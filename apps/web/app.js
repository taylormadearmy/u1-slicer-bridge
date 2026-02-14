/**
 * Main Application Logic
 * Alpine.js component for U1 Slicer Bridge UI
 */

function app() {
    return {
        // UI State
        dragOver: false,
        uploadProgress: 0,
        uploadPhase: 'idle', // 'idle' | 'uploading' | 'processing'
        error: null,

        // Current workflow step: 'upload' | 'configure' | 'slicing' | 'complete'
        currentStep: 'upload',

        // Data
        uploads: [],
        jobs: [],               // All slicing jobs
        filaments: [],
        selectedIds: {},        // Track selected items { upload_id: bool, job_id: bool }
        lastSelectedIndex: {},  // Track last selected index per list { uploads: num, jobs: num }
        selectedUpload: null,     // Current upload object
        selectedFilament: null,   // Selected filament ID (single filament mode)
        selectedFilaments: [],    // Selected filament IDs (multi-filament mode)
        selectedPlate: null,       // Selected plate ID for multi-plate files
        plates: [],               // Available plates for selected upload
        platesLoading: false,     // Loading state for plates
        detectedColors: [],       // Colors detected from 3MF file
        filamentOverride: false,  // Whether user wants to manually override filament assignment
        maxExtruders: 4,
        multicolorNotice: null,

        // Printer status
        printerConnected: false,
        printerStatus: 'Checking...',

        // Slicing settings
        sliceSettings: {
            layer_height: 0.2,
            infill_density: 15,
            wall_count: 3,
            infill_pattern: 'gyroid',
            supports: false,
            nozzle_temp: null,
            bed_temp: null,
            bed_type: null,
            filament_colors: [],  // Override colors per extruder
            extruder_assignments: [0, 1, 2, 3],  // Which extruder each color uses (E1=0, E2=1, etc)
        },

        // Results
        sliceResult: null,
        sliceProgress: 0,         // Progress percentage (0-100)

        // Polling interval
        sliceInterval: null,

        /**
         * Initialize the application
         */
        async init() {
            console.log('U1 Slicer Bridge - Initializing...');

            // Load initial data
            await this.checkPrinterStatus();
            await this.loadFilaments();
            await this.loadRecentUploads();
            await this.loadJobs();

            // Set up periodic printer status check
            setInterval(() => this.checkPrinterStatus(), 30000); // Every 30 seconds
        },

        /**
         * Check printer connection status
         */
        async checkPrinterStatus() {
            try {
                const status = await api.getPrinterStatus();
                this.printerConnected = status.connected;
                this.printerStatus = status.connected ? 'Connected' : 'Offline';
            } catch (err) {
                this.printerConnected = false;
                this.printerStatus = 'Error';
                console.error('Failed to check printer status:', err);
            }
        },

        /**
         * Load filaments from API
         */
        async loadFilaments() {
            try {
                const response = await api.listFilaments();
                this.filaments = response.filaments || [];
                console.log(`Loaded ${this.filaments.length} filaments`);
            } catch (err) {
                this.showError('Failed to load filaments');
                console.error(err);
            }
        },

        /**
         * Initialize default filaments
         */
        async initDefaultFilaments() {
            try {
                await api.initDefaultFilaments();
                await this.loadFilaments();
                console.log('Default filaments initialized');
            } catch (err) {
                this.showError('Failed to initialize default filaments');
                console.error(err);
            }
        },

        /**
         * Load recent uploads
         */
        async loadRecentUploads() {
            try {
                const response = await api.listUploads();
                this.uploads = response.uploads || [];
                console.log(`Loaded ${this.uploads.length} recent uploads`);
            } catch (err) {
                this.showError('Failed to load uploads');
                console.error(err);
            }
        },

        /**
         * Load all slicing jobs
         */
        async loadJobs() {
            try {
                this.jobs = await api.getJobs();
                console.log(`Loaded ${this.jobs.length} jobs`);
            } catch (err) {
                this.showError('Failed to load jobs');
                console.error(err);
            }
        },

        /**
         * Delete an upload and all its jobs
         */
        async deleteUpload(uploadId) {
            if (!confirm('Delete this file and all its sliced versions?')) return;
            
            try {
                await api.deleteUpload(uploadId);
                this.uploads = this.uploads.filter(u => u.upload_id !== uploadId);
                this.jobs = this.jobs.filter(j => j.upload_id !== uploadId);
                if (this.selectedUpload?.upload_id === uploadId) {
                    this.selectedUpload = null;
                    this.currentStep = 'upload';
                }
            } catch (err) {
                this.showError('Failed to delete upload');
                console.error(err);
            }
        },

        /**
         * Delete a single job
         */
        async deleteJob(jobId) {
            if (!confirm('Delete this sliced file?')) return;
            
            try {
                await api.deleteJob(jobId);
                this.jobs = this.jobs.filter(j => j.job_id !== jobId);
            } catch (err) {
                this.showError('Failed to delete job');
                console.error(err);
            }
        },

        /**
         * Toggle selection of an item
         */
        toggleSelect(type, id, index, event) {
            const isShift = event?.shiftKey;
            const listKey = type === 'upload' ? 'uploads' : 'jobs';
            const list = this[listKey];
            
            if (isShift && this.lastSelectedIndex[listKey] !== undefined) {
                const lastIdx = this.lastSelectedIndex[listKey];
                const start = Math.min(lastIdx, index);
                const end = Math.max(lastIdx, index);
                
                for (let i = start; i <= end; i++) {
                    const item = list[i];
                    const key = `${type}_${type === 'upload' ? item.upload_id : item.job_id}`;
                    this.selectedIds[key] = true;
                }
            } else {
                const key = `${type}_${id}`;
                this.selectedIds[key] = !this.selectedIds[key];
            }
            
            this.lastSelectedIndex[listKey] = index;
        },

        /**
         * Check if item is selected
         */
        isSelected(type, id) {
            return !!this.selectedIds[`${type}_${id}`];
        },

        /**
         * Delete all selected items
         */
        async deleteSelected() {
            const selectedCount = Object.values(this.selectedIds).filter(v => v).length;
            if (selectedCount === 0) {
                this.showError('No items selected');
                return;
            }
            
            if (!confirm(`Delete ${selectedCount} selected item(s)?`)) return;
            
            try {
                // Delete selected uploads (which will cascade to jobs)
                for (const upload of this.uploads) {
                    if (this.selectedIds[`upload_${upload.upload_id}`]) {
                        await api.deleteUpload(upload.upload_id);
                    }
                }
                
                // Delete remaining selected jobs
                for (const job of this.jobs) {
                    if (this.selectedIds[`job_${job.job_id}`]) {
                        await api.deleteJob(job.job_id);
                    }
                }
                
                // Refresh lists
                await this.loadRecentUploads();
                await this.loadJobs();
                this.selectedIds = {};
            } catch (err) {
                this.showError('Failed to delete some items');
                console.error(err);
            }
        },

        /**
         * Handle file drop
         */
        handleFileDrop(event) {
            this.dragOver = false;
            const files = event.dataTransfer.files;
            if (files.length > 0) {
                this.handleFileUpload(files[0]);
            }
        },

        /**
         * Handle file select from input
         */
        handleFileInput(event) {
            const files = event.target.files;
            if (files.length > 0) {
                this.handleFileUpload(files[0]);
            }
        },

        /**
         * Handle file upload (Step 1)
         */
        async handleFileUpload(file) {
            // Validate file type
            if (!file.name.endsWith('.3mf')) {
                this.showError('Please upload a .3mf file');
                return;
            }

            console.log(`Uploading: ${file.name} (${(file.size / 1024 / 1024).toFixed(2)} MB)`);
            this.currentStep = 'upload';
            this.uploadProgress = 0;
            this.uploadPhase = 'uploading';

            try {
                console.log('🔄 Starting upload...');
                const result = await api.uploadFile(file, (status) => {
                    if (typeof status === 'number') {
                        this.uploadPhase = 'uploading';
                        this.uploadProgress = status;
                        console.log(`📤 Upload progress: ${status}%`);
                        return;
                    }

                    this.uploadPhase = status?.phase || 'uploading';
                    this.uploadProgress = typeof status?.progress === 'number' ? status.progress : this.uploadProgress;

                    if (this.uploadPhase === 'processing') {
                        console.log('🧠 Upload complete, processing 3MF...');
                    } else {
                        console.log(`📤 Upload progress: ${this.uploadProgress}%`);
                    }
                });

                console.log('✅ Upload complete:', result);
                console.log('   - is_multi_plate:', result.is_multi_plate);
                console.log('   - plates:', result.plates?.length);
                console.log('   - plate_count:', result.plate_count);
                
                this.uploadProgress = 0;
                this.uploadPhase = 'idle';

                // Add to uploads list
                this.uploads.unshift(result);

                // Select this upload and move to configure step
                this.selectedUpload = result;
                this.selectedPlate = null;
                this.plates = [];
                this.platesLoading = false;
                this.currentStep = 'configure';

                // Initialize detected colors and filament assignment state from upload response
                this.filamentOverride = false;
                this.applyDetectedColors(result.detected_colors || []);
                
                // Always reload plate info so we get latest validation + preview URLs.
                this.platesLoading = true;
                this.plates = [];
                try {
                    const platesData = await api.getUploadPlates(result.upload_id);
                    this.platesLoading = false;

                    if (platesData.is_multi_plate && platesData.plates && platesData.plates.length > 0) {
                        this.selectedUpload.is_multi_plate = true;
                        this.selectedUpload.plate_count = platesData.plate_count;
                        this.plates = platesData.plates;
                        console.log(`✅ Loaded ${this.plates.length} plates for new upload`);
                    } else {
                        this.selectedUpload.is_multi_plate = false;
                        this.plates = [];
                        console.log('Single plate file');
                    }
                } catch (err) {
                    this.platesLoading = false;
                    this.selectedUpload.is_multi_plate = false;
                    this.plates = [];
                    console.warn('Could not load plates for new upload:', err);
                }
            } catch (err) {
                this.uploadProgress = 0;
                this.uploadPhase = 'idle';
                this.showError(`Upload failed: ${err.message}`);
                console.error(err);
            }
        },

/**
         * Select an existing upload (from recent uploads list)
         */
        async selectUpload(upload) {
            console.log('Selected upload:', upload.upload_id);
            this.selectedUpload = upload;
            this.selectedPlate = null;  // Reset plate selection
            this.plates = [];          // Reset plates list
            this.platesLoading = true; // Set loading state
            
            // Fetch full upload details to get detected colors and warnings
            try {
                const uploadDetails = await api.getUpload(upload.upload_id);
                // Merge upload details with selected upload
                this.selectedUpload = { ...this.selectedUpload, ...uploadDetails };
                this.applyDetectedColors(uploadDetails.detected_colors || []);
            } catch (e) {
                console.warn('Could not fetch upload details:', e);
                this.applyDetectedColors([]);
            }

            this.filamentOverride = false;
            
            this.currentStep = 'configure';
            
            // Try to load plates - this works for both multi-plate and single-plate files
            // If it's multi-plate, we'll get plates data; if single, we'll get empty plates
            try {
                console.log('Loading plates for upload:', upload.upload_id);
                
                // Add timeout wrapper
                const platesDataPromise = api.getUploadPlates(upload.upload_id);
                const timeoutPromise = new Promise((_, reject) => 
                    setTimeout(() => reject(new Error('API timeout')), 10000)
                );
                
                const platesData = await Promise.race([platesDataPromise, timeoutPromise]);
                console.log('Plates data received:', platesData);
                this.platesLoading = false;
                
                if (platesData.is_multi_plate && platesData.plates && platesData.plates.length > 0) {
                    // Update the selected upload with multi-plate info
                    this.selectedUpload.is_multi_plate = true;
                    this.selectedUpload.plate_count = platesData.plate_count;
                    this.plates = platesData.plates;
                    console.log(`✅ Loaded ${this.plates.length} plates for upload ${upload.upload_id}`);
                } else {
                    // Single plate file - that's fine
                    this.selectedUpload.is_multi_plate = false;
                    this.plates = [];
                    console.log('Single plate file - no plates to load');
                    console.log('Single plate file');
                }
            } catch (err) {
                console.error('❌ Could not load plates:', err.message, err);
                this.platesLoading = false;
                this.selectedUpload.is_multi_plate = false;
                this.plates = [];
            }
        },

        /**
         * Load plates for a multi-plate upload
         */
        async loadPlates(uploadId) {
            // Guard against undefined uploadId - try to get it from selectedUpload
            if (!uploadId) {
                uploadId = this.selectedUpload?.upload_id;
                if (!uploadId) {
                    console.warn('loadPlates called with undefined uploadId and no selectedUpload');
                    return;
                }
            }
            
            try {
                const response = await api.getUploadPlates(uploadId);
                this.plates = response.plates || [];
                console.log(`Loaded ${this.plates.length} plates for upload ${uploadId}`);
                
                // Auto-select the first plate that fits and is printable
                const firstFitPlate = this.plates.find(p => p.validation && p.validation.fits && p.printable);
                if (firstFitPlate) {
                    this.selectedPlate = firstFitPlate.plate_id;
                    console.log('Auto-selected first valid plate:', firstFitPlate.plate_id);
                } else if (this.plates.length > 0) {
                    // Fallback to first plate if none fit
                    this.selectedPlate = this.plates[0].plate_id;
                    console.log('No plates fit build volume, selected first plate:', this.selectedPlate);
                }
            } catch (err) {
                console.error('Failed to load plates:', err);
                this.showError('Failed to load plate information');
            }
        },

        /**
         * Select a plate for slicing
         */
        selectPlate(plateId) {
            this.selectedPlate = plateId;
            console.log('Selected plate:', plateId);
        },

        /**
         * Start slicing (Step 3)
         */
        async startSlice() {
            // Check if we have filaments selected (either single or multi)
            const hasSingleFilament = this.selectedFilament && this.selectedFilament > 0;
            const hasMultiFilaments = this.selectedFilaments && this.selectedFilaments.length > 0;
            
            if (!this.selectedUpload || (!hasSingleFilament && !hasMultiFilaments)) {
                this.showError('Please select an upload and filament(s)');
                return;
            }

            // For multi-plate files, require plate selection
            if (this.selectedUpload.is_multi_plate && !this.selectedPlate) {
                this.showError('Please select a plate to slice');
                return;
            }

            // Capture current upload info before starting (in case selection changes)
            const uploadId = this.selectedUpload.upload_id;
            const filename = this.selectedUpload.filename;
            const isMultiPlate = this.selectedUpload.is_multi_plate;
            const selectedPlateId = this.selectedPlate;
            
            console.log(`Starting slice for: ${filename} (ID: ${uploadId}, multi-plate: ${isMultiPlate}, plate: ${selectedPlateId})`);
            console.log('Settings:', this.sliceSettings);
            console.log('Selected filaments:', hasMultiFilaments ? this.selectedFilaments : this.selectedFilament);

            this.currentStep = 'slicing';
            this.sliceProgress = 0;

            try {
                let result;
                
                // Prepare slice settings with filament info
                const sliceSettings = {
                    ...this.sliceSettings
                };
                
                // Reorder colors and filament_ids based on extruder assignments
                if (hasMultiFilaments && this.sliceSettings.extruder_assignments) {
                    const assignments = this.sliceSettings.extruder_assignments;
                    const colors = [...sliceSettings.filament_colors];
                    const filaments = [...this.selectedFilaments];
                    
                    // Create reordered arrays based on extruder assignments
                    const reorderedColors = [null, null, null, null];
                    const reorderedFilaments = [null, null, null, null];
                    
                    assignments.forEach((extruderIdx, colorIdx) => {
                        if (extruderIdx < 4 && colorIdx < colors.length) {
                            reorderedColors[extruderIdx] = colors[colorIdx];
                            reorderedFilaments[extruderIdx] = filaments[colorIdx];
                        }
                    });

                    // Keep slot indices stable so E2/E3 don't collapse to E1/E2.
                    const maxAssigned = assignments.length > 0
                        ? Math.min(4, Math.max(...assignments) + 1)
                        : Math.min(4, filaments.length);

                    // Fill gaps with a filament already selected for this slice
                    // to avoid accidental mixed-temperature placeholder slots.
                    const defaultFilament = filaments[0]
                        || this.selectedFilament
                        || this.filaments.find(f => f.is_default)?.id
                        || 1;

                    sliceSettings.filament_colors = reorderedColors
                        .slice(0, maxAssigned)
                        .map(c => c || '#FFFFFF');
                    sliceSettings.filament_ids = reorderedFilaments
                        .slice(0, maxAssigned)
                        .map(f => f || defaultFilament);
                    sliceSettings.extruder_assignments = [...assignments];
                } else if (hasMultiFilaments) {
                    // No custom extruder assignments - use default
                    sliceSettings.filament_ids = this.selectedFilaments;
                } else {
                    sliceSettings.filament_id = this.selectedFilament;
                }
                
                if (isMultiPlate) {
                    // Slice specific plate
                    console.log(`Slicing plate ${selectedPlateId} from upload ${uploadId}`);
                    result = await api.slicePlate(uploadId, selectedPlateId, sliceSettings);
                } else {
                    // Slice regular upload
                    console.log(`Slicing upload ${uploadId}`);
                    result = await api.sliceUpload(uploadId, sliceSettings);
                }

                console.log('Slice started:', result);

                if (result.status === 'completed') {
                    // Synchronous slicing (completed immediately)
                    this.sliceResult = result;
                    this.sliceProgress = 100;
                    this.currentStep = 'complete';
                    await this.loadJobs(); // Refresh sliced history immediately
                } else {
                    // Async slicing - poll for completion
                    this.pollSliceStatus(result.job_id);
                }
            } catch (err) {
                this.showError(`Slicing failed: ${err.message}`);
                this.currentStep = 'configure';
                console.error(err);
            }
        },

        /**
         * Poll slicing job status
         */
        pollSliceStatus(jobId) {
            if (this.sliceInterval) {
                clearInterval(this.sliceInterval);
            }

            this.sliceInterval = setInterval(async () => {
                try {
                    const job = await api.getJobStatus(jobId);
                    console.log('Slice status:', job.status);

                    // Increment progress (fake progress since API doesn't provide real progress)
                    this.sliceProgress = Math.min(90, this.sliceProgress + 5);

                    if (job.status === 'completed') {
                        clearInterval(this.sliceInterval);
                        this.sliceResult = job;
                        this.sliceProgress = 100;
                        this.currentStep = 'complete';
                        console.log('Slicing completed');
                        this.loadJobs(); // Refresh jobs list
                    } else if (job.status === 'failed') {
                        clearInterval(this.sliceInterval);
                        this.showError(`Slicing failed: ${job.error_message || 'Unknown error'}`);
                        this.currentStep = 'configure';
                    }
                } catch (err) {
                    console.error('Failed to check slice status:', err);
                }
            }, 2000); // Poll every 2 seconds
        },

        /**
         * Reset workflow to start over
         */
        resetWorkflow() {
            this.currentStep = 'upload';
            this.selectedUpload = null;
            this.sliceResult = null;
            this.sliceProgress = 0;
            this.uploadProgress = 0;
            this.uploadPhase = 'idle';

            if (this.sliceInterval) {
                clearInterval(this.sliceInterval);
                this.sliceInterval = null;
            }
        },

        /**
         * View a sliced job (load its result for viewing/downloading)
         */
        async viewJob(job) {
            try {
                const status = await api.getJobStatus(job.job_id);
                this.selectedUpload = {
                    upload_id: job.upload_id,
                    filename: job.filename,
                    file_size: 0
                };
                this.sliceResult = status;
                this.currentStep = 'complete';
            } catch (err) {
                this.showError('Failed to load job');
                console.error(err);
            }
        },

        /**
         * Format time in seconds to human readable string
         */
        formatTime(seconds) {
            if (!seconds) return '0h 0m';
            const hours = Math.floor(seconds / 3600);
            const minutes = Math.floor((seconds % 3600) / 60);
            return `${hours}h ${minutes}m`;
        },

        /**
         * Auto-assign filaments to detected colors from 3MF
         */
        autoAssignFilaments() {
            if (this.detectedColors.length === 0) return;

            this.selectedFilaments = [];

            // For each detected color, find the closest matching filament
            for (const color of this.detectedColors) {
                if (this.selectedFilaments.length >= this.maxExtruders) break;
                
                // Find filament with closest color
                let bestFilament = null;
                let bestDistance = Infinity;
                
                for (const filament of this.filaments) {
                    const distance = this.colorDistance(color, filament.color_hex);
                    if (distance < bestDistance) {
                        bestDistance = distance;
                        bestFilament = filament;
                    }
                }
                
                if (bestFilament) {
                    this.selectedFilaments.push(bestFilament.id);
                }
            }
            
            console.log('Auto-assigned filaments:', this.selectedFilaments);
        },

        /**
         * Calculate color distance (simple Euclidean in RGB space)
         */
        colorDistance(hex1, hex2) {
            const rgb1 = this.hexToRgb(hex1);
            const rgb2 = this.hexToRgb(hex2);
            if (!rgb1 || !rgb2) return Infinity;
            
            return Math.sqrt(
                Math.pow(rgb1.r - rgb2.r, 2) +
                Math.pow(rgb1.g - rgb2.g, 2) +
                Math.pow(rgb1.b - rgb2.b, 2)
            );
        },

        /**
         * Convert hex color to RGB object
         */
        hexToRgb(hex) {
            if (!hex) return null;
            // Handle #RGB format
            if (hex.length === 4) {
                hex = '#' + hex[1] + hex[1] + hex[2] + hex[2] + hex[3] + hex[3];
            }
            const result = /^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(hex);
            return result ? {
                r: parseInt(result[1], 16),
                g: parseInt(result[2], 16),
                b: parseInt(result[3], 16)
            } : null;
        },

        /**
         * Get filament by ID
         */
        getFilamentById(id) {
            return this.filaments.find(f => f.id === id);
        },

        /**
         * Toggle filament override mode
         */
        toggleFilamentOverride() {
            if (this.multicolorNotice) {
                this.filamentOverride = false;
                this.showError('This file uses more than 4 colors. Override mode is disabled on U1.');
                return;
            }
            this.filamentOverride = !this.filamentOverride;
            if (!this.filamentOverride) {
                // Re-auto-assign when turning off override
                this.applyDetectedColors(this.detectedColors || []);
            }
        },

        /**
         * Apply detected colors with U1 extruder limits.
         */
        applyDetectedColors(colors) {
            this.detectedColors = colors;
            this.selectedFilaments = [];

            const limitedColors = (colors || []).slice(0, this.maxExtruders);
            this.sliceSettings.filament_colors = [...limitedColors];
            this.sliceSettings.extruder_assignments = limitedColors.map((_, idx) => idx);

            if (!colors || colors.length === 0) {
                this.multicolorNotice = null;
                this.setDefaultFilament();
                return;
            }

            if (colors.length > this.maxExtruders) {
                this.multicolorNotice = `Detected ${colors.length} colors, but U1 supports up to ${this.maxExtruders} extruders. Defaulting to single-filament mode.`;
                this.setDefaultFilament();
                return;
            }

            this.multicolorNotice = null;
            this.selectedFilament = null;
            this.autoAssignFilaments();
        },

        /**
         * Select default filament in single-filament mode.
         */
        setDefaultFilament() {
            const defaultFilament = this.filaments.find(f => f.is_default);
            if (defaultFilament) {
                this.selectedFilament = defaultFilament.id;
            }
        },

        /**
         * Set extruder assignment while keeping assignments unique.
         * If target extruder is already used, swap assignments.
         */
        setExtruderAssignment(colorIdx, extruderIdx) {
            const assignments = this.sliceSettings.extruder_assignments || [];
            const prev = assignments[colorIdx];
            const conflictIdx = assignments.findIndex((v, i) => i !== colorIdx && v === extruderIdx);

            if (conflictIdx >= 0) {
                assignments[conflictIdx] = prev;
            }
            assignments[colorIdx] = extruderIdx;
            this.sliceSettings.extruder_assignments = [...assignments];
        },

        /**
         * Format filament length in mm to meters
         */
        formatFilament(mm) {
            if (!mm) return '0.0m';
            return `${(mm / 1000).toFixed(1)}m`;
        },

        /**
         * Show error message
         */
        showError(message) {
            this.error = message;
            setTimeout(() => {
                this.error = null;
            }, 5000); // Auto-dismiss after 5 seconds
        },

        /**
         * Cleanup intervals on destroy
         */
        destroy() {
            if (this.sliceInterval) {
                clearInterval(this.sliceInterval);
            }
        },
    };
}
