# Build stage: compile rtl_433 from source, pinned to the exact version already
# validated running natively on this Pi (25.02), so the container's behavior
# doesn't drift from what was tested. Building from source (rather than a
# third-party prebuilt image) also avoids trusting an external image's supply
# chain for reproducibility.
FROM python:3.12-slim-bookworm AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    cmake \
    git \
    libusb-1.0-0-dev \
    librtlsdr-dev \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

ARG RTL433_VERSION=25.02
RUN git clone --depth 1 --branch ${RTL433_VERSION} https://github.com/merbanan/rtl_433.git /usr/src/rtl_433 \
    && cd /usr/src/rtl_433 \
    && mkdir build && cd build \
    && cmake .. \
    && make -j"$(nproc)" \
    && make install

# Final stage: slim runtime image - only the compiled binary and its runtime
# shared libs make it in, not the whole build toolchain.
FROM python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    libusb-1.0-0 \
    librtlsdr0 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /usr/local/bin/rtl_433 /usr/local/bin/rtl_433

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py manifest.json ./

ENV DATA_DIR=/app/data
RUN mkdir -p /app/data /app/logs

# The listener touches $DATA_DIR/heartbeat whenever it confirms rtl_433 is alive
# and producing output (a decoded press or its periodic stats heartbeat). If
# that file goes stale, the SDR is unreachable - surfaced here at the
# `docker compose ps` level so it's visible without tailing logs. Threshold
# (180s) is ~3x the default RTL433_STATS_INTERVAL (60s).
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "\
import os, sys, time; \
f = os.path.join(os.environ.get('DATA_DIR', '/app/data'), 'heartbeat'); \
sys.exit(0 if os.path.exists(f) and time.time() - os.path.getmtime(f) < 180 else 1)"

CMD ["python", "home_automation.py"]
