#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# Tickets Hunter container entrypoint
#
# Boots Xvfb (virtual X display) + x11vnc + noVNC so the headed Chrome that
# zendriver launches becomes visible at http://localhost:${NOVNC_PORT}/ .
# Then forwards to one of three modes:
#   settings  -> python src/settings.py   (tornado settings UI on :16888)
#   run       -> python src/nodriver_tixcraft.py
#   shell     -> bash (interactive debugging)
# -----------------------------------------------------------------------------
set -euo pipefail

: "${DISPLAY:=:99}"
: "${SCREEN_GEOMETRY:=1440x900x24}"
: "${VNC_PORT:=5900}"
: "${NOVNC_PORT:=6080}"
: "${VNC_PASSWORD:=}"

log() { printf '[entrypoint] %s\n' "$*"; }

start_xstack() {
    # Clean stale X locks left over from a previous container run / restart.
    # /tmp survives `docker restart`, so without this Xvfb fails with
    # "Server is already active for display 99" on every restart.
    local display_num="${DISPLAY#:}"
    rm -f "/tmp/.X${display_num}-lock" "/tmp/.X11-unix/X${display_num}" 2>/dev/null || true

    log "Starting Xvfb on ${DISPLAY} (${SCREEN_GEOMETRY})..."
    Xvfb "${DISPLAY}" -screen 0 "${SCREEN_GEOMETRY}" -ac +extension RANDR -nolisten tcp &
    # Wait for X to be ready
    for _ in $(seq 1 20); do
        if xdpyinfo -display "${DISPLAY}" >/dev/null 2>&1; then break; fi
        sleep 0.2
    done

    log "Starting x11vnc on :${VNC_PORT}..."
    local x11vnc_auth=(-nopw)
    if [[ -n "${VNC_PASSWORD}" ]]; then
        mkdir -p /root/.vnc
        x11vnc -storepasswd "${VNC_PASSWORD}" /root/.vnc/passwd >/dev/null
        x11vnc_auth=(-rfbauth /root/.vnc/passwd)
    fi
    x11vnc -display "${DISPLAY}" -forever -shared -rfbport "${VNC_PORT}" \
           -quiet -bg "${x11vnc_auth[@]}"

    log "Starting noVNC (websockify) on :${NOVNC_PORT} -> :${VNC_PORT}..."
    websockify --web=/usr/share/novnc/ "${NOVNC_PORT}" "localhost:${VNC_PORT}" \
        >/var/log/websockify.log 2>&1 &

    log "Display ready. Open http://localhost:${NOVNC_PORT}/ to view the browser."
}

case "${1:-settings}" in
    settings)
        start_xstack
        log "Launching settings UI (tornado on :16888)..."
        cd "${APP_HOME}/src"
        exec python settings.py
        ;;
    run|hunt)
        start_xstack
        log "Launching nodriver_tixcraft.py..."
        cd "${APP_HOME}/src"
        exec python nodriver_tixcraft.py
        ;;
    shell|bash)
        start_xstack
        log "Dropping into interactive shell. \$DISPLAY=${DISPLAY}"
        exec bash
        ;;
    no-x)
        # Headless / no X stack at all — useful for CI or pure-API testing
        log "Skipping X stack (no-x mode). Running: ${*:2}"
        shift
        exec "$@"
        ;;
    *)
        # Pass through arbitrary command, X stack still booted for convenience
        start_xstack
        exec "$@"
        ;;
esac
