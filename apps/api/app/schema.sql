-- Database schema for u1-slicer-bridge

CREATE TABLE IF NOT EXISTS uploads (
    id SERIAL PRIMARY KEY,
    filename TEXT NOT NULL,
    file_path TEXT NOT NULL,
    file_size BIGINT NOT NULL,
    uploaded_at TIMESTAMP NOT NULL DEFAULT NOW()
);

-- Add plate bounds validation columns
ALTER TABLE uploads ADD COLUMN IF NOT EXISTS plate_validated BOOLEAN DEFAULT false;
ALTER TABLE uploads ADD COLUMN IF NOT EXISTS bounds_min_x REAL;
ALTER TABLE uploads ADD COLUMN IF NOT EXISTS bounds_min_y REAL;
ALTER TABLE uploads ADD COLUMN IF NOT EXISTS bounds_min_z REAL;
ALTER TABLE uploads ADD COLUMN IF NOT EXISTS bounds_max_x REAL;
ALTER TABLE uploads ADD COLUMN IF NOT EXISTS bounds_max_y REAL;
ALTER TABLE uploads ADD COLUMN IF NOT EXISTS bounds_max_z REAL;
ALTER TABLE uploads ADD COLUMN IF NOT EXISTS bounds_warning TEXT;

-- Upload metadata cache (avoids re-parsing 3MF on every GET request)
ALTER TABLE uploads ADD COLUMN IF NOT EXISTS is_multi_plate BOOLEAN DEFAULT false;
ALTER TABLE uploads ADD COLUMN IF NOT EXISTS plate_count INTEGER DEFAULT 0;
ALTER TABLE uploads ADD COLUMN IF NOT EXISTS detected_colors TEXT;      -- JSON array of hex color strings
ALTER TABLE uploads ADD COLUMN IF NOT EXISTS file_print_settings TEXT;  -- JSON object of support/brim settings
ALTER TABLE uploads ADD COLUMN IF NOT EXISTS plate_metadata TEXT;       -- JSON: full plate info with bounds, validation, colors, previews

-- ============================================================================
-- OLD TABLES (removed - plate-based workflow)
-- ============================================================================
-- These tables were part of the old object-by-object normalization workflow.
-- They have been removed in favor of storing plate bounds in the uploads table.
--
-- Removed tables:
-- - objects: Individual object tracking (no longer needed)
-- - normalization_jobs: Per-object normalization tracking (no longer needed)

-- Filament profiles
CREATE TABLE IF NOT EXISTS filaments (
    id SERIAL PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    material TEXT NOT NULL,
    nozzle_temp INTEGER NOT NULL,
    bed_temp INTEGER NOT NULL,
    print_speed INTEGER,
    bed_type TEXT DEFAULT 'PEI',
    is_default BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

-- Add bed_type column if it doesn't exist (for existing databases)
ALTER TABLE filaments ADD COLUMN IF NOT EXISTS bed_type TEXT DEFAULT 'PEI';

-- Add color and extruder columns for multifilament support
ALTER TABLE filaments ADD COLUMN IF NOT EXISTS color_hex VARCHAR(7) DEFAULT '#FFFFFF';
ALTER TABLE filaments ADD COLUMN IF NOT EXISTS extruder_index INTEGER DEFAULT 0;
ALTER TABLE filaments ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE;
ALTER TABLE filaments ADD COLUMN IF NOT EXISTS source_type TEXT DEFAULT 'manual';
ALTER TABLE filaments ADD COLUMN IF NOT EXISTS slicer_settings TEXT;  -- JSON blob of OrcaSlicer-native filament settings
ALTER TABLE filaments ADD COLUMN IF NOT EXISTS density REAL DEFAULT 1.24;  -- g/cm³ (PLA default)

-- ============================================================================
-- OLD TABLES (removed - plate-based workflow)
-- ============================================================================
-- Removed tables:
-- - bundles: Object grouping (no longer needed - we slice entire uploads)
-- - bundle_objects: Many-to-many mapping (no longer needed)

-- Slicing jobs (plate-based workflow)
CREATE TABLE IF NOT EXISTS slicing_jobs (
    id SERIAL PRIMARY KEY,
    job_id TEXT UNIQUE NOT NULL,
    upload_id INTEGER REFERENCES uploads(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'pending',
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    log_path TEXT,
    gcode_path TEXT,
    gcode_size BIGINT,
    estimated_time_seconds INTEGER,
    filament_used_mm REAL,
    layer_count INTEGER,
    three_mf_path TEXT,
    filament_colors TEXT,  -- JSON array of color hex codes used
    filament_used_g TEXT,  -- JSON array of per-extruder gram weights
    error_message TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_slicing_jobs_job_id ON slicing_jobs(job_id);
CREATE INDEX IF NOT EXISTS idx_slicing_jobs_upload_id ON slicing_jobs(upload_id);

-- Migration: Add filament_colors column to existing databases
ALTER TABLE slicing_jobs ADD COLUMN IF NOT EXISTS filament_colors TEXT;

-- Migration: Add filament_used_g column to existing databases
ALTER TABLE slicing_jobs ADD COLUMN IF NOT EXISTS filament_used_g TEXT;

-- Persistent extruder preset mapping (E1-E4)
CREATE TABLE IF NOT EXISTS extruder_presets (
    slot INTEGER PRIMARY KEY,
    filament_id INTEGER REFERENCES filaments(id) ON DELETE SET NULL,
    color_hex VARCHAR(7) NOT NULL DEFAULT '#FFFFFF',
    updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_extruder_preset_slot CHECK (slot BETWEEN 1 AND 4)
);

-- Persistent default slicing settings
CREATE TABLE IF NOT EXISTS slicing_defaults (
    id INTEGER PRIMARY KEY,
    layer_height REAL NOT NULL DEFAULT 0.2,
    infill_density INTEGER NOT NULL DEFAULT 15,
    wall_count INTEGER NOT NULL DEFAULT 3,
    infill_pattern TEXT NOT NULL DEFAULT 'gyroid',
    supports BOOLEAN NOT NULL DEFAULT FALSE,
    enable_prime_tower BOOLEAN NOT NULL DEFAULT FALSE,
    prime_volume INTEGER,
    prime_tower_width INTEGER,
    prime_tower_brim_width INTEGER,
    prime_tower_brim_chamfer BOOLEAN NOT NULL DEFAULT TRUE,
    prime_tower_brim_chamfer_max_width INTEGER,
    nozzle_temp INTEGER,
    bed_temp INTEGER,
    bed_type TEXT,
    updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_slicing_defaults_single_row CHECK (id = 1)
);

-- Slicing override columns (3-way mode: model/orca/override)
ALTER TABLE slicing_defaults ADD COLUMN IF NOT EXISTS support_type TEXT;
ALTER TABLE slicing_defaults ADD COLUMN IF NOT EXISTS support_threshold_angle INTEGER;
ALTER TABLE slicing_defaults ADD COLUMN IF NOT EXISTS brim_type TEXT;
ALTER TABLE slicing_defaults ADD COLUMN IF NOT EXISTS brim_width REAL;
ALTER TABLE slicing_defaults ADD COLUMN IF NOT EXISTS brim_object_gap REAL;
ALTER TABLE slicing_defaults ADD COLUMN IF NOT EXISTS skirt_loops INTEGER;
ALTER TABLE slicing_defaults ADD COLUMN IF NOT EXISTS skirt_distance REAL;
ALTER TABLE slicing_defaults ADD COLUMN IF NOT EXISTS skirt_height INTEGER;
ALTER TABLE slicing_defaults ADD COLUMN IF NOT EXISTS setting_modes TEXT;  -- JSON: {"setting_key": "model"|"orca"|"override"}

-- Persistent printer connection settings
CREATE TABLE IF NOT EXISTS printer_settings (
    id INTEGER PRIMARY KEY,
    moonraker_url TEXT,
    updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_printer_settings_single_row CHECK (id = 1)
);
ALTER TABLE printer_settings ADD COLUMN IF NOT EXISTS makerworld_cookies TEXT;
ALTER TABLE printer_settings ADD COLUMN IF NOT EXISTS makerworld_enabled BOOLEAN DEFAULT false;

-- ============================================================================
-- OLD WORKFLOW CLEANUP (already migrated)
-- ============================================================================
-- These tables were part of the old object-by-object normalization workflow
-- and have been removed in favor of plate-based validation.
--
-- Dropped tables:
-- - objects (individual object tracking)
-- - normalization_jobs
-- - bundles
-- - bundle_objects
--
-- Migration already applied. Tables dropped if they existed.
