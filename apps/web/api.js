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
                        onProgress({ phase: 'uploading', progress });
                    }
                });

                // Upload bytes are fully transferred; server may still be parsing/validating.
                xhr.upload.addEventListener('load', () => {
                    onProgress({ phase: 'processing', progress: 100 });
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
    async listUploads(limit = 20, offset = 0) {
        return this.fetch(`/upload?limit=${limit}&offset=${offset}`);
    }

    /**
     * Slice an upload directly to G-code
     * @param {number} uploadId - The upload ID to slice
     * @param {object} settings - Slicing settings
     * @returns {Promise<{job_id: string, status: string}>}
     */
    async sliceUpload(uploadId, settings) {
        const payload = {
            layer_height: settings.layer_height,
            infill_density: settings.infill_density,
            wall_count: settings.wall_count,
            infill_pattern: settings.infill_pattern,
            supports: settings.supports,
            support_type: settings.support_type || null,
            support_threshold_angle: settings.support_threshold_angle ?? null,
            brim_type: settings.brim_type || null,
            brim_width: settings.brim_width ?? null,
            brim_object_gap: settings.brim_object_gap ?? null,
            skirt_loops: settings.skirt_loops ?? null,
            skirt_distance: settings.skirt_distance ?? null,
            skirt_height: settings.skirt_height ?? null,
            enable_prime_tower: settings.enable_prime_tower,
            prime_volume: settings.prime_volume,
            prime_tower_width: settings.prime_tower_width,
            prime_tower_brim_width: settings.prime_tower_brim_width,
            prime_tower_brim_chamfer: settings.prime_tower_brim_chamfer,
            prime_tower_brim_chamfer_max_width: settings.prime_tower_brim_chamfer_max_width,
            enable_flow_calibrate: settings.enable_flow_calibrate,
            nozzle_temp: settings.nozzle_temp,
            bed_temp: settings.bed_temp,
            bed_type: settings.bed_type,
            filament_colors: settings.filament_colors,
            extruder_assignments: settings.extruder_assignments
        };

        // Support both single filament_id and filament_ids array
        if (settings.filament_ids && settings.filament_ids.length > 0) {
            payload.filament_ids = settings.filament_ids;
        } else if (settings.filament_id) {
            payload.filament_id = settings.filament_id;
        }

        return this.fetch(`/uploads/${uploadId}/slice`, {
            method: 'POST',
            body: JSON.stringify(payload),
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
        const payload = {
            plate_id: plateId,
            layer_height: settings.layer_height,
            infill_density: settings.infill_density,
            wall_count: settings.wall_count,
            infill_pattern: settings.infill_pattern,
            supports: settings.supports,
            support_type: settings.support_type || null,
            support_threshold_angle: settings.support_threshold_angle ?? null,
            brim_type: settings.brim_type || null,
            brim_width: settings.brim_width ?? null,
            brim_object_gap: settings.brim_object_gap ?? null,
            skirt_loops: settings.skirt_loops ?? null,
            skirt_distance: settings.skirt_distance ?? null,
            skirt_height: settings.skirt_height ?? null,
            enable_prime_tower: settings.enable_prime_tower,
            prime_volume: settings.prime_volume,
            prime_tower_width: settings.prime_tower_width,
            prime_tower_brim_width: settings.prime_tower_brim_width,
            prime_tower_brim_chamfer: settings.prime_tower_brim_chamfer,
            prime_tower_brim_chamfer_max_width: settings.prime_tower_brim_chamfer_max_width,
            enable_flow_calibrate: settings.enable_flow_calibrate,
            nozzle_temp: settings.nozzle_temp,
            bed_temp: settings.bed_temp,
            bed_type: settings.bed_type,
            filament_colors: settings.filament_colors,
            extruder_assignments: settings.extruder_assignments
        };

        // Support both single filament_id and filament_ids array
        if (settings.filament_ids && settings.filament_ids.length > 0) {
            payload.filament_ids = settings.filament_ids;
        } else if (settings.filament_id) {
            payload.filament_id = settings.filament_id;
        }

        return this.fetch(`/uploads/${uploadId}/slice-plate`, {
            method: 'POST',
            body: JSON.stringify(payload),
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
     * Update filament profile
     */
    async updateFilament(filamentId, data) {
        return this.fetch(`/filaments/${filamentId}`, {
            method: 'PUT',
            body: JSON.stringify(data),
        });
    }

    /**
     * Delete filament profile
     */
    async deleteFilament(filamentId) {
        return this.fetch(`/filaments/${filamentId}`, {
            method: 'DELETE',
        });
    }

    /**
     * Set one filament as default
     */
    async setDefaultFilament(filamentId) {
        return this.fetch(`/filaments/${filamentId}/default`, {
            method: 'POST',
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
     * Import filament profile JSON file
     */
    async importFilamentProfile(file) {
        const formData = new FormData();
        formData.append('file', file);

        const response = await fetch(`${this.baseUrl}/filaments/import`, {
            method: 'POST',
            body: formData,
        });

        if (!response.ok) {
            const error = await response.json().catch(() => ({ detail: response.statusText }));
            throw new Error(error.detail || `HTTP ${response.status}`);
        }

        return await response.json();
    }

    /**
     * Export filament profile as OrcaSlicer-compatible JSON
     * @param {number} filamentId - The filament ID to export
     */
    async exportFilamentProfile(filamentId) {
        const response = await fetch(`${this.baseUrl}/filaments/${filamentId}/export`);
        if (!response.ok) {
            const error = await response.json().catch(() => ({ detail: response.statusText }));
            throw new Error(error.detail || `HTTP ${response.status}`);
        }
        const data = await response.json();
        // Trigger download as JSON file
        const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `${data.name || 'filament'}.json`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
        return data;
    }

    /**
     * Preview filament profile import without saving
     */
    async previewFilamentProfileImport(file) {
        const formData = new FormData();
        formData.append('file', file);

        const response = await fetch(`${this.baseUrl}/filaments/import/preview`, {
            method: 'POST',
            body: formData,
        });

        if (!response.ok) {
            const error = await response.json().catch(() => ({ detail: response.statusText }));
            throw new Error(error.detail || `HTTP ${response.status}`);
        }

        return await response.json();
    }

    /**
     * Get extruder presets + default slicing settings
     */
    async getExtruderPresets() {
        return this.fetch('/presets/extruders');
    }

    /**
     * Get Orca process profile defaults for UI hints
     */
    async getOrcaDefaults() {
        return this.fetch('/presets/orca-defaults');
    }

    /**
     * Save extruder presets + default slicing settings
     */
    async saveExtruderPresets(payload) {
        return this.fetch('/presets/extruders', {
            method: 'PUT',
            body: JSON.stringify(payload),
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

    /**
     * List all slicing jobs
     * @returns {Promise<Array>}
     */
    async getJobs(limit = 20, offset = 0) {
        return this.fetch(`/jobs?limit=${limit}&offset=${offset}`);
    }

    /**
     * Delete an upload and all associated jobs
     * @param {string} uploadId - The upload ID to delete
     */
    async deleteUpload(uploadId) {
        return this.fetch(`/upload/${uploadId}`, {
            method: 'DELETE',
        });
    }

    /**
     * Delete a single slicing job
     * @param {string} jobId - The job ID to delete
     */
    async deleteJob(jobId) {
        return this.fetch(`/jobs/${jobId}`, {
            method: 'DELETE',
        });
    }

    // -----------------------------------------------------------------------
    // MakerWorld Integration
    // -----------------------------------------------------------------------

    /**
     * Look up a MakerWorld model by URL
     * @param {string} url - MakerWorld model URL
     * @returns {Promise<{design_id, title, author, thumbnail, profiles}>}
     */
    async lookupMakerWorld(url) {
        return this.fetch('/makerworld/lookup', {
            method: 'POST',
            body: JSON.stringify({ url }),
        });
    }

    /**
     * Download a 3MF from MakerWorld and process it
     * @param {string} url - MakerWorld model URL
     * @param {number} instanceId - Profile/instance ID to download
     * @returns {Promise<{upload_id, filename, ...}>} Same as upload response
     */
    async downloadMakerWorld(url, instanceId) {
        return this.fetch('/makerworld/download', {
            method: 'POST',
            body: JSON.stringify({ url, instance_id: instanceId }),
        });
    }

    // -----------------------------------------------------------------------
    // Printer Settings & Print Control
    // -----------------------------------------------------------------------

    async getPrinterSettings() {
        return this.fetch('/printer/settings');
    }

    async savePrinterSettings(data) {
        return this.fetch('/printer/settings', {
            method: 'PUT',
            body: JSON.stringify(data),
        });
    }

    async sendToPrinter(jobId) {
        return this.fetch('/printer/print', {
            method: 'POST',
            body: JSON.stringify({ job_id: jobId }),
        });
    }

    async getPrintStatus() {
        return this.fetch('/printer/print/status');
    }

    async pausePrint() {
        return this.fetch('/printer/pause', { method: 'POST' });
    }

    async resumePrint() {
        return this.fetch('/printer/resume', { method: 'POST' });
    }

    async cancelPrint() {
        return this.fetch('/printer/cancel', { method: 'POST' });
    }
}

// Export singleton instance
const api = new ApiClient();
