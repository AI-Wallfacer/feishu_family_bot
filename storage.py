import base64
import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet, InvalidToken
from flask import g

import config

SCHEMA_VERSION = 1
MEMORY_TYPES = ("profile", "preference", "relationship", "reminder_hint")

_cipher = None


def utc_now():
    return datetime.now(timezone.utc)


def iso_utc(dt):
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def parse_iso_utc(value):
    if not value:
        return None
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def get_local_timezone(name=None):
    try:
        return ZoneInfo(name or config.DEFAULT_TIMEZONE)
    except Exception:
        return ZoneInfo("Asia/Shanghai")


def safe_int(value, default=0):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def split_models(value):
    if not value:
        return []
    raw_items = value if isinstance(value, list) else str(value).replace("\n", ",").split(",")
    models = []
    seen = set()
    for item in raw_items:
        model = str(item).strip()
        if model and model not in seen:
            seen.add(model)
            models.append(model)
    return models


def ensure_parent_dir(path):
    import os

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)


def get_cipher():
    global _cipher
    if _cipher is None:
        seed = config.SETTINGS_ENCRYPTION_KEY.encode("utf-8")
        key = base64.urlsafe_b64encode(hashlib.sha256(seed).digest())
        _cipher = Fernet(key)
    return _cipher


def encrypt_secret(value):
    if not value:
        return ""
    return get_cipher().encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt_secret(value):
    if not value:
        return ""
    try:
        return get_cipher().decrypt(value.encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError):
        return ""


def mask_secret(value):
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}{'*' * (len(value) - 8)}{value[-4:]}"


def open_db_connection():
    ensure_parent_dir(config.BOT_DB_PATH)
    conn = sqlite3.connect(config.BOT_DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def get_db():
    if "db" not in g:
        ensure_database_initialized()
        g.db = open_db_connection()
    return g.db


def close_db():
    db = g.pop("db", None)
    if db is not None:
        db.close()


def ensure_database_initialized():
    conn = open_db_connection()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS ai_groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                api_base TEXT NOT NULL DEFAULT '',
                api_key_encrypted TEXT NOT NULL DEFAULT '',
                models_json TEXT NOT NULL DEFAULT '[]',
                enabled INTEGER NOT NULL DEFAULT 1,
                priority INTEGER NOT NULL DEFAULT 0,
                last_model_refresh_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS memory_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                memory_type TEXT NOT NULL,
                content TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                source_message_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS schedules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                target_type TEXT NOT NULL DEFAULT 'chat_id',
                target_id TEXT NOT NULL,
                message_text TEXT NOT NULL,
                schedule_type TEXT NOT NULL,
                schedule_expr TEXT,
                timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
                next_run_at TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                is_running INTEGER NOT NULL DEFAULT 0,
                last_run_at TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS triggers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                trigger_type TEXT NOT NULL,
                keyword TEXT NOT NULL DEFAULT '',
                reply_text TEXT NOT NULL,
                priority INTEGER NOT NULL DEFAULT 0,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        conn.execute("UPDATE schedules SET is_running = 0 WHERE is_running != 0")
        import_default_groups(conn)
        conn.commit()
    finally:
        conn.close()


def import_default_groups(conn):
    if conn.execute("SELECT COUNT(*) FROM ai_groups").fetchone()[0] > 0:
        return
    now = iso_utc(utc_now())
    for index, group in enumerate(config.AI_GROUPS):
        conn.execute(
            """
            INSERT INTO ai_groups(
                name, api_base, api_key_encrypted, models_json, enabled, priority, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 1, ?, ?, ?)
            """,
            (
                group["name"],
                (group.get("base") or config.AI_API_BASE or "").strip(),
                encrypt_secret(group.get("key", "")),
                json.dumps(split_models(group.get("models", "")), ensure_ascii=False),
                index,
                now,
                now,
            ),
        )


def row_to_group(row, include_secret=True):
    secret = decrypt_secret(row["api_key_encrypted"])
    models = json.loads(row["models_json"] or "[]")
    return {
        "id": row["id"],
        "name": row["name"],
        "sort_order": row["priority"],
        "priority": row["priority"],
        "base_url": row["api_base"],
        "api_base": row["api_base"],
        "api_key": secret if include_secret else "",
        "api_key_masked": mask_secret(secret),
        "models": models,
        "models_display": ", ".join(models),
        "enabled": bool(row["enabled"]),
        "last_model_refresh_at": row["last_model_refresh_at"],
        "updated_at": row["updated_at"],
        "created_at": row["created_at"],
    }


def load_groups(include_disabled=True, include_secret=True, conn=None):
    close_conn = False
    if conn is None:
        conn = open_db_connection()
        close_conn = True
    try:
        sql = "SELECT * FROM ai_groups"
        if not include_disabled:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY priority ASC, id ASC"
        rows = conn.execute(sql).fetchall()
        return [row_to_group(row, include_secret=include_secret) for row in rows]
    finally:
        if close_conn:
            conn.close()


def get_group_by_id(group_id, conn=None):
    close_conn = False
    if conn is None:
        conn = open_db_connection()
        close_conn = True
    try:
        row = conn.execute("SELECT * FROM ai_groups WHERE id = ?", (group_id,)).fetchone()
        return row_to_group(row) if row else None
    finally:
        if close_conn:
            conn.close()


def get_group_by_name(name):
    conn = open_db_connection()
    try:
        row = conn.execute(
            "SELECT * FROM ai_groups WHERE lower(name) = lower(?) LIMIT 1",
            (name,),
        ).fetchone()
        return row_to_group(row) if row else None
    finally:
        conn.close()


def load_triggers(include_disabled=False):
    conn = open_db_connection()
    try:
        sql = "SELECT * FROM triggers"
        if not include_disabled:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY priority ASC, id ASC"
        rows = conn.execute(sql).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["match_mode"] = item["trigger_type"]
            item["sort_order"] = item["priority"]
            item["enabled"] = bool(item["enabled"])
            result.append(item)
        return result
    finally:
        conn.close()


def load_memories(user_id, chat_id, limit=3):
    conn = open_db_connection()
    try:
        rows = conn.execute(
            """
            SELECT * FROM memory_items
            WHERE active = 1 AND user_id = ? AND chat_id = ?
            ORDER BY updated_at DESC, id DESC
            LIMIT ?
            """,
            (user_id, chat_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def list_memories(search_query=""):
    db = get_db()
    sql = "SELECT * FROM memory_items"
    params = []
    if search_query:
        sql += " WHERE user_id LIKE ? OR chat_id LIKE ? OR content LIKE ?"
        params = [f"%{search_query}%", f"%{search_query}%", f"%{search_query}%"]
    sql += " ORDER BY updated_at DESC, id DESC"
    rows = db.execute(sql, params).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["enabled"] = bool(item["active"])
        result.append(item)
    return result


def save_memory_item(user_id, chat_id, memory_type, content, source_message_id):
    if memory_type not in MEMORY_TYPES or not content.strip():
        return
    conn = open_db_connection()
    try:
        now = iso_utc(utc_now())
        existing = conn.execute(
            """
            SELECT id FROM memory_items
            WHERE user_id = ? AND chat_id = ? AND memory_type = ? AND content = ? AND active = 1
            LIMIT 1
            """,
            (user_id, chat_id, memory_type, content.strip()),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE memory_items SET updated_at = ?, source_message_id = ? WHERE id = ?",
                (now, source_message_id, existing["id"]),
            )
        else:
            conn.execute(
                """
                INSERT INTO memory_items(
                    user_id, chat_id, memory_type, content, active, source_message_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 1, ?, ?, ?)
                """,
                (user_id, chat_id, memory_type, content.strip(), source_message_id, now, now),
            )
        conn.commit()
    finally:
        conn.close()


def format_schedule_next_run(schedule_type, schedule_expr, timezone_name, now_dt=None):
    now_dt = now_dt or utc_now()
    tz = get_local_timezone(timezone_name)
    local_now = now_dt.astimezone(tz)

    if schedule_type == "once":
        schedule_expr = (schedule_expr or "").strip()
        local_target = None
        parse_error = None
        for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M"):
            try:
                local_target = datetime.strptime(schedule_expr, fmt).replace(tzinfo=tz)
                break
            except ValueError as exc:
                parse_error = exc
        if local_target is None:
            raise ValueError("一次性任务时间格式应为 YYYY-MM-DDTHH:MM 或 YYYY-MM-DD HH:MM") from parse_error
        if local_target <= local_now:
            raise ValueError("一次性任务时间必须晚于当前时间")
        return iso_utc(local_target)

    if schedule_type == "daily":
        hour, minute = [safe_int(part) for part in schedule_expr.split(":")[:2]]
        local_target = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if local_target <= local_now:
            local_target += timedelta(days=1)
        return iso_utc(local_target)

    if schedule_type == "weekly":
        weekday_text, time_text = [part.strip() for part in schedule_expr.split(" ", 1)]
        target_weekday = safe_int(weekday_text, 0)
        hour, minute = [safe_int(part) for part in time_text.split(":")[:2]]
        local_target = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        local_target += timedelta(days=(target_weekday - local_target.weekday()) % 7)
        if local_target <= local_now:
            local_target += timedelta(days=7)
        return iso_utc(local_target)

    raise ValueError("不支持的定时类型")


def format_local_datetime_for_display(value, timezone_name):
    dt = parse_iso_utc(value)
    if not dt:
        return "-"
    return dt.astimezone(get_local_timezone(timezone_name)).strftime("%Y-%m-%d %H:%M")
