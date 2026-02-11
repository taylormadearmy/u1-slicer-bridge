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
     * Normalize objects in an upload
     */
    async normalize(uploadId, options = {}) {
        return this.fetch(`/normalize/${uploadId}`, {
            method: 'POST',
            body: JSON.stringify({
                printer_profile: options.printer_profile || 'snapmaker_u1',
                object_ids: options.object_ids || null,
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
     * Create a bundle
     */
    async createBundle(data) {
        return this.fetch('/bundles', {
            method: 'POST',
            body: JSON.stringify(data),
        });
    }

    /**
     * Get bundle details
     */
    async getBundle(bundleId) {
        return this.fetch(`/bundles/${bundleId}`);
    }

    /**
     * List all bundles
     */
    async listBundles() {
        return this.fetch('/bundles');
    }

    /**
     * Slice a bundle to G-code
     */
    async slice(bundleId, settings = {}) {
        return this.fetch(`/bundles/${bundleId}/slice`, {
            method: 'POST',
            body: JSON.stringify(settings),
        });
    }

    /**
     * Get slicing job status
     */
    async getSlicingJob(jobId) {
        return this.fetch(`/slicing/jobs/${jobId}`);
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
     * Get G-code preview metadata
     */
    async getGCodeMetadata(jobId) {
        return this.fetch(`/slicing/jobs/${jobId}/gcode/metadata`);
    }

    /**
     * Get G-code layer geometry
     */
    async getGCodeLayers(jobId, start = 0, count = 20) {
        return this.fetch(`/slicing/jobs/${jobId}/gcode/layers?start=${start}&count=${count}`);
    }

    /**
     * Download G-code file
     */
    downloadGCode(jobId) {
        // Open download in new window
        window.open(`/api/slicing/jobs/${jobId}/download`, '_blank');
    }

    /**
     * List recent slicing jobs
     */
    async listSlicingJobs(limit = 20, offset = 0) {
        return this.fetch(`/slicing/jobs?limit=${limit}&offset=${offset}`);
    }
}

// Export singleton instance
const api = new ApiClient();
