#!/usr/bin/env bash
# venv 构建脚本（Ubuntu / macOS）
# 用法: bash setup_venv.sh
set -e
cd "$(dirname "$0")"

PY=python3

# Ubuntu 上 venv 模块可能未装（Debian 系把它拆成了单独的包）
if ! $PY -m venv --help >/dev/null 2>&1; then
    echo "缺少 python3-venv，请先执行: sudo apt install python3-venv python3-pip"
    exit 1
fi

if [ ! -d .venv ]; then
    echo ">> 创建虚拟环境 .venv ..."
    $PY -m venv .venv
fi

echo ">> 安装依赖 ..."
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

# 检查 ffmpeg
if ! command -v ffmpeg >/dev/null 2>&1; then
    echo ""
    echo "[警告] 未检测到 ffmpeg，请安装:"
    echo "  Ubuntu: sudo apt install ffmpeg"
    echo "  macOS:  brew install ffmpeg"
fi

echo ""
echo "完成。使用方法:"
echo "  source .venv/bin/activate"
echo "  python split_shots.py ./videos"
