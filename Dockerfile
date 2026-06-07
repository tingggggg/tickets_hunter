# =============================================================================
# Tickets Hunter — Development container
# Python 3.11.9 + Chromium runtime deps + Xvfb/x11vnc/noVNC for headed browser
# =============================================================================
FROM python:3.11.9-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Taipei \
    DISPLAY=:99 \
    SCREEN_GEOMETRY=1440x900x24 \
    VNC_PORT=5900 \
    NOVNC_PORT=6080 \
    APP_HOME=/app

# -----------------------------------------------------------------------------
# System packages
#   - chromium pulled in only to satisfy Chrome-for-Testing runtime deps that
#     zendriver downloads to src/webdriver/ on first run
#   - fonts-noto-cjk: Traditional Chinese rendering on tixcraft/KKTIX pages
#   - libgl1 / libglib2.0-0: required by opencv-python (cv2)
#   - xvfb + x11vnc + novnc + websockify: headed browser visible via web VNC
#   - tini: clean PID 1 / signal handling for the entrypoint shell script
# -----------------------------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        wget \
        unzip \
        git \
        tini \
        tzdata \
        # OpenCV / Pillow runtime
        libgl1 \
        libglib2.0-0 \
        libsm6 \
        libxext6 \
        libxrender1 \
        # Chrome runtime deps (installing chromium is the easiest way to get them all)
        chromium \
        # CJK fonts
        fonts-noto-cjk \
        fonts-noto-cjk-extra \
        # Headed browser virtual display + web VNC
        xvfb \
        x11vnc \
        novnc \
        websockify \
        # Useful for debugging inside the container
        procps \
        net-tools \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

# Symlink noVNC's vnc.html as index.html so users can hit http://localhost:6080/ directly
RUN ln -sf /usr/share/novnc/vnc.html /usr/share/novnc/index.html

WORKDIR ${APP_HOME}

# -----------------------------------------------------------------------------
# Python deps — copied first to maximise Docker layer caching
# -----------------------------------------------------------------------------
COPY requirement.txt ./
RUN pip install --upgrade pip && pip install -r requirement.txt

# Source is mounted at runtime via docker-compose for hot reload; we still COPY
# so the image can also be used standalone (e.g. `docker run` without compose).
COPY . .

# Persisted volumes mounted at runtime
VOLUME ["/app/src/webdriver"]

EXPOSE 16888 6080 5900

COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["settings"]
