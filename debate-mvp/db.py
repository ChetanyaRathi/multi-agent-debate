import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "debate.db"


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def now():
    return datetime.now(timezone.utc).isoformat()


def init_db():
    conn = get_conn()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS posts (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            question   TEXT NOT NULL,
            status     TEXT NOT NULL DEFAULT 'open',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS comments (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id    INTEGER NOT NULL,
            agent_name TEXT NOT NULL,
            parent_id  INTEGER,
            body       TEXT NOT NULL,
            turn       INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY (post_id)   REFERENCES posts(id),
            FOREIGN KEY (parent_id) REFERENCES comments(id)
        );
        """
    )
    conn.commit()
    conn.close()


def create_post(question):
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO posts (question, created_at) VALUES (?, ?)",
        (question, now()),
    )
    conn.commit()
    pid = cur.lastrowid
    conn.close()
    return pid


def update_post_status(post_id, status):
    conn = get_conn()
    conn.execute("UPDATE posts SET status = ? WHERE id = ?", (status, post_id))
    conn.commit()
    conn.close()


def add_comment(post_id, agent_name, body, parent_id=None, turn=0):
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO comments (post_id, agent_name, parent_id, body, turn, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (post_id, agent_name, parent_id, body, turn, now()),
    )
    conn.commit()
    cid = cur.lastrowid
    conn.close()
    return cid


def get_post(post_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_comments(post_id):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM comments WHERE post_id = ? ORDER BY turn, id", (post_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_posts():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM posts ORDER BY id DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def last_comment_id(post_id):
    conn = get_conn()
    row = conn.execute(
        "SELECT id FROM comments WHERE post_id = ? ORDER BY id DESC LIMIT 1", (post_id,)
    ).fetchone()
    conn.close()
    return row["id"] if row else None
