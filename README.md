# 🤖 Feishu Family Bot - 飞书群聊 AI 助手

基于 Flask 的飞书群聊智能机器人，面向家庭群和小范围使用场景。V2 版本改成了单体 Flask + 单机 SQLite + 服务端渲染管理面板 + 单消费者消息队列的轻量架构，适合部署在 Render 免费实例上。

## ✨ 特性

- 多模型分组管理：支持多个 AI 分组、自动降级和手动切换
- Web 管理面板：可在网页端管理模型分组、长期记忆、定时任务和触发规则
- 单消费者队列：消息按顺序排队处理，并显示“前方还有几条”
- 关键词触发：支持配置关键词规则，命中后可直接回复或全触发
- 群聊长期记忆：只在群聊场景启用，私聊不写入也不读取长期记忆
- 定时消息：支持一次性、每天、每周触发
- 飞书卡片回复：统一使用更好看的卡片式输出
- 消息去重：TTL 缓存防止重复处理
- Token 缓存：飞书 tenant_access_token 自动缓存并提前刷新

## 💬 聊天指令

| 指令 | 功能 |
|------|------|
| `/model` | 查看当前可用模型分组和选择状态 |
| `/model 分组名` | 切换到指定模型分组 |
| `/auto` | 恢复自动切换模式 |
| `/clear` | 清除当前会话的对话历史 |
| `/help` | 显示帮助信息 |

## 📁 项目结构

```
├── bot.py              # 主程序：Webhook、队列、后台路由、飞书消息处理
├── storage.py          # SQLite、加密、模型/记忆/任务/触发规则存储
├── templates/          # Flask 服务端渲染后台模板
├── config.py           # 配置文件：飞书凭证、AI 分组、服务参数
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
| `BOT_DB_PATH` | SQLite 数据库路径，默认 `data/bot.db` | ❌ |
| `ADMIN_PASSWORD` | 管理面板登录密码 | ✅ |
| `FLASK_SECRET_KEY` | Flask 会话签名密钥 | ✅ |
| `SETTINGS_ENCRYPTION_KEY` | 加密保存 API Key 的密钥 | ✅ |
| `AI_API_BASE` | 全局默认 AI API 地址（分组未单独配置时使用） | ❌ |
| `AI_KEY_CLAUDE` | Claude 分组 API Key | 至少填一组 |
| `AI_BASE_CLAUDE` | Claude 分组 API 地址（不填则用全局） | ❌ |
| `AI_KEY_CODEX` | GPT 分组 API Key | 至少填一组 |
| `AI_BASE_CODEX` | GPT 分组 API 地址（不填则用全局） | ❌ |
| `AI_KEY_CN` | 国内模型分组 API Key | 至少填一组 |
| `AI_BASE_CN` | 国内模型分组 API 地址（不填则用全局） | ❌ |
| `AI_KEY_GEMINI` | Gemini 分组 API Key | 至少填一组 |
| `AI_BASE_GEMINI` | Gemini 分组 API 地址（不填则用全局） | ❌ |
| `AI_MAX_TOKENS` | 最大输出 token 数，默认 `2048` | ❌ |
| `AI_MODELS_CODEX` | GPT 分组模型列表 | ❌ |
| `AI_MODELS_GEMINI` | Gemini 分组模型列表 | ❌ |
| `BOT_TIMEZONE` | 定时任务时区，默认 `Asia/Shanghai` | ❌ |
| `QUEUE_MAX_PENDING` | 队列最大等待数 | ❌ |
| `QUEUE_STALE_SECONDS` | 队列任务超时时间（秒） | ❌ |
| `SCHEDULER_POLL_SECONDS` | 定时任务轮询间隔（秒） | ❌ |
| `SYSTEM_PROMPT` | 自定义机器人人设提示词 | ❌ |

### 4. 配置飞书 Webhook

1. 访问 [飞书开放平台](https://open.feishu.cn/app) 创建或选择应用
2. 事件订阅中将请求地址填入：`https://your-app.onrender.com/webhook`
3. 订阅事件：`im.message.receive_v1`
4. 权限管理中添加权限：
   - `im:message`（接收消息）
   - `im:message:send_as_bot`（发送消息）
   - `im:message:resource`（读取图片资源，图片识别需要）
5. 发布应用版本，将 Bot 添加到群聊

### 5. 管理面板

部署后可访问管理面板管理：

- AI 分组和模型列表
- 长期记忆
- 触发规则
- 定时消息
- 队列状态

建议给后台设置强密码，并使用独立的 `FLASK_SECRET_KEY` 和 `SETTINGS_ENCRYPTION_KEY`。

### 6. Render 免费实例注意事项

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

分组按配置顺序依次尝试，没有配置 Key 的分组会自动跳过。用户也可以在飞书中通过 `/model 分组名` 指令手动切换。

### 修改机器人人设

设置环境变量 `SYSTEM_PROMPT` 即可自定义机器人性格和能力，默认人设为家庭助手。

### 推荐的 Render 免费实例配置

```bash
AI_MAX_TOKENS=2048
QUEUE_MAX_PENDING=8
QUEUE_STALE_SECONDS=300
SCHEDULER_POLL_SECONDS=15
```

## ❓ 常见问题

| 问题 | 排查方向 |
|------|----------|
| Bot 没有回复 | 检查 Render 日志、Webhook URL 是否正确、Bot 是否已加入群聊 |
| AI 调用失败 | 检查 API Key 和余额，查看 Render 日志中的具体错误 |
| 群聊不回复 | 确认消息中 @了机器人，检查日志中 BOT_OPEN_ID 是否正确获取 |
| 图片识别不工作 | 确认已添加 `im:message:resource` 权限，且使用的模型支持 vision |
| Render 访问变慢 | 很可能是免费实例休眠后的冷启动，确认是否已配置 14 分钟保活访问 `/health` |
| 私聊没有长期记忆 | 这是 V2 的设计：私聊只做正常对话，不写入也不读取长期记忆 |

## 📄 License

MIT
