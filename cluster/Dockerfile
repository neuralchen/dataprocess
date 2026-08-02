# 视频处理 worker 镜像：ffmpeg（含 NVENC）+ PySceneDetect
# 项目代码通过 hostPath 挂载到 /project，改代码后无需重建镜像
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg python3 python3-pip \
        libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

RUN pip3 install --no-cache-dir "scenedetect>=0.6.4" opencv-python-headless numpy

# 让 nvidia-container-runtime 注入驱动与 NVENC 能力
ENV NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility,video

WORKDIR /project
CMD ["bash"]
