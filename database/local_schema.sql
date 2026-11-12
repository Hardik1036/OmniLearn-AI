-- ============================================================================
-- OmniLearn AI — Local System Application SQLite Extensions
-- Executed AFTER database/schema.sql by local_app/local_db.py ONLY.
-- Do NOT execute on Cloudflare D1.
-- ============================================================================

PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;
PRAGMA temp_store = MEMORY;

-- Instant Local Full-Text Search (FTS5) across ALL ingested docs, web articles & YT transcripts
CREATE VIRTUAL TABLE IF NOT EXISTS sources_fts USING fts5(
    source_id UNINDEXED,
    course_id UNINDEXED,
    source_type UNINDEXED,
    title,
    raw_text_excerpt,
    tokenize = 'porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS trg_sources_ai AFTER INSERT ON course_sources BEGIN
    INSERT INTO sources_fts(source_id, course_id, source_type, title, raw_text_excerpt)
    VALUES (new.id, new.course_id, new.source_type, new.title, new.raw_text_excerpt);
END;

CREATE TRIGGER IF NOT EXISTS trg_sources_ad AFTER DELETE ON course_sources BEGIN
    DELETE FROM sources_fts WHERE source_id = old.id;
END;

CREATE TRIGGER IF NOT EXISTS trg_sources_au AFTER UPDATE ON course_sources BEGIN
    DELETE FROM sources_fts WHERE source_id = old.id;
    INSERT INTO sources_fts(source_id, course_id, source_type, title, raw_text_excerpt)
    VALUES (new.id, new.course_id, new.source_type, new.title, new.raw_text_excerpt);
END;

-- Idempotent backfill if course_sources was seeded prior to local_schema.sql
INSERT INTO sources_fts(source_id, course_id, source_type, title, raw_text_excerpt)
SELECT cs.id, cs.course_id, cs.source_type, cs.title, cs.raw_text_excerpt
FROM course_sources cs
WHERE NOT EXISTS (
    SELECT 1 FROM sources_fts f WHERE f.source_id = cs.id
);
