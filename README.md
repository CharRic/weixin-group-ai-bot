# Weixin Group AI Bot

一个运行在 Linux 容器桌面中的微信群 AI 助手。它读取本机微信数据库，只响应真实的群聊 `@`，并通过桌面 UI 把回答发回原群。

## 主要能力

- 自动发现当前账号加入的群聊，不维护固定群白名单。
- 只处理消息元数据中真实 `atuserlist` 包含机器人账号的消息，文本里手写昵称不会触发。
- 每个群独立保存游标、待回复队列、摘要和对话历史，避免跨群混用上下文。
- 单群上下文预算默认为 12,800 tokens；超出预算后滚动压缩旧消息，并把摘要带入后续会话。
- 发送前后校验目标群、窗口标题与数据库落库结果；路由不确定时停止发送。
- 支持 `preview` 模式，确认行为正确后再切换为 `send`。

> 这是非官方的实验项目。微信客户端、数据库结构和 UI 随版本变化，自动化可能失效。请只在自己的账号与设备上使用，并遵守适用的服务条款和法律。

## 目录

```text
bot.py                 群消息轮询、上下文、摘要、发送与确认
ai_client.py           OpenAI-compatible Chat Completions 客户端
wechat_db.py           微信 SQLCipher 数据库密钥发现与只读快照
bot_ui/ui.py           容器桌面内的 UI 自动化脚本
services/weixin-bot    s6 服务入口
compose.yaml           WeChat Selkies 容器配置
```

## 环境要求

- Linux amd64 主机
- Docker Engine 与 Docker Compose v2
- Python 3.10+（用于初始化数据库密钥和运行测试）
- 一个 OpenAI-compatible Chat Completions API

## 快速开始

```bash
git clone https://github.com/CharRic/weixin-group-ai-bot.git
cd weixin-group-ai-bot
cp .env.example .env
cp ai.json.example ai.json
cp bot.json.example bot.json
chmod 600 .env ai.json bot.json
docker compose up -d
```

端口只绑定到 `127.0.0.1`。从另一台电脑访问桌面时，应使用 SSH 隧道，例如：

```bash
ssh -N -L 18080:127.0.0.1:18080 your-user@your-server
```

浏览器打开 `http://127.0.0.1:18080`，登录容器桌面并在微信中完成登录。不要把这两个端口直接暴露到公网。

随后在项目目录创建虚拟环境并提取当前账号数据库所需的密钥：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python wechat_db.py
```

把自己的微信 ID 写入 `bot.json` 的 `bot_id`。先保持 `"mode": "preview"`，观察日志确认只捕获预期消息；确认无误后再改为 `"mode": "send"`，并仅重启机器人子服务或等待其自动拉起。

```bash
docker compose logs -f desktop
```

配置文件包含 API 密钥和账号信息，运行数据包含群名与消息内容。它们均已列入 `.gitignore`，不要手动提交。

## AI 配置

`ai.json` 使用 OpenAI-compatible 接口：

```json
{
  "base_url": "https://api.example.com",
  "api_key": "your-key",
  "model": "your-model",
  "max_tokens": 1024,
  "thinking": "disabled"
}
```

如果服务商不支持 `thinking` 参数，可从配置中删除它。

## 测试

```bash
python3 -m unittest -v
```

测试覆盖真实艾特过滤、动态群发现、群级上下文隔离、滚动摘要，以及多项防串群发送校验。

## 安全说明

- 机器人以只读方式复制并解密微信数据库快照，不会修改微信数据库。
- UI 自动化天然比官方 API 脆弱；重复群名、窗口识别失败或发送确认异常时采用 fail-closed 策略。
- 在提交或分享日志前，先清理微信 ID、群 ID、群名、消息、API 密钥和数据库密钥。
