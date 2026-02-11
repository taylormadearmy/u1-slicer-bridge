-- Database schema for u1-slicer-bridge

CREATE TABLE IF NOT EXISTS uploads (
    id SERIAL PRIMARY KEY,
    filename TEXT NOT NULL,
    file_path TEXT NOT NULL,
    file_size BIGINT NOT NULL,
    uploaded_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS objects (
    id SERIAL PRIMARY KEY,
    upload_id INTEGER NOT NULL REFERENCES uploads(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    object_id TEXT NOT NULL,
    vertices INTEGER,
    triangles INTEGER,
    extracted_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_objects_upload_id ON objects(upload_id);

-- Extend objects table for normalization tracking
ALTER TABLE objects ADD COLUMN IF NOT EXISTS bounds_min_x REAL;
ALTER TABLE objects ADD COLUMN IF NOT EXISTS bounds_min_y REAL;
ALTER TABLE objects ADD COLUMN IF NOT EXISTS bounds_min_z REAL;
ALTER TABLE objects ADD COLUMN IF NOT EXISTS bounds_max_x REAL;
ALTER TABLE objects ADD COLUMN IF NOT EXISTS bounds_max_y REAL;
ALTER TABLE objects ADD COLUMN IF NOT EXISTS bounds_max_z REAL;
ALTER TABLE objects ADD COLUMN IF NOT EXISTS normalized_at TIMESTAMP;
ALTER TABLE objects ADD COLUMN IF NOT EXISTS normalized_path TEXT;
ALTER TABLE objects ADD COLUMN IF NOT EXISTS transform_data JSONB;
ALTER TABLE objects ADD COLUMN IF NOT EXISTS normalization_status TEXT DEFAULT 'pending';
ALTER TABLE objects ADD COLUMN IF NOT EXISTS normalization_error TEXT;

-- Track normalization jobs
CREATE TABLE IF NOT EXISTS normalization_jobs (
    id SERIAL PRIMARY KEY,
    job_id TEXT UNIQUE NOT NULL,
    upload_id INTEGER NOT NULL REFERENCES uploads(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'pending',
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    log_path TEXT,
    error_message TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_normalization_jobs_job_id ON normalization_jobs(job_id);
CREATE INDEX IF NOT EXISTS idx_normalization_jobs_upload_id ON normalization_jobs(upload_id);

-- Filament profiles
CREATE TABLE IF NOT EXISTS filaments (
    id SERIAL PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    material TEXT NOT NULL,
    nozzle_temp INTEGER NOT NULL,
    bed_temp INTEGER NOT NULL,
    print_speed INTEGER,
    is_default BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

-- Bundles (groups of objects for printing)
CREATE TABLE IF NOT EXISTS bundles (
    id SERIAL PRIMARY KEY,
    bundle_id TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    filament_id INTEGER REFERENCES filaments(id),
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

-- Bundle objects (many-to-many between bundles and normalized objects)
CREATE TABLE IF NOT EXISTS bundle_objects (
    id SERIAL PRIMARY KEY,
    bundle_id INTEGER NOT NULL REFERENCES bundles(id) ON DELETE CASCADE,
    object_id INTEGER NOT NULL REFERENCES objects(id) ON DELETE CASCADE,
    added_at TIMESTAMP NOT NULL DEFAULT NOW(),
    UNIQUE(bundle_id, object_id)
);

CREATE INDEX IF NOT EXISTS idx_bundles_bundle_id ON bundles(bundle_id);
CREATE INDEX IF NOT EXISTS idx_bundle_objects_bundle_id ON bundle_objects(bundle_id);
CREATE INDEX IF NOT EXISTS idx_bundle_objects_object_id ON bundle_objects(object_id);

-- Slicing jobs
CREATE TABLE IF NOT EXISTS slicing_jobs (
    id SERIAL PRIMARY KEY,
    job_id TEXT UNIQUE NOT NULL,
    bundle_id INTEGER NOT NULL REFERENCES bundles(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'pending',
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    log_path TEXT,
    gcode_path TEXT,
    gcode_size BIGINT,
    estimated_time_seconds INTEGER,
    filament_used_mm REAL,
    error_message TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_slicing_jobs_job_id ON slicing_jobs(job_id);
CREATE INDEX IF NOT EXISTS idx_slicing_jobs_bundle_id ON slicing_jobs(bundle_id);

-- Track slicing metadata on bundles
ALTER TABLE bundles ADD COLUMN IF NOT EXISTS sliced_at TIMESTAMP;
ALTER TABLE bundles ADD COLUMN IF NOT EXISTS gcode_path TEXT;
ALTER TABLE bundles ADD COLUMN IF NOT EXISTS print_time_estimate INTEGER;
ALTER TABLE bundles ADD COLUMN IF NOT EXISTS filament_estimate REAL;

-- Track generated 3MF files for debugging (M9)
ALTER TABLE slicing_jobs ADD COLUMN IF NOT EXISTS three_mf_path TEXT;
