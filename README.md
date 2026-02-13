# 飞书群聊 AI Bot

基于 Flask + Claude API 的飞书群聊机器人，本地运行，无需云服务器。

## 环境要求

- Python 3.8+
- ngrok（用于内网穿透）
- 飞书开放平台应用（App ID 和 Secret）
- Claude API Key

## 安装步骤

1. 安装 Python 依赖：
```bash
pip install -r requirements.txt
```

2. 下载并安装 ngrok：
   - 访问 https://ngrok.com/download
   - 注册账号并获取 authtoken
   - 运行 `ngrok config add-authtoken <your_token>`

## 配置说明

编辑 `config.py` 文件，填入你的凭证：

```python
# 飞书配置（从飞书开放平台获取）
FEISHU_APP_ID = "cli_xxxxxxxx"
FEISHU_APP_SECRET = "xxxxxxxx"
FEISHU_VERIFICATION_TOKEN = "xxxxxxxx"

# Claude API 配置
CLAUDE_API_KEY = "sk-ant-xxxxxxxx"
```

### 获取飞书凭证

1. 访问 https://open.feishu.cn/app
2. 创建企业自建应用
3. 在"凭证与基础信息"页面获取 App ID 和 App Secret
4. 在"事件订阅"页面获取 Verification Token
5. 在"权限管理"中添加以下权限：
   - `im:message`（接收消息）
   - `im:message:send_as_bot`（发送消息）
6. 发布应用版本并添加到群聊

## 启动步骤

### 1. 启动 ngrok
```bash
ngrok http 5000
```

记录生成的公网 URL，例如：`https://xxxx-xx-xx-xx-xx.ngrok-free.app`

### 2. 配置飞书 Webhook

1. 进入飞书开放平台 → 你的应用 → 事件订阅
2. 设置请求地址：`https://xxxx-xx-xx-xx-xx.ngrok-free.app/webhook`
3. 订阅事件：`im.message.receive_v1`（接收消息）
4. 保存配置

### 3. 启动 Bot
```bash
python bot.py
```

看到以下输出表示启动成功：
```
飞书 Bot 启动中...
监听端口: 5000
请确保 ngrok 已启动: ngrok http 5000
 * Running on http://0.0.0.0:5000
```

## 使用说明

1. 将 Bot 添加到飞书群聊
2. 在群聊中发送消息（Bot 会自动回复所有消息）
3. 查看终端日志了解运行状态

## 常见问题

### Bot 没有回复消息？

1. 检查终端是否有错误日志
2. 确认 ngrok 正常运行且 URL 未过期
3. 确认飞书 Webhook 配置正确
4. 确认 Bot 已添加到群聊且有接收消息权限

### Claude API 调用失败？

1. 检查 API Key 是否正确
2. 确认账户有足够的额度
3. 查看终端错误信息

### ngrok URL 过期？

免费版 ngrok 每次重启 URL 会变化，需要：
1. 重新启动 ngrok
2. 在飞书开放平台更新 Webhook URL

## 注意事项

- 本地电脑需保持开机和网络连接
- ngrok 免费版有请求限制（40 请求/分钟）
- Claude API 按 token 计费，注意控制成本
- 建议在测试群聊中先测试，避免打扰他人
