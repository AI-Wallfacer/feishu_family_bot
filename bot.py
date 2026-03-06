import json
import time
import base64
import traceback
import threading
from concurrent.futures import ThreadPoolExecutor
import requests
from collections import deque
from cachetools import TTLCache
from flask import Flask, request, jsonify
import config

app = Flask(__name__)

# 消息去重（TTL 缓存，5 分钟过期，最多 2000 条）
processed_messages = TTLCache(maxsize=2000, ttl=300)
_dedup_lock = threading.Lock()

# 多轮对话上下文（30 分钟过期，最多 500 个会话）
chat_history = TTLCache(maxsize=500, ttl=1800)
_history_lock = threading.Lock()

# 用户模型选择（30 分钟过期，最多 500 个用户）
user_model_choice = TTLCache(maxsize=500, ttl=1800)
_model_choice_lock = threading.Lock()

# 用户名称缓存（12 小时）
user_name_cache = TTLCache(maxsize=500, ttl=43200)
_user_name_lock = threading.Lock()

# 飞书消息最大长度
MAX_MSG_LEN = 4000

# 机器人自身 open_id
BOT_OPEN_ID = None

# 飞书 tenant_access_token 缓存
_token_cache = {"token": None, "expire_at": 0}
_token_lock = threading.Lock()
_message_executor = ThreadPoolExecutor(max_workers=config.MESSAGE_WORKERS)


def _log_message_task_result(future):
    """回收线程池任务异常，避免后台异常静默丢失。"""
    try:
        future.result()
    except Exception:
        print("[错误] 消息任务执行失败")
        traceback.print_exc()


def get_tenant_access_token():
    """获取飞书 tenant_access_token（带缓存，提前 5 分钟刷新）"""
    with _token_lock:
        now = time.time()
        if _token_cache["token"] and now < _token_cache["expire_at"] - 300:
            return _token_cache["token"]

        url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
        payload = {
            "app_id": config.FEISHU_APP_ID,
            "app_secret": config.FEISHU_APP_SECRET
        }
        try:
            response = requests.post(url, json=payload, timeout=10)
            data = response.json()
            token = data.get("tenant_access_token")
            if not token:
                print(f"[Token] 获取失败: {data}")
                return _token_cache["token"]  # 返回旧 token（可能为 None）
            expire = data.get("expire", 7200)
            _token_cache["token"] = token
            _token_cache["expire_at"] = now + expire
            print(f"[Token] 已刷新，有效期 {expire}s")
            return token
        except Exception as e:
            print(f"[Token] 请求异常: {e}")
            return _token_cache["token"]


def get_bot_open_id():
    """获取机器人自身的 open_id"""
    global BOT_OPEN_ID
    try:
        token = get_tenant_access_token()
        url = "https://open.feishu.cn/open-apis/bot/v3/info"
        headers = {"Authorization": f"Bearer {token}"}
        resp = requests.get(url, headers=headers, timeout=10)
        data = resp.json()
        if data.get("code") == 0:
            BOT_OPEN_ID = data["bot"]["open_id"]
            print(f"[Bot] open_id: {BOT_OPEN_ID}")
        else:
            print(f"[Bot] 获取 open_id 失败: {data}")
    except Exception as e:
        print(f"[Bot] 获取 open_id 异常: {e}")


def get_user_display_name(user_open_id):
    """根据 open_id 获取飞书用户名称，用于家庭成员身份识别。"""
    if not user_open_id or user_open_id == "unknown":
        return None

    with _user_name_lock:
        cached_name = user_name_cache.get(user_open_id)
    if cached_name:
        return cached_name

    try:
        token = get_tenant_access_token()
        url = f"https://open.feishu.cn/open-apis/contact/v3/users/{user_open_id}"
        headers = {"Authorization": f"Bearer {token}"}
        params = {"user_id_type": "open_id"}
        resp = requests.get(url, headers=headers, params=params, timeout=10)
        data = resp.json()
        if data.get("code") != 0:
            print(f"[User] 获取名称失败: {data}")
            return None

        user = data.get("data", {}).get("user", {})
        name = user.get("name") or user.get("en_name")
        if name:
            with _user_name_lock:
                user_name_cache[user_open_id] = name
            return name
    except Exception as e:
        print(f"[User] 获取名称异常: {e}")
    return None


def build_sender_context(sender_open_id):
    """基于群内名称映射构建发言人上下文。"""
    display_name = get_user_display_name(sender_open_id)
    if not display_name:
        return ""

    family_role = config.FAMILY_MEMBER_NAME_MAP.get(display_name.strip())
    if not family_role:
        return ""

    return f"[当前发言人: {family_role}，群内名称: {display_name}] "


def call_ai(messages, group_name=None):
    """调用 AI API，支持多分组多 Key 自动切换，统一 OpenAI 格式。
    group_name: 指定分组名时只用该分组，为 None 时按顺序自动切换。
    """
    for group in config.AI_GROUPS:
        # 如果指定了分组，跳过不匹配的
        if group_name and group["name"].lower() != group_name.lower():
            continue

        api_key = group["key"]
        if not api_key:
            continue

        api_base = group.get("base") or config.AI_API_BASE
        if not api_base:
            continue

        g_name = group["name"]
        models = [m.strip() for m in group["models"].split(",") if m.strip()]

        for model in models:
            try:
                url = f"{api_base}/v1/chat/completions"
                headers = {
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json"
                }
                payload = {
                    "model": model,
                    "max_tokens": config.AI_MAX_TOKENS,
                    "messages": [{"role": "system", "content": config.SYSTEM_PROMPT}] + messages
                }

                resp = requests.post(url, headers=headers, json=payload, timeout=120)
                result = resp.json()
                print(f"[AI响应 {g_name}/{model}] {json.dumps(result, ensure_ascii=False)[:200]}")

                if "error" in result:
                    print(f"[{g_name}/{model} 失败] 尝试下一个...")
                    continue

                choices = result.get("choices")
                if not choices:
                    print(f"[{g_name}/{model} 失败] 响应无 choices，尝试下一个...")
                    continue

                return choices[0]["message"]["content"]

            except Exception as e:
                print(f"[{g_name}/{model} 调用失败] {e}")
                continue

    return "抱歉，所有模型都无法回复，请稍后再试。"


def build_card(text):
    """构建飞书卡片消息"""
    return json.dumps({
        "elements": [{"tag": "markdown", "content": text}]
    })


def reply_card(message_id, text):
    """用卡片消息回复，返回回复消息的 message_id"""
    try:
        token = get_tenant_access_token()
        url = f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reply"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        payload = {
            "msg_type": "interactive",
            "content": build_card(text)
        }
        resp = requests.post(url, headers=headers, json=payload, timeout=10)
        result = resp.json()
        if result.get("code") == 0:
            reply_id = result.get("data", {}).get("message_id")
            print(f"[回复成功] {text[:50]}")
            return reply_id
        else:
            print(f"发送消息失败: {result.get('code')}, {result.get('msg')}")
    except Exception as e:
        print(f"回复消息失败: {e}")
    return None


def update_card(message_id, text):
    """更新卡片消息内容"""
    try:
        token = get_tenant_access_token()
        url = f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        payload = {
            "msg_type": "interactive",
            "content": build_card(text)
        }
        resp = requests.patch(url, headers=headers, json=payload, timeout=10)
        result = resp.json()
        if result.get("code") == 0:
            print(f"[更新成功] {text[:50]}")
        else:
            print(f"更新消息失败: {result.get('code')}, {result.get('msg')}")
    except Exception as e:
        print(f"更新消息失败: {e}")


def looks_like_math_text(text):
    """只在疑似公式场景下做清洗，避免误伤普通对话文本。"""
    markers = (
        "$", r"\(", r"\)", r"\[", r"\]", r"\frac", r"\sqrt", r"\sum",
        r"\int", r"\times", r"\cdot", r"\div", r"\leq", r"\geq",
        r"\neq", r"\approx", r"\alpha", r"\beta", r"\gamma", r"\pi",
        r"\theta", r"\lambda", r"\pm"
    )
    return any(marker in text for marker in markers)


def clean_math_format(text):
    """将常见 LaTeX 公式转成更适合飞书展示的纯文本。"""
    import re

    if not looks_like_math_text(text):
        return text

    def unwrap_inline(match):
        return match.group(1).strip()

    def unwrap_block(match):
        return f"\n{match.group(1).strip()}\n"

    # 先去掉常见数学定界符
    text = re.sub(r"\$\$(.+?)\$\$", unwrap_block, text, flags=re.S)
    text = re.sub(r"\$(.+?)\$", unwrap_inline, text, flags=re.S)
    text = re.sub(r"\\\[(.+?)\\\]", unwrap_block, text, flags=re.S)
    text = re.sub(r"\\\((.+?)\\\)", unwrap_inline, text, flags=re.S)

    # 处理更结构化的 LaTeX 语法
    frac_pattern = re.compile(r"\\frac\s*\{([^{}]+)\}\s*\{([^{}]+)\}")
    while frac_pattern.search(text):
        text = frac_pattern.sub(r"(\1)/(\2)", text)

    text = re.sub(r"\\sqrt\s*\{([^{}]+)\}", r"sqrt(\1)", text)
    text = re.sub(r"\\sqrt\s+([A-Za-z0-9]+)", r"sqrt(\1)", text)
    text = re.sub(r"\^\{([^{}]+)\}", r"^(\1)", text)
    text = re.sub(r"_\{([^{}]+)\}", r"_(\1)", text)
    text = re.sub(r"\\(?:text|mathrm|operatorname)\{([^{}]+)\}", r"\1", text)

    replacements = {
        r"\times": " * ",
        r"\cdot": " * ",
        r"\div": " / ",
        r"\pm": " +/- ",
        r"\leq": " <= ",
        r"\geq": " >= ",
        r"\neq": " != ",
        r"\approx": " ~= ",
        r"\sum": "sum",
        r"\int": "integral",
        r"\infty": "infinity",
        r"\alpha": "alpha",
        r"\beta": "beta",
        r"\gamma": "gamma",
        r"\pi": "pi",
        r"\theta": "theta",
        r"\lambda": "lambda",
        r"\left": "",
        r"\right": "",
    }
    for latex, plain_text in replacements.items():
        text = text.replace(latex, plain_text)

    # 清掉剩余的 LaTeX 转义前缀，但尽量保留内容本身
    text = re.sub(r"\\([A-Za-z]+)", r"\1", text)
    text = text.replace("{", "(").replace("}", ")")
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

def truncate(text, max_len=MAX_MSG_LEN):
    if looks_like_math_text(text):
        text = clean_math_format(text)
    if len(text) > max_len:
        return text[:max_len - 20] + "\n\n...(消息过长已截断)"
    return text


def download_feishu_image(message_id, image_key):
    """从飞书下载图片，返回 base64 编码字符串"""
    try:
        token = get_tenant_access_token()
        url = f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/resources/{image_key}?type=image"
        headers = {"Authorization": f"Bearer {token}"}
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code == 200 and resp.content:
            b64 = base64.b64encode(resp.content).decode("utf-8")
            content_type = resp.headers.get("Content-Type", "image/jpeg")
            print(f"[图片] 下载成功, 大小: {len(resp.content)} bytes")
            return f"data:{content_type};base64,{b64}"
        else:
            print(f"[图片] 下载失败: status={resp.status_code}")
    except Exception as e:
        print(f"[图片] 下载异常: {e}")
    return None


def check_group_connectivity(group):
    """快速检测分组 API 是否可连通"""
    api_key = group.get("key")
    if not api_key:
        return "未配置Key"
    api_base = group.get("base") or config.AI_API_BASE
    if not api_base:
        return "未配置地址"
    try:
        url = f"{api_base}/v1/models"
        headers = {"Authorization": f"Bearer {api_key}"}
        resp = requests.get(url, headers=headers, timeout=5)
        if resp.status_code == 200:
            return "可用"
        else:
            return f"异常({resp.status_code})"
    except requests.Timeout:
        return "超时"
    except Exception:
        return "不可达"


def handle_command(text, sender_id, chat_id):
    """处理用户指令，返回回复文本；非指令返回 None"""
    context_key = f"{sender_id}_{chat_id}"
    cmd = text.strip().lower()

    if cmd == "/model":
        # 显示可用分组列表，并检测连通性
        with _model_choice_lock:
            current = user_model_choice.get(context_key, None)
        lines = ["📋 可用模型分组：\n"]
        for g in config.AI_GROUPS:
            status = check_group_connectivity(g)
            if status == "可用":
                icon = "✅"
            elif status == "未配置Key" or status == "未配置地址":
                icon = "⬜"
            else:
                icon = "❌"
            is_current = " 👈 当前" if (current and g["name"].lower() == current.lower()) else ""
            models = g["models"][:60]
            lines.append(f"{icon} **{g['name']}**: {models} ({status}){is_current}")
        lines.append(f"\n当前模式：{'🎯 指定 ' + current if current else '🔄 自动切换'}")
        lines.append("\n用法：`/model 分组名` 切换，`/auto` 恢复自动")
        return "\n".join(lines)

    elif cmd.startswith("/model "):
        # 切换到指定分组
        target = text.strip()[7:].strip()
        matched = None
        for g in config.AI_GROUPS:
            if g["name"].lower() == target.lower():
                matched = g["name"]
                break
        if matched:
            with _model_choice_lock:
                user_model_choice[context_key] = matched
            return f"✅ 已切换到 **{matched}** 分组"
        else:
            names = "、".join(g["name"] for g in config.AI_GROUPS)
            return f"❌ 未找到分组「{target}」\n可用分组：{names}"

    elif cmd == "/auto":
        with _model_choice_lock:
            if context_key in user_model_choice:
                del user_model_choice[context_key]
        return "🔄 已恢复自动切换模式"

    elif cmd == "/help":
        return (
            "🤖 可用指令：\n\n"
            "`/model` — 查看可用模型分组\n"
            "`/model 分组名` — 切换到指定分组\n"
            "`/auto` — 恢复自动切换模式\n"
            "`/clear` — 清除对话历史\n"
            "`/help` — 显示本帮助"
        )

    elif cmd == "/clear":
        with _history_lock:
            if context_key in chat_history:
                del chat_history[context_key]
        return "🗑️ 对话历史已清除"

    return None


def process_message(event_data):
    """处理消息（在独立线程中运行）"""
    try:
        message = event_data.get("message", {})
        message_id = message.get("message_id")
        chat_id = message.get("chat_id")
        chat_type = message.get("chat_type", "")
        msg_type = message.get("message_type", "text")
        sender_id = event_data.get("sender", {}).get("sender_id", {}).get("open_id", "unknown")
        content = json.loads(message.get("content", "{}"))
        mentions = message.get("mentions", [])
        sender_context = build_sender_context(sender_id)

        # 消息去重（加锁保证线程安全）
        with _dedup_lock:
            if message_id in processed_messages:
                print(f"[去重] {message_id}")
                return
            processed_messages[message_id] = True

        # 解析文本和图片
        text = ""
        image_data_url = None

        if msg_type == "text":
            text = content.get("text", "").strip()
        elif msg_type == "image":
            image_key = content.get("image_key", "")
            if image_key:
                image_data_url = download_feishu_image(message_id, image_key)
            text = "请描述这张图片"
        elif msg_type == "post":
            # 富文本消息：提取文本和所有图片
            post_content = content.get("content", [])
            text_parts = []
            image_keys = []
            for line in post_content:
                for elem in line:
                    tag = elem.get("tag", "")
                    if tag == "text":
                        text_parts.append(elem.get("text", ""))
                    elif tag == "at":
                        pass  # @信息由 mentions 处理
                    elif tag == "img":
                        key = elem.get("image_key", "")
                        if key:
                            image_keys.append(key)
            text = "".join(text_parts).strip()
            if image_keys:
                image_data_url = []
                for key in image_keys:
                    url = download_feishu_image(message_id, key)
                    if url:
                        image_data_url.append(url)
                if not text:
                    text = "请描述这些图片" if len(image_data_url) > 1 else "请描述这张图片"
        else:
            print(f"[跳过] 不支持的消息类型: {msg_type}")
            return

        # 群聊中只回复 @机器人 的消息，私聊全部回复
        if chat_type == "group":
            bot_mentioned = False
            if mentions:
                for m in mentions:
                    mention_id = m.get("id", {}).get("open_id", "")
                    if mention_id == BOT_OPEN_ID:
                        bot_mentioned = True
                        break
            # 图片消息在群聊中也需要 @，但飞书图片消息无法 @，所以私聊才支持图片
            if not bot_mentioned and msg_type == "text":
                print(f"[跳过] 群聊消息未@机器人")
                return
            elif not bot_mentioned and msg_type == "image":
                print(f"[跳过] 群聊图片消息未@机器人")
                return
            for m in mentions:
                text = text.replace(m.get("key", ""), "").strip()

        if not text and not image_data_url:
            return

        # 处理指令
        if msg_type == "text" and text.startswith("/"):
            cmd_reply = handle_command(text, sender_id, chat_id)
            if cmd_reply:
                reply_card(message_id, cmd_reply)
                return

        print(f"[消息] type={msg_type}, text={text[:80]}")

        # 先回复"思考中..."
        thinking_id = reply_card(message_id, "🤔 思考中...")

        # 构建消息内容（支持多图片+文本的 vision 格式）
        if image_data_url:
            # 统一为列表
            urls = image_data_url if isinstance(image_data_url, list) else [image_data_url]
            user_content = [{"type": "image_url", "image_url": {"url": u}} for u in urls]
            prompt_text = text or "请描述这些图片"
            if sender_context:
                prompt_text = f"{sender_context}{prompt_text}"
            user_content.append({"type": "text", "text": prompt_text})
        else:
            user_content = f"{sender_context}{text}" if sender_context else text

        # 构建多轮对话上下文（加锁保证线程安全）
        # 图片 base64 不存入历史，用文本占位符代替，避免内存爆炸
        context_key = f"{sender_id}_{chat_id}"
        history_content = f"[图片] {text}" if image_data_url else text
        if sender_context:
            history_content = f"{sender_context}{history_content}"

        with _history_lock:
            if context_key not in chat_history:
                chat_history[context_key] = deque(maxlen=10)
            history = chat_history[context_key]
            history.append({"role": "user", "content": history_content})
            messages = list(history)

        # 本次请求把最后一条替换为含图片的完整内容（不影响历史存储）
        if image_data_url:
            messages[-1] = {"role": "user", "content": user_content}

        # 调用 AI（使用用户选择的模型分组）
        with _model_choice_lock:
            chosen_group = user_model_choice.get(context_key)
        reply_text = call_ai(messages, group_name=chosen_group)
        reply_text = truncate(reply_text)

        # 保存回复到上下文
        with _history_lock:
            if context_key in chat_history:
                chat_history[context_key].append({"role": "assistant", "content": reply_text})

        # 更新卡片为实际回复
        if thinking_id:
            update_card(thinking_id, reply_text)
        else:
            reply_card(message_id, reply_text)

        print(f"[完成] 回复: {reply_text[:50]}")

    except Exception as e:
        print(f"处理消息失败: {e}")
        traceback.print_exc()


def handle_webhook():
    """处理飞书 Webhook"""
    try:
        if not request.is_json:
            return jsonify({"error": "invalid request body"}), 400
        data = request.json
        if not data:
            return jsonify({"error": "empty request body"}), 400

        print(f"[收到请求] {json.dumps(data, ensure_ascii=False)[:200]}")

        # URL 验证
        if data.get("type") == "url_verification":
            challenge = data.get("challenge")
            print(f"[URL验证] challenge: {challenge}")
            return jsonify({"challenge": challenge})

        # 验证 token
        token = data.get("header", {}).get("token")
        if token != config.FEISHU_VERIFICATION_TOKEN:
            print(f"[错误] token 验证失败")
            return jsonify({"error": "invalid token"}), 401

        event = data.get("event", {})
        event_type = data.get("header", {}).get("event_type")
        print(f"[事件] 类型: {event_type}")

        if event_type == "im.message.receive_v1":
            future = _message_executor.submit(process_message, event)
            future.add_done_callback(_log_message_task_result)

        return jsonify({"code": 0})

    except Exception as e:
        print(f"[错误] Webhook 处理失败: {e}")
        return jsonify({"error": str(e)}), 500


# 初始化 BOT_OPEN_ID（注意：仅适用于单 worker 模式）
_init_done = False
_init_lock = threading.Lock()


@app.before_request
def ensure_init():
    global _init_done
    if not _init_done:
        with _init_lock:
            if not _init_done:
                get_bot_open_id()
                _init_done = True


@app.route('/', methods=['POST'])
def root_webhook():
    return handle_webhook()


@app.route('/webhook', methods=['POST'])
def webhook():
    return handle_webhook()


@app.route('/health', methods=['GET'])
@app.route('/', methods=['GET'])
def health():
    return jsonify({"status": "ok", "bot_id": BOT_OPEN_ID})


if __name__ == '__main__':
    # 启动时校验关键配置
    missing = []
    if not config.FEISHU_APP_ID:
        missing.append("FEISHU_APP_ID")
    if not config.FEISHU_APP_SECRET:
        missing.append("FEISHU_APP_SECRET")
    if not config.FEISHU_VERIFICATION_TOKEN:
        missing.append("FEISHU_VERIFICATION_TOKEN")
    has_any_ai = any(g["key"] for g in config.AI_GROUPS)
    has_any_base = config.AI_API_BASE or any(g.get("base") for g in config.AI_GROUPS)
    if not has_any_ai:
        missing.append("至少一个 AI_KEY_*")
    if not has_any_base:
        missing.append("AI_API_BASE 或至少一个 AI_BASE_*")
    if missing:
        print(f"[警告] 缺少配置: {', '.join(missing)}")

    print(f"飞书 Bot 启动中...")
    print(f"监听端口: {config.PORT}")
    get_bot_open_id()
    app.run(host='0.0.0.0', port=config.PORT, debug=False)
