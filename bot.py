import json
import time
import threading
import queue
import requests
from collections import defaultdict, deque
from cachetools import TTLCache
from flask import Flask, request, jsonify
import config

app = Flask(__name__)

# 消息去重（TTL 缓存，5 分钟过期，最多 2000 条）
processed_messages = TTLCache(maxsize=2000, ttl=300)

# 消息队列
message_queue = queue.Queue()

# 多轮对话上下文 {sender_chat_key: deque([(role, content), ...])}
# 按 "用户ID_群ID" 隔离，避免不同人的对话交叉
chat_history = defaultdict(lambda: deque(maxlen=10))

# 飞书消息最大长度
MAX_MSG_LEN = 4000

# 机器人自身 open_id（启动时获取）
BOT_OPEN_ID = None


def get_bot_open_id():
    """获取机器人自身的 open_id"""
    global BOT_OPEN_ID
    try:
        token = get_tenant_access_token()
        url = "https://open.feishu.cn/open-apis/bot/v3/info"
        headers = {"Authorization": f"Bearer {token}"}
        resp = requests.get(url, headers=headers)
        data = resp.json()
        if data.get("code") == 0:
            BOT_OPEN_ID = data["bot"]["open_id"]
            print(f"[Bot] open_id: {BOT_OPEN_ID}")
        else:
            print(f"[Bot] 获取 open_id 失败: {data}")
    except Exception as e:
        print(f"[Bot] 获取 open_id 异常: {e}")


# 排队中的消息 {message_id: reply_card_id}，用于更新排队状态
pending_replies = {}
pending_lock = threading.Lock()


def queue_worker():
    """消息队列工作线程，按顺序处理消息"""
    while True:
        event_data = message_queue.get()
        try:
            process_message(event_data)
        except Exception as e:
            print(f"[队列] 处理消息失败: {e}")
        finally:
            message_queue.task_done()
            # 更新所有排队中消息的排队人数
            update_queue_status()
            size = message_queue.qsize()
            if size > 0:
                print(f"[队列] 剩余待处理: {size} 条")


def update_queue_status():
    """更新所有排队中消息的排队人数显示"""
    with pending_lock:
        items = list(pending_replies.items())
    for i, (msg_id, card_id) in enumerate(items):
        pos = i + 1
        total = len(items)
        if total > 0:
            update_card(card_id, f"⏳ 排队中... 前方还有 {pos - 1} 人，共 {total} 人等待")


# 启动工作线程
worker = threading.Thread(target=queue_worker, daemon=True)
worker.start()


# 飞书 tenant_access_token 缓存
_token_cache = {"token": None, "expire_at": 0}


def get_tenant_access_token():
    """获取飞书 tenant_access_token（带缓存，提前 5 分钟刷新）"""
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expire_at"] - 300:
        return _token_cache["token"]

    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    payload = {
        "app_id": config.FEISHU_APP_ID,
        "app_secret": config.FEISHU_APP_SECRET
    }
    response = requests.post(url, json=payload)
    data = response.json()
    token = data.get("tenant_access_token")
    expire = data.get("expire", 7200)
    _token_cache["token"] = token
    _token_cache["expire_at"] = now + expire
    print(f"[Token] 已刷新，有效期 {expire}s")
    return token


def call_ai(messages):
    """调用 AI API 生成回复，支持多模型自动切换，自动识别接口格式"""
    models = [m.strip() for m in config.AI_MODEL.split(",")]

    # 根据模型名称判断使用哪种 API 格式
    ANTHROPIC_PREFIXES = ("claude",)

    for model in models:
        try:
            headers = {
                "Authorization": f"Bearer {config.AI_API_KEY}",
                "Content-Type": "application/json"
            }

            is_anthropic = model.lower().startswith(ANTHROPIC_PREFIXES)

            if is_anthropic:
                url = f"{config.AI_API_BASE}/v1/messages"
                headers["x-api-key"] = config.AI_API_KEY
                headers["anthropic-version"] = "2023-06-01"
                payload = {
                    "model": model,
                    "max_tokens": config.AI_MAX_TOKENS,
                    "system": config.SYSTEM_PROMPT,
                    "messages": messages
                }
                resp = requests.post(url, headers=headers, json=payload, timeout=60)
                result = resp.json()
                print(f"[AI响应 {model}] {json.dumps(result, ensure_ascii=False)[:200]}")
                if "error" in result:
                    print(f"[模型 {model} 失败] 尝试下一个...")
                    continue
                return result["content"][0]["text"]
            else:
                url = f"{config.AI_API_BASE}/v1/chat/completions"
                payload = {
                    "model": model,
                    "max_tokens": config.AI_MAX_TOKENS,
                    "messages": [{"role": "system", "content": config.SYSTEM_PROMPT}] + messages
                }
                resp = requests.post(url, headers=headers, json=payload, timeout=60)
                result = resp.json()
                print(f"[AI响应 {model}] {json.dumps(result, ensure_ascii=False)[:200]}")
                if "error" in result:
                    print(f"[模型 {model} 失败] 尝试下一个...")
                    continue
                return result["choices"][0]["message"]["content"]

        except Exception as e:
            print(f"[模型 {model} 调用失败] {e}")
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
        resp = requests.post(url, headers=headers, json=payload)
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
        resp = requests.patch(url, headers=headers, json=payload)
        result = resp.json()

        if result.get("code") == 0:
            print(f"[更新成功] {text[:50]}")
        else:
            print(f"更新消息失败: {result.get('code')}, {result.get('msg')}")
    except Exception as e:
        print(f"更新消息失败: {e}")


def truncate(text, max_len=MAX_MSG_LEN):
    """截断过长的消息"""
    if len(text) > max_len:
        return text[:max_len - 20] + "\n\n...(消息过长已截断)"
    return text


def process_message(event_data):
    """处理消息"""
    try:
        message = event_data.get("message", {})
        message_id = message.get("message_id")
        chat_id = message.get("chat_id")
        chat_type = message.get("chat_type", "")
        sender_id = event_data.get("sender", {}).get("sender_id", {}).get("open_id", "unknown")
        content = json.loads(message.get("content", "{}"))
        text = content.get("text", "").strip()
        mentions = event_data.get("message", {}).get("mentions", [])

        # 消息去重（TTLCache 自动过期）
        if message_id in processed_messages:
            return
        processed_messages[message_id] = True

        # 群聊中只回复 @机器人 的消息，私聊全部回复
        if chat_type == "group":
            bot_mentioned = False
            if mentions:
                for m in mentions:
                    mention_id = m.get("id", {}).get("open_id", "")
                    if mention_id == BOT_OPEN_ID:
                        bot_mentioned = True
                        break
            if not bot_mentioned:
                print(f"[跳过] 群聊消息未@机器人")
                return
            # 去掉 @机器人 的占位符
            for m in mentions:
                text = text.replace(m.get("key", ""), "").strip()

        # 跳过空消息
        if not text:
            return

        print(f"[消息] {text[:80]}")

        # 从排队状态中取出卡片 ID，更新为"思考中..."
        with pending_lock:
            thinking_id = pending_replies.pop(message_id, None)

        if thinking_id:
            update_card(thinking_id, "🤔 思考中...")
        else:
            thinking_id = reply_card(message_id, "🤔 思考中...")

        # 构建多轮对话上下文（按用户+群隔离）
        context_key = f"{sender_id}_{chat_id}"
        history = chat_history[context_key]
        history.append({"role": "user", "content": text})
        messages = list(history)

        # 调用 AI API
        reply_text = call_ai(messages)
        reply_text = truncate(reply_text)

        # 保存 AI 回复到上下文
        history.append({"role": "assistant", "content": reply_text})

        # 更新"思考中..."为实际回复
        if thinking_id:
            update_card(thinking_id, reply_text)
        else:
            reply_card(message_id, reply_text)

    except Exception as e:
        print(f"处理消息失败: {e}")


def handle_webhook():
    """处理飞书 Webhook 的核心逻辑"""
    try:
        data = request.json
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

        # 处理消息事件
        event = data.get("event", {})
        event_type = data.get("header", {}).get("event_type")
        print(f"[事件] 类型: {event_type}")

        if event_type == "im.message.receive_v1":
            # 入队前先发排队卡片
            msg = event.get("message", {})
            msg_id = msg.get("message_id")
            queue_size = message_queue.qsize()
            if queue_size > 0:
                card_id = reply_card(msg_id, f"⏳ 排队中... 前方还有 {queue_size} 人等待")
                if card_id:
                    with pending_lock:
                        pending_replies[msg_id] = card_id
            # 放入消息队列，按顺序处理
            message_queue.put(event)

        # 立即返回 200
        return jsonify({"code": 0})

    except Exception as e:
        print(f"[错误] Webhook 处理失败: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/', methods=['POST'])
def root_webhook():
    """根路径也处理 Webhook（兼容不同 URL 配置）"""
    return handle_webhook()


@app.route('/webhook', methods=['POST'])
def webhook():
    """接收飞书 Webhook"""
    return handle_webhook()


@app.route('/health', methods=['GET'])
@app.route('/', methods=['GET'])
def health():
    """健康检查"""
    return jsonify({"status": "ok"})


if __name__ == '__main__':
    print(f"飞书 Bot 启动中...")
    print(f"监听端口: {config.PORT}")
    get_bot_open_id()
    print(f"请确保 ngrok 已启动: ngrok http {config.PORT}")
    app.run(host='0.0.0.0', port=config.PORT, debug=False)
