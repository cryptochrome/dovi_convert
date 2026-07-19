# =============================================================================
# dovi_convert Docker Image
# Phase 1: CLI container with web-based terminal (ttyd)
# =============================================================================
#
# Build:   docker build -t dovi_convert .
#
# Run (CLI):
#   docker run -it --rm \
#     --hostname dovi-convert \
#     -e PUID=1000 -e PGID=1000 \
#     -v /path/to/movies:/data \
#     dovi_convert
#
# Run (CLI with fast temp storage):
#   docker run -it --rm \
#     -e PUID=1000 -e PGID=1000 \
#     -v /path/to/movies:/data \
#     -v /path/to/ssd:/cache \
#     dovi_convert
#   Then use: dovi_convert convert /data/movie.mkv --temp /cache
#
# Run (Web Terminal):
#   docker run -d \
#     --hostname dovi-convert \
#     -e PUID=1000 -e PGID=1000 \
#     -p 7681:7681 \
#     -v /path/to/movies:/data \
#     dovi_convert
#   Then open http://localhost:7681
#
# =============================================================================

FROM debian:trixie-slim

LABEL maintainer="cryptochrome"
LABEL description="Dolby Vision Profile 7 to Profile 8.1 converter with all dependencies"
LABEL version="1.0"

# =============================================================================
# Environment variables (user-configurable)
# =============================================================================
# PUID/PGID: Set these to match your NAS user for correct file permissions
# TZ: Timezone for logs (e.g., Europe/Berlin, America/New_York)
# UMASK: File creation mask. 022=default (rw-r--r--), 002=group-writable (rw-rw-r--),
#        000=world-writable (rw-rw-rw-, common for UNRAID)
ENV PUID=1000
ENV PGID=1000
ENV TZ=UTC
ENV UMASK=022
ENV TMUX_HISTORY_LIMIT=50000

# Prevent interactive prompts during package installation
ENV DEBIAN_FRONTEND=noninteractive

# Set Bash as default shell
SHELL ["/bin/bash", "-c"]

# =============================================================================
# Install system dependencies
# =============================================================================
RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
    bash \
    python3 \
    curl \
    ca-certificates \
    gosu \
    ffmpeg \
    mkvtoolnix \
    mediainfo \
    wget \
    tzdata \
    nano \
    file \
    less \
    locales \
    tmux \
    bash-completion \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* \
    && echo "en_US.UTF-8 UTF-8" > /etc/locale.gen \
    && locale-gen en_US.UTF-8

ENV LANG=en_US.UTF-8
ENV LANGUAGE=en_US:en
ENV LC_ALL=en_US.UTF-8

# =============================================================================
# Install ttyd (web terminal) from GitHub releases
# =============================================================================
ARG TTYD_VERSION
ARG TARGETARCH

RUN case "${TARGETARCH}" in \
    "amd64") TTYD_ARCH="x86_64" ;; \
    "arm64") TTYD_ARCH="aarch64" ;; \
    *) echo "Unsupported architecture: ${TARGETARCH}" && exit 1 ;; \
    esac && \
    wget -q "https://github.com/tsl0922/ttyd/releases/download/${TTYD_VERSION}/ttyd.${TTYD_ARCH}" \
    -O /usr/local/bin/ttyd && \
    chmod +x /usr/local/bin/ttyd

# =============================================================================
# Install dovi_tool from GitHub releases
# =============================================================================
ARG DOVI_TOOL_VERSION
# TARGETARCH is inherited from above (BuildKit sets this automatically)

RUN case "${TARGETARCH}" in \
    "amd64") DOVI_ARCH="x86_64-unknown-linux-musl" ;; \
    "arm64") DOVI_ARCH="aarch64-unknown-linux-musl" ;; \
    *) echo "Unsupported architecture: ${TARGETARCH}" && exit 1 ;; \
    esac && \
    wget -q "https://github.com/quietvoid/dovi_tool/releases/download/${DOVI_TOOL_VERSION}/dovi_tool-${DOVI_TOOL_VERSION}-${DOVI_ARCH}.tar.gz" \
    -O /tmp/dovi_tool.tar.gz && \
    tar -xzf /tmp/dovi_tool.tar.gz -C /usr/local/bin && \
    chmod +x /usr/local/bin/dovi_tool && \
    rm /tmp/dovi_tool.tar.gz

# =============================================================================
# Copy dovi_convert script (Python version)
# =============================================================================
WORKDIR /app

COPY dovi_convert.py /app/dovi_convert.py
RUN chmod +x /app/dovi_convert.py

# Create symlink so it's available as a command
RUN ln -s /app/dovi_convert.py /usr/local/bin/dovi_convert
RUN ln -s /usr/local/bin/dovi_convert /usr/local/bin/dovi

# =============================================================================
# Create data volume mount point
# =============================================================================
RUN mkdir -p /data
WORKDIR /data

# =============================================================================
# Init script (handles PUID/PGID user creation)
# =============================================================================
COPY <<'EOF' /init
#!/bin/bash
set -e

# Return the outer ID that container ID 0 maps to.
# /proc/self/{uid,gid}_map is provided by the Linux kernel for user namespaces.
mapped_root_id() {
    awk '$1 == 0 && $3 > 0 { print $2; found=1; exit } END { if (!found) exit 1 }' "$1" 2>/dev/null
}

# PUID/PGID 0 is required by rootless Docker because container root maps to the
# unprivileged host user. Allow it only when both kernel maps prove that the
# outer IDs are non-root; otherwise retain the non-root safety requirement.
validate_requested_ids() {
    local uid_map_file="$1"
    local gid_map_file="$2"
    local outer_uid
    local outer_gid

    if [ "${PUID}" != "0" ]; then
        return 0
    fi

    if [ "${PGID}" != "0" ]; then
        echo "ERROR: PUID=0 requires PGID=0 for rootless Docker."
        return 1
    fi

    outer_uid=$(mapped_root_id "${uid_map_file}" || true)
    outer_gid=$(mapped_root_id "${gid_map_file}" || true)

    if [[ ! "${outer_uid}" =~ ^[0-9]+$ || ! "${outer_gid}" =~ ^[0-9]+$ ]] || \
       [ "${outer_uid}" = "0" ] || [ "${outer_gid}" = "0" ]; then
        echo "ERROR: PUID=0/PGID=0 is allowed only when container root maps to non-root host IDs."
        echo "Use PUID/PGID 0 with rootless Docker, or a non-root UID/GID with standard Docker."
        return 1
    fi

    echo "Rootless/user-namespaced runtime detected."
    echo "Container root maps to outer UID=${outer_uid}, GID=${outer_gid}."
}

if ! validate_requested_ids /proc/self/uid_map /proc/self/gid_map; then
    exit 1
fi

# Get or create group with target GID
EXISTING_GROUP=$(getent group "${PGID}" | cut -d: -f1 || true)
if [ -z "${EXISTING_GROUP}" ]; then
    groupadd -g "${PGID}" dovi
    TARGET_GROUP="dovi"
else
    TARGET_GROUP="${EXISTING_GROUP}"
fi

# Get or create user with target UID
# -K UID_MIN=1 -K UID_MAX=65535 overrides Debian's default UID_MIN=1000,
# allowing low UIDs needed by UNRAID (99), Synology, etc.
EXISTING_USER=$(getent passwd "${PUID}" | cut -d: -f1 || true)
if [ -z "${EXISTING_USER}" ]; then
    useradd -K UID_MIN=1 -K UID_MAX=65535 -u "${PUID}" -g "${TARGET_GROUP}" -m -s /bin/bash dovi
    TARGET_USER="dovi"
else
    TARGET_USER="${EXISTING_USER}"
fi

# Note: We do NOT chown bind-mount directories (/data, /cache).
# Users must ensure their PUID/PGID matches the ownership of their files.

# Set timezone
if [ -n "${TZ}" ] && [ -f "/usr/share/zoneinfo/${TZ}" ]; then
    ln -sf "/usr/share/zoneinfo/${TZ}" /etc/localtime
    echo "${TZ}" > /etc/timezone
fi

# Set umask for file creation permissions (inherited by gosu'd processes and child shells)
if [[ "${UMASK}" =~ ^[0-7]{3,4}$ ]]; then
    umask "${UMASK}"
fi

# Configure tmux defaults
if [[ "${TMUX_HISTORY_LIMIT}" =~ ^[0-9]+$ ]]; then
    TMUX_HISTORY_LIMIT_VALUE="${TMUX_HISTORY_LIMIT}"
else
    TMUX_HISTORY_LIMIT_VALUE="50000"
fi
{
    echo "set -g history-limit ${TMUX_HISTORY_LIMIT_VALUE}"
    echo "set -g default-terminal tmux-256color"
} > /etc/tmux.conf

# If running interactively (docker run -it), just exec bash as the user
if [ -t 0 ] && [ "$#" -eq 0 ]; then
    exec gosu "${TARGET_USER}" bash
fi

# If arguments provided (docker run ... bash), run them as user
if [ "$#" -gt 0 ]; then
    exec gosu "${TARGET_USER}" "$@"
fi

# Default: start ttyd web terminal
echo "Starting web terminal on port 7681..."
echo "User: ${TARGET_USER} (PUID=${PUID}, PGID=${PGID})"
exec gosu "${TARGET_USER}" ttyd \
    --port 7681 \
    --writable \
    -t "theme={'background': '#1e1e2e', 'foreground': '#cdd6f4', 'cursor': '#f5e0dc', 'selection': '#585b70', 'black': '#45475a', 'red': '#f38ba8', 'green': '#a6e3a1', 'yellow': '#f9e2af', 'blue': '#89b4fa', 'magenta': '#f5c2e7', 'cyan': '#94e2d5', 'white': '#bac2de', 'brightBlack': '#585b70', 'brightRed': '#f38ba8', 'brightGreen': '#a6e3a1', 'brightYellow': '#f9e2af', 'brightBlue': '#89b4fa', 'brightMagenta': '#f5c2e7', 'brightCyan': '#94e2d5', 'brightWhite': '#a6adc8'}" \
    -t "fontSize=16" \
    -t "cursorBlink=true" \
    -t 'fontFamily="\"JetBrains Mono\", \"Fira Code\", Menlo, \"Cascadia Mono\", \"Cascadia Code\", Consolas, \"DejaVu Sans Mono\", monospace"' \
    -t "scrollback=10000" \
    --debug 1 \
    bash /app/welcome.sh
EOF
RUN chmod +x /init

# =============================================================================
# Welcome script (shown in terminal)
# =============================================================================
COPY <<'EOF' /app/welcome.sh
#!/bin/bash
echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║               dovi_convert Docker Container                  ║"
echo "╠══════════════════════════════════════════════════════════════╣"
echo "║  Usage:                                                      ║"
echo "║    dovi (or dovi_convert)    # Show quick help               ║"
echo "║    dovi help                 # Show full help text           ║"
echo "║                                                              ║"
echo "║  Session persistence:                                        ║"
echo "║    tmux new -s convert       # start persistent session      ║"
echo "║    tmux attach -t convert    # reconnect to session          ║"
echo "║                                                              ║"
echo "║  Your files are mounted at: /data                            ║"
echo "║                                                              ║"
echo "║  For more information, visit:                                ║"
echo "║  https://docs.doviconvert.com                                ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
exec bash
EOF
RUN chmod +x /app/welcome.sh

# =============================================================================
# Expose ttyd web terminal port
# =============================================================================
EXPOSE 7681

# =============================================================================
# Entrypoint: init script handles user creation and command execution
# =============================================================================
ENTRYPOINT ["/init"]
