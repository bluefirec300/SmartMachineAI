CREATE TABLE IF NOT EXISTS equipment_aliases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    equipment_id INTEGER NOT NULL,
    alias TEXT NOT NULL,
    UNIQUE (equipment_id, alias),
    FOREIGN KEY (equipment_id)
        REFERENCES equipment(id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_equipment_aliases_alias
ON equipment_aliases(alias);

CREATE TABLE IF NOT EXISTS equipment_tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    equipment_id INTEGER NOT NULL,
    tag_id INTEGER NOT NULL,
    relationship_type TEXT NOT NULL
        CHECK (relationship_type IN ('main', 'related')),
    display_order INTEGER NOT NULL DEFAULT 0,
    UNIQUE (equipment_id, tag_id, relationship_type),
    FOREIGN KEY (equipment_id)
        REFERENCES equipment(id)
        ON DELETE CASCADE,
    FOREIGN KEY (tag_id)
        REFERENCES tags(id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_equipment_tags_equipment
ON equipment_tags(equipment_id);

CREATE INDEX IF NOT EXISTS idx_equipment_tags_tag
ON equipment_tags(tag_id);
