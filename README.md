# 🤖 Feishu Family Bot — 飞书群聊 AI 助手

基于 Flask 的飞书群聊智能机器人，支持多 AI 模型自动切换、多轮对话、图片识别、聊天指令，适合部署在 Render 免费实例上做家庭群轻量使用。

## ✨ 特性

- 多模型分组 & 自动降级：按优先级自动切换，单个模型失败自动尝试下一个
- 默认 GPT 分组：开箱即用支持 `gpt-5.4,gpt-5.3,gpt-5.2`
- 聊天内切换模型：通过 `/model` 指令快速查看当前分组配置并切换
- 图片识别：发送图片给 Bot，AI 自动识别并回复（需模型支持 vision）
- 多轮对话：基于 sender + chat 维度保留最近 10 轮上下文（30 分钟过期）
- 回复样式可切换：默认使用卡片消息渲染正式回复，也可切换为普通文本消息
- 群聊 @触发：群聊中仅响应 @机器人 的消息，私聊自动回复
- 消息去重：TTL 缓存防止重复处理（5 分钟窗口）
- Token 缓存：飞书 tenant_access_token 自动缓存 & 提前刷新

## 💬 聊天指令

| 指令 | 功能 |
|------|------|
| `/model` | 查看可用模型分组和当前选择 |
| `/model 分组名` | 切换到指定模型分组 |
| `/auto` | 恢复自动切换模式 |
| `/clear` | 清除对话历史 |
| `/help` | 显示帮助信息 |

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
| `AI_API_BASE` | 全局默认 AI API 地址（分组未单独配置时使用） | ❌ |
| `AI_KEY_CLAUDE` | Claude 分组 API Key | 至少填一组 |
| `AI_BASE_CLAUDE` | Claude 分组 API 地址（不填则用全局） | ❌ |
| `AI_KEY_CODEX` | GPT 分组 API Key | 至少填一组 |
| `AI_BASE_CODEX` | GPT 分组 API 地址（不填则用全局） | ❌ |
| `AI_KEY_CN` | 国内模型分组 API Key | 至少填一组 |
| `AI_BASE_CN` | 国内模型分组 API 地址（不填则用全局） | ❌ |
| `AI_KEY_GEMINI` | Gemini 分组 API Key | 至少填一组 |
| `AI_BASE_GEMINI` | Gemini 分组 API 地址（不填则用全局） | ❌ |
| `AI_MAX_TOKENS` | 最大输出 token 数（默认 2048） | ❌ |
| `AI_MODELS_CODEX` | GPT 分组模型列表（默认 `gpt-5.4,gpt-5.3,gpt-5.2`） | ❌ |
| `AI_MODELS_GEMINI` | Gemini 分组模型列表（默认 `gemini-3.1-pro-preview,gemini-3-pro`） | ❌ |
| `SYSTEM_PROMPT` | 自定义机器人人设提示词 | ❌ |
| `MESSAGE_WORKERS` | 后台消息处理线程数（默认 `2`） | ❌ |
| `FEISHU_REPLY_STYLE` | 回复样式，默认 `card`，可选 `text` | ❌ |

### 4. 配置飞书 Webhook

1. 访问 [飞书开放平台](https://open.feishu.cn/app) → 创建/选择应用
2. 事件订阅 → 请求地址填入：`https://your-app.onrender.com/webhook`
3. 订阅事件：`im.message.receive_v1`
4. 权限管理 → 添加权限：
   - `im:message`（接收消息）
   - `im:message:send_as_bot`（发送消息）
   - `im:message:resource`（读取图片资源，图片识别需要）
5. 发布应用版本，将 Bot 添加到群聊

### 5. Render 免费实例注意事项

- Render 免费 Web Service 大约 15 分钟无请求会休眠
- 休眠后再次收到请求，冷启动可能会延迟几十秒
- 如果你只是家庭群轻量使用，免费实例通常够用，但不适合高并发和长期后台任务
- 建议用 `cron-job.org` 或其他外部保活服务每 14 分钟访问一次 `/health`
- 保活只解决冷启动问题，不等于长期记忆或后台常驻任务

## 🔧 自定义

### 调整模型分组

模型列表支持通过环境变量覆盖：

```bash
AI_MODELS_CLAUDE="claude-sonnet-4-5-20250929,claude-haiku-4-5"
AI_MODELS_CODEX="gpt-5.4,gpt-5.3,gpt-5.2"
AI_MODELS_CN="glm-5,kimi-k2.5"
AI_MODELS_GEMINI="gemini-3.1-pro-preview,gemini-3-pro"
```

分组按 `config.py` 中 `AI_GROUPS` 的顺序依次尝试，没有配置 Key 的分组会自动跳过。用户也可以在飞书中通过 `/model 分组名` 指令手动切换。

### 修改机器人人设

设置环境变量 `SYSTEM_PROMPT` 即可自定义机器人性格和能力，默认人设为家庭助手。

### 推荐的 Render 免费实例配置

```bash
MESSAGE_WORKERS=2
FEISHU_REPLY_STYLE=card
AI_MAX_TOKENS=2048
```

说明：

- `MESSAGE_WORKERS=2` 更适合 Render 免费实例的 `0.1 CPU / 512 MB`
- `FEISHU_REPLY_STYLE=card` 更适合需要结构化排版的正式回复

## ❓ 常见问题

| 问题 | 排查方向 |
|------|----------|
| Bot 没有回复 | 检查 Render 日志、Webhook URL 是否正确、Bot 是否已加入群聊 |
| AI 调用失败 | 检查 API Key 和余额，查看 Render 日志中的具体错误 |
| 群聊不回复 | 确认消息中 @了机器人，检查日志中 BOT_OPEN_ID 是否正确获取 |
| 图片识别不工作 | 确认已添加 `im:message:resource` 权限，且使用的模型支持 vision |
| Render 访问变慢 | 很可能是免费实例休眠后的冷启动，确认是否已配置 14 分钟保活访问 `/health` |

## 📄 License

MIT
