# speed_bot

基于 [SpeedCentre+ 用户 API](https://api.speedcentre.plus) 的 Telegram 群组节点测试 Bot。

群成员发送订阅链接或节点分享链接，Bot 提交测试任务、实时刷新进度，完成后把结果图发到群里。

## 使用流程

### 用订阅名测试（推荐）

管理员把订阅保存到 SpeedCentre+ 账号后，群成员直接在群里用订阅名测试，订阅链接不会出现在群里：

1. 管理员私聊 Bot：`/sub add 3399 https://订阅链接`（也可以在 SpeedCentre+ 网页的订阅管理里添加）。
2. 群里发 `/speed 3399`，Bot 回复后端选择按钮（每页 5 个，可翻页，「❌ 终止操作」取消）。
3. 选完后端再选排序方式：订阅顺序（默认）/ 节点名 / 平均速度升降序 / 延迟升降序。
4. 显示「⏳ 任务 3399 准备中…」→「⚡ 任务 3399 进行中… [████░░] 25%」，带「❌ 取消任务」按钮。
5. 完成后发送结果图并 @ 发起人，附「📊 查看详情」分享链接（需配置 `SCP_SHARE_URL`）。

`/sub` 列出可用的订阅名（不显示链接），管理员可用 `/sub del 名称` 删除。只有发起人（或管理员）能点菜单按钮。

### 临时订阅（私聊发送）

为避免订阅泄露，**订阅链接只在私聊里发送，测试结果发在群里**：

1. 群成员在群里发 `/test`（或 `/speed`、`/ping`、`/udp`、`/topo`）。
2. Bot 回复一个「🔒 私聊发送订阅」按钮，点开进入与 Bot 的私聊。
3. 在私聊里直接发送订阅链接或节点链接（可附加 `-f 正则`、`-s 后端ID`）。
4. `/test` 会在私聊里弹出测试项目菜单；快捷命令直接开测。
5. 任务提交后，进度和结果图发到群里并 @ 发起人。

群里如果有人直接发了节点链接（`ss://`、`vmess://` 等）或疑似订阅的链接，Bot 会**立即删除**该消息，
并 @ 发送者附上私聊按钮（引导消息 60 秒后自动删除）。“疑似订阅”默认按 `token=`、`/sub`、`subscribe`、
`/api/v1/client`、`clash` 等特征判断，可用 `SUB_LINK_PATTERN` 自定义。

私聊时结果发往哪个群：最近一次点按钮的群（30 分钟内有效）；只配置了一个授权群时默认发到该群。
Bot 会校验私聊用户是该群成员。不属于任何授权群的用户只有在 `ALLOW_PRIVATE=true` 或身为管理员时可以私聊测试，结果发回私聊。

## 命令

| 命令 | 说明 |
| --- | --- |
| `/test` | 弹出选择菜单，自选测试项目后开始（见下） |
| `/speed` | 测速 |
| `/ping` | 延迟测试：RTT、HTTPS 延迟、HTTP 状态码、丢包 |
| `/udp` | UDP NAT 类型 |
| `/topo` | 入口/出口拓扑分析（拓扑图） |
| `/backends` | 后端列表与队列 |
| `/tasks` | 最近任务 |
| `/result <任务ID>` | 重新获取结果图（加 `topo` 获取拓扑图） |
| `/cancel <任务ID>` | 取消任务（进度消息上也有取消按钮） |
| `/id` | 查看当前群组 ID / 用户 ID |

### 自选测试项目

`/test` 解析完节点后会在私聊里弹出按钮菜单：

- 一键套用预设：全面 / 测速 / 延迟 / UDP 类型 / 拓扑分析
- 逐项勾选：延迟 RTT、HTTPS 延迟、丢包率、HTTP 状态码、测速、UDP 类型、出入口拓扑、劫持检测
- 进入「🎬 流媒体解锁」子菜单，勾选服务端提供的流媒体脚本（来自 `/api/v1/scripts`）

- 点「🖥 后端」选择测试后端（默认自动选择）

点「▶️ 开始测试」提交。勾选了出入口拓扑时会额外发送一张拓扑图。菜单 10 分钟内有效。

### 自选后端

- 可选的后端来自 `/api/v1/backends`：只列出**在线**且**允许 Copilot 调用**的后端，按当前排队数从少到多排序，每页 8 个，可翻页。
- `/test`：在菜单里点「🖥 后端」选择；快捷命令（`/speed` 等）：有多个后端可选时先弹出后端按钮，点一下立即开测。
- 也可以在链接后加 `-s 后端ID或名称` 直接指定，跳过选择。
- 管理员可以用 `ALLOWED_BACKENDS` 限制可选后端，用 `DEFAULT_SLAVE_ID` 设置默认选中的后端，
  用 `BACKEND_SELECT=false` 关闭快捷命令的后端选择（直接用默认/自动）。
- `/backends` 查看所有后端：🟢 在线、🔴 离线、🚫 不可选。

### 支持的输入

- 订阅链接（以 `clash.meta` UA 拉取，支持 Clash YAML 和 base64 分享链接列表）
- 节点分享链接：`ss://` `ssr://` `vmess://` `vless://`（含 Reality）`trojan://` `hysteria://` `hysteria2://`/`hy2://` `tuic://` `socks5://` `anytls://`

出于安全考虑，Bot 拉取订阅时会拒绝指向内网、本机或保留地址的链接（包括重定向后的地址），防止有人借 Bot 探测部署机器所在的内网。

结果优先使用 API 的图片导出；若套餐不支持图片导出，则回退为文本结果。

## 部署（Debian / Ubuntu 服务器）

### 1. 准备 Bot

在 [@BotFather](https://t.me/BotFather) 创建 Bot 拿到 Token，把 Bot 拉进群，
并**设为群管理员、授予「删除消息」权限**（自动删除订阅链接需要该权限，管理员身份也能让 Bot 看到群内所有消息）。

服务器需要能访问 Telegram、`api.speedcentre.plus` 以及用户的订阅地址。国内服务器请在 `.env` 里加
`HTTPS_PROXY=http://代理地址:端口`。

### 2. 安装（systemd，推荐）

```bash
sudo apt update && sudo apt install -y git
sudo git clone https://github.com/yida123/speed_bot.git /opt/speed_bot
# 私有仓库：用 https://<用户名>:<token>@github.com/yida123/speed_bot.git，或配置 deploy key
sudo bash /opt/speed_bot/deploy/install.sh
```

脚本会安装 Python、创建 `speedbot` 系统用户和虚拟环境、注册开机自启的 `speed-bot` 服务，并生成 `/opt/speed_bot/.env`。

### 3. 填写配置并启动

```bash
sudo nano /opt/speed_bot/.env      # 填 TG_BOT_TOKEN、SCP_API_KEY、ADMIN_USER_IDS
sudo systemctl start speed-bot
```

在群里发 `/id` 拿到群组 ID，填到 `ALLOWED_CHAT_IDS`（**务必设置**，否则任何群都能消耗你的积分），然后重启：

```bash
sudo systemctl restart speed-bot
```

### 日常运维

```bash
sudo systemctl status speed-bot           # 运行状态
sudo journalctl -u speed-bot -f           # 实时日志
sudo systemctl restart speed-bot          # 改完 .env 后重启
sudo bash /opt/speed_bot/deploy/install.sh --update   # 拉取最新代码并重启
```

服务挂掉会在 5 秒后自动重启，服务器重启后自动运行。

### 或者用 Docker

```bash
sudo apt install -y docker.io docker-compose-v2
cd /opt/speed_bot && cp .env.example .env && nano .env
sudo docker compose up -d --build         # 启动
sudo docker compose logs -f               # 日志
git pull && sudo docker compose up -d --build   # 更新
```

### 本地直接运行（调试用）

```bash
pip install -r requirements.txt
cp .env.example .env && nano .env
python -m bot.main
```

## 配置项

见 [.env.example](.env.example)。主要有：

- `ALLOWED_CHAT_IDS`：授权群组，逗号分隔
- `ADMIN_USER_IDS`：管理员，可在任何会话使用、可取消他人任务
- `ALLOW_PRIVATE`：是否允许不属于授权群的用户私聊测试（结果发回私聊）
- `DELETE_SUB_MESSAGE`：群里出现订阅/节点链接时自动删除（默认开启）
- `SUB_LINK_PATTERN`：自定义“疑似订阅链接”的判断正则
- `MAX_NODES`：单次最多节点数（超过的部分会被截断）
- `MAX_TASKS_PER_CHAT`：每个群同时运行的任务数
- `DEFAULT_SLAVE_ID`：默认选中的后端，留空自动选择
- `ALLOWED_BACKENDS`：允许用户选择的后端 ID，逗号分隔，留空为全部可用后端
- `BACKEND_SELECT`：快捷命令是否先让用户选择后端（默认 true）
- `SORT_SELECT`：是否让用户选择结果图的排序方式（默认 true）
- `SCP_TASK_URL`：网页查看任务的链接模板，如 `https://网页地址/tasks/{task_id}`，配置后进度消息显示「在 SpeedCentre+ 查看」
- `SCP_SHARE_URL`：分享页链接模板，如 `https://网页地址/share/{uuid}`，配置后结果自动创建分享（隐藏节点地址等敏感信息）并显示「查看详情」

## 测试

```bash
pip install pytest
python -m pytest
```
