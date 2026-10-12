#!/bin/bash
# ZUsage 一键安装/更新：菜单栏 App + zusage CLI + launchd 定时同步 + 开机登录项
#
# 项目目录可放任意位置（含桌面），因为本脚本会把采集器部署一份「运行时副本」到
# ~/.zcode/cli/agent-usage/sync.py，并让 launchd 与 App 都指向这份副本。
# 原因：macOS TCC 保护 ~/Desktop 等目录，launchd 拉起的进程直接读桌面下的脚本会
# 报 "Operation not permitted"。改动源码后重跑本脚本即可重新部署。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
DATA="$HOME/.zcode/cli/agent-usage"
RUNTIME="$DATA/sync.py"
PY=/usr/bin/python3
PLIST_LABEL="local.zusage.agentusage"
PLIST_DST="$HOME/Library/LaunchAgents/$PLIST_LABEL.plist"

echo "[0/6] 补执行位…"
chmod +x "$ROOT/install.sh" "$ROOT/update.sh" "$ROOT/menubar/build.sh" "$ROOT/cli/zusage" "$ROOT/sync/sync.py" "$ROOT/tests/smoke_test.py" 2>/dev/null || true

echo "[1/6] 构建菜单栏 App…"
"$ROOT/menubar/build.sh"

echo "[2/6] 安装 zusage CLI + 部署采集器运行时副本…"
mkdir -p "$HOME/.local/bin" "$DATA"
ln -sfn "$ROOT/cli/zusage" "$HOME/.local/bin/zusage"
cp "$ROOT/sync/sync.py" "$RUNTIME"

echo "[3/6] 生成 launchd 定时同步（60 秒，指向运行时副本）…"
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST_DST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$PLIST_LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PY</string>
        <string>$RUNTIME</string>
    </array>
    <key>StartInterval</key>
    <integer>60</integer>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardErrorPath</key>
    <string>$DATA/sync.err</string>
</dict>
</plist>
PLIST
launchctl bootout "gui/$(id -u)/$PLIST_LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_DST"

echo "[4/6] 告诉 App 同步脚本位置…"
defaults write local.zusage.zusage syncScriptPath "$RUNTIME"

echo "[5/6] 添加开机登录项…"
osascript -e "tell application \"System Events\" to make login item at end with properties {path:\"$HOME/Applications/ZUsage.app\", hidden:true, name:\"ZUsage\"}" >/dev/null 2>&1 || true

echo "[6/6] 首次同步 + 收紧数据文件权限…"
"$PY" "$RUNTIME" || true
chmod 600 "$DATA"/usage.db* 2>/dev/null || true

echo "完成：⚡菜单栏图标 · zusage 命令 · 60s 定时同步"
echo "自检：python3 \"$ROOT/tests/smoke_test.py\""
