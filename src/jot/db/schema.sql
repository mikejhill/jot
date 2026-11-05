CREATE TABLE projects (
    id INTEGER PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE jots (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    profile TEXT NOT NULL,
    status TEXT NOT NULL,
    category TEXT NOT NULL CHECK(category IN ('open', 'active', 'done', 'cancelled')),
    project_id INTEGER REFERENCES projects(id),
    rank TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(metadata) AND json_type(metadata) = 'object'),
    version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    archived_at TEXT,
    deleted_at TEXT
);
CREATE INDEX jots_rank ON jots(deleted_at, archived_at, rank);
CREATE INDEX jots_profile_status ON jots(profile, status);
CREATE INDEX jots_category ON jots(category);
CREATE INDEX jots_project ON jots(project_id);
CREATE TABLE labels (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
CREATE TABLE jot_labels (
    jot_id INTEGER NOT NULL REFERENCES jots(id) ON DELETE CASCADE,
    label_id INTEGER NOT NULL REFERENCES labels(id) ON DELETE CASCADE,
    PRIMARY KEY(jot_id, label_id)
);
CREATE TABLE comments (
    id INTEGER PRIMARY KEY,
    jot_id INTEGER NOT NULL REFERENCES jots(id) ON DELETE CASCADE,
    author TEXT NOT NULL,
    body TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'comment',
    reply_to INTEGER REFERENCES comments(id),
    created_at TEXT NOT NULL,
    edited_at TEXT,
    deleted_at TEXT
);
CREATE INDEX comments_jot ON comments(jot_id, id);
CREATE TABLE links (
    src INTEGER NOT NULL REFERENCES jots(id) ON DELETE CASCADE,
    dst INTEGER NOT NULL REFERENCES jots(id) ON DELETE CASCADE,
    rel TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(src, dst, rel),
    CHECK(src <> dst)
);
CREATE INDEX links_dst ON links(dst);
-- Append-only history of committed changes. Also the feed plugins consume by id.
CREATE TABLE events (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    jot_id INTEGER REFERENCES jots(id) ON DELETE SET NULL,
    version INTEGER,
    actor TEXT NOT NULL,
    origin TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    reason TEXT,
    data TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(data))
);
CREATE INDEX events_jot ON events(jot_id, id);
CREATE VIRTUAL TABLE jots_fts USING fts5(title, body, comments);
CREATE TRIGGER jots_fts_insert AFTER INSERT ON jots BEGIN
    INSERT INTO jots_fts(rowid, title, body, comments) VALUES (new.id, new.title, new.body, '');
END;
CREATE TRIGGER jots_fts_update AFTER UPDATE OF title, body ON jots BEGIN
    UPDATE jots_fts SET title = new.title, body = new.body WHERE rowid = new.id;
END;
CREATE TRIGGER jots_fts_delete AFTER DELETE ON jots BEGIN
    DELETE FROM jots_fts WHERE rowid = old.id;
END;
CREATE TRIGGER comments_fts_insert AFTER INSERT ON comments BEGIN
    UPDATE jots_fts SET comments = coalesce((SELECT group_concat(body, ' ') FROM comments
        WHERE jot_id = new.jot_id AND deleted_at IS NULL), '') WHERE rowid = new.jot_id;
END;
CREATE TRIGGER comments_fts_update AFTER UPDATE OF body, deleted_at ON comments BEGIN
    UPDATE jots_fts SET comments = coalesce((SELECT group_concat(body, ' ') FROM comments
        WHERE jot_id = new.jot_id AND deleted_at IS NULL), '') WHERE rowid = new.jot_id;
END;
