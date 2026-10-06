# speed_bot

基于 [SpeedCentre+ 用户 API](https://api.speedcentre.plus) 的 Telegram 群组测速 Bot。

| 谁 | 测什么 | 怎么触发 | 结果 | 次数 |
| --- | --- | --- | --- | --- |
| 系统 | 本机场订阅 | 按时间表自动（每日时间点、固定间隔或 cron） | 发到群里并置顶 | 不计 |
| 管理员 | 本机场订阅 | 群里 `/speed`（可选订阅/后端/排序）或 `/autotest`（立即跑一遍自动测速） | 发到群里 | 不限 |
| 群成员 | 自己的任意订阅（不限于本机场） | 群里 `/speed` → 点「🔒 私聊发送订阅」→ 私聊发送链接 | 发到群里并 @ 发起人 | 默认不限（可设每日次数、冷却时间、单次节点上限） |

管理员可以直接在私聊里管理授权群、管理员、封禁名单、本机场订阅、自动测速时间和各项限制，见[管理员命令](#管理员命令)。

## 使用流程

### 群成员测自己的订阅

1. 在群里发 `/speed`，Bot 回复「🔒 私聊发送订阅」按钮（只有一个授权群时，也可以直接私聊 Bot 发链接）。
2. 点按钮进入私聊，直接发送订阅链接或节点链接，可附加 `-f 关键词` 只测名称包含关键词的节点（多个用 | 分隔，如 `-f "香港|HK"`）、`-s 后端ID` 指定后端。
3. 在私聊里选择**测速后端**（每页 5 个，可翻页）和**排序方式**（订阅顺序 / 节点名 / 平均速度升降序）。
4. 提交后，群里显示「⏳ 任务 群友订阅 准备中…」→「⚡ 进行中… `[████░░░░] 25%`」，完成后发结果图并 @ 发起人。

Bot 会校验私聊用户是该群成员。默认不限测速次数；设置了 `DAILY_LIMIT` 时，**任务提交成功才计入次数**，中途点「❌ 终止操作」不计，次数按 `TIMEZONE` 每天 0 点重置，重启不清零。

### 本机场节点状态

- 按 `SCHEDULE_TIMES` / `/schedule` 设置的时间（默认每天 09:00）依次测速每个本机场订阅，结果图按平均速度降序发到群里，
  并**置顶最新一轮的结果**（自动取消置顶上一轮的，置顶通知会被删掉）。
- 自动测速失败、订阅拉取失败，或没有速度的节点占比达到 `ANOMALY_ALERT_PERCENT`（默认 50%）时，私聊提醒所有管理员。
- 管理员发 `/autotest` 立即跑一遍同样的测速（群里发：结果发到本群；私聊发：结果发到所有自动测速群）；
  发 `/speed` 则可以挑选订阅、后端和排序。

### 命令

| 命令 | 说明 |
| --- | --- |
| `/speed` | 群成员：引导私聊测速；管理员：测速本机场订阅（`/speed 订阅名` 直接指定） |
| `/autotest` | 管理员：立即测速所有本机场订阅 |
| `/sub` | 本机场订阅名、自动测速时间、自己今日剩余次数（不显示订阅地址） |
| `/backends` | 测试后端列表：🟢 在线、🔴 离线、🚫 不可选 |
| `/cancel` | 取消自己的任务：回复进度消息发送，或 `/cancel 任务ID`（只有一个进行中的任务时直接发送即可）；管理员可取消任何任务 |
| `/result <任务ID>` | 重新获取结果图 |
| `/id` | 查看群组 ID / 用户 ID（私聊也可用） |

为防止泄露，群里出现节点链接（`ss://`、`vmess://` 等）或疑似订阅的链接会被**立即删除**，并给发送者一个私聊按钮。
“疑似订阅”默认按 `token=`、`/sub`、`subscribe`、`/api/v1/client`、`clash` 等特征判断，可用 `SUB_LINK_PATTERN` 自定义。

### 管理员命令

建议在私聊里使用（在群里使用时，命令和回复同样会在 10 秒后删除）。管理员私聊 bot 发一次 `/start` 后，私聊的命令菜单里会多出这些命令，
`/help` 也会列出。修改**立即生效**，保存在 `DATA_DIR/settings.json`（systemd 部署为 `/var/lib/speed-bot/settings.json`），
重启不丢，并**优先于 `.env` 和 `subscriptions.yaml` 中的同名配置**。

| 命令 | 说明 |
| --- | --- |
| `/settings` | 查看全部设置 |
| `/schedule` | 查看/设置自动测速时间，例如 `/schedule 09:00,21:00`、`/schedule 6h`、`/schedule 30m`、`/schedule 0 8-22/2 * * *`、`/schedule off` |
| `/airport` | 本机场订阅：`/airport add 名称 订阅链接`（仅私聊）、`/airport del 名称`、`/airport` 列表 |
| `/group` | 授权群：`/group add 群组ID`、`/group del 群组ID`；在要授权的群里直接发 `/group add` 也可以。不能删除最后一个授权群 |
| `/admin` | 管理员：`/admin add 用户ID`、`/admin del 用户ID`（或在群里回复某人的消息发送）。**仅超级管理员**（`.env` 的 `ADMIN_USER_IDS`）可用，超级管理员不能被移除 |
| `/ban` `/unban` | 禁止/恢复某个用户测速：`/ban 用户ID`，或在群里回复他的消息发送 `/ban`；`/ban` 查看名单 |
| `/cooldown` | 群成员两次测速的最短间隔，如 `/cooldown 5m`（`30s`、`1h`，`0` 不限） |
| `/maxnodes` | 群成员单次最多测几个节点，如 `/maxnodes 50`（`0` 不限，仍受 `MAX_NODES` 限制） |
| `/limit` | 群成员每天测速次数（`0` 不限） |
| `/creditalert` | 当天积分消耗超过多少时私聊提醒管理员（每天一次，`0` 不提醒） |
| `/alert` | 本机场异常提醒阈值（百分比，`0` 不提醒） |
| `/pin on` / `/pin off` | 是否置顶自动测速结果 |
| `/status` | 运行时间、进行中的任务、今日测速次数和积分消耗、下次自动测速、后端在线数 |
| `/tasks` | 进行中的任务列表，每个任务带「取消」按钮 |
| `/stopall` | 取消所有进行中的任务，本轮本机场测速剩下的订阅也不再继续 |

自动测速时间支持三种写法（按 `TIMEZONE` 计算，两次至少间隔 10 分钟）：

- 每天固定时间：`09:00,21:00`
- 固定间隔：`6h`（0、6、12、18 点整）、`30m`（每小时 0 分和 30 分）。间隔需要能整除 1 小时或 24 小时
  （`10m` `15m` `20m` `30m`、`1h` `2h` `3h` `4h` `6h` `8h` `12h` `24h`），其他间隔请用 cron 或每日时间点
- 标准 5 段 cron「分 时 日 月 周」：`0 */4 * * *`、`0 8-22/2 * * *`、`30 9 * * 1-5`（工作日 09:30）、`0 9 * * mon`，
  支持 `*` `,` `-` `/`、月份和星期的英文缩写，以及 `@daily`、`@hourly` 等

提醒是私聊发给所有管理员的，管理员需要先私聊过 bot（发一次 `/start`）才能收到。

## 配置本机场订阅

本机场订阅写在 `subscriptions.yaml`（参考 [subscriptions.example.yaml](subscriptions.example.yaml)）：

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
不配置这个文件也能运行，只是没有自动测速，只提供群成员测速。

也可以不改文件，直接私聊 bot 发 `/airport add 名称 订阅链接` 添加。注意：用命令改过一次之后，以 bot 保存的列表为准，
`subscriptions.yaml` 不再生效。

出于安全考虑，拉取订阅时会拒绝指向内网、本机或保留地址的链接（包括重定向后的地址）。

## 部署（Debian / Ubuntu 服务器）

### 1. 准备 Bot

在 [@BotFather](https://t.me/BotFather) 创建 Bot 拿到 Token，把 Bot 拉进群，
并**设为群管理员、授予「删除消息」和「置顶消息」权限**（自动删除订阅链接、10 秒后清理提示和命令需要删除权限，
置顶自动测速结果需要置顶权限，管理员身份也能让 Bot 看到群内所有消息）。群里最终只保留测速结果图。

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

也可以不改 `.env`：管理员在群里直接发 `/group add`，或私聊 bot 发 `/group add 群组ID`。

### 日常运维

```bash
sudo systemctl status speed-bot           # 运行状态
sudo journalctl -u speed-bot -f           # 实时日志
sudo systemctl restart speed-bot          # 改完配置后重启
sudo bash /opt/speed_bot/deploy/install.sh --update   # 拉取最新代码并重启
```

服务挂掉会在 5 秒后自动重启，服务器重启后自动运行。每日次数记录（`usage.json`）、当天统计（`stats.json`）和管理员用命令改的
设置（`settings.json`）保存在 `/var/lib/speed-bot/`，重启不丢。想让某项设置重新以 `.env` 为准：先 `sudo systemctl stop speed-bot`，
把 `settings.json` 里那一项的值改成 `null`（或删掉这一项，注意保持合法的 JSON，例如去掉多余的逗号），再启动。
`settings.json` 无法读取或不是合法 JSON 时，bot 会拒绝启动并在日志里说明，避免带着空设置运行（授权群变成“不限”）或把文件覆盖掉。

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

- `ALLOWED_CHAT_IDS`：授权群组，逗号分隔（可用 `/group` 修改）
- `ADMIN_USER_IDS`：超级管理员，可手动测速本机场订阅，测速不限次数，可取消他人任务，可用管理员命令，可添加其他管理员
- `SUBSCRIPTIONS_FILE`：本机场订阅配置文件路径，默认 `subscriptions.yaml`（可用 `/airport` 修改）
- `SCHEDULE_TIMES`：自动测速时间，每日时间点 `09:00,21:00`、间隔 `6h` / `30m` 或 cron `0 */4 * * *`；
  不设置时默认 09:00，设为空（`SCHEDULE_TIMES=`）则不自动测速（可用 `/schedule` 修改）
- `COOLDOWN_SECONDS`、`MEMBER_MAX_NODES`：群成员两次测速的最短间隔（秒）、单次最多节点数，默认 `0` 不限（`/cooldown`、`/maxnodes`）
- `CREDIT_ALERT`：当天积分消耗超过该值时私聊提醒管理员，默认 `0` 不提醒（`/creditalert`）
- `ANOMALY_ALERT_PERCENT`：本机场自动测速失败或没有速度的节点占比达到该值时提醒管理员，默认 `50`，`0` 不提醒（`/alert`）
- `PIN_AUTO_RESULT`：置顶最新一轮本机场测速结果，默认 `true`（`/pin`）
- `AUTO_CHAT_IDS`：自动测速结果发到哪些群，留空为所有 `ALLOWED_CHAT_IDS`；设置了授权群时，只会发到其中仍是授权群的群（被 `/group del` 移除的群不再收到）
- `AUTO_SLAVE_ID`：自动测速使用的后端，留空用 `DEFAULT_SLAVE_ID` 或自动选择
- `DAILY_LIMIT`：群成员每人每天测速次数，默认 `0` 不限（管理员和自动测速不计，可用 `/limit` 修改）
- `TIMEZONE`：按哪个时区计算“每天”，默认 `Asia/Shanghai`
- `MAX_NODES`：单次最多节点数（超过的部分会被截断）
- `DEFAULT_SLAVE_ID`：默认后端，留空自动选择
- `ALLOWED_BACKENDS`：允许选择的后端 ID，逗号分隔，留空为全部可用后端
- `BACKEND_SELECT` / `SORT_SELECT`：是否让用户选择后端 / 排序方式（默认 true；关闭时用默认后端、按平均速度降序）
- `MAX_TASKS_PER_CHAT`：每个群同时运行的测速任务数（自动测速不受限制）
- `SCP_TASK_URL`：网页查看任务的链接模板，如 `https://网页地址/tasks/{task_id}`，配置后进度消息显示「在 SpeedCentre+ 查看」
- `SCP_SHARE_URL`：默认留空，不创建分享。填 `https://web.speedcentre.plus/share?share_id={uuid}` 时，
  测速完成后自动创建公开分享（隐藏节点地址等敏感信息），结果图下显示「📊 查看详情」
- 测速配置：`SPEED_DOWNLOAD_URL`、`SPEED_DURATION`（默认 8 秒）、`SPEED_THREADS`（默认 4）、`PING_URL`、
  `PING_AVERAGE_OVER`（默认 3）、`STUN_URL`、`TASK_RETRY`（默认 3）、`DNS_SERVERS`。默认值与
  [SpeedCentre+ 官方对接示例](https://scx.gitbook.io/sc/scp-docs/speedcentre+-copilot-shi-yong)一致。每次提交测速都会带上完整配置——
  API 文档把 `configs` 标为可选，但后端当前在省略它时会异常断连。`SPEED_DOWNLOAD_URL` 填 `INTL_ANTIHIJACK` 可启用
  [内置反劫持测速](https://scx.gitbook.io/sc/scp-docs/liao-jie-geng-duo)，也支持自定义反劫持和 Telegram 下载测速
- `AUTO_DELETE_SECONDS`：群里除测速结果外的消息（提示、菜单、进度消息、用户发的命令）多少秒后删除，默认 10，`0` 表示不删。进度消息在结果图发出后才开始计时；结果没能发出（超时、跟踪出错、发送失败）时保留进度消息，它带有任务 ID，可用 `/result` 取回结果；私聊消息不删；bot 重启前会先删掉还没到时间的消息
- `DELETE_SUB_MESSAGE`：群里出现订阅/节点链接时自动删除（默认开启）
- `SUB_LINK_PATTERN`：自定义“疑似订阅链接”的判断正则

## 测试

```bash
pip install pytest
python -m pytest
```
