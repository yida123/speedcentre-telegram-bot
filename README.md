# speed_bot

基于 [SpeedCentre+ 用户 API](https://api.speedcentre.plus) 的 Telegram 群组节点测试 Bot。

群成员发送订阅链接或节点分享链接，Bot 提交测试任务、实时刷新进度，完成后把结果图发到群里。

## 功能

| 命令 | 说明 |
| --- | --- |
| `/test <链接>` | 弹出选择菜单，自选测试项目后开始（见下） |
| `/speed <链接>` | 测速 |
| `/ping <链接>` | 延迟测试：RTT、HTTPS 延迟、HTTP 状态码、丢包 |
| `/udp <链接>` | UDP NAT 类型 |
| `/topo <链接>` | 入口/出口拓扑分析（拓扑图） |
| `/backends` | 后端列表与队列 |
| `/tasks` | 最近任务 |
| `/result <任务ID>` | 重新获取结果图（加 `topo` 获取拓扑图） |
| `/cancel <任务ID>` | 取消任务（进度消息上也有取消按钮） |
| `/id` | 查看当前群组 ID / 用户 ID |

### 自选测试项目

`/test` 解析完节点后会弹出按钮菜单，发起人（或管理员）可以：

- 一键套用预设：全面 / 测速 / 延迟 / UDP 类型 / 拓扑分析
- 逐项勾选：延迟 RTT、HTTPS 延迟、丢包率、HTTP 状态码、测速、UDP 类型、出入口拓扑、劫持检测
- 进入「🎬 流媒体解锁」子菜单，勾选服务端提供的流媒体脚本（来自 `/api/v1/scripts`）

点「▶️ 开始测试」提交。勾选了出入口拓扑时会额外发送一张拓扑图。菜单 10 分钟内有效。

`/speed`、`/ping`、`/udp`、`/topo` 是快捷命令，不弹菜单，直接按预设开测。

### 通用用法

- 链接可以直接写在命令后面，也可以**回复**一条含链接的消息再发命令。
- `-f 正则` 按节点名过滤：`/test https://sub -f "香港|HK"`
- `-s 后端ID` 指定后端：`/speed https://sub -s xxx`

支持的输入：

- 订阅链接（以 `clash.meta` UA 拉取，支持 Clash YAML 和 base64 分享链接列表）
- 节点分享链接：`ss://` `ssr://` `vmess://` `vless://`（含 Reality）`trojan://` `hysteria://` `hysteria2://`/`hy2://` `tuic://` `socks5://` `anytls://`

结果优先使用 API 的图片导出；若套餐不支持图片导出，则回退为文本结果。

## 部署

1. 在 [@BotFather](https://t.me/BotFather) 创建 Bot 拿到 Token，并把 Bot 拉进群。
   - 群内使用 `/test@你的bot` 形式的命令即可，不需要关闭 Privacy Mode。
   - 如需开启 `DELETE_SUB_MESSAGE`（自动删除含订阅链接的消息），需要给 Bot 删除消息的管理员权限。
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
- `ALLOW_PRIVATE`：是否允许普通用户私聊使用
- `MAX_NODES`：单次最多节点数（超过的部分会被截断）
- `MAX_TASKS_PER_CHAT`：每个群同时运行的任务数
- `DEFAULT_SLAVE_ID`：默认后端，留空自动选择

## 测试

```bash
pip install pytest
python -m pytest
```
