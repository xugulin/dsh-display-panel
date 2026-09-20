#!/bin/bash
# 发布到 npm（让它可以像 dsh-browser-panel 那样一条命令安装，也便于插件市场收录）。
#
# 前提：已登录 npm（`npm login`，或在本机 ~/.npmrc 里放 Automation token）。
set -euo pipefail
cd "$(dirname "$0")/.."

if ! npm whoami >/dev/null 2>&1; then
  echo "✗ 未登录 npm。请先执行： npm login"
  echo "  （或把 Automation token 写进 ~/.npmrc： //registry.npmjs.org/:_authToken=<token>）"
  exit 1
fi
echo "  登录身份：$(npm whoami)"

# 公开包必做：--access public；先干跑确认内容
echo
echo "=== 将要发布的内容 ==="
npm pack --dry-run 2>&1 | grep -E "npm notice [0-9.]+[kMB]? +|total files|package size" | sed 's/npm notice//' | sed 's/^/  /'
echo
read -r -p "确认发布？(y/N) " ans
[ "${ans:-N}" = "y" ] || { echo "已取消"; exit 0; }

npm publish --access public
echo
echo "✓ 已发布：https://www.npmjs.com/package/$(node -p "require('./package.json').name")"
echo "  安装： npm i <包名>   （DSH 侧仍按 README 登记进 dsh.profile.bundles）"
