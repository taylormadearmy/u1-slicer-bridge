FROM python:3.11-slim

# Install dependencies for AppImage and GUI libs (for Orca Slicer)
# Note: Orca Slicer is a GUI app that needs many libraries even in headless mode
RUN apt-get update && apt-get install -y \
    wget \
    xvfb \
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
    fuse \
    libwebkit2gtk-4.1 \
    && rm -rf /var/lib/apt/lists/*

# Download and extract Orca Slicer v2.3.1 AppImage (pinned version)
# Extract AppImage to avoid FUSE requirement in Docker
RUN wget -O /tmp/orca-slicer.AppImage \
    https://github.com/OrcaSlicer/OrcaSlicer/releases/download/v2.3.1/OrcaSlicer_Linux_AppImage_Ubuntu2404_V2.3.1.AppImage && \
    chmod +x /tmp/orca-slicer.AppImage && \
    cd /tmp && \
    ./orca-slicer.AppImage --appimage-extract && \
    mv squashfs-root /opt/orca-slicer && \
    ln -s /opt/orca-slicer/AppRun /usr/local/bin/orca-slicer && \
    rm /tmp/orca-slicer.AppImage

# Create directories
RUN mkdir -p /data/slices /data/logs /cache/slicing

# Install Orca Slicer printer, process, and filament configs in user directory
# This allows Orca to reference profiles by name
RUN mkdir -p /root/.config/OrcaSlicer/user/machine \
             /root/.config/OrcaSlicer/user/process \
             /root/.config/OrcaSlicer/user/filament

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy Orca profiles
COPY orca_profiles /app/orca_profiles

# Install Snapmaker U1 printer, process, and filament profiles into Orca's config directory
# Also install the common base profile that our process inherits from
RUN cp "/opt/orca-slicer/resources/profiles/Anycubic/process/fdm_process_common.json" \
       "/root/.config/OrcaSlicer/user/process/" && \
    cp "/app/orca_profiles/printer/Snapmaker U1 (0.4 nozzle) - multiplate.json" \
       "/root/.config/OrcaSlicer/user/machine/" && \
    cp "/app/orca_profiles/process/0.20mm Standard @Snapmaker U1.json" \
       "/root/.config/OrcaSlicer/user/process/" && \
    cp "/app/orca_profiles/filament/PLA @Snapmaker U1.json" \
       "/root/.config/OrcaSlicer/user/filament/"

COPY app /app
CMD ["uvicorn","main:app","--host","0.0.0.0","--port","8000"]
