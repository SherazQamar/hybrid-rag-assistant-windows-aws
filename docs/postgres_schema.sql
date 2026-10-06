-- Target schema for the full project: PostgreSQL 16 + pgvector on a small AWS instance.
-- The POC uses SQLite (app/db.py) with the SAME tables; only the storage module changes.
-- Applied by migrations (Alembic or numbered SQL files) through the SSM tunnel - never a public port.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE users (
    id BIGSERIAL PRIMARY KEY,
    username CITEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin','user')),
    active BOOLEAN NOT NULL DEFAULT TRUE,
    totp_secret TEXT,                       -- encrypted at application level in production
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE documents (
    id BIGSERIAL PRIMARY KEY,
    owner_id BIGINT NOT NULL REFERENCES users(id),
    filename TEXT NOT NULL,
    file_type TEXT NOT NULL,
    visibility TEXT NOT NULL CHECK (visibility IN ('private','shared')),
    status TEXT NOT NULL,
    error TEXT,
    sha256 TEXT NOT NULL,
    stored_key TEXT,                        -- S3 key (SSE-KMS) or local path of the original
    size_bytes BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at TIMESTAMPTZ
);

CREATE TABLE index_versions (
    id BIGSERIAL PRIMARY KEY,
    profile_name TEXT NOT NULL,
    chunking JSONB NOT NULL,
    embed_provider TEXT NOT NULL,
    embed_model TEXT NOT NULL,
    embed_dim INT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('building','active','failed','retired')),
    progress REAL NOT NULL DEFAULT 0,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    activated_at TIMESTAMPTZ
);
-- at most one active version
CREATE UNIQUE INDEX one_active_version ON index_versions ((status)) WHERE status = 'active';

CREATE TABLE chunks (
    id BIGSERIAL PRIMARY KEY,
    doc_id BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    index_version BIGINT NOT NULL REFERENCES index_versions(id) ON DELETE CASCADE,
    ordinal INT NOT NULL,
    text TEXT NOT NULL,
    page INT,
    section TEXT,
    start_char INT NOT NULL,
    end_char INT NOT NULL,
    n_chars INT NOT NULL,
    n_tokens INT NOT NULL,
    embedding vector(384) NOT NULL,         -- dimension fixed per index version
    tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
    UNIQUE (doc_id, index_version, ordinal)
);
CREATE INDEX chunks_hnsw ON chunks USING hnsw (embedding vector_cosine_ops);   -- dense: cosine
CREATE INDEX chunks_tsv ON chunks USING gin (tsv);                             -- keyword: full-text
-- NOTE: PostgreSQL ranks keyword hits with ts_rank_cd (cover density), which is NOT BM25.
-- If true BM25 is required on Postgres, use the ParadeDB pg_search extension or compute BM25 in SQL.

-- chats, messages, memories, profiles, kv, usage_log, retrieval_log: same columns as app/db.py

-- Least privilege: the app connects as app_rw (DML only), migrations run as app_owner.
-- CREATE ROLE app_owner LOGIN; CREATE ROLE app_rw LOGIN;
-- GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO app_rw;
-- pg_hba.conf: hostssl only, scram-sha-256, listen on 127.0.0.1 (reached through the SSM tunnel).
