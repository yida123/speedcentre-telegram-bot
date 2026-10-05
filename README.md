# speed_bot

基于 [SpeedCentre+ 用户 API](https://api.speedcentre.plus) 的 Telegram 群组节点测试 Bot。

群成员发送订阅链接或节点分享链接，Bot 提交测试任务、实时刷新进度，完成后把结果图发到群里。

## 使用流程

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

点「▶️ 开始测试」提交。勾选了出入口拓扑时会额外发送一张拓扑图。菜单 10 分钟内有效。

### 支持的输入

- 订阅链接（以 `clash.meta` UA 拉取，支持 Clash YAML 和 base64 分享链接列表）
- 节点分享链接：`ss://` `ssr://` `vmess://` `vless://`（含 Reality）`trojan://` `hysteria://` `hysteria2://`/`hy2://` `tuic://` `socks5://` `anytls://`

出于安全考虑，Bot 拉取订阅时会拒绝指向内网、本机或保留地址的链接（包括重定向后的地址），防止有人借 Bot 探测部署机器所在的内网。

结果优先使用 API 的图片导出；若套餐不支持图片导出，则回退为文本结果。

## 部署

1. 在 [@BotFather](https://t.me/BotFather) 创建 Bot 拿到 Token，并把 Bot 拉进群。
   - **把 Bot 设为群管理员并授予「删除消息」权限**：自动删除订阅链接需要该权限，且管理员身份能让 Bot 看到群内所有消息。
     （如果不设管理员，需要在 BotFather 里 `/setprivacy` → Disable，否则 Bot 看不到普通消息，也删不掉。）
2. 复制配置：`cp .env.example .env`，填写 `TG_BOT_TOKEN`、`SCP_API_KEY`。
3. 在群里发 `/id` 获取群组 ID，填到 `ALLOWED_CHAT_IDS`（**强烈建议设置**，否则任何群都能消耗你的积分）。
4. 运行：

```bash
pip install -r requirements.txt
python -m bot.main
```

或 Docker：

```bash
docker build -t speed_bot .
docker run -d --name speed_bot --env-file .env --restart unless-stopped speed_bot
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
- `DEFAULT_SLAVE_ID`：默认后端，留空自动选择

## 测试

```bash
pip install pytest
python -m pytest
```
