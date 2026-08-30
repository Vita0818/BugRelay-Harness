#!/usr/bin/env bash
# Bug Relay 一键启动（Ubuntu 开箱即用）
#
# 用法：
#   ./run.sh                              # 默认 http://127.0.0.1:8080
#   ./run.sh 0.0.0.0 9000                 # 自定义监听地址/端口
#   BUGRELAY_ARENA_REPO=/path/to/arena ./run.sh   # 环境变量指定 arena_repo（优先级最高）
#
# 首次运行自动创建 .venv 并安装依赖；之后每次启动幂等（依赖已装则秒过）。
set -eo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"

# 1. venv（Ubuntu 系统 pip 受 PEP 668 限制，必须用虚拟环境）
if [ ! -x .venv/bin/python ]; then
    "$PY" -m venv .venv || {
        echo "错误：无法创建虚拟环境。Ubuntu 上请先执行: sudo apt install -y python3-venv" >&2
        exit 1
    }
fi
# shellcheck disable=SC1091
source .venv/bin/activate

# 2. 依赖（幂等）
python -m pip install -q -r requirements.txt || {
    echo "错误：依赖安装失败，请检查网络或 pip 源。" >&2
    exit 1
}

HOST="${1:-127.0.0.1}"
PORT="${2:-8080}"

# 3. arena_repo 路径提示（仅提示，不校验不创建）
if [ -n "${BUGRELAY_ARENA_REPO:-}" ]; then
    echo "[bugrelay] arena_repo（环境变量）: ${BUGRELAY_ARENA_REPO}"
else
    echo "[bugrelay] arena_repo（config.json）: $(python -c 'from core.utils import arena_path; print(arena_path())')"
fi

echo "[bugrelay] 启动 Web 控制台: http://${HOST}:${PORT}"
exec python -m uvicorn app:app --host "$HOST" --port "$PORT"
