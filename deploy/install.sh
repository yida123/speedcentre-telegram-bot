#!/usr/bin/env bash
# 在 Debian / Ubuntu 上安装或更新 speed_bot（systemd 服务）。
# 用法：
#   sudo bash deploy/install.sh           # 首次安装 / 重新安装依赖并重启
#   sudo bash deploy/install.sh --update  # 拉取最新代码后重新安装并重启
set -euo pipefail

SERVICE=speed-bot
SERVICE_USER=speedbot
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ $EUID -ne 0 ]]; then
    echo "请用 root 运行：sudo bash $0 $*" >&2
    exit 1
fi

if [[ "${1:-}" == "--update" ]]; then
    echo ">> 拉取最新代码"
    git -c safe.directory="$APP_DIR" -C "$APP_DIR" pull --ff-only
fi

echo ">> 安装系统依赖"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip ca-certificates git >/dev/null

if ! id "$SERVICE_USER" &>/dev/null; then
    echo ">> 创建系统用户 $SERVICE_USER"
    useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi

echo ">> 创建虚拟环境并安装 Python 依赖"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

if [[ ! -f "$APP_DIR/.env" ]]; then
    cp "$APP_DIR/.env.example" "$APP_DIR/.env"
    NEW_ENV=1
fi
if [[ ! -f "$APP_DIR/subscriptions.yaml" ]]; then
    cp "$APP_DIR/subscriptions.example.yaml" "$APP_DIR/subscriptions.yaml"
    NEW_ENV=1
fi
# .env 和订阅配置里有 Token、API Key 和订阅地址，只允许服务用户读取
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR"
chmod 600 "$APP_DIR/.env" "$APP_DIR/subscriptions.yaml"

echo ">> 安装 systemd 服务"
sed "s#__APP_DIR__#$APP_DIR#g" "$APP_DIR/deploy/speed-bot.service" > "/etc/systemd/system/$SERVICE.service"
systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null

if [[ -n "${NEW_ENV:-}" ]]; then
    echo
    echo "已生成配置文件，请先填写："
    echo "    $APP_DIR/.env                （TG_BOT_TOKEN、SCP_API_KEY、ADMIN_USER_IDS）"
    echo "    $APP_DIR/subscriptions.yaml  （要测速的订阅名称和地址）"
    echo "然后执行："
    echo "    sudo systemctl start $SERVICE"
    exit 0
fi

systemctl restart "$SERVICE"
sleep 2
systemctl --no-pager --lines=5 status "$SERVICE" || true
echo
echo "查看日志：journalctl -u $SERVICE -f"
