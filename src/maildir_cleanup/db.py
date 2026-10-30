"""SQLite inventory: message headers and body text, and the deletion plan. Everything here is derived from
the mail clone and the training set, so the file can be deleted and rebuilt with `scan`."""

import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    user TEXT NOT NULL,
    folder TEXT NOT NULL,          -- display name, INBOX for the top level
    kind TEXT NOT NULL,            -- inbox | sent | drafts | junk | trash | other
    dir TEXT NOT NULL,             -- folder directory relative to the mail root
    sub TEXT NOT NULL,             -- cur | new
    name TEXT NOT NULL,            -- filename including flags
    key TEXT NOT NULL,             -- filename before the flag separator
    relpath TEXT NOT NULL UNIQUE,
    flags TEXT NOT NULL DEFAULT '',
    size INTEGER,
    ts INTEGER,
    from_addr TEXT, from_name TEXT, from_domain TEXT, org TEXT,
    to_addrs TEXT, subject TEXT,
    message_id TEXT, in_reply_to TEXT, refs TEXT,
    list_id TEXT, list_unsub INTEGER, precedence TEXT, auto_submitted TEXT,
    return_domain TEXT, bulk_hdrs INTEGER,
    spam_score REAL, spam_flag INTEGER,
    content_type TEXT,
    error TEXT,
    protect TEXT                   -- reason this message is never deleted (set by analyze)
);
CREATE INDEX IF NOT EXISTS messages_user ON messages(user);
CREATE INDEX IF NOT EXISTS messages_org ON messages(org);

CREATE TABLE IF NOT EXISTS texts (
    message INTEGER PRIMARY KEY REFERENCES messages(id),
    text TEXT NOT NULL             -- first few KB of readable body text (set by extract)
);

-- Cache of model documents read from training/ copies (see model.labelled). Derived: safe to delete.
-- Labels themselves live in training/, not here.
CREATE TABLE IF NOT EXISTS example_docs (
    example TEXT PRIMARY KEY,      -- copy filename + TAB + folder kind
    doc TEXT NOT NULL              -- JSON [meta tokens, subject, body text]
);

CREATE TABLE IF NOT EXISTS plan (
    message INTEGER PRIMARY KEY REFERENCES messages(id),
    reason TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""

MESSAGE_FIELDS = (
    "user", "folder", "kind", "dir", "sub", "name", "key", "relpath", "flags", "size", "ts",
    "from_addr", "from_name", "from_domain", "org", "to_addrs", "subject",
    "message_id", "in_reply_to", "refs", "list_id", "list_unsub", "precedence", "auto_submitted",
    "return_domain", "bulk_hdrs", "spam_score", "spam_flag", "content_type", "error",
)


def connect(path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    return conn


def get_meta(conn, k, default=None):
    row = conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return row[0] if row else default


def set_meta(conn, k, v):
    conn.execute("INSERT OR REPLACE INTO meta(k, v) VALUES (?, ?)", (k, str(v)))
