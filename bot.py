import json
import time
import threading
import requests
from collections import defaultdict, deque
from cachetools import TTLCache
from flask import Flask, request, jsonify
import config

app = Flask(__name__)

# 消息去重（TTL 缓存，5 分钟过期，最多 2000 条）
processed_messages = TTLCache(maxsize=2000, ttl=300)

# 多轮对话上下文 {sender_chat_key: deque([(role, content), ...])}
chat_history = defaultdict(lambda: deque(maxlen=10))

# 飞书消息最大长度
MAX_MSG_LEN = 4000

# 机器人自身 open_id
BOT_OPEN_ID = None

# 飞书 tenant_access_token 缓存
_token_cache = {"token": None, "expire_at": 0}
_token_lock = threading.Lock()


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
        response = requests.post(url, json=payload, timeout=10)
        data = response.json()
        token = data.get("tenant_access_token")
        expire = data.get("expire", 7200)
        _token_cache["token"] = token
        _token_cache["expire_at"] = now + expire
        print(f"[Token] 已刷新，有效期 {expire}s")
        return token


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


def call_ai(messages):
    """调用 AI API，支持多分组多 Key 自动切换，统一 OpenAI 格式"""
    for group in config.AI_GROUPS:
        api_key = group["key"]
        group_name = group["name"]
        models = [m.strip() for m in group["models"].split(",")]

        for model in models:
            try:
                url = f"{config.AI_API_BASE}/v1/chat/completions"
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
                print(f"[AI响应 {group_name}/{model}] {json.dumps(result, ensure_ascii=False)[:200]}")

                if "error" in result:
                    print(f"[{group_name}/{model} 失败] 尝试下一个...")
                    continue

                return result["choices"][0]["message"]["content"]

            except Exception as e:
                print(f"[{group_name}/{model} 调用失败] {e}")
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


def truncate(text, max_len=MAX_MSG_LEN):
    if len(text) > max_len:
        return text[:max_len - 20] + "\n\n...(消息过长已截断)"
    return text


def process_message(event_data):
    """处理消息（在独立线程中运行）"""
    try:
        message = event_data.get("message", {})
        message_id = message.get("message_id")
        chat_id = message.get("chat_id")
        chat_type = message.get("chat_type", "")
        sender_id = event_data.get("sender", {}).get("sender_id", {}).get("open_id", "unknown")
        content = json.loads(message.get("content", "{}"))
        text = content.get("text", "").strip()
        mentions = message.get("mentions", [])

        # 消息去重
        if message_id in processed_messages:
            print(f"[去重] {message_id}")
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
                print(f"[跳过] 群聊消息未@机器人, BOT_OPEN_ID={BOT_OPEN_ID}")
                return
            for m in mentions:
                text = text.replace(m.get("key", ""), "").strip()

        if not text:
            return

        print(f"[消息] {text[:80]}")

        # 先回复"思考中..."
        thinking_id = reply_card(message_id, "🤔 思考中...")

        # 构建多轮对话上下文
        context_key = f"{sender_id}_{chat_id}"
        history = chat_history[context_key]
        history.append({"role": "user", "content": text})
        messages = list(history)

        # 调用 AI
        reply_text = call_ai(messages)
        reply_text = truncate(reply_text)

        # 保存回复到上下文
        history.append({"role": "assistant", "content": reply_text})

        # 更新卡片为实际回复
        if thinking_id:
            update_card(thinking_id, reply_text)
        else:
            reply_card(message_id, reply_text)

        print(f"[完成] 回复: {reply_text[:50]}")

    except Exception as e:
        print(f"处理消息失败: {e}")
        import traceback
        traceback.print_exc()


def handle_webhook():
    """处理飞书 Webhook"""
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

        event = data.get("event", {})
        event_type = data.get("header", {}).get("event_type")
        print(f"[事件] 类型: {event_type}")

        if event_type == "im.message.receive_v1":
            # 直接开线程处理，不用队列
            t = threading.Thread(target=process_message, args=(event,), daemon=True)
            t.start()

        return jsonify({"code": 0})

    except Exception as e:
        print(f"[错误] Webhook 处理失败: {e}")
        return jsonify({"error": str(e)}), 500


# 初始化 BOT_OPEN_ID
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
    print(f"飞书 Bot 启动中...")
    print(f"监听端口: {config.PORT}")
    get_bot_open_id()
    app.run(host='0.0.0.0', port=config.PORT, debug=False)
