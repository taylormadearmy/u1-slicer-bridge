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
    error_message TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_slicing_jobs_job_id ON slicing_jobs(job_id);
CREATE INDEX IF NOT EXISTS idx_slicing_jobs_upload_id ON slicing_jobs(upload_id);

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
