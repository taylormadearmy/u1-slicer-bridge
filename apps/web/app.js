/**
 * Main Application Logic
 * Alpine.js component for U1 Slicer Bridge UI
 */

function app() {
    let placementViewer = null;
    let placementViewerRefreshQueued = false;
    let viewerAbortController = null;

    return {
        // UI State
        dragOver: false,
        uploadProgress: 0,
        uploadPhase: 'idle', // 'idle' | 'uploading' | 'processing'
        error: null,

        // Current workflow step: 'upload' | 'selectplate' | 'configure' | 'slicing' | 'complete'
        currentStep: 'upload',
        activeTab: 'upload', // kept for compatibility — always 'upload' now
        showSettingsModal: false,
        showStorageDrawer: false,
        showPrinterStatus: false,

        // Confirm dialog state
        confirmModal: { open: false, title: '', message: '', html: '', confirmText: 'OK', destructive: false, suppressKey: null, suppressChecked: false },
        _confirmResolve: null,

        // App version (fetched from API)
        appVersion: '',

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
        objectLayout: null,       // M33: build-item layout metadata for editing
        objectLayoutLoading: false,
        objectLayoutError: null,
        objectGeometryLoading: false,
        objectGeometryLod: null,
        objectGeometry: null,     // M33/M36: optional per-build-item mesh geometry for placement viewer
        objectGeometryRefinedIndices: [], // build_item_index values fetched at higher LOD
        placementLoadMetrics: null, // dev telemetry for layout/geometry/viewer timings
        _placementLoadSeq: 0,
        _placementLoadTimer: null,
        _placementPanelObserver: null,
        _pendingPlacementLoad: null,
        placementPanelVisible: false,
        showPlacementModifiers: false, // Viewer-only toggle (hide modifier meshes by default)
        objectTransformEdits: {}, // build_item_index -> {translate_x_mm, translate_y_mm, rotate_z_deg}
        objectDrag: null,         // legacy 2D drag state (unused after 3D viewer swap; kept for compatibility)
        selectedLayoutObject: null, // build_item_index selected in Object Placement UI / 3D viewer
        placementInteractionMode: 'move', // default mode for drag interaction
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
        presetMessageOk: true,
        _presetMessageTimer: null,
        filamentSyncing: false,
        syncPreviewOpen: false,
        syncPreviewSlots: [],
        syncApplyColors: true,
        syncApplyProfiles: true,
        maxExtruders: 4,
        multicolorNotice: null,

        // Printer status
        printerConnected: false,
        printerBusy: false,
        printerStatus: 'Checking...',
        printerWebcams: [],
        printerHasFilamentConfig: false,
        printerFilamentSlots: [],
        webcamsExpanded: false,
        webcamImageFallback: {},
        webcamImageNonce: Date.now(),

        // Printer settings (for Settings modal)
        printerSettings: { moonraker_url: '', makerworld_cookies: '' },
        hasMakerWorldCookies: false,
        makerWorldEnabled: false,
        printerSettingsSaving: false,
        printerTestResult: null,

        // Backup & Restore
        settingsExporting: false,
        settingsImporting: false,
        settingsImportFile: null,
        settingsBackupMessage: null,
        settingsBackupOk: false,

        // MakerWorld import
        makerWorldUrl: '',
        makerWorldLoading: false,
        makerWorldDownloading: false,
        makerWorldModel: null,         // { design_id, title, author, thumbnail, profiles }
        makerWorldSelectedProfile: null, // instance_id

        // Multiple copies (M32)
        copyCount: 1,
        copySelectValue: '1',
        copyCountInput: null,
        copiesApplying: false,
        copyGridInfo: null,  // { cols, rows, fits_bed, max_copies }

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
            wipe_tower_x: null,
            wipe_tower_y: null,
            enable_flow_calibrate: true,
            bed_temp: null,
            bed_type: null,
            scale_percent: 100,
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
            wipe_tower_x: null,
            wipe_tower_y: null,
            enable_flow_calibrate: true,
            nozzle_temp: null,
            bed_temp: null,
            bed_type: null,
            scale_percent: 100,
            filament_colors: [],  // Override colors per extruder
            extruder_assignments: [0, 1, 2, 3],  // Which extruder each color uses (E1=0, E2=1, etc)
        },

        // Results
        sliceResult: null,
        sliceProgress: 0,         // Progress percentage (0-100)
        sliceMessage: '',         // Current slicer phase message

        // Polling interval
        sliceInterval: null,
        sliceJobId: null,         // Current slicing job ID for cancellation

        /**
         * Initialize the application
         */
        async init() {
            console.log('U1 Slicer Bridge - Initializing...');

            // Load initial data — printer check runs in parallel so it
            // doesn't block file lists when Moonraker is slow/unreachable.
            this.fetchVersion(); // non-blocking
            this.loadOrcaDefaults(); // non-blocking
            this.loadPrinterSettings(); // non-blocking, pre-load for settings modal
            this.checkPrinterStatus(false); // non-blocking — updates header indicator async
            await this.loadFilaments();
            await this.loadExtruderPresets();
            await this.loadRecentUploads();
            await this.loadJobs();

            // Set up periodic printer status check
            setInterval(() => this.checkPrinterStatus(this.webcamsExpanded), 30000); // Every 30 seconds

            // Handle PWA share target — auto-populate MakerWorld URL if shared
            this._handleShareTarget();
        },

        _handleShareTarget() {
            const params = new URLSearchParams(window.location.search);
            if (!params.get('share')) return;

            // Extract URL from share params (Android puts URL in 'text' or 'url')
            const sharedUrl = params.get('url') || params.get('text') || '';
            const makerWorldUrl = this._extractMakerWorldUrl(sharedUrl);

            // Clean the URL bar (remove query params)
            window.history.replaceState({}, '', '/');

            if (makerWorldUrl && this.makerWorldEnabled) {
                this.makerWorldUrl = makerWorldUrl;
                this.lookupMakerWorld();
            } else if (makerWorldUrl && !this.makerWorldEnabled) {
                // Shared a MakerWorld URL but feature is disabled
                this.errorMessage = 'MakerWorld integration is disabled. Enable it in Settings to import shared links.';
            }
        },

        _extractMakerWorldUrl(text) {
            if (!text) return null;
            // Find a MakerWorld URL anywhere in the shared text
            const match = text.match(/https?:\/\/(?:www\.)?makerworld\.com\/[^\s]*/i);
            return match ? match[0] : null;
        },

        /**
         * Check printer connection status
         */
        async checkPrinterStatus(includeWebcams = false) {
            try {
                const status = await api.getPrinterStatus(includeWebcams);
                this.printerConnected = status.connected;
                this.printerBusy = ['printing', 'paused'].includes(status.print_status?.state);
                if (includeWebcams) {
                    this.printerWebcams = Array.isArray(status.webcams)
                        ? status.webcams.filter(cam => cam && cam.enabled !== false)
                        : [];
                    this.webcamImageFallback = {};
                    this.webcamImageNonce = Date.now();
                }
                this.printerHasFilamentConfig = status.print_status?.has_filament_config || false;
                this.printerFilamentSlots = status.print_status?.filament_slots || [];
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
                if (includeWebcams) this.printerWebcams = [];
                console.error('Failed to check printer status:', err);
            }
        },

        async fetchVersion() {
            try {
                const res = await fetch('/api/healthz');
                const data = await res.json();
                if (data.version) this.appVersion = data.version;
            } catch (e) { /* non-critical */ }
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
                if (this._presetMessageTimer) clearTimeout(this._presetMessageTimer);
                this._presetMessageTimer = setTimeout(() => {
                    this.presetMessage = null;
                }, 5000);
            }
        },

        async prepareSyncPreview() {
            this.presetMessage = null;
            this.presetMessageOk = true;
            this.syncPreviewOpen = false;
            this.filamentSyncing = true;
            try {
                await this.checkPrinterStatus();

                if (!this.printerConnected) {
                    this.presetMessage = 'Printer offline — cannot sync filament colors.';
                    this.presetMessageOk = false;
                    return;
                }

                const slots = (this.printerFilamentSlots || []).filter((slot) => slot && slot.color && slot.loaded !== false);
                if (slots.length === 0) {
                    this.presetMessage = 'No filament detected on printer.';
                    this.presetMessageOk = false;
                    return;
                }

                const preview = [];
                for (let i = 0; i < this.maxExtruders; i++) {
                    const printerSlot = i < slots.length ? slots[i] : null;
                    const preset = this.extruderPresets[i];
                    const currentFilament = preset.filament_id
                        ? (this.filaments || []).find(f => f.id === Number(preset.filament_id))
                        : null;
                    const match = printerSlot && printerSlot.material_type
                        ? this.findFilamentMatchForSlot(printerSlot)
                        : null;

                    preview.push({
                        slotIndex: i,
                        label: `E${i + 1}`,
                        hasFilament: !!printerSlot,
                        currentColor: preset.color_hex || '#FFFFFF',
                        newColor: printerSlot ? printerSlot.color : null,
                        colorChanged: printerSlot ? (preset.color_hex || '#FFFFFF').toUpperCase() !== (printerSlot.color || '').toUpperCase() : false,
                        currentFilamentId: preset.filament_id || null,
                        currentFilamentName: currentFilament ? currentFilament.name : null,
                        matchedFilament: match ? { id: match.id, name: match.name } : null,
                        profileChanged: match ? match.id !== Number(preset.filament_id) : false,
                        printerMaterial: printerSlot ? printerSlot.material_type : null,
                        printerManufacturer: printerSlot ? printerSlot.manufacturer : null,
                    });
                }

                this.syncPreviewSlots = preview;
                this.syncApplyColors = true;
                this.syncApplyProfiles = true;
                this.syncPreviewOpen = true;
            } catch (err) {
                this.presetMessage = `Failed to sync filament colors: ${err.message}`;
                this.presetMessageOk = false;
                this.showError(this.presetMessage);
            } finally {
                this.filamentSyncing = false;
            }
        },

        cancelSyncPreview() {
            this.syncPreviewOpen = false;
            this.syncPreviewSlots = [];
        },

        async applySyncPreview() {
            try {
                for (const slot of this.syncPreviewSlots) {
                    if (!slot.hasFilament) continue;
                    if (this.syncApplyColors && slot.newColor) {
                        this.extruderPresets[slot.slotIndex].color_hex = slot.newColor;
                    }
                    if (this.syncApplyProfiles && slot.matchedFilament) {
                        this.extruderPresets[slot.slotIndex].filament_id = slot.matchedFilament.id;
                    }
                }

                await this.saveExtruderPresets();
                this.syncPreviewOpen = false;
                this.syncPreviewSlots = [];

                if (this._presetMessageTimer) clearTimeout(this._presetMessageTimer);
                const parts = [];
                if (this.syncApplyColors) parts.push('colors');
                if (this.syncApplyProfiles) parts.push('filament profiles');
                this.presetMessage = `Synced ${parts.join(' and ')} from printer.`;
                this.presetMessageOk = true;
                this._presetMessageTimer = setTimeout(() => { this.presetMessage = null; }, 5000);
            } catch (err) {
                this.presetMessage = `Failed to sync: ${err.message}`;
                this.presetMessageOk = false;
                this.showError(this.presetMessage);
            }
        },

        normalizeMaterialType(value) {
            if (!value || typeof value !== 'string') return '';
            const compact = value.trim().toUpperCase().replace(/\s+/g, '');
            const aliases = {
                'PLA+': 'PLA', 'PLAPLUS': 'PLA', 'PET-G': 'PETG',
                'ABS+': 'ABS', 'NYLON': 'PA', 'PA6': 'PA', 'PA12': 'PA',
            };
            return aliases[compact] || compact;
        },

        findFilamentMatchForSlot(slot) {
            if (!slot.material_type || !this.filaments || this.filaments.length === 0) return null;
            const afcMat = this.normalizeMaterialType(slot.material_type);
            let bestMatch = null;
            let bestScore = 0;
            for (const fil of this.filaments) {
                let score = 0;
                const filMat = this.normalizeMaterialType(fil.material);
                if (filMat === afcMat) score += 100;
                else continue;
                if (slot.manufacturer && fil.name) {
                    const mfr = slot.manufacturer.toLowerCase();
                    const name = fil.name.toLowerCase();
                    if (name.includes(mfr) || mfr.includes(name.split(' ')[0])) score += 40;
                    else {
                        const tokens = mfr.split(/[\s_\-]+/);
                        if (tokens.some((t) => t.length >= 3 && name.includes(t))) score += 20;
                    }
                }
                if (!bestMatch) score += 5;
                if (score > bestScore) {
                    bestScore = score;
                    bestMatch = fil;
                }
            }
            return bestScore > 0 ? bestMatch : null;
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
            s.scale_percent = this.sliceSettings.scale_percent ?? 100;
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

        async openPrinterStatus() {
            this.showSettingsModal = false;
            this.showStorageDrawer = false;
            this.showPrinterStatus = true;
            if (this.webcamsExpanded) {
                this.webcamImageFallback = {};
                this.webcamImageNonce = Date.now();
            }
            await this.checkPrinterStatus(this.webcamsExpanded);
            // Start polling for live updates
            this.pollPrintStatus();
            if (!this.printMonitorInterval) {
                this.startPrintMonitorPolling();
            }
        },

        async toggleWebcamsExpanded() {
            this.webcamsExpanded = !this.webcamsExpanded;
            if (this.webcamsExpanded) {
                this.webcamImageFallback = {};
                this.webcamImageNonce = Date.now();
                await this.checkPrinterStatus(true);
            } else {
                this.webcamImageFallback = {};
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

        async selectUploadFromDrawer(upload) {
            if (this.currentStep === 'slicing') {
                if (!await this.showConfirm({
                    title: 'Cancel Slicing?',
                    message: 'Slicing is still in progress. Do you want to cancel it?',
                    confirmText: 'Cancel Slice',
                    destructive: true,
                })) return;
                this.cancelActiveSlice();
            }
            this.closeStorage();
            this.selectUpload(upload);
        },

        async viewJobFromDrawer(job) {
            if (this.currentStep === 'slicing') {
                if (!await this.showConfirm({
                    title: 'Cancel Slicing?',
                    message: 'Slicing is still in progress. Do you want to cancel it?',
                    confirmText: 'Cancel Slice',
                    destructive: true,
                })) return;
                this.cancelActiveSlice();
            }
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
            if (!await this.showConfirm({ title: 'Delete Filament', message: `Delete filament '${filamentName}'?`, confirmText: 'Delete', destructive: true })) return;

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
            if (!await this.showConfirm({ title: 'Delete File', message: 'Delete this file and all its sliced versions?', confirmText: 'Delete', destructive: true })) return;
            
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
            if (!await this.showConfirm({ title: 'Delete Slice', message: 'Delete this sliced file?', confirmText: 'Delete', destructive: true })) return;
            
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
                // Keep uploadPhase as 'processing' until the step transition
                // completes — resetting to 'idle' here would briefly flash the
                // upload dropzone while plates are still loading.
                this.uploadPhase = 'processing';

                // Add to uploads list
                this.uploads.unshift(result);

                // Select this upload for configuration
                this.selectedUpload = result;
                this.selectedPlate = null;
                this.plates = [];
                this.platesLoading = false;
                this.resetObjectLayoutState();
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

                // Load plate info before setting step (multi-plate → selectplate, single → configure)
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

                    this.uploadPhase = 'idle';
                    if (this.selectedUpload?.is_multi_plate) {
                        this.resetObjectLayoutState();
                        this.currentStep = 'selectplate';
                        this.autoSelectFirstPlate();
                    } else {
                        this.currentStep = 'configure';
                        this.queueObjectLayoutLoad(result.upload_id, null, 0);
                    }

                    // Apply file-embedded print settings (support, brim) as defaults
                    const fps = platesData.file_print_settings || result.file_print_settings;
                    this.fileSettings = fps && Object.keys(fps).length > 0 ? fps : null;
                    if (this.fileSettings) {
                        this.applyFileSettings(this.fileSettings);
                        console.log('Applied file print settings:', this.fileSettings);
                    }
                } catch (err) {
                    this.uploadPhase = 'idle';
                    this.platesLoading = false;
                    this.selectedUpload.is_multi_plate = false;
                    this.plates = [];
                    this.objectLayoutError = null;
                    this.currentStep = 'configure';
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

        // ----- MakerWorld Import -----

        /**
         * Look up a MakerWorld model by URL
         */
        async lookupMakerWorld() {
            const url = (this.makerWorldUrl || '').trim();
            if (!url) return;

            this.makerWorldLoading = true;
            this.makerWorldModel = null;
            this.makerWorldSelectedProfile = null;
            this.error = null;

            try {
                const result = await api.lookupMakerWorld(url);
                this.makerWorldModel = result;
                // Auto-select first profile
                if (result.profiles && result.profiles.length > 0) {
                    this.makerWorldSelectedProfile = result.profiles[0].instance_id;
                }
            } catch (err) {
                this.showError(`MakerWorld lookup failed: ${err.message}`);
            } finally {
                this.makerWorldLoading = false;
            }
        },

        /**
         * Download a 3MF from MakerWorld and import it
         */
        async downloadMakerWorld() {
            if (!this.makerWorldModel || !this.makerWorldSelectedProfile) return;

            this.makerWorldDownloading = true;
            this.error = null;

            try {
                const result = await api.downloadMakerWorld(
                    this.makerWorldUrl.trim(),
                    this.makerWorldSelectedProfile
                );

                // Feed into the same post-upload flow as manual uploads
                this.uploads.unshift(result);
                this.selectedUpload = result;
                this.selectedPlate = null;
                this.plates = [];
                this.platesLoading = false;
                this.resetObjectLayoutState();
                this.activeTab = 'upload';

                if (this.filaments.length === 0) {
                    await this.loadFilaments();
                }

                this.filamentOverride = false;
                this.applyDetectedColors(result.detected_colors || []);
                this.resetJobOverrideSettings();

                // Load plate info before setting step (multi-plate → selectplate, single → configure)
                this.platesLoading = true;
                this.plates = [];
                try {
                    const platesData = await api.getUploadPlates(result.upload_id);
                    this.platesLoading = false;

                    if (platesData.is_multi_plate && platesData.plates && platesData.plates.length > 0) {
                        this.selectedUpload.is_multi_plate = true;
                        this.selectedUpload.plate_count = platesData.plate_count;
                        this.plates = platesData.plates;
                    } else {
                        this.selectedUpload.is_multi_plate = false;
                        this.plates = [];
                    }

                    if (this.selectedUpload?.is_multi_plate) {
                        this.resetObjectLayoutState();
                        this.currentStep = 'selectplate';
                        this.autoSelectFirstPlate();
                    } else {
                        this.currentStep = 'configure';
                        this.queueObjectLayoutLoad(result.upload_id, null, 0);
                    }

                    const fps = platesData.file_print_settings || result.file_print_settings;
                    this.fileSettings = fps && Object.keys(fps).length > 0 ? fps : null;
                    if (this.fileSettings) this.applyFileSettings(this.fileSettings);
                } catch (err) {
                    this.platesLoading = false;
                    this.selectedUpload.is_multi_plate = false;
                    this.plates = [];
                    this.currentStep = 'configure';
                    const fps = result.file_print_settings;
                    this.fileSettings = fps && Object.keys(fps).length > 0 ? fps : null;
                    if (this.fileSettings) this.applyFileSettings(this.fileSettings);
                }

                // Reset MakerWorld state
                this.makerWorldModel = null;
                this.makerWorldUrl = '';
                this.makerWorldSelectedProfile = null;
            } catch (err) {
                this.showError(`MakerWorld download failed: ${err.message}`);
            } finally {
                this.makerWorldDownloading = false;
            }
        },

        clearMakerWorld() {
            this.makerWorldUrl = '';
            this.makerWorldModel = null;
            this.makerWorldSelectedProfile = null;
            this.makerWorldLoading = false;
            this.makerWorldDownloading = false;
        },

        /**
         * Select an existing upload (from recent uploads list)
         */
        async selectUpload(upload) {
            console.log('Selected upload:', upload.upload_id);

            // Re-selecting the same upload preserves all configure state
            if (this.selectedUpload && this.selectedUpload.upload_id === upload.upload_id) {
                if (this.selectedUpload.is_multi_plate && !this.selectedPlate) {
                    this.currentStep = 'selectplate';
                } else {
                    this.currentStep = 'configure';
                }
                this.activeTab = 'upload';
                return;
            }

            this.selectedUpload = upload;
            this.selectedPlate = null;
            this.plates = [];
            this.platesLoading = true;
            this.resetObjectLayoutState();
            this.filamentOverride = false;
            this.resetJobOverrideSettings();
            this.activeTab = 'upload';
            // Immediately leave the upload/home page so it doesn't flash
            // while async plate/details loading runs. The final step
            // ('configure' or 'selectplate') is set once data arrives.
            this.currentStep = 'configure';

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

            if (this.selectedUpload?.is_multi_plate) {
                this.resetObjectLayoutState();
                this.currentStep = 'selectplate';
                this.autoSelectFirstPlate();
            } else {
                this.currentStep = 'configure';
                this.queueObjectLayoutLoad(uploadId, null, 0);
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

        resetObjectLayoutState() {
            this.objectLayout = null;
            this.objectLayoutLoading = false;
            this.objectLayoutError = null;
            this.objectGeometryLoading = false;
            this.objectGeometryLod = null;
            this.objectGeometry = null;
            this.objectGeometryRefinedIndices = [];
            this.placementLoadMetrics = null;
            this._placementLoadSeq = Number(this._placementLoadSeq || 0) + 1; // invalidate inflight requests
            if (this._placementLoadTimer) {
                clearTimeout(this._placementLoadTimer);
                this._placementLoadTimer = null;
            }
            this._pendingPlacementLoad = null;
            this.objectTransformEdits = {};
            this.selectedLayoutObject = null;
            if (placementViewer) {
                placementViewer.setLayout(null, null);
            }
        },

        initPlacementPanelObserver() {
            const panel = this.$refs?.placementPanel;
            if (!panel) return;
            if (this._placementPanelObserver) return;
            const onVisible = () => {
                this.placementPanelVisible = true;
                const pending = this._pendingPlacementLoad;
                if (pending?.uploadId) {
                    this._pendingPlacementLoad = null;
                    this.queueObjectLayoutLoad(pending.uploadId, pending.plateId ?? null, 0);
                }
            };
            if (!('IntersectionObserver' in window)) {
                onVisible();
                return;
            }
            this._placementPanelObserver = new IntersectionObserver((entries) => {
                const visible = entries.some((e) => e.isIntersecting || e.intersectionRatio > 0);
                this.placementPanelVisible = visible;
                if (visible) onVisible();
            }, { root: null, threshold: 0.05 });
            this._placementPanelObserver.observe(panel);
        },

        _isPlacementPanelVisibleForLoading() {
            // Multi-plate files benefit most from lazy-loading; for single-plate keep current behavior
            // if the observer is not ready yet to avoid blank panels.
            if (!this.selectedUpload?.is_multi_plate) return true;
            return !!this.placementPanelVisible;
        },

        async loadObjectLayout(uploadId = null, plateId = null) {
            uploadId = uploadId || this.selectedUpload?.upload_id;
            if (!uploadId) return;

            if (plateId == null && this.selectedUpload?.is_multi_plate) {
                plateId = this.selectedPlate || null;
            }

            if (this.selectedUpload?.is_multi_plate && !plateId) {
                this.objectLayoutLoading = false;
                this.objectLayoutError = null;
                this.objectGeometryLoading = false;
                this.objectGeometryLod = null;
                this.objectLayout = null;
                this.objectGeometry = null;
                this.objectTransformEdits = {};
                this.selectedLayoutObject = null;
                this.placementLoadMetrics = null;
                this.schedulePlacementViewerRefresh();
                return;
            }

            if (!this._isPlacementPanelVisibleForLoading()) {
                this._pendingPlacementLoad = { uploadId, plateId: plateId ?? null };
                return;
            }

            this.objectLayoutLoading = true;
            this.objectGeometryLoading = false;
            this.objectGeometryLod = null;
            this.objectLayoutError = null;
            this.objectGeometry = null;
            this.objectGeometryRefinedIndices = [];
            const requestedUploadId = uploadId;
            const requestedPlateId = plateId ?? null;
            const requestSeq = Number(this._placementLoadSeq || 0) + 1;
            this._placementLoadSeq = requestSeq;
            const tStart = this._placementNow();
            const metrics = {
                upload_id: requestedUploadId,
                plate_id: requestedPlateId,
                include_modifiers: !!this.showPlacementModifiers,
            };

            const isStale = () => {
                if (Number(this._placementLoadSeq || 0) !== requestSeq) return true;
                if (this.selectedUpload?.upload_id !== requestedUploadId) return true;
                if ((this.selectedUpload?.is_multi_plate || false) && (this.selectedPlate || null) !== requestedPlateId) return true;
                return false;
            };

            // Abort any in-flight viewer requests and create a new controller.
            // This frees browser connections immediately so slice polling isn't blocked.
            viewerAbortController?.abort();
            viewerAbortController = new AbortController();
            const viewerSignal = { signal: viewerAbortController.signal };

            try {
                const tLayout0 = this._placementNow();
                const layout = await api.getUploadLayout(requestedUploadId, requestedPlateId, viewerSignal);
                metrics.layout_fetch_ms = Math.round((this._placementNow() - tLayout0) * 10) / 10;
                metrics.layout_backend_ms = Number(layout?.timing_ms?.total || 0) || null;

                if (isStale()) { this.objectLayoutLoading = false; return; }

                this.objectLayout = layout;
                if (this.isObjectPlacementTransformApproximate()) {
                    this.objectTransformEdits = {};
                    this.placementInteractionMode = 'move';
                }
                this.objectLayoutLoading = false;
                this.objectGeometry = null; // render proxies immediately
                this.objectGeometryLoading = true;
                this.objectGeometryLod = 'proxies';
                const firstObj = Array.isArray(layout?.objects) && layout.objects.length > 0 ? Number(layout.objects[0].build_item_index || 0) : null;
                const currentSel = Number(this.selectedLayoutObject || 0);
                const hasCurrent = Array.isArray(layout?.objects) && layout.objects.some(o => Number(o.build_item_index || 0) === currentSel);
                this.selectedLayoutObject = hasCurrent ? currentSel : (firstObj || null);
                metrics.first_preview_ms = Math.round((this._placementNow() - tStart) * 10) / 10;
                this.placementLoadMetrics = { ...metrics };
                window.__u1PlacementViewerMetrics = this.placementLoadMetrics;
                this.schedulePlacementViewerRefresh();

                this.objectGeometryLod = 'placement_low';
                const tGeomLow0 = this._placementNow();
                let lowGeometry = null;
                try {
                    lowGeometry = await api.getUploadGeometry(
                        requestedUploadId,
                        requestedPlateId,
                        !!this.showPlacementModifiers,
                        'placement_low',
                        null,
                        viewerSignal,
                    );
                } catch (geomErr) {
                    if (geomErr.name === 'AbortError') { this.objectLayoutLoading = false; this.objectGeometryLoading = false; return; }
                    console.warn('Failed to load placement geometry low LOD (falling back to proxies):', geomErr);
                }
                if (isStale()) { this.objectLayoutLoading = false; this.objectGeometryLoading = false; return; }

                metrics.geometry_low_fetch_ms = Math.round((this._placementNow() - tGeomLow0) * 10) / 10;
                metrics.geometry_low_backend_ms = Number(lowGeometry?.timing_ms?.total || 0) || null;
                if (lowGeometry) {
                    this.objectGeometry = lowGeometry;
                    this.objectGeometryLod = String(lowGeometry?.lod || 'placement_low');
                } else {
                    this.objectGeometryLod = 'proxies';
                }
                this.objectGeometryLoading = false;
                this.placementLoadMetrics = { ...(this.placementLoadMetrics || {}), ...metrics };
                window.__u1PlacementViewerMetrics = this.placementLoadMetrics;
                this.schedulePlacementViewerRefresh();

                if (!lowGeometry) {
                    metrics.total_visible_ms = Math.round((this._placementNow() - tStart) * 10) / 10;
                    this.placementLoadMetrics = { ...(this.placementLoadMetrics || {}), ...metrics };
                    window.__u1PlacementViewerMetrics = this.placementLoadMetrics;
                    console.info('[placement-viewer] load metrics', this.placementLoadMetrics);
                    return;
                }
                await this.refineSelectedPlacementGeometry(requestedUploadId, requestedPlateId, metrics, isStale, tStart);
            } catch (err) {
                if (err.name === 'AbortError' || isStale()) { this.objectLayoutLoading = false; this.objectGeometryLoading = false; return; }
                this.objectLayoutLoading = false;
                this.objectGeometryLoading = false;
                this.objectGeometryLod = null;
                this.objectLayoutError = err.message || 'Failed to load object layout';
                console.warn('Failed to load object layout:', err);
                this.schedulePlacementViewerRefresh();
            }
        },

        selectLayoutObject(buildItemIndex) {
            const idx = Number(buildItemIndex || 0);
            this.selectedLayoutObject = Number.isInteger(idx) && idx > 0 ? idx : null;
            if (placementViewer) {
                placementViewer.setSelected(this.selectedLayoutObject);
            }
            if (this.selectedLayoutObject && this.objectLayout && !this.objectLayoutLoading) {
                this.refineSelectedPlacementGeometry(this.selectedUpload?.upload_id, this.selectedPlate || null);
            }
        },

        togglePlacementModifiers() {
            if (!this.selectedUpload?.upload_id) return;
            this.loadObjectLayout(this.selectedUpload.upload_id, this.selectedPlate || null);
        },

        _mergePlacementGeometryObjects(baseGeometry, refinedGeometry) {
            if (!baseGeometry || !Array.isArray(baseGeometry.objects) || !refinedGeometry || !Array.isArray(refinedGeometry.objects)) {
                return refinedGeometry || baseGeometry || null;
            }
            const mergedByIndex = new Map();
            for (const obj of baseGeometry.objects) {
                const idx = Number(obj?.build_item_index || 0);
                if (idx > 0) mergedByIndex.set(idx, obj);
            }
            for (const obj of refinedGeometry.objects) {
                const idx = Number(obj?.build_item_index || 0);
                if (idx > 0) mergedByIndex.set(idx, obj);
            }
            return {
                ...baseGeometry,
                ...refinedGeometry,
                objects: Array.from(mergedByIndex.values()).sort((a, b) => Number(a?.build_item_index || 0) - Number(b?.build_item_index || 0)),
            };
        },

        async refineSelectedPlacementGeometry(uploadId = null, plateId = null, metrics = null, isStaleFn = null, tStart = null) {
            uploadId = uploadId || this.selectedUpload?.upload_id;
            if (!uploadId || !this.objectGeometry || this.objectLayoutLoading || !this.objectLayout) return;
            const selectedIdx = Number(this.selectedLayoutObject || 0);
            if (!Number.isInteger(selectedIdx) || selectedIdx < 1) return;

            const existing = Array.isArray(this.objectGeometry?.objects)
                ? this.objectGeometry.objects.find((o) => Number(o?.build_item_index || 0) === selectedIdx)
                : null;
            if (!existing) return;
            const alreadyRefined = (this.objectGeometryRefinedIndices || []).includes(selectedIdx);
            const needsRefine = !!existing.mesh_decimated || Number(existing.triangle_count || 0) < Number(existing.original_triangle_count || 0);
            if (!needsRefine || alreadyRefined) {
                if (metrics && tStart != null) {
                    metrics.total_visible_ms = Math.round((this._placementNow() - tStart) * 10) / 10;
                    this.placementLoadMetrics = { ...(this.placementLoadMetrics || {}), ...metrics };
                    window.__u1PlacementViewerMetrics = this.placementLoadMetrics;
                    console.info('[placement-viewer] load metrics', this.placementLoadMetrics);
                }
                return;
            }

            const stale = typeof isStaleFn === 'function' ? isStaleFn : (() => false);
            this.objectGeometryLoading = true;
            this.objectGeometryLod = 'placement_high';
            const tGeomHigh0 = this._placementNow();
            let highGeometry = null;
            const refineSignal = viewerAbortController ? { signal: viewerAbortController.signal } : {};
            try {
                highGeometry = await api.getUploadGeometry(
                    uploadId,
                    plateId,
                    !!this.showPlacementModifiers,
                    'placement_high',
                    selectedIdx,
                    refineSignal,
                );
            } catch (geomHighErr) {
                if (geomHighErr.name === 'AbortError') { this.objectGeometryLoading = false; return; }
                console.warn('Failed to load placement geometry high LOD for selected object:', geomHighErr);
            }
            if (stale()) { this.objectGeometryLoading = false; return; }

            if (metrics) {
                metrics.geometry_high_fetch_ms = Math.round((this._placementNow() - tGeomHigh0) * 10) / 10;
                metrics.geometry_high_backend_ms = Number(highGeometry?.timing_ms?.total || 0) || null;
            }
            if (highGeometry) {
                this.objectGeometry = this._mergePlacementGeometryObjects(this.objectGeometry, highGeometry);
                this.objectGeometryLod = String(highGeometry?.lod || 'placement_high');
                this.objectGeometryRefinedIndices = Array.from(new Set([...(this.objectGeometryRefinedIndices || []), selectedIdx]));
                this.schedulePlacementViewerRefresh();
            }
            this.objectGeometryLoading = false;
            if (metrics && tStart != null) {
                metrics.total_visible_ms = Math.round((this._placementNow() - tStart) * 10) / 10;
                this.placementLoadMetrics = { ...(this.placementLoadMetrics || {}), ...metrics };
                window.__u1PlacementViewerMetrics = this.placementLoadMetrics;
                console.info('[placement-viewer] load metrics', this.placementLoadMetrics);
            }
        },

        initPlacementViewer() {
            if (!this.$refs || !this.$refs.placementViewerCanvas) return;
            if (!window.MeshPlacementViewer) return;
            if (placementViewer && placementViewer.canvas !== this.$refs.placementViewerCanvas) {
                placementViewer.destroy();
                placementViewer = null;
            }
            if (!placementViewer) {
                placementViewer = new window.MeshPlacementViewer(this.$refs.placementViewerCanvas, {
                    onSelect: (buildItemIndex) => {
                        this.selectLayoutObject(buildItemIndex);
                    },
                    getInteractionMode: () => this.placementInteractionMode,
                    canEditObjects: () => !this.isObjectPlacementTransformApproximate(),
                    onPoseEdit: (buildItemIndex, nextPose) => {
                        this.applyPlacementViewerPose(buildItemIndex, nextPose);
                    },
                    onPrimeTowerMove: (nextPose) => {
                        this.applyPlacementPrimeTowerPose(nextPose);
                    },
                    onSceneStats: (stats) => {
                        this.recordPlacementViewerSceneStats(stats);
                    },
                });
                placementViewer.init();
                window.__u1PlacementViewer = placementViewer;
            }
            this.schedulePlacementViewerRefresh();
        },

        schedulePlacementViewerRefresh() {
            if (placementViewerRefreshQueued) return;
            placementViewerRefreshQueued = true;
            requestAnimationFrame(() => {
                placementViewerRefreshQueued = false;
                this.refreshPlacementViewer();
            });
        },

        _placementNow() {
            return (window.performance && typeof window.performance.now === 'function')
                ? window.performance.now()
                : Date.now();
        },

        recordPlacementViewerSceneStats(stats) {
            const next = {
                ...(this.placementLoadMetrics || {}),
                viewer_scene: stats || null,
            };
            this.placementLoadMetrics = next;
            window.__u1PlacementViewerMetrics = next;
            console.info('[placement-viewer] scene stats', next);
        },

        refreshPlacementViewer() {
            if (!placementViewer) return;
            if (!this.objectLayout || this.objectLayoutError || this.objectLayoutLoading) {
                placementViewer.setLayout(null, null);
                placementViewer.setPrimeTower?.(null);
                return;
            }
            const t0 = this._placementNow();
            placementViewer.setLayout(this.objectLayout, (obj) => this.getObjectEffectivePoseForViewer(obj), this.objectGeometry);
            placementViewer.setPrimeTower?.(this.getPrimeTowerPreviewConfig());
            placementViewer.setInteractionMode?.(this.placementInteractionMode);
            placementViewer.setSelected(this.selectedLayoutObject);
            placementViewer.refreshPoses();
            const refreshMs = Math.round((this._placementNow() - t0) * 10) / 10;
            const next = {
                ...(this.placementLoadMetrics || {}),
                viewer_refresh_ms: refreshMs,
                viewer_geometry_lod: this.objectGeometryLod || null,
            };
            this.placementLoadMetrics = next;
            window.__u1PlacementViewerMetrics = next;
        },

        isObjectPlacementTransformApproximate() {
            const frame = this.objectLayout?.placement_frame || null;
            if (!frame || typeof frame !== 'object') return false;
            const caps = frame.capabilities;
            if (caps && typeof caps === 'object' && Object.prototype.hasOwnProperty.call(caps, 'object_transform_edit')) {
                return !Boolean(caps.object_transform_edit);
            }
            if (!this.selectedUpload?.is_multi_plate) return false;
            return String(frame.confidence || '').toLowerCase() === 'approximate';
        },

        getObjectPlacementTransformWarning() {
            if (!this.isObjectPlacementTransformApproximate()) return '';
            const notes = Array.isArray(this.objectLayout?.placement_frame?.notes)
                ? this.objectLayout.placement_frame.notes.filter(Boolean)
                : [];
            if (notes.length > 0) return String(notes[0]);
            return 'Object move/rotate is temporarily disabled for this plate because placement mapping is approximate and can produce misleading previews / failed slices. Prime tower move still works.';
        },

        getObjectTransformsPayload() {
            if (this.isObjectPlacementTransformApproximate()) {
                return [];
            }
            const edits = this.objectTransformEdits || {};
            const payload = [];

            for (const [buildItemIndexRaw, edit] of Object.entries(edits)) {
                if (!edit) continue;
                const buildItemIndex = Number(buildItemIndexRaw);
                if (!Number.isInteger(buildItemIndex) || buildItemIndex < 1) continue;

                const tx = Number(edit.translate_x_mm || 0);
                const ty = Number(edit.translate_y_mm || 0);
                const rz = Number(edit.rotate_z_deg || 0);
                if (Math.abs(tx) < 1e-9 && Math.abs(ty) < 1e-9 && Math.abs(rz) < 1e-9) continue;

                payload.push({
                    build_item_index: buildItemIndex,
                    translate_x_mm: tx,
                    translate_y_mm: ty,
                    rotate_z_deg: rz,
                });
            }

            payload.sort((a, b) => a.build_item_index - b.build_item_index);
            return payload;
        },

        getPrimeTowerPreviewConfig() {
            if (!this.sliceSettings?.enable_prime_tower || !this.objectLayout?.build_volume) return null;
            const vol = this.objectLayout.build_volume || {};
            const bedW = Math.max(1, Number(vol.x || 270));
            const bedH = Math.max(1, Number(vol.y || 270));
            const width = Math.max(10, Number(this.sliceSettings.prime_tower_width || 35));
            const brim = Math.max(0, Number(this.sliceSettings.prime_tower_brim_width ?? 3));
            const footprintW = width + (2 * brim);
            const footprintH = width + (2 * brim); // approximate preview footprint only
            const rawX = this.sliceSettings.wipe_tower_x;
            const rawY = this.sliceSettings.wipe_tower_y;
            const hasExplicit = Number.isFinite(Number(rawX)) && Number.isFinite(Number(rawY));
            const defaultAnchor = {
                x: Math.max(0, bedW - width - brim - 6),
                y: Math.max(0, bedH - width - brim - 6),
            };
            const anchor = this._clampPrimeTowerAnchorPose(
                hasExplicit ? Number(rawX) : defaultAnchor.x,
                hasExplicit ? Number(rawY) : defaultAnchor.y,
                width,
                bedW,
                bedH,
            );
            return {
                x: anchor.x,
                y: anchor.y,
                width,
                brim_width: brim,
                footprint_w: footprintW,
                footprint_h: footprintH,
                anchor_is_slicer_coords: true,
                explicit_position: hasExplicit,
            };
        },

        applyPlacementPrimeTowerPose(nextPose) {
            if (!nextPose || !this.sliceSettings?.enable_prime_tower || !this.objectLayout?.build_volume) return;
            const vol = this.objectLayout.build_volume || {};
            const bedW = Math.max(1, Number(vol.x || 270));
            const bedH = Math.max(1, Number(vol.y || 270));
            const width = Math.max(10, Number(this.sliceSettings.prime_tower_width || 35));
            const anchor = this._clampPrimeTowerAnchorPose(
                Number(nextPose.x || 0),
                Number(nextPose.y || 0),
                width,
                bedW,
                bedH,
            );
            this.sliceSettings.wipe_tower_x = Number(anchor.x.toFixed(3));
            this.sliceSettings.wipe_tower_y = Number(anchor.y.toFixed(3));
            this.schedulePlacementViewerRefresh();
        },

        _clampPrimeTowerAnchorPose(x, y, width, bedW, bedH) {
            const anchorMaxX = Math.max(0, Number(bedW || 270) - Number(width || 35));
            const anchorMaxY = Math.max(0, Number(bedH || 270) - Number(width || 35));
            return {
                x: Math.min(anchorMaxX, Math.max(0, Number(x || 0))),
                y: Math.min(anchorMaxY, Math.max(0, Number(y || 0))),
            };
        },

        resetPrimeTowerPosition() {
            this.sliceSettings.wipe_tower_x = null;
            this.sliceSettings.wipe_tower_y = null;
            this.schedulePlacementViewerRefresh();
        },

        onPrimeTowerPreviewSettingChanged() {
            this.schedulePlacementViewerRefresh();
        },

        getObjectTransformValue(buildItemIndex, key) {
            return Number(this.objectTransformEdits?.[buildItemIndex]?.[key] || 0);
        },

        setObjectTransformField(buildItemIndex, key, value) {
            if (this.isObjectPlacementTransformApproximate()) return;
            const idx = Number(buildItemIndex);
            if (!Number.isInteger(idx) || idx < 1) return;
            const numeric = Number(value);
            const next = Number.isFinite(numeric) ? numeric : 0;
            const current = { ...(this.objectTransformEdits[idx] || {}) };
            current[key] = next;

            const tx = Number(current.translate_x_mm || 0);
            const ty = Number(current.translate_y_mm || 0);
            const rz = Number(current.rotate_z_deg || 0);
            if (Math.abs(tx) < 1e-9 && Math.abs(ty) < 1e-9 && Math.abs(rz) < 1e-9) {
                const edits = { ...(this.objectTransformEdits || {}) };
                delete edits[idx];
                this.objectTransformEdits = edits;
                this.schedulePlacementViewerRefresh();
                return;
            }

            this.objectTransformEdits = {
                ...(this.objectTransformEdits || {}),
                [idx]: {
                    translate_x_mm: tx,
                    translate_y_mm: ty,
                    rotate_z_deg: rz,
                },
            };
            this.schedulePlacementViewerRefresh();
        },

        resetObjectTransform(buildItemIndex) {
            const idx = Number(buildItemIndex);
            if (!Number.isInteger(idx) || idx < 1) return;
            const edits = { ...(this.objectTransformEdits || {}) };
            delete edits[idx];
            this.objectTransformEdits = edits;
            this.schedulePlacementViewerRefresh();
        },

        resetAllObjectTransforms() {
            this.objectTransformEdits = {};
            this.schedulePlacementViewerRefresh();
        },

        getObjectBaseTranslation(obj) {
            // Preferred path: backend returns canonical bed-local pose hints.
            const uiPose = obj?.ui_base_pose;
            if (uiPose && typeof uiPose === 'object') {
                return {
                    x: Number(uiPose.x || 0),
                    y: Number(uiPose.y || 0),
                    z: Number(uiPose.z || 0),
                };
            }

            // For Bambu-style multi-plate files, Orca often uses model_settings.config
            // assemble_item transforms as the effective placement. Prefer that pose in
            // the placement viewer so preview and slice behavior align. Use assemble XY,
            // but keep Z from the core build-item transform because some Bambu files
            // store packed/project-space Z in assemble_item (causes floating previews).
            const coreT = (obj?.translation || [0, 0, 0]);
            const t = (
                (this.selectedUpload?.is_multi_plate && Array.isArray(obj?.assemble_translation) && obj.assemble_translation.length >= 3)
                    ? obj.assemble_translation
                    : coreT
            );
            return {
                x: Number(t[0] || 0),
                y: Number(t[1] || 0),
                z: Number(coreT[2] || 0),
            };
        },

        getPlacementViewerDisplayOffset() {
            const frame = this.objectLayout?.placement_frame;
            if (frame && frame.canonical === 'bed_local_xy_mm') {
                const off = Array.isArray(frame.offset_xy) ? frame.offset_xy : [0, 0];
                // When ui_base_pose is present, backend has already applied any required
                // preview normalization. Keep viewer/object edits in bed-local space.
                const hasUiBasePose = Array.isArray(this.objectLayout?.objects)
                    && this.objectLayout.objects.some(o => o?.ui_base_pose);
                if (hasUiBasePose) {
                    return { x: 0, y: 0 };
                }
                return {
                    x: Number(off[0] || 0),
                    y: Number(off[1] || 0),
                };
            }

            if (!this.selectedUpload?.is_multi_plate || !this.selectedPlate || !this.objectLayout?.build_volume) {
                return { x: 0, y: 0 };
            }
            const objs = Array.isArray(this.objectLayout?.objects) ? this.objectLayout.objects : [];
            const vol = this.objectLayout.build_volume || {};
            const bedW = Math.max(1, Number(vol.x || 270));
            const bedH = Math.max(1, Number(vol.y || 270));

            let centerX = null;
            let centerY = null;
            const assembleBounds = objs
                .map(o => o?.assemble_world_bounds)
                .filter(b => b?.min && b?.max);
            if (assembleBounds.length > 0) {
                let minX = Number.POSITIVE_INFINITY;
                let minY = Number.POSITIVE_INFINITY;
                let maxX = Number.NEGATIVE_INFINITY;
                let maxY = Number.NEGATIVE_INFINITY;
                for (const b of assembleBounds) {
                    minX = Math.min(minX, Number(b.min[0] || 0));
                    minY = Math.min(minY, Number(b.min[1] || 0));
                    maxX = Math.max(maxX, Number(b.max[0] || 0));
                    maxY = Math.max(maxY, Number(b.max[1] || 0));
                }
                centerX = (minX + maxX) / 2;
                centerY = (minY + maxY) / 2;
            } else {
                const vb = this.objectLayout?.validation?.bounds;
                if (vb?.min && vb?.max) {
                    centerX = (Number(vb.min[0] || 0) + Number(vb.max[0] || 0)) / 2;
                    centerY = (Number(vb.min[1] || 0) + Number(vb.max[1] || 0)) / 2;
                }
            }

            if (!Number.isFinite(centerX) || !Number.isFinite(centerY)) {
                if (objs.length > 0) {
                    const sum = objs.reduce((acc, o) => {
                        const t = this.getObjectBaseTranslation(o);
                        acc.x += t.x;
                        acc.y += t.y;
                        return acc;
                    }, { x: 0, y: 0 });
                    centerX = sum.x / objs.length;
                    centerY = sum.y / objs.length;
                } else {
                    centerX = bedW / 2;
                    centerY = bedH / 2;
                }
            }

            return {
                x: (bedW / 2) - Number(centerX || 0),
                y: (bedH / 2) - Number(centerY || 0),
            };
        },

        getObjectEffectivePose(obj) {
            const base = this.getObjectBaseTranslation(obj);
            const idx = Number(obj?.build_item_index || 0);
            const edit = (idx > 0 ? this.objectTransformEdits?.[idx] : null) || {};
            return {
                x: base.x + Number(edit.translate_x_mm || 0),
                y: base.y + Number(edit.translate_y_mm || 0),
                z: base.z,
                rotate_z_deg: Number(edit.rotate_z_deg || 0),
            };
        },

        getObjectEffectivePoseForViewer(obj) {
            const pose = this.getObjectEffectivePose(obj);
            const offset = this.getPlacementViewerDisplayOffset();
            return {
                ...pose,
                x: Number(pose.x || 0) + Number(offset.x || 0),
                y: Number(pose.y || 0) + Number(offset.y || 0),
            };
        },

        applyPlacementViewerPose(buildItemIndex, nextPose) {
            if (this.isObjectPlacementTransformApproximate()) return;
            const idx = Number(buildItemIndex || 0);
            if (!Number.isInteger(idx) || idx < 1) return;
            const obj = (this.objectLayout?.objects || []).find(o => Number(o.build_item_index || 0) === idx);
            if (!obj) return;

            const base = this.getObjectBaseTranslation(obj);
            const offset = this.getPlacementViewerDisplayOffset();
            const nextX = Number(nextPose?.x || base.x) - Number(offset.x || 0);
            const nextY = Number(nextPose?.y || base.y) - Number(offset.y || 0);
            const nextRz = Number(nextPose?.rotate_z_deg || 0);

            this.setObjectTransformField(idx, 'translate_x_mm', nextX - base.x);
            this.setObjectTransformField(idx, 'translate_y_mm', nextY - base.y);
            this.setObjectTransformField(idx, 'rotate_z_deg', nextRz);
        },

        getBedEditorStyle() {
            const vol = this.objectLayout?.build_volume || {};
            const w = Math.max(1, Number(vol.x || 270));
            const h = Math.max(1, Number(vol.y || 270));
            const ratio = h / w;
            return `aspect-ratio: ${w} / ${h};`;
        },

        getObjectMarkerStyle(obj) {
            const vol = this.objectLayout?.build_volume || {};
            const bedW = Math.max(1, Number(vol.x || 270));
            const bedH = Math.max(1, Number(vol.y || 270));
            const pose = this.getObjectEffectivePose(obj);
            const xPct = Math.min(100, Math.max(0, (pose.x / bedW) * 100));
            const yPct = Math.min(100, Math.max(0, 100 - (pose.y / bedH) * 100));
            return `left:${xPct}%;top:${yPct}%;transform:translate(-50%,-50%) rotate(${pose.rotate_z_deg}deg);`;
        },

        beginObjectDrag(event, obj) {
            if (!obj || !obj.build_item_index) return;
            if (event.button !== undefined && event.button !== 0) return;

            const bedEl = event.target?.closest?.('[data-bed-editor]');
            if (!bedEl) return;
            const rect = bedEl.getBoundingClientRect();
            if (!rect || rect.width <= 0 || rect.height <= 0) return;

            const idx = Number(obj.build_item_index);
            this.objectDrag = {
                build_item_index: idx,
                startClientX: Number(event.clientX || 0),
                startClientY: Number(event.clientY || 0),
                startTx: this.getObjectTransformValue(idx, 'translate_x_mm'),
                startTy: this.getObjectTransformValue(idx, 'translate_y_mm'),
                bedRect: { left: rect.left, top: rect.top, width: rect.width, height: rect.height },
                bedW: Math.max(1, Number(this.objectLayout?.build_volume?.x || 270)),
                bedH: Math.max(1, Number(this.objectLayout?.build_volume?.y || 270)),
            };

            if (event.preventDefault) event.preventDefault();
        },

        onObjectDragMove(event) {
            if (!this.objectDrag) return;
            const d = this.objectDrag;
            const dxPx = Number(event.clientX || 0) - d.startClientX;
            const dyPx = Number(event.clientY || 0) - d.startClientY;
            const dxMm = (dxPx / d.bedRect.width) * d.bedW;
            const dyMm = (-dyPx / d.bedRect.height) * d.bedH;
            this.setObjectTransformField(d.build_item_index, 'translate_x_mm', d.startTx + dxMm);
            this.setObjectTransformField(d.build_item_index, 'translate_y_mm', d.startTy + dyMm);
            if (event.preventDefault) event.preventDefault();
        },

        endObjectDrag() {
            this.objectDrag = null;
        },

        queueObjectLayoutLoad(uploadId, plateId = null, delayMs = 0) {
            if (this._placementLoadTimer) {
                clearTimeout(this._placementLoadTimer);
                this._placementLoadTimer = null;
            }
            this._placementLoadTimer = setTimeout(() => {
                this._placementLoadTimer = null;
                this.loadObjectLayout(uploadId, plateId);
            }, Math.max(0, Number(delayMs || 0)));
        },

        /**
         * Select a plate for slicing
         */
        autoSelectFirstPlate() {
            if (!this.plates || this.plates.length === 0) return;
            const firstFit = this.plates.find(p => p.validation && p.validation.fits && p.printable);
            this.selectPlate(firstFit ? firstFit.plate_id : this.plates[0].plate_id);
        },

        proceedFromSelectPlate() {
            if (this.selectedUpload?.is_multi_plate && !this.selectedPlate) return;
            this.currentStep = 'configure';
            // Explicitly trigger placement load — the IntersectionObserver may
            // not fire reliably when the panel transitions from display:none.
            this.placementPanelVisible = true;
            this.queueObjectLayoutLoad(
                this.selectedUpload?.upload_id,
                this.selectedPlate,
                50,  // tiny delay to let Alpine render the configure panel first
            );
        },

        backToSelectPlate() {
            this.currentStep = 'selectplate';
        },

        selectPlate(plateId) {
            this.selectedPlate = plateId;
            this.objectTransformEdits = {};
            this.selectedLayoutObject = null;
            // Update detected colors from the selected plate's per-plate colors
            const plate = this.plates.find(p => p.plate_id === plateId);
            if (plate && plate.detected_colors && plate.detected_colors.length > 0) {
                this.detectedColors = plate.detected_colors;
            }
            // Explicit plate selection should always trigger a layout load,
            // even if the placement panel hasn't scrolled into view yet.
            this.placementPanelVisible = true;
            // Signal loading immediately so consumers don't see stale data
            // between now and when the queued load starts.
            this.objectLayoutLoading = true;
            // Let the plate card selection/thumbnail render first, then load the
            // heavier placement layout/geometry work.
            this.queueObjectLayoutLoad(this.selectedUpload?.upload_id, plateId, 0);
            console.log('Selected plate:', plateId);
        },

        // ---------------------------------------------------------------
        // Multiple Copies (M32)
        // ---------------------------------------------------------------

        async applyCopies(n) {
            n = parseInt(n, 10);
            if (!n || n < 1 || n > 100 || !this.selectedUpload) return;
            if (n === this.copyCount) return;

            this.copiesApplying = true;
            this.copyGridInfo = null;
            try {
                if (n === 1) {
                    // Reset to single copy
                    await fetch(`/api/upload/${this.selectedUpload.upload_id}/copies`, { method: 'DELETE' });
                    this.copyCount = 1;
                    this.copySelectValue = '1';
                    this.copyGridInfo = null;
                } else {
                    const response = await fetch(`/api/upload/${this.selectedUpload.upload_id}/copies`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ copies: n, spacing: 5.0 }),
                    });
                    if (!response.ok) {
                        const err = await response.json().catch(() => ({ detail: 'Failed' }));
                        this.showError(err.detail || 'Failed to apply copies');
                        // Revert select to current copyCount
                        this.copySelectValue = [1, 2, 4, 6, 9].includes(this.copyCount) ? String(this.copyCount) : 'custom';
                        return;
                    }
                    const result = await response.json();
                    this.copyCount = n;
                    this.copySelectValue = [1, 2, 4, 6, 9].includes(n) ? String(n) : 'custom';
                    this.copyGridInfo = result;
                    if (!result.fits_bed) {
                        this.showError(`${n} copies may exceed the build plate. Consider fewer copies.`);
                    }
                }
            } catch (err) {
                this.showError(`Copies failed: ${err.message}`);
            } finally {
                this.copiesApplying = false;
            }
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

            const requestedScale = Number(this.sliceSettings.scale_percent) || 100;
            const scalePercent = Math.min(500, Math.max(10, requestedScale));
            this.sliceSettings.scale_percent = scalePercent;

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
            this.sliceProgress = 1;
            this.sliceMessage = 'Preparing...';

            // Abort any in-flight viewer/layout requests to free browser connections
            // for the slice POST and progress polling
            viewerAbortController?.abort();
            viewerAbortController = null;
            this.accordionColours = false;
            this.accordionSettings = false;

            // Generate a job_id so we can poll progress immediately
            const clientJobId = `slice_${Array.from(crypto.getRandomValues(new Uint8Array(6)), b => b.toString(16).padStart(2, '0')).join('')}`;
            this.sliceJobId = clientJobId;

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
                const objectTransforms = this.getObjectTransformsPayload();
                if (objectTransforms.length > 0) {
                    sliceSettings.object_transforms = objectTransforms;
                }

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
                
                // Include client-generated job_id for progress polling
                sliceSettings.job_id = clientJobId;

                // Start polling — this is the SOLE mechanism for progress and completion.
                this.pollSliceStatus(clientJobId);

                // Fire the slice POST without awaiting. The backend's GIL blocks the
                // event loop during CPU-heavy profile embedding (~20s for large Bambu
                // files), preventing poll responses. By not awaiting, we avoid holding
                // JS state and let polling handle everything. The POST error path
                // catches network/validation failures that polling can't detect.
                const slicePromise = isMultiPlate
                    ? (console.log(`Slicing plate ${selectedPlateId} from upload ${uploadId} (job: ${clientJobId})`),
                       api.slicePlate(uploadId, selectedPlateId, sliceSettings))
                    : (console.log(`Slicing upload ${uploadId} (job: ${clientJobId})`),
                       api.sliceUpload(uploadId, sliceSettings));

                slicePromise.catch(err => {
                    // Only handle if we're still on the slicing step (polling hasn't
                    // already transitioned us to complete/failed)
                    if (this.currentStep === 'slicing') {
                        clearInterval(this.sliceInterval);
                        // Suppress error for user-initiated cancellation
                        if (!err.message?.includes('cancelled')) {
                            this.showError(`Slicing failed: ${err.message}`);
                        }
                        this.currentStep = 'configure';
                        console.error('Slice POST failed:', err);
                    }
                });
            } catch (err) {
                clearInterval(this.sliceInterval);
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

            // Animate fake progress 1-4% while waiting for real backend data.
            // The backend's GIL blocks poll responses during CPU-heavy embedding
            // (~20s for large Bambu files). This gives the user visual feedback.
            let fakeProgress = 1;
            this.sliceProgress = 1;
            this.sliceMessage = 'Preparing...';
            let gotRealProgress = false;

            let pollCount = 0;
            this.sliceInterval = setInterval(async () => {
                const myPoll = ++pollCount;

                // Animate fake progress until real data arrives
                if (!gotRealProgress) {
                    fakeProgress = Math.min(fakeProgress + 0.2, 4);
                    this.sliceProgress = Math.round(fakeProgress);
                }

                try {
                    const job = await api.getJobStatus(jobId);

                    // Use real progress from the API
                    if (job.progress !== undefined && job.progress > 0) {
                        gotRealProgress = true;
                        this.sliceProgress = job.progress;
                    }
                    if (job.progress_message) {
                        this.sliceMessage = job.progress_message;
                    }

                    if (job.status === 'completed') {
                        clearInterval(this.sliceInterval);
                        this.sliceResult = job;
                        this.sliceProgress = 100;
                        this.sliceMessage = 'Complete';
                        this.currentStep = 'complete';
                        console.log('Slicing completed via poll');
                        this.loadJobs();
                    } else if (job.status === 'failed') {
                        clearInterval(this.sliceInterval);
                        // Don't show error toast for user-initiated cancellation
                        if (job.error_message !== 'Cancelled') {
                            this.showError(`Slicing failed: ${job.error_message || 'Unknown error'}`);
                        }
                        this.currentStep = 'configure';
                    }
                } catch (err) {
                    if (!err.message?.includes('404')) {
                        console.error(`[poll #${myPoll}] error:`, err);
                    }
                }
            }, 1000);
        },

        /**
         * Contextual header title based on current step
         */
        headerTitle() {
            switch (this.currentStep) {
                case 'upload': return 'U1 Slicer Bridge';
                case 'selectplate': return this.selectedUpload?.filename
                    ? 'Select Plate: ' + this.selectedUpload.filename
                    : 'Select Plate';
                case 'configure': return this.selectedUpload?.filename
                    ? 'Configure: ' + this.selectedUpload.filename
                    : 'Configure';
                case 'slicing': return this.selectedUpload?.filename
                    ? 'Slicing: ' + this.selectedUpload.filename
                    : 'Slicing...';
                case 'complete': return 'G-code Ready';
                default: return 'U1 Slicer Bridge';
            }
        },

        /**
         * Reset workflow with confirmation if state exists
         */
        async confirmResetWorkflow() {
            // Active slice in progress — confirm cancellation first
            if (this.currentStep === 'slicing') {
                if (!await this.showConfirm({
                    title: 'Cancel Slicing?',
                    message: 'Slicing is still in progress. Do you want to cancel it?',
                    confirmText: 'Cancel Slice',
                    destructive: true,
                })) return;
                // Reset UI immediately — cancel API fires in background
                this.cancelActiveSlice();
                this.resetWorkflow();
                return;
            }
            if (this.selectedUpload || this.sliceResult) {
                if (!await this.showConfirm({
                    title: 'Start Over',
                    html: 'Your uploads and sliced files are saved in <strong>My Files</strong> <svg class="inline w-4 h-4 -mt-0.5 text-gray-500" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z"/></svg> and can be reopened any time.',
                    confirmText: 'Start Over',
                    suppressKey: 'suppress_start_over'
                })) return;
            }
            this.resetWorkflow();
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
            this.clearMakerWorld();
            this.sliceProgress = 0;
            this.sliceMessage = '';
            this.sliceJobId = null;
            this.uploadProgress = 0;
            this.uploadPhase = 'idle';
            this.resetJobOverrideSettings();
            this.stopPrintMonitorPolling();
            this.printMonitorActive = false;
            this.printSending = false;
            this.printState = null;
            this.copyCount = 1;
            this.copySelectValue = '1';
            this.copyCountInput = null;
            this.copiesApplying = false;
            this.copyGridInfo = null;

            if (this.sliceInterval) {
                clearInterval(this.sliceInterval);
                this.sliceInterval = null;
            }
        },

        async cancelActiveSlice() {
            if (this.sliceInterval) {
                clearInterval(this.sliceInterval);
                this.sliceInterval = null;
            }
            if (this.sliceJobId) {
                try {
                    await api.cancelSlice(this.sliceJobId);
                } catch (e) {
                    console.warn('Cancel slice request failed:', e);
                }
                this.sliceJobId = null;
            }
        },

        /**
         * Return to configure step with all settings preserved (for reslicing).
         */
        async goBackToConfigure() {
            this.sliceResult = null;
            this.sliceProgress = 0;
            this.sliceMessage = '';
            this.currentStep = 'configure';
            this.activeTab = 'upload';

            if (this.sliceInterval) {
                clearInterval(this.sliceInterval);
                this.sliceInterval = null;
            }

            // Rehydrate upload details when returning from complete view.
            // This preserves multicolour metadata even if selectedUpload was
            // populated from a job record that lacks detected_colors/file_size.
            const uploadId = this.selectedUpload?.upload_id;
            if (!uploadId) return;

            try {
                const [uploadDetails, platesData] = await Promise.all([
                    api.getUpload(uploadId).catch(() => null),
                    api.getUploadPlates(uploadId).catch(() => null),
                ]);

                if (uploadDetails) {
                    this.selectedUpload = { ...this.selectedUpload, ...uploadDetails };

                    const serverColors = uploadDetails.detected_colors || [];
                    const missingColors = !this.detectedColors || this.detectedColors.length === 0;
                    const downgradedColors =
                        serverColors.length > 1 &&
                        ((this.detectedColors?.length || 0) <= 1 || (this.selectedFilaments?.length || 0) <= 1);

                    if (missingColors || downgradedColors) {
                        if (this.filaments.length === 0) {
                            await this.loadFilaments();
                        }
                        this.applyDetectedColors(serverColors);
                    }
                }

                if (platesData && platesData.is_multi_plate && Array.isArray(platesData.plates)) {
                    this.selectedUpload.is_multi_plate = true;
                    this.selectedUpload.plate_count = platesData.plate_count;
                    this.plates = platesData.plates;
                } else if (this.selectedUpload) {
                    this.selectedUpload.is_multi_plate = false;
                    this.plates = [];
                }
            } catch (err) {
                console.warn('Failed to rehydrate upload after returning to configure:', err);
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

            // First 4 colors get identity assignment; extras round-robin
            this.sliceSettings.extruder_assignments = this.detectedColors
                .map((_, idx) => idx < this.maxExtruders ? idx : idx % this.maxExtruders);

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

            for (let i = 0; i < colors.length; i++) {
                const detectedColor = colors[i];
                let bestSlot = null;
                let bestDistance = Infinity;

                // First 4 colors get unique slots; extras can share any slot
                const requireUnique = i < this.maxExtruders;

                for (const slot of presetSlots) {
                    if (requireUnique && usedSlots.has(slot.slotIdx)) continue;
                    const distance = this.colorDistance(detectedColor, slot.colorHex);
                    if (distance < bestDistance) {
                        bestDistance = distance;
                        bestSlot = slot;
                    }
                }

                if (!bestSlot) {
                    return null;
                }

                if (requireUnique) usedSlots.add(bestSlot.slotIdx);
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
            this.filamentOverride = !this.filamentOverride;
            if (this.filamentOverride) {
                if (!this.sliceSettings.extruder_assignments || this.sliceSettings.extruder_assignments.length === 0) {
                    this.sliceSettings.extruder_assignments = (this.detectedColors || [])
                        .map((_, idx) => idx < this.maxExtruders ? idx : idx % this.maxExtruders);
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
            const colorCount = (this.detectedColors || []).length;
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

            // Use actual detected colors from the 3MF file, not preset colors.
            // Preset colors default to #FFFFFF which would mask the real file colors.
            const allColors = colors || [];
            this.sliceSettings.filament_colors = allColors.length > 0
                ? allColors.map((c) => c || '#FFFFFF')
                : this.extruderPresets.map((p) => p.color_hex || '#FFFFFF');
            // Default assignments: first 4 get identity, extras round-robin
            this.sliceSettings.extruder_assignments = allColors.map((_, idx) =>
                idx < this.maxExtruders ? idx : idx % this.maxExtruders);

            if (!colors || colors.length === 0) {
                this.multicolorNotice = null;
                this.setDefaultFilament();
                return;
            }

            // Single-color files always use single-filament mode.
            // Multicolor mapping would pad to 2+ extruders and can crash Orca.
            if (colors.length <= 1) {
                this.multicolorNotice = null;
                this.setDefaultFilament();
                return;
            }

            this.multicolorNotice = null;
            this.selectedFilament = null;

            const mappedFromPresets = this.mapDetectedColorsToPresetSlots(allColors);
            if (mappedFromPresets && mappedFromPresets.filamentIds.length === allColors.length) {
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
         * Set extruder assignment for a colour index.
         * Multiple colours can share the same extruder (needed for >4 colour files).
         */
        setExtruderAssignment(colorIdx, extruderIdx) {
            const assignments = this.sliceSettings.extruder_assignments || [];
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

        /**
         * Show a styled confirm dialog. Returns a promise that resolves to true/false.
         * Options:
         *   html - rich HTML message (used instead of message if provided)
         *   suppressKey - localStorage key; if set, shows "Don't show again" checkbox
         */
        showConfirm({ title = 'Confirm', message = '', html = '', confirmText = 'OK', destructive = false, suppressKey = null } = {}) {
            if (suppressKey && localStorage.getItem(suppressKey) === '1') return Promise.resolve(true);
            return new Promise(resolve => {
                this._confirmResolve = resolve;
                this.confirmModal = { open: true, title, message, html, confirmText, destructive, suppressKey, suppressChecked: false };
            });
        },

        resolveConfirm(value) {
            if (value && this.confirmModal.suppressKey && this.confirmModal.suppressChecked) {
                localStorage.setItem(this.confirmModal.suppressKey, '1');
            }
            this.confirmModal.open = false;
            if (this._confirmResolve) {
                this._confirmResolve(value);
                this._confirmResolve = null;
            }
        },

        // ----- Printer Settings (Settings modal) -----

        async loadPrinterSettings() {
            try {
                const data = await api.getPrinterSettings();
                this.printerSettings = {
                    moonraker_url: data.moonraker_url || '',
                    makerworld_cookies: '',  // never returned by API (sensitive)
                };
                this.hasMakerWorldCookies = data.has_makerworld_cookies || false;
                this.makerWorldEnabled = data.makerworld_enabled || false;
            } catch (err) {
                console.warn('Failed to load printer settings:', err);
            }
        },

        async savePrinterSettings() {
            this.printerSettingsSaving = true;
            this.printerTestResult = null;
            try {
                const payload = {
                    moonraker_url: this.printerSettings.moonraker_url || '',
                    makerworld_enabled: this.makerWorldEnabled,
                };
                // Only send cookies if user typed something new (field is blank on load for security)
                if (this.printerSettings.makerworld_cookies) {
                    payload.makerworld_cookies = this.printerSettings.makerworld_cookies;
                }
                await api.savePrinterSettings(payload);
                // Update cookie status indicator
                if (this.printerSettings.makerworld_cookies) {
                    this.hasMakerWorldCookies = true;
                    this.printerSettings.makerworld_cookies = '';  // clear from memory after save
                }
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

        async clearMakerWorldCookies() {
            try {
                await api.savePrinterSettings({
                    moonraker_url: this.printerSettings.moonraker_url || '',
                    makerworld_cookies: '',
                });
                this.hasMakerWorldCookies = false;
            } catch (err) {
                console.warn('Failed to clear MakerWorld cookies:', err);
            }
        },

        // ---------------------------------------------------------------
        // Backup & Restore
        // ---------------------------------------------------------------

        async exportSettings() {
            this.settingsExporting = true;
            this.settingsBackupMessage = null;
            try {
                const response = await fetch('/api/settings/export');
                if (!response.ok) throw new Error(`Export failed: HTTP ${response.status}`);
                const blob = await response.blob();
                const url = URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                const date = new Date().toISOString().slice(0, 10);
                a.download = `u1-slicer-settings-${date}.json`;
                document.body.appendChild(a);
                a.click();
                document.body.removeChild(a);
                URL.revokeObjectURL(url);
                this.settingsBackupOk = true;
                this.settingsBackupMessage = 'Settings exported successfully.';
            } catch (err) {
                this.settingsBackupOk = false;
                this.settingsBackupMessage = `Export failed: ${err.message}`;
            } finally {
                this.settingsExporting = false;
                setTimeout(() => { this.settingsBackupMessage = null; }, 5000);
            }
        },

        async importSettings() {
            if (!this.settingsImportFile) return;
            this.settingsImporting = true;
            this.settingsBackupMessage = null;
            try {
                const formData = new FormData();
                formData.append('file', this.settingsImportFile);
                const response = await fetch('/api/settings/import', {
                    method: 'POST',
                    body: formData,
                });
                if (!response.ok) {
                    const err = await response.json().catch(() => ({ detail: 'Import failed' }));
                    throw new Error(err.detail || `HTTP ${response.status}`);
                }
                const result = await response.json();
                // Reload all settings from DB
                await this.loadFilaments();
                await this.loadExtruderPresets();
                await this.loadPrinterSettings();
                this.settingsBackupOk = true;
                this.settingsBackupMessage = `Settings restored: ${result.filaments_imported} filaments, presets & defaults updated.`;
                this.settingsImportFile = null;
            } catch (err) {
                this.settingsBackupOk = false;
                this.settingsBackupMessage = `Import failed: ${err.message}`;
            } finally {
                this.settingsImporting = false;
                setTimeout(() => { this.settingsBackupMessage = null; }, 8000);
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
            if (!await this.showConfirm({ title: 'Cancel Print', message: 'Cancel the current print?', confirmText: 'Cancel Print', destructive: true })) return;
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

        webcamOpenUrl(webcam) {
            if (!webcam) return '';
            return webcam.stream_url || webcam.stream_url_alt || webcam.snapshot_url || webcam.snapshot_url_alt || '';
        },

        webcamImageKey(webcam, index) {
            return `${index}:${webcam?.name || ''}:${webcam?.snapshot_url || ''}:${webcam?.snapshot_url_alt || ''}:${webcam?.stream_url || ''}:${webcam?.stream_url_alt || ''}`;
        },

        webcamImageCandidates(webcam) {
            if (!webcam) return [];
            const candidates = [
                webcam.snapshot_url,
                webcam.snapshot_url_alt,
                webcam.stream_url,
                webcam.stream_url_alt,
            ].filter(Boolean);
            return [...new Set(candidates)];
        },

        webcamImageUrl(webcam, index) {
            if (!webcam) return '';
            const key = this.webcamImageKey(webcam, index);
            const candidates = this.webcamImageCandidates(webcam);
            if (candidates.length === 0) return '';
            const stage = Number(this.webcamImageFallback[key] || 0);
            const baseUrl = candidates[Math.min(stage, candidates.length - 1)] || '';
            if (!baseUrl) return '';
            const separator = baseUrl.includes('?') ? '&' : '?';
            return `${baseUrl}${separator}_cb=${this.webcamImageNonce}`;
        },

        webcamImageAvailable(webcam, index) {
            return Boolean(this.webcamImageUrl(webcam, index));
        },

        handleWebcamImageError(webcam, index) {
            const key = this.webcamImageKey(webcam, index);
            const candidates = this.webcamImageCandidates(webcam);
            if (candidates.length <= 1) return;
            const current = Number(this.webcamImageFallback[key] || 0);
            if (current >= candidates.length - 1) return;
            this.webcamImageFallback[key] = current + 1;
        },

        /**
         * Cleanup intervals on destroy
         */
        destroy() {
            if (this.sliceInterval) {
                clearInterval(this.sliceInterval);
            }
            this.stopPrintMonitorPolling();
            if (placementViewer) {
                placementViewer.destroy();
                placementViewer = null;
                window.__u1PlacementViewer = null;
            }
        },
    };
}
