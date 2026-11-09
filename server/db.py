"""SQLite persistence for sessions, messages, memories and usage."""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from typing import Iterator

from . import config

SCHEMA_VERSION = 2


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with db() as conn:
        # Older builds created this index with rowid, which some SQLite builds reject.
        conn.execute("DROP INDEX IF EXISTS idx_messages_session")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                workspace TEXT,
                model TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                pinned INTEGER NOT NULL DEFAULT 0,
                archived INTEGER NOT NULL DEFAULT 0,
                summary TEXT,
                summary_upto TEXT,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                total_prompt_tokens INTEGER NOT NULL DEFAULT 0,
                total_completion_tokens INTEGER NOT NULL DEFAULT 0,
                total_cost_usd REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                role TEXT NOT NULL,
                content TEXT NOT NULL DEFAULT '',
                raw TEXT NOT NULL DEFAULT '',
                reasoning TEXT NOT NULL DEFAULT '',
                tool_calls TEXT NOT NULL DEFAULT '[]',
                tool_name TEXT,
                tool_args TEXT,
                meta TEXT,
                attachments TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'complete',
                model TEXT,
                tokens INTEGER NOT NULL DEFAULT 0,
                prompt_tokens INTEGER NOT NULL DEFAULT 0,
                completion_tokens INTEGER NOT NULL DEFAULT 0,
                cost_usd REAL NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_messages_session_created ON messages(session_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_messages_created ON messages(created_at);
            CREATE TABLE IF NOT EXISTS memories (
                id TEXT PRIMARY KEY,
                scope TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(scope);
            CREATE TABLE IF NOT EXISTS kv (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            """
        )
        _ensure_columns(conn)
        conn.execute(
            "INSERT OR REPLACE INTO kv(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        # A crash while streaming should not leave a permanent spinner.
        conn.execute(
            "UPDATE messages SET status='interrupted' WHERE status='streaming'"
        )


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """Additive migrations for databases created by earlier builds."""
    session_cols = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
    for name, ddl in (
        ("total_prompt_tokens", "INTEGER NOT NULL DEFAULT 0"),
        ("total_completion_tokens", "INTEGER NOT NULL DEFAULT 0"),
        ("total_cost_usd", "REAL NOT NULL DEFAULT 0"),
    ):
        if name not in session_cols:
            conn.execute(f"ALTER TABLE sessions ADD COLUMN {name} {ddl}")
    message_cols = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
    for name, ddl in (
        ("attachments", "TEXT NOT NULL DEFAULT '[]'"),
        ("prompt_tokens", "INTEGER NOT NULL DEFAULT 0"),
        ("completion_tokens", "INTEGER NOT NULL DEFAULT 0"),
        ("cost_usd", "REAL NOT NULL DEFAULT 0"),
    ):
        if name not in message_cols:
            conn.execute(f"ALTER TABLE messages ADD COLUMN {name} {ddl}")


def _session_row(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "title": row["title"],
        "workspace": row["workspace"],
        "model": row["model"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "pinned": bool(row["pinned"]),
        "archived": bool(row["archived"]),
        "summary": row["summary"],
        "summary_upto": row["summary_upto"],
        "total_tokens": row["total_tokens"],
        "total_prompt_tokens": row["total_prompt_tokens"],
        "total_completion_tokens": row["total_completion_tokens"],
        "total_cost_usd": row["total_cost_usd"],
    }


def create_session(title: str = "New chat", workspace: str | None = None,
                   model: str | None = None) -> dict:
    sid = new_id("ses")
    ts = config.now()
    workspace = workspace or str(config.workspace_path())
    with db() as conn:
        conn.execute(
            "INSERT INTO sessions(id,title,workspace,model,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?)",
            (sid, title, workspace, model, ts, ts),
        )
        row = conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    return _session_row(row)


def list_sessions(query: str = "", limit: int = 300) -> list[dict]:
    query = (query or "").strip()
    with db() as conn:
        if query:
            like = f"%{query}%"
            rows = conn.execute(
                """
                SELECT s.*,
                       (SELECT COUNT(*) FROM messages m WHERE m.session_id=s.id) AS message_count,
                       (SELECT content FROM messages m WHERE m.session_id=s.id AND m.role='user'
                        ORDER BY m.rowid DESC LIMIT 1) AS preview
                FROM sessions s
                WHERE s.archived=0 AND (
                    s.title LIKE ? OR EXISTS(
                        SELECT 1 FROM messages m2 WHERE m2.session_id=s.id AND m2.content LIKE ?
                    )
                )
                ORDER BY s.pinned DESC, s.updated_at DESC LIMIT ?
                """,
                (like, like, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT s.*,
                       (SELECT COUNT(*) FROM messages m WHERE m.session_id=s.id) AS message_count,
                       (SELECT content FROM messages m WHERE m.session_id=s.id AND m.role='user'
                        ORDER BY m.rowid DESC LIMIT 1) AS preview
                FROM sessions s WHERE s.archived=0
                ORDER BY s.pinned DESC, s.updated_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
    out = []
    for row in rows:
        item = _session_row(row)
        item["message_count"] = row["message_count"]
        preview = (row["preview"] or "").strip().replace("\n", " ")
        item["preview"] = preview[:120]
        out.append(item)
    return out


def get_session(session_id: str) -> dict | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    return _session_row(row) if row else None


def touch_session(session_id: str, **fields) -> None:
    allowed = {"title", "workspace", "model", "pinned", "archived", "summary",
               "summary_upto", "total_tokens", "total_prompt_tokens",
               "total_completion_tokens", "total_cost_usd"}
    sets = []
    values = []
    for key, value in fields.items():
        if key in allowed:
            sets.append(f"{key}=?")
            values.append(value)
    sets.append("updated_at=?")
    values.append(config.now())
    values.append(session_id)
    with db() as conn:
        conn.execute(f"UPDATE sessions SET {', '.join(sets)} WHERE id=?", values)


def add_usage(session_id: str, prompt_tokens: int = 0, completion_tokens: int = 0,
              cost_usd: float = 0.0) -> dict:
    prompt_tokens = max(0, int(prompt_tokens or 0))
    completion_tokens = max(0, int(completion_tokens or 0))
    total = prompt_tokens + completion_tokens
    with db() as conn:
        conn.execute(
            """UPDATE sessions SET
                 total_tokens = total_tokens + ?,
                 total_prompt_tokens = total_prompt_tokens + ?,
                 total_completion_tokens = total_completion_tokens + ?,
                 total_cost_usd = total_cost_usd + ?,
                 updated_at = ?
               WHERE id = ?""",
            (total, prompt_tokens, completion_tokens, float(cost_usd or 0), config.now(), session_id),
        )
        row = conn.execute(
            "SELECT total_tokens,total_prompt_tokens,total_completion_tokens,total_cost_usd "
            "FROM sessions WHERE id=?",
            (session_id,),
        ).fetchone()
    return dict(row) if row else {}


def _recalc_session_usage(conn: sqlite3.Connection, session_id: str) -> None:
    row = conn.execute(
        """SELECT COALESCE(SUM(prompt_tokens),0) AS p,
                  COALESCE(SUM(completion_tokens),0) AS c,
                  COALESCE(SUM(cost_usd),0) AS cost
           FROM messages WHERE session_id=?""",
        (session_id,),
    ).fetchone()
    p, c = int(row["p"] or 0), int(row["c"] or 0)
    conn.execute(
        """UPDATE sessions SET total_prompt_tokens=?, total_completion_tokens=?,
               total_tokens=?, total_cost_usd=? WHERE id=?""",
        (p, c, p + c, float(row["cost"] or 0), session_id),
    )


def delete_session(session_id: str) -> None:
    with db() as conn:
        conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))


def _message_row(row: sqlite3.Row) -> dict:
    try:
        tool_calls = json.loads(row["tool_calls"] or "[]")
    except Exception:
        tool_calls = []
    try:
        meta = json.loads(row["meta"]) if row["meta"] else None
    except Exception:
        meta = None
    try:
        attachments = json.loads(row["attachments"] or "[]")
    except Exception:
        attachments = []
    return {
        "id": row["id"],
        "session_id": row["session_id"],
        "role": row["role"],
        "content": row["content"],
        "raw": row["raw"],
        "reasoning": row["reasoning"],
        "tool_calls": tool_calls,
        "tool_name": row["tool_name"],
        "tool_args": row["tool_args"],
        "meta": meta,
        "attachments": attachments if isinstance(attachments, list) else [],
        "status": row["status"],
        "model": row["model"],
        "tokens": row["tokens"],
        "prompt_tokens": row["prompt_tokens"],
        "completion_tokens": row["completion_tokens"],
        "cost_usd": row["cost_usd"],
        "created_at": row["created_at"],
        "rowid": row["rowid"] if "rowid" in row.keys() else None,
    }


def add_message(session_id: str, role: str, content: str = "", *, raw: str = "",
                reasoning: str = "", tool_calls: list | None = None,
                tool_name: str | None = None, tool_args: str | None = None,
                meta: dict | None = None, attachments: list | None = None,
                status: str = "complete", model: str | None = None,
                tokens: int = 0, prompt_tokens: int = 0,
                completion_tokens: int = 0, cost_usd: float = 0.0) -> dict:
    mid = new_id("msg")
    ts = config.now()
    with db() as conn:
        conn.execute(
            """INSERT INTO messages(id,session_id,role,content,raw,reasoning,tool_calls,
               tool_name,tool_args,meta,attachments,status,model,tokens,prompt_tokens,
               completion_tokens,cost_usd,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                mid, session_id, role, content, raw, reasoning,
                json.dumps(tool_calls or [], ensure_ascii=False),
                tool_name, tool_args,
                json.dumps(meta, ensure_ascii=False) if meta else None,
                json.dumps(attachments or [], ensure_ascii=False),
                status, model, tokens, int(prompt_tokens or 0),
                int(completion_tokens or 0), float(cost_usd or 0), ts,
            ),
        )
        conn.execute("UPDATE sessions SET updated_at=? WHERE id=?", (ts, session_id))
        row = conn.execute("SELECT rowid, * FROM messages WHERE id=?", (mid,)).fetchone()
    return _message_row(row)


def update_message(message_id: str, **fields) -> None:
    allowed = {"content", "raw", "reasoning", "tool_calls", "tool_name", "tool_args",
               "meta", "attachments", "status", "model", "tokens", "prompt_tokens",
               "completion_tokens", "cost_usd"}
    sets = []
    values = []
    for key, value in fields.items():
        if key not in allowed:
            continue
        if key in {"tool_calls", "meta", "attachments"}:
            value = json.dumps(value, ensure_ascii=False) if value is not None else None
        sets.append(f"{key}=?")
        values.append(value)
    if not sets:
        return
    values.append(message_id)
    with db() as conn:
        conn.execute(f"UPDATE messages SET {', '.join(sets)} WHERE id=?", values)


def get_messages(session_id: str, include_raw: bool = True) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT rowid, * FROM messages WHERE session_id=? ORDER BY rowid",
            (session_id,),
        ).fetchall()
    out = [_message_row(row) for row in rows]
    if not include_raw:
        for item in out:
            item.pop("raw", None)
    return out


def get_message(message_id: str) -> dict | None:
    with db() as conn:
        row = conn.execute("SELECT rowid, * FROM messages WHERE id=?", (message_id,)).fetchone()
    return _message_row(row) if row else None


def delete_message(message_id: str) -> None:
    with db() as conn:
        row = conn.execute(
            "SELECT session_id FROM messages WHERE id=?", (message_id,)
        ).fetchone()
        if not row:
            return
        conn.execute("DELETE FROM messages WHERE id=?", (message_id,))
        _recalc_session_usage(conn, row["session_id"])
        conn.execute("UPDATE sessions SET updated_at=? WHERE id=?", (config.now(), row["session_id"]))


def delete_messages_from(session_id: str, message_id: str, include: bool = True) -> None:
    with db() as conn:
        target = conn.execute(
            "SELECT rowid FROM messages WHERE id=? AND session_id=?",
            (message_id, session_id),
        ).fetchone()
        if not target:
            return
        op = ">=" if include else ">"
        conn.execute(
            f"DELETE FROM messages WHERE session_id=? AND rowid {op} ?",
            (session_id, target["rowid"]),
        )
        _recalc_session_usage(conn, session_id)
        conn.execute("UPDATE sessions SET updated_at=? WHERE id=?", (config.now(), session_id))


def last_user_message(session_id: str) -> dict | None:
    with db() as conn:
        row = conn.execute(
            "SELECT rowid, * FROM messages WHERE session_id=? AND role='user' "
            "ORDER BY rowid DESC LIMIT 1",
            (session_id,),
        ).fetchone()
    return _message_row(row) if row else None


def message_count(session_id: str) -> int:
    with db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE session_id=?", (session_id,)
        ).fetchone()
    return int(row["n"])


def list_memories(workspace: str | None = None, query: str = "") -> list[dict]:
    workspace = workspace or str(config.workspace_path())
    with db() as conn:
        if query.strip():
            like = f"%{query.strip()}%"
            rows = conn.execute(
                "SELECT * FROM memories WHERE scope IN ('global', ?) AND content LIKE ? "
                "ORDER BY created_at DESC LIMIT 200",
                (workspace, like),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM memories WHERE scope IN ('global', ?) ORDER BY created_at DESC LIMIT 500",
                (workspace,),
            ).fetchall()
    return [dict(row) for row in rows]


def add_memory(content: str, scope: str = "global") -> dict:
    mid = new_id("mem")
    ts = config.now()
    with db() as conn:
        conn.execute(
            "INSERT INTO memories(id,scope,content,created_at) VALUES(?,?,?,?)",
            (mid, scope, content.strip(), ts),
        )
    return {"id": mid, "scope": scope, "content": content.strip(), "created_at": ts}


def delete_memory(memory_id: str) -> None:
    with db() as conn:
        conn.execute("DELETE FROM memories WHERE id=?", (memory_id,))


def get_kv(key: str, default: str | None = None) -> str | None:
    with db() as conn:
        row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_kv(key: str, value: str) -> None:
    with db() as conn:
        conn.execute("INSERT OR REPLACE INTO kv(key,value) VALUES(?,?)", (key, value))


def usage_summary(session_id: str | None = None) -> dict:
    """Aggregate token and hosted-equivalent cost statistics."""
    where = "WHERE m.session_id=?" if session_id else ""
    args = (session_id,) if session_id else ()
    with db() as conn:
        totals = conn.execute(
            f"""SELECT COALESCE(SUM(m.prompt_tokens),0) AS prompt,
                       COALESCE(SUM(m.completion_tokens),0) AS completion,
                       COALESCE(SUM(m.cost_usd),0) AS cost,
                       COUNT(CASE WHEN m.role='assistant' THEN 1 END) AS responses
                FROM messages m {where}""",
            args,
        ).fetchone()
        by_model = conn.execute(
            f"""SELECT COALESCE(m.model,'unknown') AS model,
                       COALESCE(SUM(m.prompt_tokens),0) AS prompt,
                       COALESCE(SUM(m.completion_tokens),0) AS completion,
                       COALESCE(SUM(m.cost_usd),0) AS cost,
                       COUNT(*) AS responses
                FROM messages m {where + ' AND ' if where else 'WHERE '} m.role='assistant'
                GROUP BY m.model ORDER BY cost DESC""",
            args,
        ).fetchall()
        sessions = conn.execute(
            """SELECT COUNT(*) AS n, COALESCE(SUM(total_tokens),0) AS tokens,
                      COALESCE(SUM(total_cost_usd),0) AS cost
               FROM sessions"""
        ).fetchone()
    prompt = int(totals["prompt"] or 0)
    completion = int(totals["completion"] or 0)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "cost_usd": round(float(totals["cost"] or 0), 6),
        "responses": int(totals["responses"] or 0),
        "sessions": int(sessions["n"] or 0),
        "session_tokens": int(sessions["tokens"] or 0),
        "session_cost_usd": round(float(sessions["cost"] or 0), 6),
        "by_model": [
            {
                "model": row["model"],
                "prompt_tokens": int(row["prompt"] or 0),
                "completion_tokens": int(row["completion"] or 0),
                "total_tokens": int(row["prompt"] or 0) + int(row["completion"] or 0),
                "cost_usd": round(float(row["cost"] or 0), 6),
                "responses": int(row["responses"] or 0),
            }
            for row in by_model
        ],
    }
