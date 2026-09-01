-- 001_create_rehearsal_items.sql
CREATE TABLE IF NOT EXISTS aos_rehearsal_items (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
