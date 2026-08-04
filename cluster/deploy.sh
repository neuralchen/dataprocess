#!/usr/bin/env bash
# k3s 视频处理集群部署（Linux / macOS）
# 用法: bash deploy.sh          进入菜单
#       bash deploy.sh deploy   直接执行某个动作
set -e
cd "$(dirname "$0")"

PY=${PYTHON:-python3}

command -v $PY >/dev/null 2>&1 || {
    echo "找不到 python3，请先安装：sudo apt install python3 python3-pip"
    exit 1
}

# paramiko 是唯一外部依赖，用于带密码的 SSH
if ! $PY -c "import paramiko" >/dev/null 2>&1; then
    echo ">> 安装依赖 paramiko ..."
    $PY -m pip install --quiet paramiko || {
        echo "安装失败，请手动执行: $PY -m pip install paramiko"
        exit 1
    }
fi

run() { $PY deploy.py "$1"; }

if [ -n "$1" ]; then
    run "$1"
    exit $?
fi

while true; do
    echo
    echo "=========== 集群部署 ==========="
    if [ -f cluster.json ]; then
        MASTER=$($PY -c "import json;print(json.load(open('cluster.json'))['master']['name'])" 2>/dev/null || echo "?")
        COUNT=$($PY -c "import json;d=json.load(open('cluster.json'));print(1+len(d.get('workers',[])))" 2>/dev/null || echo "?")
        echo "  当前配置: master=$MASTER, 共 $COUNT 个节点"
    else
        echo "  尚未配置（先执行 1）"
    fi
    echo "  1) 配置集群（录入节点、选定 master）"
    echo "  2) 检查各节点环境（只读，不做改动）"
    echo "  3) 执行部署"
    echo "  4) 查看集群状态"
    echo "  5) 卸载 k3s（保留数据）"
    echo "  q) 退出"
    read -r -p "请选择: " c
    case "$c" in
        1) run init ;;
        2) run check ;;
        3) run deploy ;;
        4) run status ;;
        5) run teardown ;;
        q|Q) exit 0 ;;
        *) echo "无效选择" ;;
    esac
done
