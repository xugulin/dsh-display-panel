#!/bin/bash
# 安装并启动"显示器服务"（每个会话一台独立显示，面板通过它取画面/注入输入）。
#
# 用法：bash scripts/install-service.sh [用户名]
#   默认用当前用户；给用户名时会写进单元文件的 User= 提示里（本服务是**用户级**单元，
#   通常直接当前用户跑即可）。
set -euo pipefail
USER_NAME="${1:-$(id -un)}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
PY="${PYTHON:-$(command -v python3)}"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT="$UNIT_DIR/dsh-display-viewer.service"

command -v Xvfb    >/dev/null || { echo "缺少 Xvfb：请先安装（Arch: sudo pacman -S xorg-server-xvfb）"; exit 1; }
command -v xdotool >/dev/null || { echo "缺少 xdotool：请先安装（Arch: sudo pacman -S xdotool）"; exit 1; }
command -v xclip   >/dev/null || { echo "缺少 xclip：中文输入需要它（Arch: sudo pacman -S xclip）"; exit 1; }
command -v import  >/dev/null || { echo "缺少 ImageMagick 的 import（Arch: sudo pacman -S imagemagick）"; exit 1; }

mkdir -p "$UNIT_DIR"
sed -e "s|@PYTHON@|$PY|" -e "s|@HERE@|$HERE|" "$HERE/service/dsh-display-viewer.service" > "$UNIT"
systemctl --user daemon-reload
systemctl --user enable --now dsh-display-viewer.service
sleep 3
systemctl --user --no-pager status dsh-display-viewer.service | head -6
echo
echo "显示器服务地址： http://127.0.0.1:8099/    （根路径是会话索引）"
echo "面板地址：       http://127.0.0.1:8099/s/<sessionId>/"
