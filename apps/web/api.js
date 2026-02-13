/**
 * API Client for U1 Slicer Bridge
 * Handles all communication with the FastAPI backend
 */

const API_BASE = '/api';

class ApiClient {
    constructor(baseUrl = API_BASE) {
        this.baseUrl = baseUrl;
    }

    /**
     * Generic fetch wrapper with error handling
     */
    async fetch(endpoint, options = {}) {
        try {
            const response = await fetch(`${this.baseUrl}${endpoint}`, {
                ...options,
                headers: {
                    'Content-Type': 'application/json',
                    ...options.headers,
                },
            });

            if (!response.ok) {
                const error = await response.json().catch(() => ({ detail: response.statusText }));

                // Handle validation errors (422)
                if (Array.isArray(error.detail)) {
                    const messages = error.detail.map(e => `${e.loc.join('.')}: ${e.msg}`).join(', ');
                    throw new Error(messages);
                }

                throw new Error(error.detail || `HTTP ${response.status}`);
            }

            return await response.json();
        } catch (error) {
            console.error(`API Error [${endpoint}]:`, error);
            throw error;
        }
    }

    /**
     * Upload a 3MF file
     * @param {File} file - The 3MF file to upload
     * @param {Function} onProgress - Callback for upload progress (0-100)
     * @returns {Promise<{upload_id: number, filename: string, objects: Array}>}
     */
    async uploadFile(file, onProgress = null) {
        const formData = new FormData();
        formData.append('file', file);

        return new Promise((resolve, reject) => {
            const xhr = new XMLHttpRequest();

            // Track upload progress
            if (onProgress) {
                xhr.upload.addEventListener('progress', (e) => {
                    if (e.lengthComputable) {
                        const progress = Math.round((e.loaded / e.total) * 100);
                        onProgress(progress);
                    }
                });
            }

            xhr.addEventListener('load', () => {
                if (xhr.status >= 200 && xhr.status < 300) {
                    try {
                        const response = JSON.parse(xhr.responseText);
                        // Add object_count for UI compatibility
                        response.object_count = response.objects?.length || 0;
                        resolve(response);
                    } catch (e) {
                        reject(new Error('Failed to parse response'));
                    }
                } else {
                    try {
                        const error = JSON.parse(xhr.responseText);
                        reject(new Error(error.detail || `HTTP ${xhr.status}`));
                    } catch (e) {
                        reject(new Error(`HTTP ${xhr.status}`));
                    }
                }
            });

            xhr.addEventListener('error', () => {
                reject(new Error('Network error'));
            });

            xhr.addEventListener('abort', () => {
                reject(new Error('Upload cancelled'));
            });

            // NOTE: API uses /upload (singular), not /uploads
            xhr.open('POST', `${this.baseUrl}/upload`);
            xhr.send(formData);
        });
    }

    /**
     * Get upload details
     */
    async getUpload(uploadId) {
        return this.fetch(`/upload/${uploadId}`);
    }

    /**
     * List all uploads
     */
    async listUploads() {
        return this.fetch('/upload');
    }

    /**
     * Slice an upload directly to G-code
     * @param {number} uploadId - The upload ID to slice
     * @param {object} settings - Slicing settings
     * @returns {Promise<{job_id: string, status: string}>}
     */
    async sliceUpload(uploadId, settings) {
        return this.fetch(`/uploads/${uploadId}/slice`, {
            method: 'POST',
            body: JSON.stringify({
                filament_id: settings.filament_id,
                layer_height: settings.layer_height,
                infill_density: settings.infill_density,
                supports: settings.supports,
                nozzle_temp: settings.nozzle_temp,
                bed_temp: settings.bed_temp,
                bed_type: settings.bed_type
            }),
        });
    }

    /**
     * Get plate information for a multi-plate upload
     * @param {number} uploadId - The upload ID
     * @returns {Promise<{is_multi_plate: boolean, plates: Array}>}
     */
    async getUploadPlates(uploadId) {
        return this.fetch(`/uploads/${uploadId}/plates`);
    }

    /**
     * Slice a specific plate from a multi-plate upload
     * @param {number} uploadId - The upload ID
     * @param {number} plateId - The plate ID to slice
     * @param {object} settings - Slicing settings
     * @returns {Promise<{job_id: string, status: string}>}
     */
    async slicePlate(uploadId, plateId, settings) {
        return this.fetch(`/uploads/${uploadId}/slice-plate`, {
            method: 'POST',
            body: JSON.stringify({
                plate_id: plateId,
                filament_id: settings.filament_id,
                layer_height: settings.layer_height,
                infill_density: settings.infill_density,
                supports: settings.supports,
                nozzle_temp: settings.nozzle_temp,
                bed_temp: settings.bed_temp,
                bed_type: settings.bed_type
            }),
        });
    }

    /**
     * Create a new filament
     */
    async createFilament(data) {
        return this.fetch('/filaments', {
            method: 'POST',
            body: JSON.stringify(data),
        });
    }

    /**
     * List all filaments
     */
    async listFilaments() {
        return this.fetch('/filaments');
    }

    /**
     * Initialize default filaments
     */
    async initDefaultFilaments() {
        return this.fetch('/filaments/init-defaults', {
            method: 'POST',
        });
    }

    /**
     * Get job status
     * @param {string} jobId - The job ID to check
     * @returns {Promise<{job_id: string, status: string, metadata: object}>}
     */
    async getJobStatus(jobId) {
        return this.fetch(`/jobs/${jobId}`);
    }

    /**
     * Get printer status
     */
    async getPrinterStatus() {
        return this.fetch('/printer/status');
    }

    /**
     * Health check
     */
    async healthCheck() {
        return this.fetch('/healthz');
    }

    /**
     * Get G-code layer metadata for viewer
     */
    async getGCodeMetadata(jobId) {
        return this.fetch(`/jobs/${jobId}/gcode/metadata`);
    }

    /**
     * Get G-code layer geometry for viewer
     */
    async getGCodeLayers(jobId, start = 0, count = 20) {
        return this.fetch(`/jobs/${jobId}/gcode/layers?start=${start}&count=${count}`);
    }

    /**
     * Download G-code file
     */
    downloadGCode(jobId) {
        // Open download in new window
        window.open(`/jobs/${jobId}/download`, '_blank');
    }
}

// Export singleton instance
const api = new ApiClient();
