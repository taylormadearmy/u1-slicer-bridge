FROM python:3.11-slim

# Install dependencies for AppImage and GUI libs
RUN apt-get update && apt-get install -y \
    wget \
    xvfb \
    libglu1-mesa \
    libgl1 \
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
    fuse \
    && rm -rf /var/lib/apt/lists/*

# Download Orca Slicer v2.3.1 AppImage (pinned version)
RUN wget -O /usr/local/bin/orca-slicer \
    https://github.com/OrcaSlicer/OrcaSlicer/releases/download/v2.3.1/OrcaSlicer_Linux_AppImage_Ubuntu2404_V2.3.1.AppImage && \
    chmod +x /usr/local/bin/orca-slicer

# Create directories
RUN mkdir -p /data/slices /data/logs /cache/slicing

# Install Python dependencies (if needed for helper scripts)
COPY requirements.txt /tmp/
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Copy Orca profiles
COPY orca_profiles /app/orca_profiles

WORKDIR /app

CMD ["sleep", "infinity"]
