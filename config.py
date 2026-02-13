import os

# 飞书配置
FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", "cli_a90437f4d238dbd2")
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "eAgo2QXqwRFk1hVn9chsIefviKdKGvF2")
FEISHU_VERIFICATION_TOKEN = os.environ.get("FEISHU_VERIFICATION_TOKEN", "utbzWcGrSHSiLUsWvdtA3bo0widrYcQI")
FEISHU_ENCRYPT_KEY = os.environ.get("FEISHU_ENCRYPT_KEY", "")

# AI API 配置
AI_API_KEY = os.environ.get("AI_API_KEY", "sk-wyoOOY7oAKD3KQaRI6vOLRWFrzJ0ZfHji4Xe2T5RfqD2O36H")
AI_API_BASE = os.environ.get("AI_API_BASE", "https://api.wow3.top")
AI_MODEL = os.environ.get("AI_MODEL", "gemini-3-flash-preview,kimi-k2.5,glm-4.7,gpt-5.2,claude-opus-4-6,claude-opus-4-6-thinking,gpt-5.2-codex")
AI_MODE = os.environ.get("AI_MODE", "openai")
AI_MAX_TOKENS = int(os.environ.get("AI_MAX_TOKENS", "4096"))

# System Prompt
SYSTEM_PROMPT = os.environ.get("SYSTEM_PROMPT", """你是张小凡，一个温馨的家庭助手机器人。你服务于一个四口之家：爸爸、妈妈、哥哥和妹妹。

你的性格特点：
- 温暖、耐心、有亲和力，像家里的一位贴心成员
- 对爸爸妈妈礼貌尊重，对哥哥妹妹活泼友好
- 回答问题时通俗易懂，避免过于生硬的技术语言
- 适当使用轻松幽默的语气，让家庭氛围更温馨

你可以帮助家人：
- 解答学习和生活中的各种问题
- 提供菜谱、健康建议、生活小窍门
- 陪聊天、讲故事、推荐电影和书籍
- 帮忙规划日程、提醒重要事项

请用中文回复，语气自然亲切。""")

# 服务配置
PORT = int(os.environ.get("PORT", "5000"))
