FROM python:3.11-slim

# Buildx sets TARGETARCH as amd64/arm64; map to Flatpak arch names.
ARG TARGETARCH
ARG SNAPMAKER_ORCA_VERSION=v2.2.4

# Install Flatpak + headless X support
RUN apt-get update && apt-get install -y \
    wget \
    xvfb \
   flatpak \
   ca-certificates \
   dbus \
   dbus-x11 \
   libglu1-mesa \
   libgl1 \
   libegl1 \
   libxrender1 \
   libxkbcommon0 \
   libdbus-1-3 \
   libxcb-icccm4 \
   libxcb-image0 \
   libxcb-keysyms1 \
   libxcb-render-util0 \
   libxcb-xinerama0 \
   libxcb-xkb1 \
   libxkbcommon-x11-0 \
   libgtk-3-0 \
   libgstreamer1.0-0 \
   libgstreamer-plugins-base1.0-0 \
   libcairo2 \
   libpango-1.0-0 \
   libgdk-pixbuf-2.0-0 \
   libatk1.0-0 \
   libwebkit2gtk-4.1 \
    && rm -rf /var/lib/apt/lists/*

# Install Snapmaker OrcaSlicer Flatpak bundle from GitHub Releases (multi-arch)
RUN set -eux; \
   case "${TARGETARCH:-$(dpkg --print-architecture)}" in \
      amd64) \
         FLATPAK_ARCH="x86_64"; \
         ORCA_BUNDLE_URL="https://github.com/Snapmaker/OrcaSlicer/releases/download/${SNAPMAKER_ORCA_VERSION}/Snapmaker_Orca-Linux-flatpak_V2.2.4_x86_64_Beta.flatpak"; \
         ;; \
      arm64) \
         FLATPAK_ARCH="aarch64"; \
         ORCA_BUNDLE_URL="https://github.com/Snapmaker/OrcaSlicer/releases/download/${SNAPMAKER_ORCA_VERSION}/Snapmaker_Orca-Linux-flatpak_V2.2.4_aarch64_Beta.flatpak"; \
         ;; \
      *) echo "Unsupported architecture: ${TARGETARCH:-$(dpkg --print-architecture)}"; exit 1 ;; \
   esac; \
   wget -O /tmp/orca-slicer.flatpak "${ORCA_BUNDLE_URL}"; \
   flatpak remote-add --if-not-exists flathub \
      https://flathub.org/repo/flathub.flatpakrepo; \
   flatpak install --system --noninteractive --arch="${FLATPAK_ARCH}" \
      /tmp/orca-slicer.flatpak; \
   ORCA_BINARY_PATH="$(find /var/lib/flatpak/app -type f -path '*/files/bin/snapmaker-orca' | head -n 1)"; \
   test -n "${ORCA_BINARY_PATH}"; \
   printf '%s' "${ORCA_BINARY_PATH}" > /etc/orca-binary-path; \
   rm -f /tmp/orca-slicer.flatpak; \
   printf '%s\n' \
      '#!/bin/sh' \
      'set -eu' \
      'BIN="$(cat /etc/orca-binary-path)"' \
      'ROOT="$(dirname "$(dirname "${BIN}")")"' \
      'export LD_LIBRARY_PATH="${ROOT}/lib:${ROOT}/lib64:${LD_LIBRARY_PATH:-}"' \
      'export LC_ALL=C.UTF-8' \
      'exec "${BIN}" "$@"' \
      > /usr/local/bin/orca-slicer; \
   chmod +x /usr/local/bin/orca-slicer; \
   mkdir -p /data/slices /data/logs /cache/slicing; \
   mkdir -p /root/.config/OrcaSlicer/user/machine \
          /root/.config/OrcaSlicer/user/process \
          /root/.config/OrcaSlicer/user/filament

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy Orca profiles
COPY orca_profiles /app/orca_profiles

# Install Snapmaker U1 printer, process, and filament profiles into Orca's config directory
# Also install the common base profile that our process inherits from
RUN ORCA_COMMON_PROFILE="$(find /var/lib/flatpak/app \
   -type f -name fdm_process_common.json | head -n 1)" && \
    test -n "${ORCA_COMMON_PROFILE}" && \
    cp "${ORCA_COMMON_PROFILE}" \
       "/root/.config/OrcaSlicer/user/process/" && \
    cp "/app/orca_profiles/printer/Snapmaker U1 (0.4 nozzle) - multiplate.json" \
       "/root/.config/OrcaSlicer/user/machine/" && \
    cp "/app/orca_profiles/process/0.20mm Standard @Snapmaker U1.json" \
       "/root/.config/OrcaSlicer/user/process/" && \
    cp "/app/orca_profiles/filament/PLA @Snapmaker U1.json" \
       "/root/.config/OrcaSlicer/user/filament/"

COPY app /app

ARG APP_VERSION=dev
ENV APP_VERSION=${APP_VERSION}

CMD ["uvicorn","main:app","--host","0.0.0.0","--port","8000"]
