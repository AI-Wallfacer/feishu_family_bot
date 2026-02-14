# 🤖 Feishu Family Bot — 飞书群聊 AI 助手

基于 Flask 的飞书群聊智能机器人，支持多 AI 模型自动切换、多轮对话上下文，部署在 Render 云平台。

## ✨ 特性

- 多模型分组 & 自动降级：Claude → GPT → 国内模型 → Gemini，按优先级自动切换，单个模型失败自动尝试下一个
- 多轮对话：基于 sender + chat 维度保留最近 10 轮上下文
- 卡片消息：先回复"🤔 思考中..."，AI 响应后原地更新为实际回复
- 群聊 @触发：群聊中仅响应 @机器人 的消息，私聊自动回复
- 消息去重：TTL 缓存防止重复处理（5 分钟窗口，最多 2000 条）
- Token 缓存：飞书 tenant_access_token 自动缓存 & 提前刷新

## 📁 项目结构

```
├── bot.py              # 主程序：Webhook 处理、AI 调用、消息收发
├── config.py           # 配置文件：飞书凭证、AI 分组、系统提示词
├── render.yaml         # Render 云平台部署配置
└── requirements.txt    # Python 依赖
```

## 🚀 部署到 Render

### 1. Fork 本仓库到你的 GitHub

### 2. 在 [Render](https://render.com) 创建 Web Service

关联你 Fork 的仓库，Render 会自动识别 `render.yaml` 完成构建配置。

### 3. 配置环境变量

在 Render 的 Environment 面板中设置以下变量：

| 变量名 | 说明 | 必填 |
|--------|------|------|
| `FEISHU_APP_ID` | 飞书应用 App ID | ✅ |
| `FEISHU_APP_SECRET` | 飞书应用 App Secret | ✅ |
| `FEISHU_VERIFICATION_TOKEN` | 飞书事件订阅验证 Token | ✅ |
| `AI_API_BASE` | AI API 地址（兼容 OpenAI 格式） | ✅ |
| `AI_KEY_CLAUDE` | Claude 分组 API Key | 至少填一组 |
| `AI_KEY_CODEX` | GPT 分组 API Key | 至少填一组 |
| `AI_KEY_CN` | 国内模型分组 API Key | 至少填一组 |
| `AI_KEY_GEMINI` | Gemini 分组 API Key | 至少填一组 |
| `AI_MAX_TOKENS` | 最大输出 token 数（默认 4096） | ❌ |
| `SYSTEM_PROMPT` | 自定义机器人人设提示词 | ❌ |

### 4. 配置飞书 Webhook

1. 访问 [飞书开放平台](https://open.feishu.cn/app) → 创建/选择应用
2. 事件订阅 → 请求地址填入：`https://your-app.onrender.com/webhook`
3. 订阅事件：`im.message.receive_v1`
4. 权限管理 → 添加权限：
   - `im:message`（接收消息）
   - `im:message:send_as_bot`（发送消息）
5. 发布应用版本，将 Bot 添加到群聊

## 🔧 自定义

### 调整模型分组

模型列表也支持通过环境变量覆盖：

```bash
AI_MODELS_CLAUDE="claude-sonnet-4-5-20250929,claude-haiku-4-5"
AI_MODELS_CODEX="gpt-5.2"
AI_MODELS_CN="glm-4.7,kimi-k2.5"
AI_MODELS_GEMINI="gemini-3-pro-preview"
```

分组按 `config.py` 中 `AI_GROUPS` 的顺序依次尝试，没有配置 Key 的分组会自动跳过。

### 修改机器人人设

设置环境变量 `SYSTEM_PROMPT` 即可自定义机器人性格和能力，默认人设为家庭助手。

## ❓ 常见问题

| 问题 | 排查方向 |
|------|----------|
| Bot 没有回复 | 检查 Render 日志、Webhook URL 是否正确、Bot 是否已加入群聊 |
| AI 调用失败 | 检查 API Key 和余额，查看 Render 日志中的具体错误 |
| 群聊不回复 | 确认消息中 @了机器人，检查日志中 BOT_OPEN_ID 是否正确获取 |

## � License

MIT
