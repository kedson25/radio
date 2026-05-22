#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/radio}"
APP_USER="${APP_USER:-radio}"
APP_GROUP="${APP_GROUP:-radio}"
SERVICE_NAME="${SERVICE_NAME:-radio}"
PORTA="${WEB_PORT:-8000}"
ORIGEM="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "$(id -u)" -ne 0 ]; then
  echo "Execute como root: sudo bash instalar.sh"
  exit 1
fi

echo "[1/8] Atualizando sistema..."
apt update
DEBIAN_FRONTEND=noninteractive apt upgrade -y

echo "[2/8] Instalando dependencias do sistema..."
DEBIAN_FRONTEND=noninteractive apt install -y \
  python3 \
  python3-pip \
  python3-venv \
  ffmpeg \
  nodejs \
  curl \
  rsync

echo "[3/8] Criando usuario ${APP_USER}..."
if ! getent group "${APP_GROUP}" >/dev/null 2>&1; then
  groupadd --system "${APP_GROUP}"
fi

if ! id -u "${APP_USER}" >/dev/null 2>&1; then
  useradd --system --create-home --gid "${APP_GROUP}" --shell /usr/sbin/nologin "${APP_USER}"
fi

echo "[4/8] Criando diretorios em ${APP_DIR}..."
mkdir -p "${APP_DIR}/musicas" "${APP_DIR}/erros" "${APP_DIR}/static"

echo "[5/8] Copiando arquivos do projeto..."
install -m 0644 "${ORIGEM}/radio.py" "${APP_DIR}/radio.py"
install -m 0644 "${ORIGEM}/requirements.txt" "${APP_DIR}/requirements.txt"
rsync -a --delete "${ORIGEM}/static/" "${APP_DIR}/static/"
if [ -f "${ORIGEM}/cookies.txt" ]; then
  install -m 0600 "${ORIGEM}/cookies.txt" "${APP_DIR}/cookies.txt"
fi

echo "[6/8] Instalando dependencias Python em virtualenv..."
python3 -m venv "${APP_DIR}/venv"
"${APP_DIR}/venv/bin/python" -m pip install --upgrade pip
"${APP_DIR}/venv/bin/pip" install -r "${APP_DIR}/requirements.txt"

echo "[7/8] Instalando servico systemd..."
install -m 0644 "${ORIGEM}/radio.service" "/etc/systemd/system/${SERVICE_NAME}.service"
chown -R "${APP_USER}:${APP_GROUP}" "${APP_DIR}"
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}.service"

echo "[8/8] Iniciando radio..."
systemctl restart "${SERVICE_NAME}.service"

IP_PUBLICO="$(curl -4 -s --max-time 3 ifconfig.me || hostname -I | awk '{print $1}')"

echo
echo "Instalacao concluida."
echo "Servico: systemctl status ${SERVICE_NAME}.service"
echo "Logs: journalctl -u ${SERVICE_NAME}.service -f"
echo "URL local: http://localhost:${PORTA}"
echo "URL VPS: http://${IP_PUBLICO}:${PORTA}"
echo
echo "Se a Oracle Cloud bloquear acesso externo, libere a porta ${PORTA} na VCN/Security List e no firewall da instancia."
