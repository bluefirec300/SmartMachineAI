PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS tag_addresses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tag_id INTEGER NOT NULL,
    driver TEXT NOT NULL,
    address TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1
        CHECK (enabled IN (0, 1)),

    FOREIGN KEY (tag_id)
        REFERENCES tags(id)
        ON DELETE CASCADE,

    UNIQUE (tag_id, driver)
);

CREATE INDEX IF NOT EXISTS idx_tag_addresses_tag_id
ON tag_addresses(tag_id);

CREATE INDEX IF NOT EXISTS idx_tag_addresses_driver
ON tag_addresses(driver);
