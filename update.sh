#!/bin/bash
# 一键更新：拉取最新代码并重装（在终端里运行——终端有桌面读取权限，
# launchd 定时自动更新反而会被 macOS TCC 拦住，所以更新是手动触发的）
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

echo "[1/2] 拉取最新代码…"
git pull --ff-only origin main

echo "[2/2] 重新安装…"
bash install.sh

echo "更新完成 ⚡  菜单栏图标若未变化，稍等一个刷新周期即可"
