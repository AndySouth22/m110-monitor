#!/usr/bin/env bash
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
    echo "Запустите uninstall.sh от root: sudo ./deploy/uninstall.sh" >&2
    exit 1
fi

PURGE=false
if [[ "${1:-}" == "--purge" ]]; then
    PURGE=true
elif [[ $# -gt 0 ]]; then
    echo "Использование: $0 [--purge]" >&2
    exit 2
fi

systemctl disable --now m110-monitor 2>/dev/null || true
rm -f /etc/systemd/system/m110-monitor.service
systemctl daemon-reload
rm -rf /opt/m110-monitor

if [[ "$PURGE" == true ]]; then
    rm -f /etc/daemons/m110-monitor.env
    rm -rf /var/log/m110-monitor
    rm -rf /var/lib/m110-monitor
    echo "Служба, программа, конфигурация, логи и outbox удалены."
else
    echo "Служба и программа удалены. Конфигурация, логи и outbox сохранены."
fi
