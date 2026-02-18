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
        activeTab: 'upload', // kept for compatibility — always 'upload' now
        showSettingsModal: false,
        showStorageDrawer: false,
        showPrinterStatus: false,

        // Data
        uploads: [],
        uploadsTotal: 0,
        uploadsHasMore: false,
        jobs: [],               // All slicing jobs
        jobsTotal: 0,
        jobsHasMore: false,
        filaments: [],
        showFilamentForm: false,
        editingFilamentId: null,
        filamentForm: {
            name: '',
            material: 'PLA',
            nozzle_temp: 210,
            bed_temp: 60,
            print_speed: 60,
            density: 1.24,
            bed_type: 'PEI',
            color_hex: '#FFFFFF',
            extruder_index: 0,
            is_default: false,
            source_type: 'manual',
        },
        importPreviewOpen: false,
        importPreviewData: null,
        importPreviewFile: null,
        selectedUpload: null,     // Current upload object
        selectedFilament: null,   // Selected filament ID (single filament mode)
        selectedFilaments: [],    // Selected filament IDs (multi-filament mode)
        selectedPlate: null,       // Selected plate ID for multi-plate files
        plates: [],               // Available plates for selected upload
        platesLoading: false,     // Loading state for plates
        detectedColors: [],       // Colors detected from 3MF file
        fileSettings: null,       // Print settings detected from 3MF file
        filamentOverride: false,  // Whether user wants to manually override filament assignment
        accordionColours: false,  // Colours & Filaments section (closed by default, summary shown)
        accordionSettings: false, // Print Settings section (closed by default, summary shown)
        extruderPresets: [
            { slot: 1, filament_id: null, color_hex: '#FFFFFF' },
            { slot: 2, filament_id: null, color_hex: '#FFFFFF' },
            { slot: 3, filament_id: null, color_hex: '#FFFFFF' },
            { slot: 4, filament_id: null, color_hex: '#FFFFFF' },
        ],
        presetsLoaded: false,
        presetsSaving: false,
        presetMessage: null,
        maxExtruders: 4,
        multicolorNotice: null,

        // Printer status
        printerConnected: false,
        printerBusy: false,
        printerStatus: 'Checking...',

        // Printer settings (for Settings modal)
        printerSettings: { moonraker_url: '' },
        printerSettingsSaving: false,
        printerTestResult: null,

        // Print monitor
        printMonitorActive: false,
        printSending: false,
        printState: null,
        printMonitorInterval: null,

        // 3-way setting modes: 'model' (use file) | 'orca' (process default) | 'override' (custom)
        settingModes: {},
        orcaDefaults: {},

        // Slicing settings
        machineSettings: {
            layer_height: 0.2,
            infill_density: 15,
            wall_count: 3,
            infill_pattern: 'gyroid',
            supports: false,
            support_type: null,
            support_threshold_angle: null,
            brim_type: null,
            brim_width: null,
            brim_object_gap: null,
            skirt_loops: null,
            skirt_distance: null,
            skirt_height: null,
            enable_prime_tower: false,
            prime_volume: null,
            prime_tower_width: null,
            prime_tower_brim_width: null,
            prime_tower_brim_chamfer: true,
            prime_tower_brim_chamfer_max_width: null,
            enable_flow_calibrate: true,
            bed_temp: null,
            bed_type: null,
        },
        useJobOverrides: false,
        sliceSettings: {
            layer_height: 0.2,
            infill_density: 15,
            wall_count: 3,
            infill_pattern: 'gyroid',
            supports: false,
            support_type: null,
            support_threshold_angle: null,
            brim_type: null,
            brim_width: null,
            brim_object_gap: null,
            skirt_loops: null,
            skirt_distance: null,
            skirt_height: null,
            enable_prime_tower: false,
            prime_volume: null,
            prime_tower_width: null,
            prime_tower_brim_width: null,
            prime_tower_brim_chamfer: true,
            prime_tower_brim_chamfer_max_width: null,
            enable_flow_calibrate: true,
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

            // Load initial data — printer check runs in parallel so it
            // doesn't block file lists when Moonraker is slow/unreachable.
            this.loadOrcaDefaults(); // non-blocking
            this.loadPrinterSettings(); // non-blocking, pre-load for settings modal
            this.checkPrinterStatus(); // non-blocking — updates header indicator async
            await this.loadFilaments();
            await this.loadExtruderPresets();
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
                this.printerBusy = ['printing', 'paused'].includes(status.print_status?.state);
                // Show print progress in header when actively printing
                if (status.print_status && status.print_status.state === 'printing') {
                    const pct = Math.round((status.print_status.progress || 0) * 100);
                    this.printerStatus = `Printing ${pct}%`;
                } else if (status.print_status && status.print_status.state === 'paused') {
                    const pct = Math.round((status.print_status.progress || 0) * 100);
                    this.printerStatus = `Paused ${pct}%`;
                } else {
                    this.printerStatus = status.connected ? 'Connected' : 'Offline';
                }
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
         * Load Orca process profile defaults for UI hints.
         */
        async loadOrcaDefaults() {
            try {
                this.orcaDefaults = await api.getOrcaDefaults();
            } catch (err) {
                console.warn('Failed to load Orca defaults:', err);
            }
        },

        /**
         * Load persistent extruder and slicing defaults presets.
         */
        async loadExtruderPresets() {
            try {
                const response = await api.getExtruderPresets();
                const presets = (response.extruders || []).slice(0, this.maxExtruders);
                if (presets.length === this.maxExtruders) {
                    this.extruderPresets = presets.map((p, idx) => ({
                        slot: idx + 1,
                        filament_id: p.filament_id || null,
                        color_hex: p.color_hex || '#FFFFFF',
                    }));
                }

                if (response.slicing_defaults) {
                    const { setting_modes, ...rest } = response.slicing_defaults;
                    this.machineSettings = {
                        ...this.machineSettings,
                        ...rest,
                    };
                    this.machineSettings.layer_height = this.normalizeLayerHeight(this.machineSettings.layer_height);
                    this.machineSettings.nozzle_temp = null;
                    if (setting_modes && typeof setting_modes === 'object') {
                        this.settingModes = setting_modes;
                    }
                }

                this.presetsLoaded = true;
                this.applyPresetDefaults();
                this.resetJobOverrideSettings();
            } catch (err) {
                console.warn('Failed to load extruder presets:', err);
            }
        },

        /**
         * Persist extruder presets and current slicing defaults.
         */
        async saveExtruderPresets() {
            if (!this.presetsLoaded) return; // Don't save before presets loaded from server
            this.presetsSaving = true;
            this.presetMessage = null;

            try {
                const payload = {
                    extruders: this.extruderPresets.map((p, idx) => ({
                        slot: idx + 1,
                        filament_id: p.filament_id || null,
                        color_hex: p.color_hex || '#FFFFFF',
                    })),
                    slicing_defaults: {
                        layer_height: this.normalizeLayerHeight(this.machineSettings.layer_height),
                        infill_density: this.machineSettings.infill_density,
                        wall_count: this.machineSettings.wall_count,
                        infill_pattern: this.machineSettings.infill_pattern,
                        supports: this.machineSettings.supports,
                        support_type: this.machineSettings.support_type,
                        support_threshold_angle: this.machineSettings.support_threshold_angle,
                        brim_type: this.machineSettings.brim_type,
                        brim_width: this.machineSettings.brim_width,
                        brim_object_gap: this.machineSettings.brim_object_gap,
                        skirt_loops: this.machineSettings.skirt_loops,
                        skirt_distance: this.machineSettings.skirt_distance,
                        skirt_height: this.machineSettings.skirt_height,
                        enable_prime_tower: this.machineSettings.enable_prime_tower,
                        prime_volume: this.machineSettings.prime_volume,
                        prime_tower_width: this.machineSettings.prime_tower_width,
                        prime_tower_brim_width: this.machineSettings.prime_tower_brim_width,
                        prime_tower_brim_chamfer: this.machineSettings.prime_tower_brim_chamfer,
                        prime_tower_brim_chamfer_max_width: this.machineSettings.prime_tower_brim_chamfer_max_width,
                        enable_flow_calibrate: this.machineSettings.enable_flow_calibrate,
                        bed_temp: this.machineSettings.bed_temp,
                        bed_type: this.machineSettings.bed_type,
                        setting_modes: this.settingModes,
                    },
                };

                await api.saveExtruderPresets(payload);
                this.presetMessage = 'Extruder presets saved.';
                this.applyPresetDefaults();
                this.resetJobOverrideSettings();
            } catch (err) {
                this.presetMessage = `Failed to save presets: ${err.message}`;
                this.showError(this.presetMessage);
            } finally {
                this.presetsSaving = false;
                setTimeout(() => {
                    this.presetMessage = null;
                }, 3000);
            }
        },

        /**
         * Apply preset defaults to active filament selections.
         *
         * Only sets selectedFilament (single-filament default).
         * selectedFilaments (multicolor) is exclusively managed by
         * applyDetectedColors() based on the actual uploaded file.
         */
        applyPresetDefaults() {
            const e1 = this.extruderPresets[0];
            if (e1 && e1.filament_id) {
                this.selectedFilament = e1.filament_id;
            }
        },

        resetJobOverrideSettings() {
            this.useJobOverrides = false;
            this.applyPrinterDefaultsToOverrides();
        },

        applyPrinterDefaultsToOverrides() {
            const modeKeys = [
                'layer_height', 'infill_density', 'wall_count', 'infill_pattern',
                'supports', 'support_type', 'support_threshold_angle',
                'brim_type', 'brim_width', 'brim_object_gap',
                'skirt_loops', 'skirt_distance', 'skirt_height',
                'enable_prime_tower', 'prime_volume', 'prime_tower_width',
                'prime_tower_brim_width', 'prime_tower_brim_chamfer',
                'prime_tower_brim_chamfer_max_width',
                'bed_temp', 'bed_type',
            ];
            const s = {};
            for (const key of modeKeys) {
                const mode = this.settingModes[key] || 'model';
                if (mode === 'override') {
                    let v = this.machineSettings[key];
                    if (key === 'layer_height' && v != null) v = this.normalizeLayerHeight(v);
                    s[key] = v ?? null;
                } else {
                    // 'model' or 'orca': use Orca process default as baseline
                    // For 'model' mode, applyFileSettings() may overwrite with file values
                    s[key] = this.orcaDefaults[key] ?? null;
                }
            }
            // Machine-level toggles (not 3-way mode)
            s.enable_flow_calibrate = this.machineSettings.enable_flow_calibrate;

            this.sliceSettings = {
                ...this.sliceSettings,
                ...s,
                nozzle_temp: null,
            };
        },

        /**
         * Apply print settings detected from a 3MF file as defaults.
         * Called after resetJobOverrideSettings() so file values override printer defaults.
         */
        applyFileSettings(settings) {
            if (!settings || Object.keys(settings).length === 0) return;

            // Only apply file values for settings in 'model' mode
            const ok = (key) => (this.settingModes[key] || 'model') === 'model';

            // Support
            if (ok('supports') && settings.enable_support !== undefined) {
                this.sliceSettings.supports = !!settings.enable_support;
            }
            if (ok('support_type') && settings.support_type) {
                this.sliceSettings.support_type = settings.support_type;
            }
            if (ok('support_threshold_angle') && settings.support_threshold_angle !== undefined) {
                this.sliceSettings.support_threshold_angle = settings.support_threshold_angle;
            }
            // Brim
            if (ok('brim_type') && settings.brim_type) {
                this.sliceSettings.brim_type = settings.brim_type;
            }
            if (ok('brim_width') && settings.brim_width !== undefined) {
                this.sliceSettings.brim_width = settings.brim_width;
            }
            if (ok('brim_object_gap') && settings.brim_object_gap !== undefined) {
                this.sliceSettings.brim_object_gap = settings.brim_object_gap;
            }
            // Skirt
            if (ok('skirt_loops') && settings.skirt_loops !== undefined) {
                this.sliceSettings.skirt_loops = settings.skirt_loops;
            }
            if (ok('skirt_distance') && settings.skirt_distance !== undefined) {
                this.sliceSettings.skirt_distance = settings.skirt_distance;
            }
            if (ok('skirt_height') && settings.skirt_height !== undefined) {
                this.sliceSettings.skirt_height = settings.skirt_height;
            }
            // Wall / Infill / Layer
            if (ok('wall_count') && settings.wall_loops !== undefined) {
                this.sliceSettings.wall_count = settings.wall_loops;
            }
            if (ok('infill_density') && settings.sparse_infill_density !== undefined) {
                this.sliceSettings.infill_density = settings.sparse_infill_density;
            }
            if (ok('infill_pattern') && settings.sparse_infill_pattern) {
                this.sliceSettings.infill_pattern = settings.sparse_infill_pattern;
            }
            if (ok('layer_height') && settings.layer_height !== undefined) {
                this.sliceSettings.layer_height = settings.layer_height;
            }
            // Prime tower
            if (ok('enable_prime_tower') && settings.enable_prime_tower !== undefined) {
                this.sliceSettings.enable_prime_tower = !!settings.enable_prime_tower;
            }
            if (ok('prime_tower_width') && settings.prime_tower_width !== undefined) {
                this.sliceSettings.prime_tower_width = settings.prime_tower_width;
            }
            if (ok('prime_tower_brim_width') && settings.prime_tower_brim_width !== undefined) {
                this.sliceSettings.prime_tower_brim_width = settings.prime_tower_brim_width;
            }
            if (ok('prime_volume') && settings.prime_volume !== undefined) {
                this.sliceSettings.prime_volume = settings.prime_volume;
            }
            // Temperature / Bed
            if (ok('bed_temp') && settings.bed_temperature !== undefined) {
                this.sliceSettings.bed_temp = settings.bed_temperature;
            }
            if (ok('bed_type') && settings.curr_bed_type) {
                this.sliceSettings.bed_type = settings.curr_bed_type;
            }
        },

        handleJobOverrideToggle(enabled) {
            const isEnabled = typeof enabled === 'boolean' ? enabled : this.useJobOverrides;
            if (isEnabled) {
                this.applyPrinterDefaultsToOverrides();
            }
        },

        printerDefaultText(value, unit = '') {
            if (value === null || value === undefined || value === '') {
                return `Filament default (printer default)`;
            }
            return `${value}${unit} (printer default)`;
        },

        /**
         * Returns 'file' if the setting came from the 3MF file, otherwise 'default'.
         * Uses a mapping from fileSettings keys to sliceSettings keys.
         */
        settingSource(sliceKey) {
            const mode = this.settingModes[sliceKey] || 'model';
            if (mode === 'override') return 'override';
            if (mode === 'orca') return 'orca';
            // mode === 'model': check if file provided the value
            if (!this.fileSettings) return 'orca';
            const keyMap = {
                layer_height: 'layer_height',
                infill_density: 'sparse_infill_density',
                infill_pattern: 'sparse_infill_pattern',
                wall_count: 'wall_loops',
                supports: 'enable_support',
                support_type: 'support_type',
                support_threshold_angle: 'support_threshold_angle',
                brim_type: 'brim_type',
                brim_width: 'brim_width',
                brim_object_gap: 'brim_object_gap',
                skirt_loops: 'skirt_loops',
                skirt_distance: 'skirt_distance',
                skirt_height: 'skirt_height',
                enable_prime_tower: 'enable_prime_tower',
                prime_tower_width: 'prime_tower_width',
                prime_tower_brim_width: 'prime_tower_brim_width',
                prime_volume: 'prime_volume',
                bed_temp: 'bed_temperature',
                bed_type: 'curr_bed_type',
            };
            const fileKey = keyMap[sliceKey];
            if (!fileKey) return 'orca';
            return this.fileSettings[fileKey] !== undefined ? 'file' : 'orca';
        },

        normalizeLayerHeight(value) {
            const numeric = Number(value);
            if (!Number.isFinite(numeric)) return 0.2;
            return Number(numeric.toFixed(2));
        },

        formatLayerHeight(value) {
            return this.normalizeLayerHeight(value).toFixed(2);
        },

        openSettings() {
            this.showStorageDrawer = false;
            this.showPrinterStatus = false;
            this.showSettingsModal = true;
            this.loadPrinterSettings();
        },

        async closeSettings() {
            this.showSettingsModal = false;
            // Auto-save all settings on close
            try {
                await Promise.all([
                    this.saveExtruderPresets(),
                    this.savePrinterSettings(),
                ]);
            } catch (err) {
                console.warn('Auto-save on close failed:', err);
            }
        },

        openStorage() {
            this.showSettingsModal = false;
            this.showPrinterStatus = false;
            this.showStorageDrawer = true;
        },

        closeStorage() {
            this.showStorageDrawer = false;
        },

        openPrinterStatus() {
            this.showSettingsModal = false;
            this.showStorageDrawer = false;
            this.showPrinterStatus = true;
            // Start polling for live updates
            this.pollPrintStatus();
            if (!this.printMonitorInterval) {
                this.startPrintMonitorPolling();
            }
        },

        closePrinterStatus() {
            this.showPrinterStatus = false;
            // Keep polling if actively printing, stop otherwise
            if (!this.printState?.state || this.printState.state === 'standby' ||
                this.printState.state === 'complete' || this.printState.state === 'error') {
                this.stopPrintMonitorPolling();
            }
        },

        groupedFiles() {
            return this.uploads.map(u => ({
                ...u,
                jobs: this.jobs.filter(j => j.upload_id === u.upload_id),
            }));
        },

        selectUploadFromDrawer(upload) {
            this.closeStorage();
            this.selectUpload(upload);
        },

        viewJobFromDrawer(job) {
            this.closeStorage();
            this.viewJob(job);
        },

        openUpload() {
            this.activeTab = 'upload';
        },

        /**
         * Initialize default filaments
         */
        async initDefaultFilaments() {
            try {
                await api.initDefaultFilaments();
                await this.loadFilaments();
                await this.loadExtruderPresets();
                console.log('Default filaments initialized');
            } catch (err) {
                this.showError('Failed to initialize default filaments');
                console.error(err);
            }
        },

        openImportFilamentDialog() {
            const input = document.getElementById('import-filament-input');
            if (input) input.click();
        },

        async importFilamentFromFile(event) {
            const file = event?.target?.files?.[0];
            if (!file) return;

            try {
                const preview = await api.previewFilamentProfileImport(file);
                this.importPreviewData = preview;
                this.importPreviewFile = file;
                this.importPreviewOpen = true;
            } catch (err) {
                this.showError(`Failed to import filament profile: ${err.message}`);
                console.error(err);
            } finally {
                event.target.value = '';
            }
        },

        cancelImportPreview() {
            this.importPreviewOpen = false;
            this.importPreviewData = null;
            this.importPreviewFile = null;
        },

        async confirmImportFilament() {
            if (!this.importPreviewFile) return;
            try {
                await api.importFilamentProfile(this.importPreviewFile);
                await this.loadFilaments();
                this.cancelImportPreview();
            } catch (err) {
                this.showError(`Failed to import filament profile: ${err.message}`);
                console.error(err);
            }
        },

        async exportFilamentProfile(filamentId) {
            try {
                await api.exportFilamentProfile(filamentId);
            } catch (err) {
                this.showError(`Failed to export filament profile: ${err.message}`);
                console.error(err);
            }
        },

        startCreateFilament() {
            this.editingFilamentId = null;
            this.filamentForm = {
                name: '',
                material: 'PLA',
                nozzle_temp: 210,
                bed_temp: 60,
                print_speed: 60,
                density: 1.24,
                bed_type: 'PEI',
                color_hex: '#FFFFFF',
                extruder_index: 0,
                is_default: false,
                source_type: 'manual',
            };
            this.showFilamentForm = true;
        },

        startEditFilament(filament) {
            this.editingFilamentId = filament.id;
            this.filamentForm = {
                name: filament.name,
                material: filament.material,
                nozzle_temp: filament.nozzle_temp,
                bed_temp: filament.bed_temp,
                print_speed: filament.print_speed || 60,
                density: filament.density ?? 1.24,
                bed_type: filament.bed_type || 'PEI',
                color_hex: filament.color_hex || '#FFFFFF',
                extruder_index: filament.extruder_index || 0,
                is_default: !!filament.is_default,
                source_type: filament.source_type || 'manual',
            };
            this.showFilamentForm = true;
        },

        cancelFilamentForm() {
            this.showFilamentForm = false;
            this.editingFilamentId = null;
        },

        async saveFilamentForm() {
            try {
                const payload = {
                    ...this.filamentForm,
                    nozzle_temp: Number(this.filamentForm.nozzle_temp),
                    bed_temp: Number(this.filamentForm.bed_temp),
                    print_speed: Number(this.filamentForm.print_speed),
                    density: Number(this.filamentForm.density) || 1.24,
                    extruder_index: Number(this.filamentForm.extruder_index),
                };

                if (this.editingFilamentId) {
                    await api.updateFilament(this.editingFilamentId, payload);
                } else {
                    await api.createFilament(payload);
                }

                await this.loadFilaments();
                await this.loadExtruderPresets();
                this.cancelFilamentForm();
            } catch (err) {
                this.showError(`Failed to save filament: ${err.message}`);
                console.error(err);
            }
        },

        async makeDefaultFilament(filamentId) {
            try {
                await api.setDefaultFilament(filamentId);
                await this.loadFilaments();
            } catch (err) {
                this.showError(`Failed to set default filament: ${err.message}`);
                console.error(err);
            }
        },

        async deleteFilament(filamentId, filamentName) {
            if (!confirm(`Delete filament '${filamentName}'?`)) return;

            try {
                await api.deleteFilament(filamentId);
                await this.loadFilaments();
                await this.loadExtruderPresets();
            } catch (err) {
                this.showError(`Failed to delete filament: ${err.message}`);
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
                this.uploadsTotal = response.total || this.uploads.length;
                this.uploadsHasMore = response.has_more || false;
                console.log(`Loaded ${this.uploads.length} of ${this.uploadsTotal} uploads`);
            } catch (err) {
                this.showError('Failed to load uploads');
                console.error(err);
            }
        },

        async loadMoreUploads() {
            try {
                const response = await api.listUploads(20, this.uploads.length);
                const more = response.uploads || [];
                this.uploads = [...this.uploads, ...more];
                this.uploadsTotal = response.total || this.uploads.length;
                this.uploadsHasMore = response.has_more || false;
                console.log(`Loaded ${more.length} more uploads (${this.uploads.length} of ${this.uploadsTotal})`);
            } catch (err) {
                this.showError('Failed to load more uploads');
                console.error(err);
            }
        },

        /**
         * Load all slicing jobs
         */
        async loadJobs() {
            try {
                const response = await api.getJobs();
                this.jobs = response.jobs || [];
                this.jobsTotal = response.total || this.jobs.length;
                this.jobsHasMore = response.has_more || false;
                console.log(`Loaded ${this.jobs.length} of ${this.jobsTotal} jobs`);
            } catch (err) {
                this.showError('Failed to load jobs');
                console.error(err);
            }
        },

        async loadMoreJobs() {
            try {
                const response = await api.getJobs(20, this.jobs.length);
                const more = response.jobs || [];
                this.jobs = [...this.jobs, ...more];
                this.jobsTotal = response.total || this.jobs.length;
                this.jobsHasMore = response.has_more || false;
                console.log(`Loaded ${more.length} more jobs (${this.jobs.length} of ${this.jobsTotal})`);
            } catch (err) {
                this.showError('Failed to load more jobs');
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
            const lower = file.name.toLowerCase();
            if (!lower.endsWith('.3mf') && !lower.endsWith('.stl')) {
                this.showError('Please upload a .3mf or .stl file');
                return;
            }

            console.log(`Uploading: ${file.name} (${(file.size / 1024 / 1024).toFixed(2)} MB)`);
            this.currentStep = 'upload';
            this.activeTab = 'upload';
            this.showSettingsModal = false;
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

                // Select this upload for configuration
                this.selectedUpload = result;
                this.selectedPlate = null;
                this.plates = [];
                this.platesLoading = false;
                this.activeTab = 'upload';

                // Ensure filaments are loaded before assigning (init() may still
                // be awaiting checkPrinterStatus when a fast upload completes).
                if (this.filaments.length === 0) {
                    await this.loadFilaments();
                }

                // Initialize detected colors and filament assignment state from upload response
                this.filamentOverride = false;
                this.applyDetectedColors(result.detected_colors || []);
                this.resetJobOverrideSettings();

                // Move to configure step AFTER colors/filaments are ready
                this.currentStep = 'configure';
                
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

                    // Apply file-embedded print settings (support, brim) as defaults
                    const fps = platesData.file_print_settings || result.file_print_settings;
                    this.fileSettings = fps && Object.keys(fps).length > 0 ? fps : null;
                    if (this.fileSettings) {
                        this.applyFileSettings(this.fileSettings);
                        console.log('Applied file print settings:', this.fileSettings);
                    }
                } catch (err) {
                    this.platesLoading = false;
                    this.selectedUpload.is_multi_plate = false;
                    this.plates = [];
                    console.warn('Could not load plates for new upload:', err);
                    // Still try to apply file settings from upload response
                    const fps = result.file_print_settings;
                    this.fileSettings = fps && Object.keys(fps).length > 0 ? fps : null;
                    if (this.fileSettings) this.applyFileSettings(this.fileSettings);
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

            // Re-selecting the same upload preserves all configure state
            if (this.selectedUpload && this.selectedUpload.upload_id === upload.upload_id) {
                this.currentStep = 'configure';
                this.activeTab = 'upload';
                return;
            }

            this.selectedUpload = upload;
            this.selectedPlate = null;
            this.plates = [];
            this.platesLoading = true;
            this.filamentOverride = false;
            this.resetJobOverrideSettings();

            // Transition immediately so the user sees the configure step
            // with its loading indicator instead of a blank stare.
            this.currentStep = 'configure';
            this.activeTab = 'upload';

            // Fetch upload details and plates in parallel to cut wall time.
            const uploadId = upload.upload_id;
            const detailsPromise = api.getUpload(uploadId).catch(e => {
                console.warn('Could not fetch upload details:', e);
                return null;
            });
            const platesPromise = api.getUploadPlates(uploadId).catch(e => {
                console.warn('Could not load plates:', e);
                return null;
            });

            const [uploadDetails, platesData] = await Promise.all([detailsPromise, platesPromise]);

            // Ensure filaments are loaded before assigning
            if (this.filaments.length === 0) {
                await this.loadFilaments();
            }

            // Apply upload details (detected colors, warnings, bounds)
            if (uploadDetails) {
                this.selectedUpload = { ...this.selectedUpload, ...uploadDetails };
                this.applyDetectedColors(uploadDetails.detected_colors || []);
            } else {
                this.applyDetectedColors([]);
            }

            // Apply plates data
            this.platesLoading = false;
            if (platesData && platesData.is_multi_plate && platesData.plates && platesData.plates.length > 0) {
                this.selectedUpload.is_multi_plate = true;
                this.selectedUpload.plate_count = platesData.plate_count;
                this.plates = platesData.plates;
                console.log(`Loaded ${this.plates.length} plates for upload ${uploadId}`);
            } else {
                this.selectedUpload.is_multi_plate = false;
                this.plates = [];
            }

            // Apply file-embedded print settings (support, brim) as defaults
            const fps = (platesData && platesData.file_print_settings)
                || (uploadDetails && uploadDetails.file_print_settings);
            this.fileSettings = fps && Object.keys(fps).length > 0 ? fps : null;
            if (this.fileSettings) {
                this.applyFileSettings(this.fileSettings);
                console.log('Applied file print settings:', this.fileSettings);
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
                    this.selectPlate(firstFitPlate.plate_id);
                    console.log('Auto-selected first valid plate:', firstFitPlate.plate_id);
                } else if (this.plates.length > 0) {
                    // Fallback to first plate if none fit
                    this.selectPlate(this.plates[0].plate_id);
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
            // Update detected colors from the selected plate's per-plate colors
            const plate = this.plates.find(p => p.plate_id === plateId);
            if (plate && plate.detected_colors && plate.detected_colors.length > 0) {
                this.detectedColors = plate.detected_colors;
            }
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

            this.sliceResult = null;  // Destroy old viewer before creating new one
            this.currentStep = 'slicing';
            this.activeTab = 'upload';
            this.sliceProgress = 0;
            this.accordionColours = false;
            this.accordionSettings = false;

            // Animate progress during the blocking slice POST.
            // Starts fast, slows down as it approaches 90%.
            const progressTimer = setInterval(() => {
                const remaining = 90 - this.sliceProgress;
                this.sliceProgress += Math.max(1, Math.floor(remaining * 0.08));
            }, 1000);

            try {
                let result;

                // Prepare slice settings: printer defaults as base, then
                // sliceSettings overlay (which includes file-detected + user edits)
                const sliceSettings = {
                    ...this.machineSettings,
                    ...this.sliceSettings,
                    nozzle_temp: null, // always from filament profile
                    filament_colors: [...(this.sliceSettings.filament_colors || [])],
                    extruder_assignments: [...(this.sliceSettings.extruder_assignments || [0, 1, 2, 3])],
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
                clearInterval(progressTimer);

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
                clearInterval(progressTimer);
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
            this.activeTab = 'upload';
            this.selectedUpload = null;
            this.fileSettings = null;
            this.sliceResult = null;
            this.sliceProgress = 0;
            this.uploadProgress = 0;
            this.uploadPhase = 'idle';
            this.resetJobOverrideSettings();
            this.stopPrintMonitorPolling();
            this.printMonitorActive = false;
            this.printSending = false;
            this.printState = null;

            if (this.sliceInterval) {
                clearInterval(this.sliceInterval);
                this.sliceInterval = null;
            }
        },

        /**
         * Return to configure step with all settings preserved (for reslicing).
         */
        goBackToConfigure() {
            this.sliceResult = null;
            this.sliceProgress = 0;
            this.currentStep = 'configure';
            this.activeTab = 'upload';

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
                // Null first to destroy old viewer before creating new one
                this.sliceResult = null;
                await new Promise(resolve => requestAnimationFrame(resolve));

                const status = await api.getJobStatus(job.job_id);
                this.selectedUpload = {
                    upload_id: job.upload_id,
                    filename: job.filename,
                    file_size: 0
                };
                this.sliceResult = status;
                this.currentStep = 'complete';
                this.activeTab = 'upload';
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

            this.sliceSettings.extruder_assignments = this.detectedColors
                .slice(0, this.maxExtruders)
                .map((_, idx) => idx);

            // Prefer machine preset filament loaded in each assigned extruder.
            this.applyPresetFilamentsForAssignments(true);

            console.log('Auto-assigned filaments:', this.selectedFilaments);
        },

        /**
         * Map detected colors to configured extruder preset slots by nearest color.
         */
        mapDetectedColorsToPresetSlots(colors) {
            const presetSlots = this.extruderPresets
                .map((preset, idx) => ({
                    slotIdx: idx,
                    filamentId: preset.filament_id,
                    colorHex: preset.color_hex || '#FFFFFF',
                }))
                .filter((slot) => !!slot.filamentId);

            if (presetSlots.length === 0) {
                return null;
            }

            const usedSlots = new Set();
            const assignments = [];
            const filamentIds = [];
            const mappedColors = [];

            for (const detectedColor of colors.slice(0, this.maxExtruders)) {
                let bestSlot = null;
                let bestDistance = Infinity;

                for (const slot of presetSlots) {
                    if (usedSlots.has(slot.slotIdx)) continue;
                    const distance = this.colorDistance(detectedColor, slot.colorHex);
                    if (distance < bestDistance) {
                        bestDistance = distance;
                        bestSlot = slot;
                    }
                }

                if (!bestSlot) {
                    return null;
                }

                usedSlots.add(bestSlot.slotIdx);
                assignments.push(bestSlot.slotIdx);
                filamentIds.push(bestSlot.filamentId);
                mappedColors.push(bestSlot.colorHex);
            }

            return {
                assignments,
                filamentIds,
                mappedColors,
            };
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

        colorName(hex) {
            const rgb = this.hexToRgb(hex);
            if (!rgb) return 'Unknown';

            const palette = [
                { name: 'Red', hex: '#FF0000' },
                { name: 'Green', hex: '#00FF00' },
                { name: 'Blue', hex: '#0000FF' },
                { name: 'Yellow', hex: '#FFFF00' },
                { name: 'Orange', hex: '#FFA500' },
                { name: 'Purple', hex: '#800080' },
                { name: 'Pink', hex: '#FFC0CB' },
                { name: 'White', hex: '#FFFFFF' },
                { name: 'Black', hex: '#000000' },
                { name: 'Gray', hex: '#808080' },
                { name: 'Brown', hex: '#8B4513' },
            ];

            let best = palette[0];
            let bestDistance = Infinity;
            for (const p of palette) {
                const d = this.colorDistance(hex, p.hex);
                if (d < bestDistance) {
                    bestDistance = d;
                    best = p;
                }
            }
            return best.name;
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
            if (this.filamentOverride) {
                if (!this.sliceSettings.extruder_assignments || this.sliceSettings.extruder_assignments.length === 0) {
                    this.sliceSettings.extruder_assignments = (this.detectedColors || [])
                        .slice(0, this.maxExtruders)
                        .map((_, idx) => idx);
                }
                this.applyPresetFilamentsForAssignments(true);
                setTimeout(() => this.applyPresetFilamentsForAssignments(true), 0);
            } else {
                // Re-auto-assign when turning off override
                this.applyDetectedColors(this.detectedColors || []);
            }
        },

        getFallbackFilamentId() {
            const e1Preset = this.extruderPresets?.[0]?.filament_id;
            if (e1Preset) return e1Preset;

            const defaultFilament = this.filaments.find(f => f.is_default);
            if (defaultFilament) return defaultFilament.id;

            const plaFilament = this.filaments.find(f => (f.material || '').toUpperCase() === 'PLA');
            if (plaFilament) return plaFilament.id;

            return this.filaments?.[0]?.id || null;
        },

        applyPresetFilamentsForAssignments(overwriteExisting = false) {
            const colorCount = Math.min((this.detectedColors || []).length, this.maxExtruders);
            if (colorCount <= 0) return;

            const assignments = this.sliceSettings.extruder_assignments || [];
            const next = [...(this.selectedFilaments || [])];
            const fallbackId = this.getFallbackFilamentId();

            for (let idx = 0; idx < colorCount; idx++) {
                const extruderIdx = assignments[idx] ?? idx;
                const presetFilamentId = this.extruderPresets?.[extruderIdx]?.filament_id || null;
                if (overwriteExisting || !next[idx]) {
                    next[idx] = presetFilamentId || fallbackId;
                }
            }

            this.selectedFilaments = next.slice(0, colorCount);
            this.syncFilamentColors();
        },

        /**
         * Apply detected colors with U1 extruder limits.
         */
        applyDetectedColors(colors) {
            this.detectedColors = colors;
            this.selectedFilaments = [];

            const limitedColors = (colors || []).slice(0, this.maxExtruders);
            // Use actual detected colors from the 3MF file, not preset colors.
            // Preset colors default to #FFFFFF which would mask the real file colors.
            this.sliceSettings.filament_colors = limitedColors.length > 0
                ? limitedColors.map((c) => c || '#FFFFFF')
                : this.extruderPresets.map((p) => p.color_hex || '#FFFFFF');
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

            // Single-color files always use single-filament mode.
            // Multicolor mapping would pad to 2+ extruders and can crash Orca.
            if (colors.length <= 1) {
                this.setDefaultFilament();
                return;
            }

            this.multicolorNotice = null;
            this.selectedFilament = null;

            const mappedFromPresets = this.mapDetectedColorsToPresetSlots(limitedColors);
            if (mappedFromPresets && mappedFromPresets.filamentIds.length === limitedColors.length) {
                this.selectedFilaments = mappedFromPresets.filamentIds;
                this.sliceSettings.extruder_assignments = mappedFromPresets.assignments;
                // Use the preset/extruder colors (what's physically loaded), not the
                // detected file colors (what the designer intended). syncFilamentColors
                // will further refine from filament profile color_hex if available.
                this.sliceSettings.filament_colors = mappedFromPresets.mappedColors;
                this.syncFilamentColors();
                return;
            }

            this.autoAssignFilaments();
        },

        /**
         * Select default filament in single-filament mode.
         */
        setDefaultFilament() {
            const presetFilamentId = this.extruderPresets[0]?.filament_id;
            if (presetFilamentId) {
                this.selectedFilament = presetFilamentId;
                return;
            }

            const defaultFilament = this.filaments.find(f => f.is_default);
            if (defaultFilament) {
                this.selectedFilament = defaultFilament.id;
                return;
            }

            const plaFilament = this.filaments.find(f => (f.material || '').toUpperCase() === 'PLA');
            if (plaFilament) {
                this.selectedFilament = plaFilament.id;
                return;
            }

            if (this.filaments.length > 0) {
                this.selectedFilament = this.filaments[0].id;
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
            // Default filament selection follows machine preset for chosen extruder.
            this.applyPresetFilamentsForAssignments(true);
        },

        /**
         * Sync filament_colors with actual selected filament profile colors.
         * Called when user changes filament dropdown or extruder assignments.
         * Preserves detected file colors when filament profile uses default #FFFFFF.
         */
        syncFilamentColors() {
            const existing = this.sliceSettings.filament_colors || [];
            const assignments = this.sliceSettings.extruder_assignments || [];
            this.sliceSettings.filament_colors = this.selectedFilaments.map((fid, idx) => {
                const fil = this.getFilamentById(fid);
                const profileColor = fil?.color_hex;
                // If the filament profile has a real (non-default) color, use it.
                if (profileColor && profileColor.toUpperCase() !== '#FFFFFF') return profileColor;
                // Use the assigned extruder slot (not position index) for preset lookup.
                const presetIdx = assignments[idx] ?? idx;
                const presetColor = this.extruderPresets?.[presetIdx]?.color_hex;
                if (presetColor && presetColor.toUpperCase() !== '#FFFFFF') return presetColor;
                return existing[idx] || '#FFFFFF';
            });
        },

        /**
         * Format filament length in mm to meters
         */
        formatFilament(mm) {
            if (!mm) return '0.0m';
            return `${(mm / 1000).toFixed(1)}m`;
        },

        /**
         * Format per-filament weight array to total grams string.
         * Returns null if no weight data available.
         */
        formatFilamentWeight(grams) {
            if (!grams || grams.length === 0) return null;
            const total = grams.reduce((a, b) => a + b, 0);
            if (total <= 0) return null;
            return total < 100 ? `${total.toFixed(1)}g` : `${Math.round(total)}g`;
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

        // ----- Printer Settings (Settings modal) -----

        async loadPrinterSettings() {
            try {
                const data = await api.getPrinterSettings();
                this.printerSettings = { moonraker_url: data.moonraker_url || '' };
            } catch (err) {
                console.warn('Failed to load printer settings:', err);
            }
        },

        async savePrinterSettings() {
            this.printerSettingsSaving = true;
            this.printerTestResult = null;
            try {
                await api.savePrinterSettings({ moonraker_url: this.printerSettings.moonraker_url || '' });
                this.printerTestResult = { ok: true, message: 'Saved' };
                // Refresh header status after URL change
                await this.checkPrinterStatus();
            } catch (err) {
                this.printerTestResult = { ok: false, message: err.message };
            } finally {
                this.printerSettingsSaving = false;
                setTimeout(() => { this.printerTestResult = null; }, 4000);
            }
        },

        async testPrinterConnection() {
            this.printerTestResult = { ok: null, message: 'Testing...' };
            try {
                // Save first so backend uses the new URL
                await api.savePrinterSettings({ moonraker_url: this.printerSettings.moonraker_url || '' });
                const status = await api.getPrinterStatus();
                if (status.connected) {
                    this.printerTestResult = { ok: true, message: 'Connected' };
                } else {
                    this.printerTestResult = { ok: false, message: 'Printer not reachable' };
                }
                // Refresh header status
                this.printerConnected = status.connected;
                this.printerStatus = status.connected ? 'Connected' : 'Offline';
            } catch (err) {
                this.printerTestResult = { ok: false, message: err.message };
            }
        },

        // ----- Print Control -----

        async sendToPrinter() {
            if (!this.sliceResult?.job_id) return;
            this.printSending = true;
            try {
                await api.sendToPrinter(this.sliceResult.job_id);
                this.printMonitorActive = true;
                this.printSending = false;
                this.startPrintMonitorPolling();
                // Open printer status page to show live progress
                this.openPrinterStatus();
            } catch (err) {
                this.printSending = false;
                this.showError(`Failed to send to printer: ${err.message}`);
            }
        },

        startPrintMonitorPolling() {
            this.stopPrintMonitorPolling();
            // Initial poll immediately
            this.pollPrintStatus();
            this.printMonitorInterval = setInterval(() => this.pollPrintStatus(), 3000);
        },

        stopPrintMonitorPolling() {
            if (this.printMonitorInterval) {
                clearInterval(this.printMonitorInterval);
                this.printMonitorInterval = null;
            }
        },

        async pollPrintStatus() {
            try {
                const status = await api.getPrintStatus();
                this.printState = status;
                // Stop polling when print finishes or errors
                if (status.state === 'complete' || status.state === 'error' || status.state === 'standby') {
                    this.stopPrintMonitorPolling();
                }
            } catch (err) {
                console.warn('Print status poll failed:', err);
            }
        },

        async pausePrint() {
            try {
                await api.pausePrint();
                await this.pollPrintStatus();
            } catch (err) {
                this.showError(`Pause failed: ${err.message}`);
            }
        },

        async resumePrint() {
            try {
                await api.resumePrint();
                this.startPrintMonitorPolling();
            } catch (err) {
                this.showError(`Resume failed: ${err.message}`);
            }
        },

        async cancelPrint() {
            if (!confirm('Cancel the current print?')) return;
            try {
                await api.cancelPrint();
                await this.pollPrintStatus();
            } catch (err) {
                this.showError(`Cancel failed: ${err.message}`);
            }
        },

        closePrintMonitor() {
            this.stopPrintMonitorPolling();
            this.printMonitorActive = false;
            this.showPrinterStatus = false;
            this.printState = null;
        },

        formatRemainingTime(ps) {
            if (!ps || !ps.progress || ps.progress <= 0 || !ps.duration) return '--';
            const elapsed = ps.duration;
            const estimated = elapsed / ps.progress;
            const remaining = Math.max(0, estimated - elapsed);
            return this.formatTime(remaining);
        },

        printProgressPercent() {
            if (!this.printState || !this.printState.progress) return 0;
            return Math.round(this.printState.progress * 100);
        },

        /**
         * Cleanup intervals on destroy
         */
        destroy() {
            if (this.sliceInterval) {
                clearInterval(this.sliceInterval);
            }
            this.stopPrintMonitorPolling();
        },
    };
}
