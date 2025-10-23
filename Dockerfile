FROM nvidia/cuda:11.8.0-cudnn8-devel-ubuntu22.04

ENV C_FORCE_ROOT 1
ENV LC_ALL=C.UTF-8
ENV LANG=C.UTF-8

# --------------------------------------------------------------------------------------------

RUN ln -sf /usr/share/zoneinfo/Europe/Moscow /etc/localtime
RUN apt update && apt install -y --no-install-recommends make git ca-certificates \
    build-essential libssl-dev zlib1g-dev libbz2-dev libreadline-dev libsqlite3-dev \
    wget curl llvm libncurses5-dev xz-utils tk-dev libxml2-dev libxmlsec1-dev \
    libffi-dev liblzma-dev python3-pip ffmpeg libsm6 libxext6 python3-setuptools \
    python3-dev libpng-dev pngquant sed
RUN apt-get clean && rm -rf /var/lib/apt/lists/*

# --------------------------------------------------------------------------------------------

RUN python3 -m pip install --upgrade pip
RUN pip3 install --upgrade pip setuptools wheel cmake

# --------------------------------------------------------------------------------------------
RUN pip install nvidia-cublas-cu12==12.9.1.4
RUN pip install tensorrt==8.6.1

WORKDIR /app
#COPY weights/ /app/weights/
RUN apt-get update && apt-get install

RUN pip install uv

COPY ./2d-render/pyproject.toml ./

RUN uv pip install --system .

COPY 2d-render/ ./

# --- WebRTC Service ---
WORKDIR /rtc_mediaserver
COPY ./pyproject.toml simple_webrtc_server.py image.png ./run.sh /rtc_mediaserver/
COPY ./rtc_mediaserver/ ./run.sh /rtc_mediaserver/rtc_mediaserver/
RUN uv sync --no-cache-dir

# ---------- copy runtime files ----------
#COPY simple_webrtc_server.py img2.png /rtc_app/
#COPY rtc_mediaserver/ /rtc_app/rtc_mediaserver/
#COPY ./run.sh /rtc_app/run.sh
RUN sed -i -e 's/\r$//' run.sh && chmod +x run.sh

EXPOSE 8080
CMD ["/rtc_mediaserver/run.sh"]