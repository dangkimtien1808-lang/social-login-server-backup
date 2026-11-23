#!/usr/bin/env bash
# 將公開 dashboard 安裝到 https://social.54ucl.com
# 執行：sudo bash deploy/install.sh
# 重複執行多次仍然安全（已完成的步驟會略過）。
set -euo pipefail

DOMAIN="social.54ucl.com"
APP_USER="c113118246"
DIR="$(cd "$(dirname "$0")" && pwd)"
HTPASSWD="/etc/nginx/.htpasswd-social"
SERVICE="social-login"

if [ "$(id -u)" -ne 0 ]; then
    echo "請使用以下指令執行：sudo bash deploy/install.sh"
    exit 1
fi

echo
echo "=== 1/5 Dashboard 登入帳號 (Basic Auth) ==="
if ! command -v htpasswd >/dev/null; then
    apt-get install -y apache2-utils
fi
if [ -s "$HTPASSWD" ]; then
    echo "已有 $HTPASSWD -> 維持不變。"
else
    read -rp "Dashboard 登入名稱 [admin]：" LOGIN_NAME
    LOGIN_NAME="${LOGIN_NAME:-admin}"
    htpasswd -c "$HTPASSWD" "$LOGIN_NAME"
fi
chown root:nginx "$HTPASSWD"
chmod 640 "$HTPASSWD"

echo
echo "=== 2/5 HTTPS 憑證 (certbot) ==="
if [ -f "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" ]; then
    echo "$DOMAIN 已有憑證 -> 略過。"
else
    certbot certonly --nginx -d "$DOMAIN" --deploy-hook "systemctl reload nginx"
fi

echo
echo "=== 3/5 以 systemd 執行 app ==="
if ! systemctl is-active --quiet "$SERVICE"; then
    # 關閉手動執行中的 uvicorn（如果有），以釋放 8000 埠
    if pkill -u "$APP_USER" -f "uvicorn app:app"; then
        echo "已關閉手動執行的 uvicorn，等待釋放 8000 埠..."
        sleep 3
    fi
fi
cp "$DIR/social-login.service" "/etc/systemd/system/$SERVICE.service"
systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null
systemctl restart "$SERVICE"
sleep 3
if ! systemctl is-active --quiet "$SERVICE"; then
    echo "錯誤：service 未執行。查看 log："
    journalctl -u "$SERVICE" -n 30 --no-pager
    exit 1
fi
echo "Service $SERVICE 執行中。"

echo
echo "=== 4/5 設定 nginx ==="
cp "$DIR/$DOMAIN.conf" "/etc/nginx/conf.d/$DOMAIN.conf"
if ! nginx -t; then
    echo "nginx 設定錯誤 -> 移除剛加入的檔案，nginx 維持原狀。"
    rm -f "/etc/nginx/conf.d/$DOMAIN.conf"
    exit 1
fi
systemctl reload nginx

echo
echo "=== 5/5 檢查 ==="
sleep 1
echo -n "內部 App (/schedules)："
curl -s -m 5 http://127.0.0.1:8000/schedules || echo "沒有回應"
echo
echo -n "https://$DOMAIN/dashboard 不帶密碼 -> "
curl -s -m 10 -o /dev/null -w "HTTP %{http_code} (401 為正確：必須登入)\n" "https://$DOMAIN/dashboard" || echo "無法連線"

echo
echo "完成。請在瀏覽器開啟：https://$DOMAIN"
