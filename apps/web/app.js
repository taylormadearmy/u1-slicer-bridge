/**
 * Main Application Logic
 * Alpine.js component for U1 Slicer Bridge UI
 */

function app() {
    return {
        // State
        dragOver: false,
        uploadProgress: 0,
        uploads: [],
        filaments: [],
        currentUploadId: null,
        currentBundleId: null,
        selectedFilament: '',
        error: null,
        printerConnected: false,
        printerStatus: 'Checking...',

        // Workflow state
        workflow: {
            normalize: 'pending',  // pending, processing, completed, failed
            bundle: 'pending',
            slice: 'pending',
        },

        // Slicing settings
        sliceSettings: {
            layer_height: 0.2,
            infill_density: 15,
            supports: false,
        },

        // Slicing result
        sliceResult: null,

        // Polling intervals
        normalizeInterval: null,
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
                this.uploadFile(files[0]);
            }
        },

        /**
         * Handle file select from input
         */
        handleFileSelect(event) {
            const files = event.target.files;
            if (files.length > 0) {
                this.uploadFile(files[0]);
            }
        },

        /**
         * Upload a file
         */
        async uploadFile(file) {
            // Validate file type
            if (!file.name.endsWith('.3mf')) {
                this.showError('Please upload a .3mf file');
                return;
            }

            console.log(`Uploading: ${file.name} (${(file.size / 1024 / 1024).toFixed(2)} MB)`);
            this.uploadProgress = 0;

            try {
                const result = await api.uploadFile(file, (progress) => {
                    this.uploadProgress = progress;
                });

                console.log('Upload complete:', result);
                this.uploadProgress = 0;

                // Add to uploads list
                this.uploads.unshift(result);

                // Auto-select this upload
                this.selectUpload(result.upload_id);
            } catch (err) {
                this.uploadProgress = 0;
                this.showError(`Upload failed: ${err.message}`);
                console.error(err);
            }
        },

        /**
         * Select an upload for processing
         */
        selectUpload(uploadId) {
            console.log('Selected upload:', uploadId);
            this.currentUploadId = uploadId;
            this.currentBundleId = null;
            this.sliceResult = null;

            // Reset workflow
            this.workflow = {
                normalize: 'pending',
                bundle: 'pending',
                slice: 'pending',
            };
        },

        /**
         * Start normalization process
         */
        async startNormalize() {
            if (!this.currentUploadId) return;

            console.log('Starting normalization for:', this.currentUploadId);
            this.workflow.normalize = 'processing';

            try {
                const result = await api.normalize(this.currentUploadId);
                console.log('Normalization started:', result);

                // Poll for completion
                this.pollNormalizeStatus(result.job_id);
            } catch (err) {
                this.workflow.normalize = 'failed';
                this.showError(`Normalization failed: ${err.message}`);
                console.error(err);
            }
        },

        /**
         * Poll normalization job status
         */
        pollNormalizeStatus(jobId) {
            if (this.normalizeInterval) {
                clearInterval(this.normalizeInterval);
            }

            this.normalizeInterval = setInterval(async () => {
                try {
                    const upload = await api.getUpload(this.currentUploadId);
                    console.log('Normalization status:', upload.normalization_status);

                    if (upload.normalization_status === 'normalized') {
                        clearInterval(this.normalizeInterval);
                        this.workflow.normalize = 'completed';
                        console.log('Normalization completed');
                    } else if (upload.normalization_status === 'failed') {
                        clearInterval(this.normalizeInterval);
                        this.workflow.normalize = 'failed';
                        this.showError('Normalization failed');
                    }
                } catch (err) {
                    console.error('Failed to check normalization status:', err);
                }
            }, 2000); // Poll every 2 seconds
        },

        /**
         * Create bundle
         */
        async createBundle() {
            if (!this.currentUploadId || !this.selectedFilament) return;

            console.log('Creating bundle for:', this.currentUploadId);
            this.workflow.bundle = 'processing';

            try {
                // First, get the upload details to fetch object IDs
                const upload = await api.getUpload(this.currentUploadId);

                // Extract the database IDs from the objects
                // Note: We need to get the actual database IDs, not the object_id field
                // For now, we'll use a workaround to get all normalized objects for this upload
                const objectIds = await this.getObjectIdsForUpload(this.currentUploadId);
                console.log('Object IDs for bundle:', objectIds);

                if (!objectIds || objectIds.length === 0) {
                    throw new Error('No normalized objects found for this upload');
                }

                const bundleData = {
                    name: `Bundle ${new Date().toLocaleString()}`,
                    object_ids: objectIds,
                    filament_id: parseInt(this.selectedFilament),
                };
                console.log('Creating bundle with data:', bundleData);

                const result = await api.createBundle(bundleData);

                console.log('Bundle created:', result);
                this.currentBundleId = result.bundle_id;
                this.workflow.bundle = 'completed';
            } catch (err) {
                this.workflow.bundle = 'failed';
                this.showError(`Failed to create bundle: ${err.message}`);
                console.error(err);
            }
        },

        /**
         * Get database IDs of normalized objects for an upload
         */
        async getObjectIdsForUpload(uploadId) {
            try {
                const response = await api.fetch(`/upload/${uploadId}/objects`);
                return response.object_ids || [];
            } catch (err) {
                console.error('Failed to get object IDs:', err);
                return [];
            }
        },

        /**
         * Start slicing process
         */
        async startSlice() {
            if (!this.currentBundleId) return;

            console.log('Starting slice for bundle:', this.currentBundleId);
            console.log('Settings:', this.sliceSettings);
            this.workflow.slice = 'processing';

            try {
                const result = await api.slice(this.currentBundleId, this.sliceSettings);
                console.log('Slice started:', result);

                if (result.status === 'completed') {
                    // Synchronous slicing (completed immediately)
                    this.workflow.slice = 'completed';
                    this.sliceResult = result;
                } else {
                    // Async slicing - poll for completion
                    this.pollSliceStatus(result.job_id);
                }
            } catch (err) {
                this.workflow.slice = 'failed';
                this.showError(`Slicing failed: ${err.message}`);
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
                    const job = await api.getSlicingJob(jobId);
                    console.log('Slice status:', job.status);

                    if (job.status === 'completed') {
                        clearInterval(this.sliceInterval);
                        this.workflow.slice = 'completed';
                        this.sliceResult = job;
                        console.log('Slicing completed');
                    } else if (job.status === 'failed') {
                        clearInterval(this.sliceInterval);
                        this.workflow.slice = 'failed';
                        this.showError(`Slicing failed: ${job.error}`);
                    }
                } catch (err) {
                    console.error('Failed to check slice status:', err);
                }
            }, 3000); // Poll every 3 seconds
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
            if (this.normalizeInterval) {
                clearInterval(this.normalizeInterval);
            }
            if (this.sliceInterval) {
                clearInterval(this.sliceInterval);
            }
        },
    };
}
