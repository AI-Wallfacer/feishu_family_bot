import os

# 飞书配置（必须通过环境变量设置）
FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
FEISHU_VERIFICATION_TOKEN = os.environ.get("FEISHU_VERIFICATION_TOKEN", "")
FEISHU_ENCRYPT_KEY = os.environ.get("FEISHU_ENCRYPT_KEY", "")

# AI API 配置
AI_API_BASE = os.environ.get("AI_API_BASE", "")  # 全局默认，分组未配置时使用
AI_MAX_TOKENS = int(os.environ.get("AI_MAX_TOKENS", "4096"))

# 多分组模型配置：每组有自己的 Key、API 地址和模型列表，按优先级排列
AI_GROUPS = [
    {
        "name": "Claude",
        "key": os.environ.get("AI_KEY_CLAUDE", ""),
        "base": os.environ.get("AI_BASE_CLAUDE", ""),
        "models": os.environ.get("AI_MODELS_CLAUDE", "claude-opus-4-6,claude-sonnet-4-5-20250929,claude-haiku-4-5")
    },
    {
        "name": "codex",
        "key": os.environ.get("AI_KEY_CODEX", ""),
        "base": os.environ.get("AI_BASE_CODEX", ""),
        "models": os.environ.get("AI_MODELS_CODEX", "gpt-5.2")
    },
    {
        "name": "国内模型",
        "key": os.environ.get("AI_KEY_CN", ""),
        "base": os.environ.get("AI_BASE_CN", ""),
        "models": os.environ.get("AI_MODELS_CN", "glm-4.7,kimi-k2.5")
    },
    {
        "name": "Gemini",
        "key": os.environ.get("AI_KEY_GEMINI", ""),
        "base": os.environ.get("AI_BASE_GEMINI", ""),
        "models": os.environ.get("AI_MODELS_GEMINI", "gemini-3-pro-preview,gemini-3-flash-preview")
    },
]

# System Prompt
SYSTEM_PROMPT = os.environ.get("SYSTEM_PROMPT", """你是一个温馨的家庭助手机器人。

你的性格特点：
- 温暖、耐心、有亲和力，像家里的一位贴心成员
- 回答问题时通俗易懂，避免过于生硬的技术语言
- 适当使用轻松幽默的语气，让氛围更温馨

你可以帮助用户：
- 解答学习和生活中的各种问题
- 提供菜谱、健康建议、生活小窍门
- 陪聊天、讲故事、推荐电影和书籍
- 帮忙规划日程、提醒重要事项

请用中文回复，语气自然亲切。""")

# 服务配置
PORT = int(os.environ.get("PORT", "5000"))
