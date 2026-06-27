#!/usr/bin/env bash
set -euo pipefail

export DISPLAY="${DISPLAY:-:99}"

PORT="${PORT:-8080}"
SCREEN_GEOMETRY="${BC_BOOTSTRAP_SCREEN_GEOMETRY:-1440x1000x24}"
PROFILE_DIR="${BC_BOOTSTRAP_PROFILE_DIR:-/app/data/playwright_bc_profile/chromium}"
START_URL="${BC_BOOTSTRAP_URL:-https://app.buildingconnected.com/opportunities/pipeline}"
VNC_PORT="${BC_BOOTSTRAP_VNC_PORT:-5900}"
NOVNC_SOURCE_WEB_DIR="${BC_BOOTSTRAP_NOVNC_SOURCE_WEB_DIR:-/usr/share/novnc}"
NOVNC_WEB_DIR="${BC_BOOTSTRAP_NOVNC_WEB_DIR:-/tmp/novnc-web}"

echo "Starting temporary BuildingConnected login bootstrap."
echo "WARNING: This exposes an interactive browser over the Railway public URL."
echo "Use only long enough to complete login/MFA, then restore the normal start command."
echo "Profile directory: ${PROFILE_DIR}"
echo "noVNC listens on Railway PORT ${PORT}."

mkdir -p "${PROFILE_DIR}"
rm -rf "${NOVNC_WEB_DIR}"
mkdir -p "${NOVNC_WEB_DIR}"
cp -a "${NOVNC_SOURCE_WEB_DIR}/." "${NOVNC_WEB_DIR}/"
cat >"${NOVNC_WEB_DIR}/index.html" <<'HTML'
<!doctype html>
<html>
  <head>
    <meta http-equiv="refresh" content="0; url=/vnc.html?autoconnect=true&resize=remote">
    <title>BuildingConnected Login Bootstrap</title>
  </head>
  <body>
    <a href="/vnc.html?autoconnect=true&resize=remote">Open noVNC</a>
  </body>
</html>
HTML

cleanup() {
    for pid in "${CHROMIUM_PID:-}" "${X11VNC_PID:-}" "${FLUXBOX_PID:-}" "${XVFB_PID:-}"; do
        if [ -n "${pid}" ] && kill -0 "${pid}" 2>/dev/null; then
            kill "${pid}" 2>/dev/null || true
        fi
    done
}
trap cleanup EXIT INT TERM

Xvfb "${DISPLAY}" -screen 0 "${SCREEN_GEOMETRY}" -ac +extension RANDR &
XVFB_PID="$!"
sleep 1

fluxbox >/tmp/fluxbox.log 2>&1 &
FLUXBOX_PID="$!"

if [ -n "${BC_BOOTSTRAP_VNC_PASSWORD:-}" ]; then
    PASSWD_FILE="/tmp/x11vnc.pass"
    x11vnc -storepasswd "${BC_BOOTSTRAP_VNC_PASSWORD}" "${PASSWD_FILE}" >/dev/null 2>&1
    X11VNC_AUTH_ARGS=(-rfbauth "${PASSWD_FILE}")
    echo "x11vnc password protection is enabled."
else
    X11VNC_AUTH_ARGS=(-nopw)
    echo "WARNING: BC_BOOTSTRAP_VNC_PASSWORD is not set; VNC has no password."
fi

x11vnc \
    -display "${DISPLAY}" \
    -localhost \
    -forever \
    -shared \
    -rfbport "${VNC_PORT}" \
    "${X11VNC_AUTH_ARGS[@]}" \
    >/tmp/x11vnc.log 2>&1 &
X11VNC_PID="$!"

CHROMIUM_BIN="${BC_BOOTSTRAP_CHROMIUM_BIN:-}"
if [ -z "${CHROMIUM_BIN}" ]; then
    CHROMIUM_BIN="$(
        python - <<'PY'
from playwright.sync_api import sync_playwright

with sync_playwright() as pw:
    print(pw.chromium.executable_path)
PY
    )"
fi

"${CHROMIUM_BIN}" \
    --no-sandbox \
    --disable-dev-shm-usage \
    --disable-blink-features=AutomationControlled \
    --start-maximized \
    --window-size=1440,1000 \
    --user-data-dir="${PROFILE_DIR}" \
    "${START_URL}" \
    >/tmp/chromium-bootstrap.log 2>&1 &
CHROMIUM_PID="$!"

echo "Chromium launched for manual BuildingConnected/Autodesk login."
echo "Open the Railway public URL and use noVNC to complete login/MFA."
echo "When finished, restore the normal Flask start command."

exec websockify --web="${NOVNC_WEB_DIR}" "0.0.0.0:${PORT}" "localhost:${VNC_PORT}"
