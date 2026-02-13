/**
 * Main Application Logic
 * Alpine.js component for U1 Slicer Bridge UI
 */

function app() {
    return {
        // UI State
        dragOver: false,
        uploadProgress: 0,
        error: null,

        // Current workflow step: 'upload' | 'configure' | 'slicing' | 'complete'
        currentStep: 'upload',

        // Data
        uploads: [],
        filaments: [],
        selectedUpload: null,     // Current upload object
        selectedFilament: null,   // Selected filament ID
        selectedPlate: null,       // Selected plate ID for multi-plate files
        plates: [],               // Available plates for selected upload
        platesLoading: false,     // Loading state for plates

        // Printer status
        printerConnected: false,
        printerStatus: 'Checking...',

        // Slicing settings
        sliceSettings: {
            layer_height: 0.2,
            infill_density: 15,
            supports: false,
            nozzle_temp: null,
            bed_temp: null,
            bed_type: null,
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

            try {
                console.log('🔄 Starting upload...');
                const result = await api.uploadFile(file, (progress) => {
                    this.uploadProgress = progress;
                    console.log(`📤 Upload progress: ${progress}%`);
                });

                console.log('✅ Upload complete:', result);
                console.log('   - is_multi_plate:', result.is_multi_plate);
                console.log('   - plates:', result.plates?.length);
                console.log('   - plate_count:', result.plate_count);
                
                this.uploadProgress = 0;

                // Add to uploads list
                this.uploads.unshift(result);

                // Select this upload and move to configure step
                this.selectedUpload = result;
                this.selectedPlate = null;
                this.plates = [];
                this.platesLoading = false;
                this.currentStep = 'configure';

                // Auto-select default filament
                const defaultFilament = this.filaments.find(f => f.is_default);
                if (defaultFilament) {
                    this.selectedFilament = defaultFilament.id;
                }
                
                // Check if upload response already has multi-plate data (it does from the API!)
                if (result.is_multi_plate && result.plates && result.plates.length > 0) {
                    this.plates = result.plates;
                    this.selectedUpload.is_multi_plate = true;
                    this.selectedUpload.plate_count = result.plate_count;
                    console.log(`✅ Using ${this.plates.length} plates from upload response`);
                } else {
                    // Not in upload response - try loading separately
                    console.log('📋 Multi-plate data not in upload response, loading separately...');
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
                }
            } catch (err) {
                this.uploadProgress = 0;
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
            
            this.currentStep = 'configure';
            
            // Auto-select default filament if not already selected
            if (!this.selectedFilament) {
                const defaultFilament = this.filaments.find(f => f.is_default);
                if (defaultFilament) {
                    this.selectedFilament = defaultFilament.id;
                }
            }
            
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
            if (!this.selectedUpload || !this.selectedFilament) {
                this.showError('Please select an upload and filament');
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

            this.currentStep = 'slicing';
            this.sliceProgress = 0;

            try {
                let result;
                
                if (isMultiPlate) {
                    // Slice specific plate
                    console.log(`Slicing plate ${selectedPlateId} from upload ${uploadId}`);
                    result = await api.slicePlate(uploadId, selectedPlateId, {
                        filament_id: this.selectedFilament,
                        ...this.sliceSettings
                    });
                } else {
                    // Slice regular upload
                    console.log(`Slicing upload ${uploadId}`);
                    result = await api.sliceUpload(uploadId, {
                        filament_id: this.selectedFilament,
                        ...this.sliceSettings
                    });
                }

                console.log('Slice started:', result);

                if (result.status === 'completed') {
                    // Synchronous slicing (completed immediately)
                    this.sliceResult = result;
                    this.sliceProgress = 100;
                    this.currentStep = 'complete';
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

            if (this.sliceInterval) {
                clearInterval(this.sliceInterval);
                this.sliceInterval = null;
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
