#!/usr/bin/env bash
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
    echo "Запустите install.sh от root: sudo ./deploy/install.sh" >&2
    exit 1
fi

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_DIR=/opt/m110-monitor
CONFIG_DIR=/etc/daemons
CONFIG_FILE="$CONFIG_DIR/m110-monitor.env"
LOG_DIR=/var/log/m110-monitor
DATA_DIR=/var/lib/m110-monitor
SERVICE_FILE=/etc/systemd/system/m110-monitor.service

if ! getent group m110-monitor >/dev/null; then
    groupadd --system m110-monitor
fi
if ! id m110-monitor >/dev/null 2>&1; then
    useradd --system --gid m110-monitor --home-dir "$INSTALL_DIR" --shell /usr/sbin/nologin m110-monitor
fi

mkdir -p "$INSTALL_DIR" "$LOG_DIR" "$DATA_DIR"
install -d -o root -g root -m 755 "$CONFIG_DIR"

if [[ "$PROJECT_DIR" != "$INSTALL_DIR" ]]; then
    for file in \
        main.py \
        decoder.py \
        database.py \
        postgres_database.py \
        outbox.py \
        logging_utils.py \
        requirements.txt \
        README.md \
        .env.example
    do
        cp "$PROJECT_DIR/$file" "$INSTALL_DIR/$file"
    done
    cp -a "$PROJECT_DIR/deploy" "$INSTALL_DIR/"
fi

PYTHON_BIN="${PYTHON_BIN:-python3.11}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    echo "Не найден Python: $PYTHON_BIN" >&2
    exit 1
fi

"$PYTHON_BIN" -m venv "$INSTALL_DIR/.venv"
"$INSTALL_DIR/.venv/bin/pip" install --requirement "$INSTALL_DIR/requirements.txt"

if [[ ! -e "$CONFIG_FILE" ]]; then
    install -o root -g m110-monitor -m 640 \
        "$INSTALL_DIR/.env.example" \
        "$CONFIG_FILE"
    sed -i 's#^LOG_FILE=.*#LOG_FILE=/var/log/m110-monitor/m110-monitor.log#' "$CONFIG_FILE"
    sed -i 's#^OUTBOX_PATH=.*#OUTBOX_PATH=/var/lib/m110-monitor/delivery-outbox.sqlite3#' "$CONFIG_FILE"
    CONFIG_CREATED=true
else
    chown root:m110-monitor "$CONFIG_FILE"
    chmod 640 "$CONFIG_FILE"
    if ! grep -q '^OUTBOX_PATH=' "$CONFIG_FILE"; then
        printf '\nOUTBOX_PATH=/var/lib/m110-monitor/delivery-outbox.sqlite3\nOUTBOX_BATCH_SIZE=100\n' >> "$CONFIG_FILE"
    fi
    CONFIG_CREATED=false
fi
chown -R m110-monitor:m110-monitor "$LOG_DIR"
chmod 750 "$LOG_DIR"
chown -R m110-monitor:m110-monitor "$DATA_DIR"
chmod 750 "$DATA_DIR"
chown -R root:root "$INSTALL_DIR"
chown -R m110-monitor:m110-monitor "$INSTALL_DIR/.venv"
install -m 0644 "$INSTALL_DIR/deploy/m110-monitor.service" "$SERVICE_FILE"
systemctl daemon-reload
systemctl enable m110-monitor

if [[ "$CONFIG_CREATED" == true ]] && grep -qx 'MYSQL_PASSWORD=change_me' "$CONFIG_FILE"; then
    echo "Создан конфигурационный файл:"
    echo "/etc/daemons/m110-monitor.env"
    echo
    echo "Перед запуском службы заполните MYSQL_PASSWORD и проверьте остальные параметры."
    echo
    echo "Служба установлена, но не запущена."
    echo "Заполните /etc/daemons/m110-monitor.env, затем выполните:"
    echo "sudo systemctl start m110-monitor"
    exit 0
fi

"$INSTALL_DIR/.venv/bin/python" "$INSTALL_DIR/main.py" \
    --config "$CONFIG_FILE" \
    --check-config
systemctl restart m110-monitor

echo "Установка завершена. Проверка:"
echo "  systemctl status m110-monitor"
echo "  journalctl -u m110-monitor -f"
echo "  tail -f /var/log/m110-monitor/m110-monitor.log"
