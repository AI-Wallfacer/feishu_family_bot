import base64
import hmac
import json
import queue
import re
import secrets
import threading
import time
import traceback
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import timedelta
from functools import wraps

import requests
from cachetools import TTLCache
from flask import Flask, Response, abort, flash, jsonify, redirect, render_template, request, session, url_for

import config
from storage import (
    MEMORY_TYPES,
    close_db,
    encrypt_secret,
    ensure_database_initialized,
    format_local_datetime_for_display,
    format_schedule_next_run,
    get_db,
    get_group_by_id,
    get_group_by_name,
    iso_utc,
    list_memories,
    load_groups,
    load_memories,
    load_triggers,
    open_db_connection,
    safe_int,
    save_memory_item,
    split_models,
    utc_now,
)

app = Flask(__name__)
app.secret_key = config.FLASK_SECRET_KEY
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=config.SESSION_LIFETIME_HOURS),
)

MAX_MSG_LEN = 4000
LOGIN_RATE_LIMIT = 5
MEMORY_EXTRACTION_PROMPT = """你是一个长期记忆提炼器。
只从单条群聊消息里提取最多 3 条适合长期记住的事实。
允许的 type 只有：profile、preference、relationship、reminder_hint。
只返回 JSON 数组，格式为 {"type":"...","content":"..."}，没有内容就返回 []。
"""

processed_messages = TTLCache(maxsize=2000, ttl=300)
chat_history = TTLCache(maxsize=500, ttl=1800)
user_model_choice = TTLCache(maxsize=500, ttl=1800)
login_attempts = TTLCache(maxsize=200, ttl=600)
recent_queue_events = deque(maxlen=20)

_dedup_lock = threading.Lock()
_history_lock = threading.Lock()
_model_choice_lock = threading.Lock()
_token_lock = threading.Lock()
_runtime_lock = threading.Lock()

BOT_OPEN_ID = None
_runtime_started = False
_token_cache = {"token": None, "expire_at": 0}


@dataclass
class QueueTask:
    task_id: str
    event_data: dict
    message_id: str
    reply_message_id: str
    sender_id: str
    chat_id: str
    chat_type: str
    preview_text: str
    enqueued_at: float


message_queue = queue.Queue()
queue_lock = threading.Lock()
pending_tasks = OrderedDict()
current_task = None
worker_thread = None
scheduler_thread = None
shutdown_event = threading.Event()


def current_csrf_token():
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_hex(16)
        session["csrf_token"] = token
    return token


def admin_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if not session.get("admin_logged_in"):
            flash("请先登录后台。", "warning")
            return redirect(url_for("admin_login"))
        return view_func(*args, **kwargs)

    return wrapped


def build_admin_snapshot(db):
    groups = []
    for group in load_groups(include_disabled=True, include_secret=True, conn=db):
        groups.append(
            {
                "name": group["name"],
                "api_base": group["base_url"],
                "api_key": group["api_key"],
                "models": group["models"],
                "enabled": bool(group["enabled"]),
                "priority": safe_int(group["priority"], 0),
                "last_model_refresh_at": group.get("last_model_refresh_at"),
            }
        )

    memory_items = []
    for row in db.execute(
        """
        SELECT user_id, chat_id, memory_type, content, active, source_message_id, created_at, updated_at
        FROM memory_items
        ORDER BY id ASC
        """
    ).fetchall():
        memory_items.append(
            {
                "user_id": row["user_id"],
                "chat_id": row["chat_id"],
                "memory_type": row["memory_type"],
                "content": row["content"],
                "active": bool(row["active"]),
                "source_message_id": row["source_message_id"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        )

    triggers = []
    for row in db.execute(
        """
        SELECT name, trigger_type, keyword, reply_text, priority, enabled, created_at, updated_at
        FROM triggers
        ORDER BY priority ASC, id ASC
        """
    ).fetchall():
        triggers.append(
            {
                "name": row["name"],
                "trigger_type": row["trigger_type"],
                "keyword": row["keyword"],
                "reply_text": row["reply_text"],
                "priority": safe_int(row["priority"], 0),
                "enabled": bool(row["enabled"]),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        )

    schedules = []
    for row in db.execute(
        """
        SELECT name, target_type, target_id, message_text, schedule_type, schedule_expr, timezone,
               enabled, next_run_at, last_run_at, last_error, created_at, updated_at
        FROM schedules
        ORDER BY id ASC
        """
    ).fetchall():
        schedules.append(
            {
                "name": row["name"],
                "target_type": row["target_type"],
                "target_id": row["target_id"],
                "message_text": row["message_text"],
                "schedule_type": row["schedule_type"],
                "schedule_expr": row["schedule_expr"],
                "timezone": row["timezone"],
                "enabled": bool(row["enabled"]),
                "next_run_at": row["next_run_at"],
                "last_run_at": row["last_run_at"],
                "last_error": row["last_error"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        )

    return {
        "snapshot_version": 1,
        "exported_at": iso_utc(utc_now()),
        "data": {
            "ai_groups": groups,
            "memory_items": memory_items,
            "triggers": triggers,
            "schedules": schedules,
        },
    }


def parse_admin_snapshot(file_storage):
    if not file_storage or not file_storage.filename:
        raise ValueError("请选择要导入的 JSON 备份文件。")
    raw = file_storage.read()
    if not raw:
        raise ValueError("备份文件内容为空。")
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except UnicodeDecodeError as exc:
        raise ValueError("备份文件不是有效的 UTF-8 JSON。") from exc
    except json.JSONDecodeError as exc:
        raise ValueError("备份文件不是合法的 JSON。") from exc
    if not isinstance(payload, dict):
        raise ValueError("备份文件格式不正确。")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("备份文件缺少 data 字段。")
    return data


def restore_admin_snapshot(db, data):
    now_iso = iso_utc(utc_now())
    tables = {
        "ai_groups": data.get("ai_groups", []),
        "memory_items": data.get("memory_items", []),
        "triggers": data.get("triggers", []),
        "schedules": data.get("schedules", []),
    }
    for value in tables.values():
        if not isinstance(value, list):
            raise ValueError("备份文件中的表数据格式不正确。")

    db.execute("DELETE FROM schedules")
    db.execute("DELETE FROM triggers")
    db.execute("DELETE FROM memory_items")
    db.execute("DELETE FROM ai_groups")

    imported = {"ai_groups": 0, "memory_items": 0, "triggers": 0, "schedules": 0}
    disabled_once = 0

    for item in tables["ai_groups"]:
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        models = split_models(item.get("models", []))
        db.execute(
            """
            INSERT INTO ai_groups(name, api_base, api_key_encrypted, models_json, enabled, priority, last_model_refresh_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name,
                str(item.get("api_base", "")).strip(),
                encrypt_secret(str(item.get("api_key", "")).strip()),
                json.dumps(models, ensure_ascii=False),
                1 if item.get("enabled", True) else 0,
                safe_int(item.get("priority"), 0),
                item.get("last_model_refresh_at"),
                str(item.get("created_at") or now_iso),
                str(item.get("updated_at") or now_iso),
            ),
        )
        imported["ai_groups"] += 1

    for item in tables["memory_items"]:
        memory_type = str(item.get("memory_type", "")).strip()
        user_id = str(item.get("user_id", "")).strip()
        chat_id = str(item.get("chat_id", "")).strip()
        content = str(item.get("content", "")).strip()
        if not user_id or not chat_id or not content or memory_type not in MEMORY_TYPES:
            continue
        db.execute(
            """
            INSERT INTO memory_items(user_id, chat_id, memory_type, content, active, source_message_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                chat_id,
                memory_type,
                content,
                1 if item.get("active", True) else 0,
                item.get("source_message_id"),
                str(item.get("created_at") or now_iso),
                str(item.get("updated_at") or now_iso),
            ),
        )
        imported["memory_items"] += 1

    for item in tables["triggers"]:
        name = str(item.get("name", "")).strip()
        trigger_type = str(item.get("trigger_type", "")).strip() or "keyword"
        keyword = str(item.get("keyword", "")).strip() if trigger_type == "keyword" else ""
        reply_text = str(item.get("reply_text", "")).strip()
        if not name or not reply_text or trigger_type not in ("keyword", "all"):
            continue
        if trigger_type == "keyword" and not keyword:
            continue
        db.execute(
            """
            INSERT INTO triggers(name, trigger_type, keyword, reply_text, priority, enabled, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name,
                trigger_type,
                keyword,
                reply_text,
                safe_int(item.get("priority"), 0),
                1 if item.get("enabled", True) else 0,
                str(item.get("created_at") or now_iso),
                str(item.get("updated_at") or now_iso),
            ),
        )
        imported["triggers"] += 1

    for item in tables["schedules"]:
        name = str(item.get("name", "")).strip()
        target_id = str(item.get("target_id", "")).strip()
        message_text = str(item.get("message_text", "")).strip()
        schedule_type = str(item.get("schedule_type", "")).strip() or "once"
        schedule_expr = str(item.get("schedule_expr", "")).strip()
        timezone_name = str(item.get("timezone", "")).strip() or config.DEFAULT_TIMEZONE
        enabled = bool(item.get("enabled", True))
        next_run_at = None
        last_error = item.get("last_error")
        if not name or not target_id or not message_text or schedule_type not in ("once", "daily", "weekly"):
            continue
        if enabled:
            try:
                next_run_at = format_schedule_next_run(schedule_type, schedule_expr, timezone_name)
                last_error = None
            except Exception:
                enabled = False
                last_error = "导入时任务时间已过期或格式不正确，已自动停用。"
                if schedule_type == "once":
                    disabled_once += 1
        db.execute(
            """
            INSERT INTO schedules(name, target_type, target_id, message_text, schedule_type, schedule_expr, timezone, next_run_at, enabled, is_running, last_run_at, last_error, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
            """,
            (
                name,
                str(item.get("target_type", "")).strip() or "chat_id",
                target_id,
                message_text,
                schedule_type,
                schedule_expr,
                timezone_name,
                next_run_at,
                1 if enabled else 0,
                item.get("last_run_at"),
                last_error,
                str(item.get("created_at") or now_iso),
                str(item.get("updated_at") or now_iso),
            ),
        )
        imported["schedules"] += 1

    return imported, disabled_once


@app.context_processor
def inject_template_globals():
    return {
        "csrf_token": current_csrf_token() if request.path.startswith("/admin") else "",
        "brand_name": "飞书 Bot 管理后台",
        "brand_subtitle": "轻量服务端渲染，适合 512MB 小机部署",
        "instance_name": "Render / 单实例",
        "current_user": "管理员" if session.get("admin_logged_in") else "未登录",
        "footer_text": "后台只做轻量配置管理，不依赖前端框架。",
    }


@app.teardown_appcontext
def teardown_db(_error=None):
    close_db()


def require_admin_csrf():
    token = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token", "")
    if not token or not hmac.compare_digest(token, session.get("csrf_token", "")):
        abort(400)


def get_client_ip():
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or "unknown"


def initialize_runtime():
    global _runtime_started
    if _runtime_started:
        return
    with _runtime_lock:
        if _runtime_started:
            return
        ensure_database_initialized()
        start_background_threads()
        get_bot_open_id()
        _runtime_started = True


@app.before_request
def ensure_init():
    initialize_runtime()
    if request.path.startswith("/admin"):
        current_csrf_token()
        if request.method == "POST":
            require_admin_csrf()


def get_tenant_access_token():
    if not config.FEISHU_APP_ID or not config.FEISHU_APP_SECRET:
        return None
    with _token_lock:
        now_ts = time.time()
        if _token_cache["token"] and now_ts < _token_cache["expire_at"] - 300:
            return _token_cache["token"]
        try:
            response = requests.post(
                "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": config.FEISHU_APP_ID, "app_secret": config.FEISHU_APP_SECRET},
                timeout=10,
            )
            data = response.json()
            token = data.get("tenant_access_token")
            if not token:
                print(f"[Token] 获取失败: {data}")
                return _token_cache["token"]
            _token_cache["token"] = token
            _token_cache["expire_at"] = now_ts + data.get("expire", 7200)
            return token
        except Exception as exc:
            print(f"[Token] 请求异常: {exc}")
            return _token_cache["token"]


def get_bot_open_id():
    global BOT_OPEN_ID
    if BOT_OPEN_ID:
        return BOT_OPEN_ID
    token = get_tenant_access_token()
    if not token:
        return None
    try:
        response = requests.get(
            "https://open.feishu.cn/open-apis/bot/v3/info",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        data = response.json()
        if data.get("code") == 0:
            BOT_OPEN_ID = data["bot"]["open_id"]
    except Exception as exc:
        print(f"[Bot] 获取 open_id 异常: {exc}")
    return BOT_OPEN_ID


def feishu_headers():
    token = get_tenant_access_token()
    if not token:
        return None
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def build_card(title, body, status="blue", meta=None):
    payload = {
        "config": {"wide_screen_mode": True},
        "header": {"template": status, "title": {"tag": "plain_text", "content": title[:80]}},
        "elements": [{"tag": "markdown", "content": body or " "}],
    }
    if meta:
        payload["elements"].append(
            {"tag": "note", "elements": [{"tag": "plain_text", "content": line[:200]} for line in meta if line]}
        )
    return json.dumps(payload, ensure_ascii=False)


def reply_card(message_id, title, body, status="blue", meta=None):
    try:
        headers = feishu_headers()
        if not headers:
            return None
        response = requests.post(
            f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reply",
            headers=headers,
            json={"msg_type": "interactive", "content": build_card(title, body, status, meta)},
            timeout=10,
        )
        data = response.json()
        if data.get("code") == 0:
            return data.get("data", {}).get("message_id")
        print(f"[回复失败] {data}")
    except Exception as exc:
        print(f"[回复异常] {exc}")
    return None


def update_card(message_id, title, body, status="blue", meta=None):
    try:
        headers = feishu_headers()
        if not headers:
            return
        response = requests.patch(
            f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}",
            headers=headers,
            json={"msg_type": "interactive", "content": build_card(title, body, status, meta)},
            timeout=10,
        )
        data = response.json()
        if data.get("code") != 0:
            print(f"[更新失败] {data}")
    except Exception as exc:
        print(f"[更新异常] {exc}")


def send_card_to_target(receive_id_type, receive_id, title, body, status="blue", meta=None):
    try:
        headers = feishu_headers()
        if not headers:
            return None, "未获取到飞书 token"
        response = requests.post(
            f"https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type={receive_id_type}",
            headers=headers,
            json={
                "receive_id": receive_id,
                "msg_type": "interactive",
                "content": build_card(title, body, status, meta),
            },
            timeout=10,
        )
        data = response.json()
        if data.get("code") == 0:
            return data.get("data", {}).get("message_id"), None
        return None, data.get("msg") or "发送失败"
    except Exception as exc:
        return None, str(exc)


def looks_like_math_text(text):
    return any(token in text for token in ("$", r"\(", r"\[", r"\frac", r"\sqrt", r"\sum", r"\int"))


def clean_math_format(text):
    text = re.sub(r"\$\$(.+?)\$\$", lambda m: f"\n{m.group(1).strip()}\n", text, flags=re.S)
    text = re.sub(r"\$(.+?)\$", lambda m: m.group(1).strip(), text, flags=re.S)
    text = re.sub(r"\\frac\s*\{([^{}]+)\}\s*\{([^{}]+)\}", r"(\1)/(\2)", text)
    text = re.sub(r"\\sqrt\s*\{([^{}]+)\}", r"sqrt(\1)", text)
    text = re.sub(r"\\([A-Za-z]+)", r"\1", text)
    return text.replace("{", "(").replace("}", ")").strip()


def truncate(text, max_len=MAX_MSG_LEN):
    if looks_like_math_text(text):
        text = clean_math_format(text)
    return text if len(text) <= max_len else text[: max_len - 20] + "\n\n...(消息过长已截断)"


def download_feishu_image(message_id, image_key):
    try:
        token = get_tenant_access_token()
        if not token:
            return None
        response = requests.get(
            f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/resources/{image_key}?type=image",
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        if response.status_code == 200 and response.content:
            return f"data:{response.headers.get('Content-Type', 'image/jpeg')};base64,{base64.b64encode(response.content).decode('utf-8')}"
    except Exception as exc:
        print(f"[图片下载异常] {exc}")
    return None


def extract_message_context(event_data, download_images=False):
    message = event_data.get("message", {})
    msg_type = message.get("message_type", "text")
    sender_id = event_data.get("sender", {}).get("sender_id", {}).get("open_id", "unknown")
    chat_type = message.get("chat_type", "")
    mentions = message.get("mentions", [])
    try:
        content = json.loads(message.get("content", "{}"))
    except json.JSONDecodeError:
        content = {}

    text = ""
    image_keys = []
    images = []
    if msg_type == "text":
        text = content.get("text", "").strip()
    elif msg_type == "image":
        if content.get("image_key"):
            image_keys = [content["image_key"]]
        text = "请描述这张图片"
    elif msg_type == "post":
        for line in content.get("content", []):
            for elem in line:
                if elem.get("tag") == "text":
                    text += elem.get("text", "")
                elif elem.get("tag") == "img" and elem.get("image_key"):
                    image_keys.append(elem["image_key"])
        text = text.strip()
        if image_keys and not text:
            text = "请描述这些图片" if len(image_keys) > 1 else "请描述这张图片"
    else:
        return {"supported": False, "reason": "不支持的消息类型"}

    if chat_type == "group":
        bot_mentioned = False
        for mention in mentions:
            if mention.get("id", {}).get("open_id") == BOT_OPEN_ID:
                bot_mentioned = True
            if mention.get("key"):
                text = text.replace(mention["key"], "").strip()
        if not bot_mentioned:
            return {"supported": False, "reason": "群聊未@机器人"}

    if download_images:
        for key in image_keys:
            image_url = download_feishu_image(message.get("message_id"), key)
            if image_url:
                images.append(image_url)

    if not text and not image_keys:
        return {"supported": False, "reason": "空消息"}

    return {
        "supported": True,
        "message_id": message.get("message_id"),
        "chat_id": message.get("chat_id"),
        "chat_type": chat_type,
        "msg_type": msg_type,
        "sender_id": sender_id,
        "text": text,
        "context_key": f"{sender_id}_{message.get('chat_id')}",
        "preview_text": truncate(text or "[图片消息]", max_len=60),
        "image_data_urls": images,
    }


def discover_models_for_group(group):
    if not group["api_key"] or not group["base_url"]:
        raise ValueError("分组缺少 API Key 或 Base URL")
    response = requests.get(
        f"{group['base_url'].rstrip('/')}/v1/models",
        headers={"Authorization": f"Bearer {group['api_key']}"},
        timeout=20,
    )
    data = response.json()
    models = split_models([item["id"] for item in data.get("data", []) if isinstance(item, dict) and item.get("id")])
    if not models:
        raise ValueError("未发现可用模型")
    return models


def call_ai(messages, preferred_group_name=None, max_tokens=None, system_prompt=None, extra_system_notes=None):
    groups = load_groups(include_disabled=False, include_secret=True)
    targets = []
    preferred_match = None
    for group in groups:
        if preferred_group_name and group["name"].lower() != preferred_group_name.lower():
            continue
        if preferred_group_name:
            preferred_match = group
        if group["api_key"] and group["base_url"] and group["models"]:
            targets.append(group)
    if preferred_group_name and preferred_match is None:
        return {"text": "当前指定模型分组不存在或已停用，请使用 /auto 恢复自动切换。", "group": preferred_group_name, "model": None}
    if preferred_group_name and not targets:
        return {"text": "当前指定模型分组配置不完整，请在后台补全模型或使用 /auto 恢复自动切换。", "group": preferred_group_name, "model": None}
    if not preferred_group_name:
        targets = [group for group in groups if group["api_key"] and group["base_url"] and group["models"]]

    for group in targets:
        for model in group["models"]:
            try:
                payload_messages = [{"role": "system", "content": system_prompt or config.SYSTEM_PROMPT}]
                if extra_system_notes:
                    payload_messages.extend({"role": "system", "content": note} for note in extra_system_notes if note)
                payload_messages.extend(messages)
                response = requests.post(
                    f"{group['base_url'].rstrip('/')}/v1/chat/completions",
                    headers={"Authorization": f"Bearer {group['api_key']}", "Content-Type": "application/json"},
                    json={"model": model, "max_tokens": max_tokens or config.AI_MAX_TOKENS, "messages": payload_messages},
                    timeout=120,
                )
                data = response.json()
                choices = data.get("choices") or []
                if data.get("error") or not choices:
                    continue
                return {"text": choices[0]["message"]["content"], "group": group["name"], "model": model}
            except Exception as exc:
                print(f"[AI调用失败 {group['name']}/{model}] {exc}")
    return {"text": "抱歉，所有模型都无法回复，请稍后再试。", "group": None, "model": None}


def should_extract_memory(text):
    return any(marker in text for marker in ("我叫", "我是", "我喜欢", "我不喜欢", "记住", "提醒我", "我家", "我习惯", "我的生日"))


def parse_json_array(text):
    try:
        data = json.loads(text)
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        match = re.search(r"\[[\s\S]*\]", text)
        if not match:
            return []
        try:
            data = json.loads(match.group(0))
            return data if isinstance(data, list) else []
        except json.JSONDecodeError:
            return []


def extract_memory_items(text, preferred_group_name):
    result = call_ai(
        [{"role": "user", "content": f"用户消息：{text}\n请只返回 JSON 数组。"}],
        preferred_group_name=preferred_group_name,
        max_tokens=400,
        system_prompt=MEMORY_EXTRACTION_PROMPT,
    )
    items = []
    for item in parse_json_array(result["text"]):
        memory_type = item.get("type")
        content = str(item.get("content", "")).strip()
        if memory_type in MEMORY_TYPES and content:
            items.append({"type": memory_type, "content": truncate(content, max_len=160)})
        if len(items) >= 3:
            break
    return items


def build_memory_notes(parsed):
    if parsed["chat_type"] != "group":
        return []
    memories = load_memories(parsed["sender_id"], parsed["chat_id"])
    if not memories:
        return []
    lines = ["以下是与当前群聊相关的长期记忆，仅在相关时参考："]
    lines.extend(f"- {item['memory_type']}: {item['content']}" for item in memories)
    return ["\n".join(lines)]


def handle_command(text, sender_id, chat_id):
    context_key = f"{sender_id}_{chat_id}"
    lower_cmd = text.strip().lower()
    if lower_cmd == "/model":
        with _model_choice_lock:
            current = user_model_choice.get(context_key)
        lines = ["### 模型分组", "", "以下信息来自当前后台配置：", ""]
        for group in load_groups(include_disabled=True, include_secret=False):
            if group["enabled"] and group["api_key"] and group["base_url"] and group["models"]:
                status = "可用"
                icon = "✅"
            elif group["enabled"]:
                status = "已启用但配置不完整"
                icon = "🟨"
            else:
                status = "已停用"
                icon = "⬜"
            marker = " 👈 当前" if current and current.lower() == group["name"].lower() else ""
            lines.append(f"{icon} **{group['name']}**：{group['models_display'] or '未配置模型'}（{status}）{marker}")
        lines.extend(["", f"当前模式：{'🎯 指定 ' + current if current else '🔄 自动切换'}", "用法：`/model 分组名` 切换，`/auto` 恢复自动"])
        return {"title": "模型分组", "body": "\n".join(lines), "status": "blue"}
    if lower_cmd.startswith("/model "):
        group = get_group_by_name(text[7:].strip())
        if not group or not group["enabled"]:
            return {"title": "切换失败", "body": "未找到可用分组。", "status": "red"}
        with _model_choice_lock:
            user_model_choice[context_key] = group["name"]
        return {"title": "切换成功", "body": f"已切换到 **{group['name']}** 分组。", "status": "green"}
    if lower_cmd == "/auto":
        with _model_choice_lock:
            user_model_choice.pop(context_key, None)
        return {"title": "已恢复自动模式", "body": "后续会自动尝试可用分组。", "status": "green"}
    if lower_cmd == "/clear":
        with _history_lock:
            chat_history.pop(context_key, None)
        return {"title": "已清除短期上下文", "body": "当前会话的短期历史已清空。", "status": "green"}
    if lower_cmd == "/help":
        return {"title": "帮助", "body": "`/model`\n`/model 分组名`\n`/auto`\n`/clear`\n`/help`", "status": "blue"}
    return None


def find_trigger(text):
    if not text:
        return None
    fallback = None
    for trigger in load_triggers():
        if trigger["trigger_type"] == "keyword" and trigger["keyword"] and trigger["keyword"] in text:
            return trigger
        if trigger["trigger_type"] == "all" and fallback is None:
            fallback = trigger
    return fallback


def queue_snapshots():
    with queue_lock:
        has_current = current_task is not None
        waiting = list(pending_tasks.values())
    result = []
    for idx, task in enumerate(waiting):
        if not task.reply_message_id:
            continue
        ahead = idx + (1 if has_current else 0)
        result.append((task.reply_message_id, task.preview_text, ahead))
    return result


def update_waiting_cards():
    for reply_message_id, preview_text, ahead in queue_snapshots():
        body = "已进入消息队列，马上开始处理。" if ahead == 0 else f"已进入消息队列，前方还有 **{ahead}** 条消息。"
        update_card(reply_message_id, "排队中", body, status="orange", meta=[f"消息预览：{preview_text}"])


def record_queue_event(task, result, message):
    recent_queue_events.appendleft(
        {
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "user_id": task.sender_id,
            "result": result,
            "message": message,
        }
    )


def process_ai_task(task):
    parsed = extract_message_context(task.event_data, download_images=True)
    if not parsed.get("supported"):
        update_card(task.reply_message_id, "已跳过", parsed.get("reason", "消息不支持处理。"), status="red")
        record_queue_event(task, "skipped", parsed.get("reason", "已跳过"))
        return

    update_card(task.reply_message_id, "思考中", "已经轮到这条消息，正在生成回复。", status="blue", meta=[f"消息预览：{task.preview_text}"])

    user_content = parsed["text"]
    if parsed["image_data_urls"]:
        user_content = [{"type": "image_url", "image_url": {"url": url}} for url in parsed["image_data_urls"]]
        user_content.append({"type": "text", "text": parsed["text"] or "请描述这些图片"})

    context_key = parsed["context_key"]
    history_content = f"[图片] {parsed['text']}" if parsed["image_data_urls"] else parsed["text"]
    with _history_lock:
        history = chat_history.get(context_key) or deque(maxlen=10)
        history.append({"role": "user", "content": history_content})
        chat_history[context_key] = history
        messages = list(history)
    if parsed["image_data_urls"]:
        messages[-1] = {"role": "user", "content": user_content}

    with _model_choice_lock:
        chosen_group = user_model_choice.get(context_key)
    ai_result = call_ai(messages, preferred_group_name=chosen_group, extra_system_notes=build_memory_notes(parsed))
    reply_text = truncate(ai_result["text"])

    with _history_lock:
        history = chat_history.get(context_key)
        if history is not None:
            history.append({"role": "assistant", "content": reply_text})
            chat_history[context_key] = history

    update_card(
        task.reply_message_id,
        "回复完成" if ai_result.get("group") else "回复失败",
        reply_text,
        status="green" if ai_result.get("group") else "red",
        meta=[line for line in (f"分组：{ai_result.get('group')}" if ai_result.get("group") else "", f"模型：{ai_result.get('model')}" if ai_result.get("model") else "") if line],
    )
    record_queue_event(task, "done" if ai_result.get("group") else "failed", reply_text[:80])

    if parsed["chat_type"] == "group" and ai_result.get("group") and should_extract_memory(parsed["text"]):
        try:
            for item in extract_memory_items(parsed["text"], ai_result.get("group")):
                save_memory_item(parsed["sender_id"], parsed["chat_id"], item["type"], item["content"], parsed["message_id"])
        except Exception as exc:
            print(f"[记忆提炼失败] {exc}")


def queue_worker_loop():
    global current_task
    while not shutdown_event.is_set():
        try:
            task_id = message_queue.get(timeout=1)
        except queue.Empty:
            continue

        with queue_lock:
            task = pending_tasks.pop(task_id, None)
            current_task = task
        update_waiting_cards()

        try:
            if not task:
                continue
            if time.time() - task.enqueued_at > config.QUEUE_STALE_SECONDS:
                update_card(task.reply_message_id, "等待超时", "这条消息在队列中等待过久，已自动过期，请稍后重试。", status="red")
                record_queue_event(task, "expired", "队列等待超时")
                continue
            process_ai_task(task)
        except Exception as exc:
            traceback.print_exc()
            if task:
                update_card(task.reply_message_id, "处理失败", "这条消息处理时发生异常，请稍后重试。", status="red")
                record_queue_event(task, "failed", str(exc))
        finally:
            with queue_lock:
                current_task = None
            update_waiting_cards()
            message_queue.task_done()


def send_schedule_message(row):
    _, error = send_card_to_target(row["target_type"], row["target_id"], row["name"], row["message_text"], status="blue", meta=["来自定时任务"])
    if error:
        raise RuntimeError(error)


def scheduler_loop():
    while not shutdown_event.is_set():
        try:
            now_iso = iso_utc(utc_now())
            conn = open_db_connection()
            try:
                due_rows = conn.execute(
                    "SELECT * FROM schedules WHERE enabled = 1 AND next_run_at IS NOT NULL AND next_run_at <= ? ORDER BY next_run_at ASC, id ASC",
                    (now_iso,),
                ).fetchall()
                for row in due_rows:
                    claimed = conn.execute("UPDATE schedules SET is_running = 1 WHERE id = ? AND is_running = 0", (row["id"],))
                    conn.commit()
                    if claimed.rowcount == 0:
                        continue
                    row = conn.execute("SELECT * FROM schedules WHERE id = ?", (row["id"],)).fetchone()
                    try:
                        send_schedule_message(row)
                        next_run_at = None
                        enabled = row["enabled"]
                        if row["schedule_type"] != "once":
                            next_run_at = format_schedule_next_run(row["schedule_type"], row["schedule_expr"], row["timezone"], now_dt=utc_now() + timedelta(seconds=1))
                        else:
                            enabled = 0
                        conn.execute(
                            "UPDATE schedules SET enabled = ?, is_running = 0, next_run_at = ?, last_run_at = ?, last_error = NULL, updated_at = ? WHERE id = ?",
                            (enabled, next_run_at, now_iso, now_iso, row["id"]),
                        )
                        conn.commit()
                    except Exception as exc:
                        conn.execute(
                            "UPDATE schedules SET is_running = 0, last_error = ?, updated_at = ? WHERE id = ?",
                            (truncate(str(exc), max_len=200), now_iso, row["id"]),
                        )
                        conn.commit()
            finally:
                conn.close()
        except Exception as exc:
            print(f"[调度器异常] {exc}")
        shutdown_event.wait(config.SCHEDULER_POLL_SECONDS)


def start_background_threads():
    global worker_thread, scheduler_thread
    if worker_thread is None or not worker_thread.is_alive():
        worker_thread = threading.Thread(target=queue_worker_loop, name="queue-worker", daemon=True)
        worker_thread.start()
    if scheduler_thread is None or not scheduler_thread.is_alive():
        scheduler_thread = threading.Thread(target=scheduler_loop, name="scheduler-worker", daemon=True)
        scheduler_thread.start()


def handle_message_event(event_data):
    parsed = extract_message_context(event_data, download_images=False)
    if not parsed.get("supported"):
        return
    if parsed["msg_type"] == "text" and parsed["text"].startswith("/"):
        command = handle_command(parsed["text"], parsed["sender_id"], parsed["chat_id"])
        if command:
            reply_card(parsed["message_id"], command["title"], command["body"], status=command["status"])
            return

    trigger = find_trigger(parsed["text"])
    if trigger:
        reply_card(parsed["message_id"], trigger["name"], trigger["reply_text"], status="green", meta=[f"触发方式：{trigger['match_mode']}"])
        return

    task = QueueTask(
        task_id=str(uuid.uuid4()),
        event_data=event_data,
        message_id=parsed["message_id"],
        reply_message_id="",
        sender_id=parsed["sender_id"],
        chat_id=parsed["chat_id"],
        chat_type=parsed["chat_type"],
        preview_text=parsed["preview_text"],
        enqueued_at=time.time(),
    )
    with queue_lock:
        total_pending = len(pending_tasks) + (1 if current_task else 0)
        if total_pending >= config.QUEUE_MAX_PENDING:
            reply_card(parsed["message_id"], "当前较忙", "消息队列已满，请稍后再试。", status="red", meta=[f"当前待处理：{total_pending} 条"])
            return
        ahead = total_pending

    reply_id = reply_card(
        parsed["message_id"],
        "排队中",
        "已进入消息队列，马上开始处理。" if ahead == 0 else f"已进入消息队列，前方还有 **{ahead}** 条消息。",
        status="orange",
        meta=[f"消息预览：{task.preview_text}"],
    )
    if not reply_id:
        return
    with queue_lock:
        task.reply_message_id = reply_id
        pending_tasks[task.task_id] = task
        message_queue.put(task.task_id)
    update_waiting_cards()


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "GET" and session.get("admin_logged_in"):
        return redirect(url_for("admin_groups"))
    if request.method == "POST":
        ip = get_client_ip()
        if login_attempts.get(ip, 0) >= LOGIN_RATE_LIMIT:
            flash("登录失败次数过多，请稍后再试。", "error")
            return redirect(url_for("admin_login"))
        if not config.ADMIN_PASSWORD:
            flash("尚未配置 ADMIN_PASSWORD，暂时无法登录后台。", "error")
            return redirect(url_for("admin_login"))
        if hmac.compare_digest(request.form.get("password", ""), config.ADMIN_PASSWORD):
            session["admin_logged_in"] = True
            session.permanent = True
            login_attempts.pop(ip, None)
            flash("登录成功。", "success")
            return redirect(url_for("admin_groups"))
        login_attempts[ip] = login_attempts.get(ip, 0) + 1
        flash("密码错误。", "error")
        return redirect(url_for("admin_login"))
    return render_template("login.html")


@app.route("/admin/logout", methods=["POST"])
@admin_required
def admin_logout():
    session.pop("admin_logged_in", None)
    flash("已退出登录。", "success")
    return redirect(url_for("admin_login"))


@app.route("/admin")
@admin_required
def admin_index():
    return redirect(url_for("admin_groups"))


@app.route("/admin/config/export", methods=["GET"])
@admin_required
def admin_config_export():
    db = get_db()
    snapshot = build_admin_snapshot(db)
    filename = f"feishu-bot-backup-{time.strftime('%Y%m%d-%H%M%S')}.json"
    return Response(
        json.dumps(snapshot, ensure_ascii=False, indent=2),
        mimetype="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.route("/admin/config/import", methods=["POST"])
@admin_required
def admin_config_import():
    db = get_db()
    try:
        payload = parse_admin_snapshot(request.files.get("backup_file"))
        imported, disabled_once = restore_admin_snapshot(db, payload)
        db.commit()
        summary = "，".join(
            [
                f"分组 {imported['ai_groups']} 条",
                f"触发规则 {imported['triggers']} 条",
                f"定时任务 {imported['schedules']} 条",
                f"长期记忆 {imported['memory_items']} 条",
            ]
        )
        flash(f"备份导入成功：{summary}。", "success")
        if disabled_once:
            flash(f"有 {disabled_once} 条一次性定时任务因时间已过期，被自动停用。", "warning")
    except Exception as exc:
        db.rollback()
        flash(f"导入备份失败：{exc}", "error")
    return redirect(url_for("admin_groups"))


@app.route("/admin/groups", methods=["GET", "POST"])
@admin_required
def admin_groups():
    db = get_db()
    if request.method == "POST":
        try:
            now_iso = iso_utc(utc_now())
            db.execute(
                "INSERT INTO ai_groups(name, api_base, api_key_encrypted, models_json, enabled, priority, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    request.form.get("name", "").strip(),
                    request.form.get("base_url", "").strip(),
                    encrypt_secret(request.form.get("api_key", "").strip()),
                    json.dumps(split_models(request.form.get("models", "")), ensure_ascii=False),
                    1 if request.form.get("enabled") else 0,
                    safe_int(request.form.get("sort_order"), 0),
                    now_iso,
                    now_iso,
                ),
            )
            db.commit()
            flash("分组已新增。", "success")
        except Exception as exc:
            db.rollback()
            flash(f"新增分组失败：{exc}", "error")
        return redirect(url_for("admin_groups"))
    edit_id = safe_int(request.args.get("edit"))
    edit_group = get_group_by_id(edit_id) if edit_id else None
    if edit_group:
        edit_group["models"] = ", ".join(edit_group["models"])
    return render_template("groups.html", groups=load_groups(include_disabled=True, include_secret=True, conn=db), edit_group=edit_group, form_action=url_for("admin_group_update", group_id=edit_id) if edit_group else None)


@app.route("/admin/groups/<int:group_id>/edit", methods=["POST"])
@admin_required
def admin_group_update(group_id):
    db = get_db()
    try:
        existing = db.execute("SELECT api_key_encrypted FROM ai_groups WHERE id = ?", (group_id,)).fetchone()
        if not existing:
            flash("分组不存在。", "error")
            return redirect(url_for("admin_groups"))
        encrypted = (
            encrypt_secret(request.form.get("api_key", "").strip())
            if request.form.get("api_key", "").strip()
            else existing["api_key_encrypted"]
        )
        db.execute(
            "UPDATE ai_groups SET name = ?, api_base = ?, api_key_encrypted = ?, models_json = ?, enabled = ?, priority = ?, updated_at = ? WHERE id = ?",
            (
                request.form.get("name", "").strip(),
                request.form.get("base_url", "").strip(),
                encrypted,
                json.dumps(split_models(request.form.get("models", "")), ensure_ascii=False),
                1 if request.form.get("enabled") else 0,
                safe_int(request.form.get("sort_order"), 0),
                iso_utc(utc_now()),
                group_id,
            ),
        )
        db.commit()
        flash("分组已保存。", "success")
    except Exception as exc:
        db.rollback()
        flash(f"保存分组失败：{exc}", "error")
    return redirect(url_for("admin_groups"))


@app.route("/admin/groups/<int:group_id>/refresh", methods=["POST"])
@admin_required
def admin_group_refresh_models(group_id):
    db = get_db()
    group = get_group_by_id(group_id)
    if not group:
        flash("分组不存在。", "error")
        return redirect(url_for("admin_groups"))
    try:
        models = discover_models_for_group(group)
        now_iso = iso_utc(utc_now())
        db.execute("UPDATE ai_groups SET models_json = ?, last_model_refresh_at = ?, updated_at = ? WHERE id = ?", (json.dumps(models, ensure_ascii=False), now_iso, now_iso, group_id))
        db.commit()
        flash(f"模型刷新成功，共发现 {len(models)} 个模型。", "success")
    except Exception as exc:
        flash(f"模型刷新失败：{exc}", "error")
    return redirect(url_for("admin_groups"))


@app.route("/admin/groups/<int:group_id>/test", methods=["POST"])
@admin_required
def admin_group_test(group_id):
    group = get_group_by_id(group_id)
    if not group:
        flash("分组不存在。", "error")
        return redirect(url_for("admin_groups"))
    try:
        models = discover_models_for_group(group)
        flash(f"分组测试成功，示例模型：{', '.join(models[:3])}", "success")
    except Exception as exc:
        flash(f"分组测试失败：{exc}", "error")
    return redirect(url_for("admin_groups"))


@app.route("/admin/groups/<int:group_id>/delete", methods=["POST"])
@admin_required
def admin_group_delete(group_id):
    db = get_db()
    row = db.execute("SELECT id FROM ai_groups WHERE id = ?", (group_id,)).fetchone()
    if not row:
        flash("分组不存在。", "error")
        return redirect(url_for("admin_groups"))
    db.execute("DELETE FROM ai_groups WHERE id = ?", (group_id,))
    db.commit()
    flash("分组已删除。", "success")
    return redirect(url_for("admin_groups"))


@app.route("/admin/memory", methods=["GET", "POST"])
@admin_required
def admin_memory():
    db = get_db()
    if request.method == "POST":
        memory_type = request.form.get("memory_type", "profile")
        if memory_type not in MEMORY_TYPES:
            flash("记忆类型不合法。", "error")
            return redirect(url_for("admin_memory"))
        if not request.form.get("user_id", "").strip() or not request.form.get("chat_id", "").strip() or not request.form.get("content", "").strip():
            flash("用户 ID、群聊 ID 和内容不能为空。", "error")
            return redirect(url_for("admin_memory"))
        now_iso = iso_utc(utc_now())
        db.execute(
            "INSERT INTO memory_items(user_id, chat_id, memory_type, content, active, source_message_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'manual', ?, ?)",
            (
                request.form.get("user_id", "").strip(),
                request.form.get("chat_id", "").strip(),
                memory_type,
                request.form.get("content", "").strip(),
                1 if request.form.get("enabled") else 0,
                now_iso,
                now_iso,
            ),
        )
        db.commit()
        flash("记忆已新增。", "success")
        return redirect(url_for("admin_memory"))
    edit_id = safe_int(request.args.get("edit"))
    edit_memory = None
    if edit_id:
        row = db.execute("SELECT * FROM memory_items WHERE id = ?", (edit_id,)).fetchone()
        if row:
            edit_memory = dict(row)
            edit_memory["enabled"] = bool(edit_memory["active"])
    search_query = request.args.get("q", "").strip()
    return render_template(
        "memory.html",
        memories=list_memories(search_query),
        edit_memory=edit_memory,
        form_action=url_for("admin_memory_update", memory_id=edit_id) if edit_memory else None,
        search_query=search_query,
    )


@app.route("/admin/memory/<int:memory_id>/edit", methods=["POST"])
@admin_required
def admin_memory_update(memory_id):
    db = get_db()
    memory_type = request.form.get("memory_type", "profile")
    if memory_type not in MEMORY_TYPES:
        flash("记忆类型不合法。", "error")
        return redirect(url_for("admin_memory"))
    if not request.form.get("user_id", "").strip() or not request.form.get("chat_id", "").strip() or not request.form.get("content", "").strip():
        flash("用户 ID、群聊 ID 和内容不能为空。", "error")
        return redirect(url_for("admin_memory"))
    db.execute(
        "UPDATE memory_items SET user_id = ?, chat_id = ?, memory_type = ?, content = ?, active = ?, updated_at = ? WHERE id = ?",
        (
            request.form.get("user_id", "").strip(),
            request.form.get("chat_id", "").strip(),
            memory_type,
            request.form.get("content", "").strip(),
            1 if request.form.get("enabled") else 0,
            iso_utc(utc_now()),
            memory_id,
        ),
    )
    db.commit()
    flash("记忆已保存。", "success")
    return redirect(url_for("admin_memory"))


@app.route("/admin/memory/<int:memory_id>/delete", methods=["POST"])
@admin_required
def admin_memory_delete(memory_id):
    db = get_db()
    db.execute("DELETE FROM memory_items WHERE id = ?", (memory_id,))
    db.commit()
    flash("记忆已删除。", "success")
    return redirect(url_for("admin_memory"))


@app.route("/admin/triggers", methods=["GET", "POST"])
@admin_required
def admin_triggers():
    db = get_db()
    if request.method == "POST":
        now_iso = iso_utc(utc_now())
        match_mode = request.form.get("match_mode", "keyword")
        keyword = request.form.get("keyword", "").strip() if match_mode == "keyword" else ""
        if match_mode == "keyword" and not keyword:
            flash("关键词规则必须填写关键词。", "error")
            return redirect(url_for("admin_triggers"))
        db.execute(
            "INSERT INTO triggers(name, trigger_type, keyword, reply_text, priority, enabled, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                request.form.get("name", "").strip(),
                match_mode,
                keyword,
                request.form.get("reply_text", "").strip(),
                safe_int(request.form.get("sort_order"), 0),
                1 if request.form.get("enabled") else 0,
                now_iso,
                now_iso,
            ),
        )
        db.commit()
        flash("触发规则已新增。", "success")
        return redirect(url_for("admin_triggers"))
    edit_id = safe_int(request.args.get("edit"))
    edit_trigger = None
    if edit_id:
        row = db.execute("SELECT * FROM triggers WHERE id = ?", (edit_id,)).fetchone()
        if row:
            edit_trigger = dict(row)
            edit_trigger["match_mode"] = edit_trigger["trigger_type"]
            edit_trigger["sort_order"] = edit_trigger["priority"]
            edit_trigger["enabled"] = bool(edit_trigger["enabled"])
    return render_template(
        "triggers.html",
        triggers=load_triggers(include_disabled=True),
        edit_trigger=edit_trigger,
        form_action=url_for("admin_trigger_update", trigger_id=edit_id) if edit_trigger else None,
    )


@app.route("/admin/triggers/<int:trigger_id>/edit", methods=["POST"])
@admin_required
def admin_trigger_update(trigger_id):
    db = get_db()
    match_mode = request.form.get("match_mode", "keyword")
    keyword = request.form.get("keyword", "").strip() if match_mode == "keyword" else ""
    if match_mode == "keyword" and not keyword:
        flash("关键词规则必须填写关键词。", "error")
        return redirect(url_for("admin_triggers", edit=trigger_id))
    db.execute(
        "UPDATE triggers SET name = ?, trigger_type = ?, keyword = ?, reply_text = ?, priority = ?, enabled = ?, updated_at = ? WHERE id = ?",
        (request.form.get("name", "").strip(), match_mode, keyword, request.form.get("reply_text", "").strip(), safe_int(request.form.get("sort_order"), 0), 1 if request.form.get("enabled") else 0, iso_utc(utc_now()), trigger_id),
    )
    db.commit()
    flash("触发规则已保存。", "success")
    return redirect(url_for("admin_triggers"))


@app.route("/admin/triggers/<int:trigger_id>/delete", methods=["POST"])
@admin_required
def admin_trigger_delete(trigger_id):
    db = get_db()
    db.execute("DELETE FROM triggers WHERE id = ?", (trigger_id,))
    db.commit()
    flash("触发规则已删除。", "success")
    return redirect(url_for("admin_triggers"))


@app.route("/admin/triggers/<int:trigger_id>/preview", methods=["POST"])
@admin_required
def admin_trigger_preview(trigger_id):
    db = get_db()
    row = db.execute("SELECT * FROM triggers WHERE id = ?", (trigger_id,)).fetchone()
    if row:
        flash(f"预览：{row['reply_text']}", "info")
    else:
        flash("触发规则不存在。", "error")
    return redirect(url_for("admin_triggers"))


@app.route("/admin/schedules", methods=["GET", "POST"])
@admin_required
def admin_schedules():
    db = get_db()
    if request.method == "POST":
        try:
            schedule_expr = request.form.get("schedule_expr", "").strip()
            timezone_name = request.form.get("timezone", config.DEFAULT_TIMEZONE).strip() or config.DEFAULT_TIMEZONE
            next_run_at = format_schedule_next_run(request.form.get("schedule_type", "once"), schedule_expr, timezone_name) if request.form.get("enabled") else None
            now_iso = iso_utc(utc_now())
            db.execute(
                "INSERT INTO schedules(name, target_type, target_id, message_text, schedule_type, schedule_expr, timezone, next_run_at, enabled, is_running, created_at, updated_at) VALUES (?, 'chat_id', ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
                (
                    request.form.get("name", "").strip(),
                    request.form.get("target_id", "").strip(),
                    request.form.get("message", "").strip(),
                    request.form.get("schedule_type", "once"),
                    schedule_expr,
                    timezone_name,
                    next_run_at,
                    1 if request.form.get("enabled") else 0,
                    now_iso,
                    now_iso,
                ),
            )
            db.commit()
            flash("定时任务已新增。", "success")
        except Exception as exc:
            db.rollback()
            flash(f"新增定时任务失败：{exc}", "error")
        return redirect(url_for("admin_schedules"))
    edit_id = safe_int(request.args.get("edit"))
    edit_schedule = None
    if edit_id:
        row = db.execute("SELECT * FROM schedules WHERE id = ?", (edit_id,)).fetchone()
        if row:
            edit_schedule = dict(row)
            edit_schedule["message"] = edit_schedule["message_text"]
    rows = db.execute("SELECT * FROM schedules ORDER BY id DESC").fetchall()
    schedules = []
    for row in rows:
        item = dict(row)
        item["message"] = item["message_text"]
        item["next_run_at"] = format_local_datetime_for_display(item["next_run_at"], item["timezone"])
        schedules.append(item)
    return render_template("schedules.html", schedules=schedules, edit_schedule=edit_schedule, form_action=url_for("admin_schedule_update", schedule_id=edit_id) if edit_schedule else None)


@app.route("/admin/schedules/<int:schedule_id>/edit", methods=["POST"])
@admin_required
def admin_schedule_update(schedule_id):
    db = get_db()
    try:
        schedule_expr = request.form.get("schedule_expr", "").strip()
        timezone_name = request.form.get("timezone", config.DEFAULT_TIMEZONE).strip() or config.DEFAULT_TIMEZONE
        next_run_at = format_schedule_next_run(request.form.get("schedule_type", "once"), schedule_expr, timezone_name) if request.form.get("enabled") else None
        db.execute(
            "UPDATE schedules SET name = ?, target_id = ?, message_text = ?, schedule_type = ?, schedule_expr = ?, timezone = ?, next_run_at = ?, enabled = ?, updated_at = ? WHERE id = ?",
            (
                request.form.get("name", "").strip(),
                request.form.get("target_id", "").strip(),
                request.form.get("message", "").strip(),
                request.form.get("schedule_type", "once"),
                schedule_expr,
                timezone_name,
                next_run_at,
                1 if request.form.get("enabled") else 0,
                iso_utc(utc_now()),
                schedule_id,
            ),
        )
        db.commit()
        flash("定时任务已保存。", "success")
    except Exception as exc:
        db.rollback()
        flash(f"保存定时任务失败：{exc}", "error")
    return redirect(url_for("admin_schedules"))


@app.route("/admin/schedules/<int:schedule_id>/run", methods=["POST"])
@admin_required
def admin_schedule_run_once(schedule_id):
    db = get_db()
    row = db.execute("SELECT * FROM schedules WHERE id = ?", (schedule_id,)).fetchone()
    if row:
        try:
            send_schedule_message(row)
            flash("试发成功。", "success")
        except Exception as exc:
            flash(f"试发失败：{exc}", "error")
    else:
        flash("定时任务不存在。", "error")
    return redirect(url_for("admin_schedules"))


@app.route("/admin/schedules/<int:schedule_id>/delete", methods=["POST"])
@admin_required
def admin_schedule_delete(schedule_id):
    db = get_db()
    db.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
    db.commit()
    flash("定时任务已删除。", "success")
    return redirect(url_for("admin_schedules"))


@app.route("/admin/queue")
@admin_required
def admin_queue():
    with queue_lock:
        queue_items = []
        if current_task:
            queue_items.append({"position": 1, "user_id": current_task.sender_id, "chat_id": current_task.chat_id, "status": "处理中", "wait_seconds": round(time.time() - current_task.enqueued_at, 1)})
        start = 2 if current_task else 1
        for idx, task in enumerate(pending_tasks.values(), start=start):
            queue_items.append({"position": idx, "user_id": task.sender_id, "chat_id": task.chat_id, "status": "等待中", "wait_seconds": round(time.time() - task.enqueued_at, 1)})
    queue_stats = {
        "pending": len([item for item in queue_items if item["status"] == "等待中"]),
        "processing": 1 if current_task else 0,
        "done": len([item for item in recent_queue_events if item["result"] == "done"]),
        "failed": len([item for item in recent_queue_events if item["result"] != "done"]),
    }
    return render_template("queue.html", queue_stats=queue_stats, queue_items=queue_items, recent_items=list(recent_queue_events))


@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        if not request.is_json:
            return jsonify({"error": "invalid request body"}), 400
        data = request.get_json(silent=True)
        if not data:
            return jsonify({"error": "empty request body"}), 400
        if not config.FEISHU_VERIFICATION_TOKEN:
            return jsonify({"error": "verification token not configured"}), 503
        if data.get("type") == "url_verification":
            if data.get("token") != config.FEISHU_VERIFICATION_TOKEN:
                return jsonify({"error": "invalid token"}), 401
            return jsonify({"challenge": data.get("challenge")})
        if data.get("header", {}).get("token") != config.FEISHU_VERIFICATION_TOKEN:
            return jsonify({"error": "invalid token"}), 401
        if data.get("header", {}).get("event_type") != "im.message.receive_v1":
            return jsonify({"code": 0})
        event = data.get("event", {})
        message_id = event.get("message", {}).get("message_id")
        if not message_id:
            return jsonify({"code": 0})
        with _dedup_lock:
            if message_id in processed_messages:
                return jsonify({"code": 0})
            processed_messages[message_id] = True
        handle_message_event(event)
        return jsonify({"code": 0})
    except Exception as exc:
        traceback.print_exc()
        return jsonify({"error": str(exc)}), 500


@app.route("/health", methods=["GET"])
@app.route("/", methods=["GET"])
def health():
    with queue_lock:
        waiting = len(pending_tasks)
        processing = 1 if current_task else 0
    return jsonify({"status": "ok", "bot_id": BOT_OPEN_ID, "queue_waiting": waiting, "queue_processing": processing})


def print_startup_warnings():
    missing = []
    for key in ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_VERIFICATION_TOKEN", "ADMIN_PASSWORD"):
        if not getattr(config, key, ""):
            missing.append(key)
    if missing:
        print(f"[警告] 缺少配置: {', '.join(missing)}")


if __name__ == "__main__":
    print_startup_warnings()
    initialize_runtime()
    app.run(host="0.0.0.0", port=config.PORT, debug=False)
