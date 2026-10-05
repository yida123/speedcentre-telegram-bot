# speed_bot

基于 [SpeedCentre+ 用户 API](https://api.speedcentre.plus) 的 Telegram 群组测速 Bot。

用来在机场群里展示节点当前的状态：**只测速配置文件里固定的订阅**，只在授权群组中可用，
每人每天限测 3 次（可配置），管理员不限。

## 使用流程

群成员在群里发 `/speed`：

1. **选择订阅**：配置了多个订阅时弹出订阅按钮；只有一个订阅、或直接发 `/speed 订阅名` 时跳过。
2. **选择测速后端**：「🤖 自动选择」或具体后端，按钮显示为 `上海电信@2Gbps (SHCT)`，每页 5 个，可翻页。
3. **选择排序方式**：订阅顺序（默认）/ 节点名（升序）/ 平均速度（升序）/ 平均速度（降序）。
4. 提交后显示「⏳ 任务 3399 准备中…」→「⚡ 任务 3399 进行中… `[████░░░░] 25%`」，带「❌ 取消任务」按钮。
5. 完成后在群里发送结果图并 @ 发起人；配置了 `SCP_SHARE_URL` 时附「📊 查看详情」分享链接。

每一步都可以点「❌ 终止操作」取消，只有发起人（或管理员）能点菜单按钮，菜单 10 分钟内有效。
**任务提交成功才计入次数**，中途终止不计。

| 命令 | 说明 |
| --- | --- |
| `/speed` | 测速（按上面的流程选择） |
| `/speed 订阅名` | 直接测速指定订阅 |
| `/sub` | 查看可测速的订阅名和自己今日剩余次数（不显示订阅地址） |
| `/backends` | 测试后端列表：🟢 在线、🔴 离线、🚫 不可选 |
| `/result <任务ID>` | 重新获取结果图 |
| `/id` | 查看群组 ID / 用户 ID（私聊也可用） |
| `/help` | 帮助 |

可选参数：`-f 正则` 只测名称匹配的节点（如 `/speed -f "香港|HK"`），`-s 后端ID或名称` 直接指定后端。

为防止泄露，群里出现节点链接（`ss://`、`vmess://` 等）或疑似订阅的链接会被**立即删除**，
“疑似订阅”默认按 `token=`、`/sub`、`subscribe`、`/api/v1/client`、`clash` 等特征判断，可用 `SUB_LINK_PATTERN` 自定义。
`/speed` 后面带链接同样会被删除并拒绝。

## 配置订阅

订阅写在 `subscriptions.yaml`（参考 [subscriptions.example.yaml](subscriptions.example.yaml)）：

```yaml
subscriptions:
  - name: "3399"
    url: https://example.com/api/v1/client/subscribe?token=xxxxxxxx
  - name: "IPLC"
    url: https://example.com/api/v1/client/subscribe?token=yyyyyyyy
```

`name` 是群里使用的名称，`url` 可以是订阅链接或节点分享链接（支持 Clash YAML、base64 订阅，以及
`ss://` `ssr://` `vmess://` `vless://` `trojan://` `hysteria://` `hy2://` `tuic://` `socks5://` `anytls://`）。
修改后重启 Bot 生效。这个文件包含订阅地址，安装脚本会把权限设为仅服务用户可读。

出于安全考虑，拉取订阅时会拒绝指向内网、本机或保留地址的链接（包括重定向后的地址）。

## 部署（Debian / Ubuntu 服务器）

### 1. 准备 Bot

在 [@BotFather](https://t.me/BotFather) 创建 Bot 拿到 Token，把 Bot 拉进群，
并**设为群管理员、授予「删除消息」权限**（自动删除订阅链接需要该权限，管理员身份也能让 Bot 看到群内所有消息）。

服务器需要能访问 Telegram、`api.speedcentre.plus` 以及订阅地址。国内服务器请在 `.env` 里加
`HTTPS_PROXY=http://代理地址:端口`。

### 2. 安装（systemd，推荐）

```bash
sudo apt update && sudo apt install -y git
sudo git clone https://github.com/yida123/speed_bot.git /opt/speed_bot
# 私有仓库：用 https://<用户名>:<token>@github.com/yida123/speed_bot.git，或配置 deploy key
sudo bash /opt/speed_bot/deploy/install.sh
```

脚本会安装 Python、创建 `speedbot` 系统用户和虚拟环境、注册开机自启的 `speed-bot` 服务，
并生成 `/opt/speed_bot/.env` 和 `/opt/speed_bot/subscriptions.yaml`。

### 3. 填写配置并启动

```bash
sudo nano /opt/speed_bot/.env                 # 填 TG_BOT_TOKEN、SCP_API_KEY、ADMIN_USER_IDS
sudo nano /opt/speed_bot/subscriptions.yaml   # 填要测速的订阅
sudo systemctl start speed-bot
```

在群里发 `/id` 拿到群组 ID，填到 `.env` 的 `ALLOWED_CHAT_IDS`（**务必设置**，否则任何群都能消耗你的积分），然后重启：

```bash
sudo systemctl restart speed-bot
```

### 日常运维

```bash
sudo systemctl status speed-bot           # 运行状态
sudo journalctl -u speed-bot -f           # 实时日志
sudo systemctl restart speed-bot          # 改完配置后重启
sudo bash /opt/speed_bot/deploy/install.sh --update   # 拉取最新代码并重启
```

服务挂掉会在 5 秒后自动重启，服务器重启后自动运行。每日次数记录保存在 `/var/lib/speed-bot/usage.json`，重启不清零。

### 或者用 Docker

```bash
sudo apt install -y docker.io docker-compose-v2
cd /opt/speed_bot
cp .env.example .env && nano .env
cp subscriptions.example.yaml subscriptions.yaml && nano subscriptions.yaml
sudo docker compose up -d --build         # 启动
sudo docker compose logs -f               # 日志
git pull && sudo docker compose up -d --build   # 更新
```

### 本地直接运行（调试用）

```bash
pip install -r requirements.txt
cp .env.example .env && cp subscriptions.example.yaml subscriptions.yaml   # 然后编辑
python -m bot.main
```

## 配置项

见 [.env.example](.env.example)。主要有：

- `ALLOWED_CHAT_IDS`：授权群组，逗号分隔
- `ADMIN_USER_IDS`：管理员，测速不限次数，可操作他人的菜单、取消他人任务
- `SUBSCRIPTIONS_FILE`：订阅配置文件路径，默认 `subscriptions.yaml`
- `DAILY_LIMIT`：每人每天测速次数，默认 3，`0` 表示不限
- `TIMEZONE`：按哪个时区计算“每天”，默认 `Asia/Shanghai`
- `MAX_NODES`：单次最多节点数（超过的部分会被截断）
- `MAX_TASKS_PER_CHAT`：每个群同时运行的任务数
- `DEFAULT_SLAVE_ID`：默认后端，留空自动选择
- `ALLOWED_BACKENDS`：允许选择的后端 ID，逗号分隔，留空为全部可用后端
- `BACKEND_SELECT` / `SORT_SELECT`：是否让用户选择后端 / 排序方式（默认 true；关闭时用默认后端、按平均速度降序）
- `SCP_TASK_URL`：网页查看任务的链接模板，如 `https://网页地址/tasks/{task_id}`，配置后进度消息显示「在 SpeedCentre+ 查看」
- `SCP_SHARE_URL`：分享页链接模板，如 `https://网页地址/share/{uuid}`，配置后结果自动创建分享（隐藏节点地址等敏感信息）并显示「查看详情」
- `DELETE_SUB_MESSAGE`：群里出现订阅/节点链接时自动删除（默认开启）
- `SUB_LINK_PATTERN`：自定义“疑似订阅链接”的判断正则

## 测试

```bash
pip install pytest
python -m pytest
```
