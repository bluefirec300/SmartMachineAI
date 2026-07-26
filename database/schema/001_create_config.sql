PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS equipment (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    description TEXT
);

CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    equipment_id INTEGER,
    tag_name TEXT NOT NULL UNIQUE,
    description TEXT,
    driver TEXT,
    address TEXT,
    data_type TEXT,
    unit TEXT,
    enabled INTEGER NOT NULL DEFAULT 1
        CHECK (enabled IN (0, 1)),
    FOREIGN KEY (equipment_id)
        REFERENCES equipment(id)
        ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_tags_equipment_id
ON tags(equipment_id);

CREATE INDEX IF NOT EXISTS idx_tags_driver
ON tags(driver);

CREATE TABLE IF NOT EXISTS thresholds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tag_name TEXT NOT NULL UNIQUE,
    low_warning REAL,
    low_alarm REAL,
    high_warning REAL,
    high_alarm REAL,
    FOREIGN KEY (tag_name)
        REFERENCES tags(tag_name)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS communication (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    machine_name TEXT NOT NULL UNIQUE,
    driver TEXT NOT NULL,
    ip TEXT,
    port INTEGER,
    plc_node INTEGER,
    pc_node INTEGER,
    timeout REAL
);

CREATE TABLE IF NOT EXISTS ai_settings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_name TEXT NOT NULL UNIQUE,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    history_default INTEGER NOT NULL DEFAULT 1,
    temperature REAL
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL
        CHECK (
            role IN (
                'operator',
                'maintenance',
                'engineer',
                'administrator'
            )
        ),
    password_hash TEXT,
    enabled INTEGER NOT NULL DEFAULT 1
        CHECK (enabled IN (0, 1))
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    username TEXT NOT NULL,
    action TEXT NOT NULL,
    entity_type TEXT,
    entity_name TEXT,
    old_value TEXT,
    new_value TEXT,
    details TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_log_timestamp
ON audit_log(timestamp);

CREATE INDEX IF NOT EXISTS idx_audit_log_username
ON audit_log(username);
