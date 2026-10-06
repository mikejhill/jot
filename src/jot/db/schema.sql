CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY, slug TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '', repo_path TEXT,
    aliases TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(aliases)),
    priority REAL NOT NULL DEFAULT 1 CHECK(priority > 0),
    archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0,1)),
    default_flow TEXT CHECK(default_flow IN ('planned','direct'))
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
    raw_input TEXT NOT NULL DEFAULT '', project_id INTEGER REFERENCES projects(id),
    type TEXT NOT NULL CHECK(type IN ('feature','bug','chore','research','idea')),
    criticality TEXT NOT NULL CHECK(criticality IN ('low','medium','high','critical')),
    status TEXT NOT NULL CHECK(status IN ('inbox','ready','planning','awaiting_approval',
        'executing','review','done','blocked','wont_do','archived')),
    flow TEXT CHECK(flow IN ('planned','direct')), repo_path TEXT, due_at TEXT,
    source TEXT NOT NULL CHECK(source IN ('cli','ui','agent')),
    parent_id INTEGER REFERENCES tasks(id),
    needs_enrichment INTEGER NOT NULL DEFAULT 1 CHECK(needs_enrichment IN (0,1)),
    claimed_by TEXT, lease_expires_at TEXT, lease_prior_status TEXT,
    blocked_prior_status TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    completed_at TEXT, deleted_at TEXT,
    CHECK((claimed_by IS NULL AND lease_expires_at IS NULL AND lease_prior_status IS NULL)
        OR (claimed_by IS NOT NULL AND lease_expires_at IS NOT NULL AND lease_prior_status IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS tasks_queue ON tasks(status, project_id, deleted_at);
CREATE INDEX IF NOT EXISTS tasks_lease ON tasks(lease_expires_at);
CREATE TABLE IF NOT EXISTS labels (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS task_labels (
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    label_id INTEGER NOT NULL REFERENCES labels(id) ON DELETE CASCADE,
    PRIMARY KEY(task_id, label_id)
);
CREATE TABLE IF NOT EXISTS task_events (
    id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    ts TEXT NOT NULL, actor TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('created','enriched','status','comment','plan',
        'question','answer','approval','run_log','result')),
    body TEXT NOT NULL CHECK(json_valid(body))
);
CREATE INDEX IF NOT EXISTS events_task ON task_events(task_id, id);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES tasks(id),
    backend TEXT NOT NULL, phase TEXT NOT NULL CHECK(phase IN ('plan','execute')),
    status TEXT NOT NULL, session_id TEXT, worktree TEXT, branch TEXT, summary TEXT,
    cost REAL, started_at TEXT NOT NULL, ended_at TEXT, model TEXT
);
CREATE TABLE IF NOT EXISTS cleanup_proposals (
    id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, items TEXT NOT NULL CHECK(json_valid(items)),
    status TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS tasks_fts USING fts5(title, description, raw_input, labels);
CREATE TRIGGER IF NOT EXISTS tasks_fts_insert AFTER INSERT ON tasks BEGIN
    INSERT INTO tasks_fts(rowid,title,description,raw_input,labels)
    VALUES(new.id,new.title,new.description,new.raw_input,'');
END;
CREATE TRIGGER IF NOT EXISTS tasks_fts_update AFTER UPDATE OF title,description,raw_input ON tasks BEGIN
    UPDATE tasks_fts SET title=new.title,description=new.description,raw_input=new.raw_input WHERE rowid=new.id;
END;
CREATE TRIGGER IF NOT EXISTS tasks_fts_delete AFTER DELETE ON tasks BEGIN
    DELETE FROM tasks_fts WHERE rowid=old.id;
END;
CREATE TRIGGER IF NOT EXISTS task_labels_fts_insert AFTER INSERT ON task_labels BEGIN
    UPDATE tasks_fts SET labels=(SELECT group_concat(l.name,' ') FROM labels l
        JOIN task_labels tl ON tl.label_id=l.id WHERE tl.task_id=new.task_id) WHERE rowid=new.task_id;
END;
CREATE TRIGGER IF NOT EXISTS task_labels_fts_delete AFTER DELETE ON task_labels BEGIN
    UPDATE tasks_fts SET labels=coalesce((SELECT group_concat(l.name,' ') FROM labels l
        JOIN task_labels tl ON tl.label_id=l.id WHERE tl.task_id=old.task_id),'') WHERE rowid=old.task_id;
END;
CREATE TRIGGER IF NOT EXISTS task_labels_fts_update AFTER UPDATE ON task_labels BEGIN
    UPDATE tasks_fts SET labels=coalesce((SELECT group_concat(l.name,' ') FROM labels l
        JOIN task_labels tl ON tl.label_id=l.id WHERE tl.task_id=tasks_fts.rowid),'')
        WHERE rowid IN (old.task_id,new.task_id);
END;
CREATE TRIGGER IF NOT EXISTS labels_fts_update AFTER UPDATE OF name ON labels BEGIN
    UPDATE tasks_fts SET labels=(SELECT group_concat(l.name,' ') FROM labels l
        JOIN task_labels tl ON tl.label_id=l.id WHERE tl.task_id=tasks_fts.rowid)
        WHERE rowid IN (SELECT task_id FROM task_labels WHERE label_id=new.id);
END;
