import os

# 飞书配置（必须通过环境变量设置）
FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
FEISHU_VERIFICATION_TOKEN = os.environ.get("FEISHU_VERIFICATION_TOKEN", "")

# AI API 配置
AI_API_BASE = os.environ.get("AI_API_BASE", "")  # 仅用于首次导入默认分组或兼容旧配置
AI_MAX_TOKENS = int(os.environ.get("AI_MAX_TOKENS", "2048"))

# 多分组模型配置：用于初始化数据库中的默认值
AI_GROUPS = [
    {
        "name": "GPT",
        "key": os.environ.get("AI_KEY_GPT")
        or os.environ.get("AI_KEY_OPENAI")
        or os.environ.get("AI_KEY_CODEX", ""),
        "base": os.environ.get("AI_BASE_GPT")
        or os.environ.get("AI_BASE_OPENAI")
        or os.environ.get("AI_BASE_CODEX", ""),
        "models": os.environ.get("AI_MODELS_GPT")
        or os.environ.get("AI_MODELS_OPENAI")
        or os.environ.get("AI_MODELS_CODEX", "gpt-5.4,gpt-5.3,gpt-5.2"),
    },
    {
        "name": "Claude",
        "key": os.environ.get("AI_KEY_CLAUDE", ""),
        "base": os.environ.get("AI_BASE_CLAUDE", ""),
        "models": os.environ.get(
            "AI_MODELS_CLAUDE",
            "claude-opus-4-6,claude-sonnet-4-5-20250929,claude-haiku-4-5",
        ),
    },
    {
        "name": "国内模型",
        "key": os.environ.get("AI_KEY_CN", ""),
        "base": os.environ.get("AI_BASE_CN", ""),
        "models": os.environ.get("AI_MODELS_CN", "glm-4.7,kimi-k2.5"),
    },
    {
        "name": "Gemini",
        "key": os.environ.get("AI_KEY_GEMINI", ""),
        "base": os.environ.get("AI_BASE_GEMINI", ""),
        "models": os.environ.get("AI_MODELS_GEMINI", "gemini-3.1-pro-preview,gemini-3-pro"),
    },
]

# System Prompt
SYSTEM_PROMPT = os.environ.get(
    "SYSTEM_PROMPT",
    """你是一个温馨的家庭助手机器人。

你的性格特点：
- 温暖、耐心、有亲和力，像家里的一位贴心成员
- 回答问题时通俗易懂，避免过于生硬的技术语言
- 适当使用轻松幽默的语气，让氛围更温馨

你可以帮助用户：
- 解答学习和生活中的各种问题
- 提供菜谱、健康建议、生活小窍门
- 陪聊天、讲故事、推荐电影和书籍
- 帮忙规划日程、提醒重要事项

数学公式格式要求：
- 禁止使用 LaTeX 格式（如 $x^2$ 或 $$\\frac{a}{b}$$）
- 使用纯文本表示：x^2、a/b、√x、∑、∫ 等 Unicode 符号
- 复杂公式分行展示，用缩进和符号对齐

请用中文回复，语气自然亲切。""",
)

# Web 后台与本地存储
BOT_DB_PATH = os.environ.get("BOT_DB_PATH", "data/bot.db")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
FLASK_SECRET_KEY = (
    os.environ.get("FLASK_SECRET_KEY")
    or FEISHU_APP_SECRET
    or "change-me-before-production"
)
SETTINGS_ENCRYPTION_KEY = (
    os.environ.get("SETTINGS_ENCRYPTION_KEY")
    or FLASK_SECRET_KEY
)

# 服务配置
PORT = int(os.environ.get("PORT", "5000"))
QUEUE_MAX_PENDING = int(os.environ.get("QUEUE_MAX_PENDING", "8"))
QUEUE_STALE_SECONDS = int(os.environ.get("QUEUE_STALE_SECONDS", "300"))
DEFAULT_TIMEZONE = os.environ.get("BOT_TIMEZONE", "Asia/Shanghai")
SCHEDULER_POLL_SECONDS = int(os.environ.get("SCHEDULER_POLL_SECONDS", "15"))
SESSION_LIFETIME_HOURS = int(os.environ.get("SESSION_LIFETIME_HOURS", "12"))
