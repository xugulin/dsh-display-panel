#!/bin/bash
# 发布到 npm（让它可以像 dsh-browser-panel 那样一条命令安装，也便于插件市场收录）。
#
# 为什么脚本里要**显式指定 registry**：很多机器（包括作者这台）的 ~/.npmrc 里写着
#   registry=https://registry.npmmirror.com
# —— 那是**只读镜像**，`npm publish` 打过去必然失败（403/EPUBLISHCONFLICT），
# 而报错信息不会提示"是 registry 写错了"。所以这里默认强制走官方 registry，
# 想发到别处请显式给 PUBLISH_REGISTRY。
#
# 凭据：优先级 ① 环境变量 NPM_TOKEN ② PUBLISH_TOKEN_FILE 指定的文件
#       ③ 已经登录的 ~/.npmrc。Automation token 不需要 OTP，最省事。
#
# 用法：
#   bash scripts/publish-npm.sh                       # 交互确认后发布
#   NPM_TOKEN=npm_xxx bash scripts/publish-npm.sh     # 用环境里的令牌
#   PUBLISH_TOKEN_FILE=/path/to/npm-token.txt bash scripts/publish-npm.sh
set -euo pipefail
cd "$(dirname "$0")/.."

REGISTRY="${PUBLISH_REGISTRY:-https://registry.npmjs.org}"
NAME=$(node -p "require('./package.json').name")
VERSION=$(node -p "require('./package.json').version")
TOKEN="${NPM_TOKEN:-}"
if [ -z "$TOKEN" ] && [ -n "${PUBLISH_TOKEN_FILE:-}" ] && [ -f "${PUBLISH_TOKEN_FILE}" ]; then
  TOKEN=$(tr -d '\r\n' <"$PUBLISH_TOKEN_FILE")
fi

echo "包：$NAME@$VERSION"
echo "registry：$REGISTRY"
[ -n "$TOKEN" ] && echo "凭据：来自 $([ -n "${NPM_TOKEN:-}" ] && echo 环境变量 NPM_TOKEN || echo "文件 $PUBLISH_TOKEN_FILE")（不回显）"

# 令牌通过命令行参数传给 npm（不写进任何配置文件）
AUTH=()
[ -n "$TOKEN" ] && AUTH=(--//"${REGISTRY#https://}"/:_authToken="$TOKEN")

# ---- 前置检查 1：身份
if ! WHO=$(npm whoami --registry="$REGISTRY" "${AUTH[@]}" 2>/dev/null); then
  echo "✗ 未登录（或令牌无效）。三种办法任选："
  echo "   ① NPM_TOKEN=<automation token> bash scripts/publish-npm.sh"
  echo "   ② PUBLISH_TOKEN_FILE=/path/to/npm-token.txt bash scripts/publish-npm.sh"
  echo "   ③ npm login --registry=$REGISTRY"
  exit 1
fi
echo "登录身份：$WHO"

# ---- 前置检查 2：这个版本是不是已经发过了（发过的版本 npm 会直接拒）
if npm view "$NAME@$VERSION" version --registry="$REGISTRY" "${AUTH[@]}" >/dev/null 2>&1; then
  echo "✗ $NAME@$VERSION 已经存在于 $REGISTRY —— 先升版本号再发。"
  exit 1
fi
echo "版本检查：$VERSION 尚未发布 ✓"

# ---- 前置检查 3：静态自检（快，避免把明显坏掉的包发出去）
if [ -f service/selfcheck.py ] && command -v python3 >/dev/null; then
  if python3 service/selfcheck.py >/tmp/publish-selfcheck.log 2>&1; then
    echo "自检：$(tail -1 /tmp/publish-selfcheck.log | tr -d '\n')"
  else
    echo "✗ service/selfcheck.py 没通过（日志 /tmp/publish-selfcheck.log）；想强行发布就跳过这一步。"
    exit 1
  fi
fi

# ---- 将要发布的内容
echo
echo "=== 将要发布的内容 ==="
npm pack --dry-run --registry="$REGISTRY" 2>&1 | grep -E "npm notice [0-9.]+[kMB]? +|total files|package size" | sed 's/npm notice//' | sed 's/^/  /'
echo
if [ -t 0 ]; then
  read -r -p "确认发布？(y/N) " ans
  [ "${ans:-N}" = "y" ] || { echo "已取消"; exit 0; }
fi

npm publish --access public --registry="$REGISTRY" "${AUTH[@]}"
echo
echo "✓ 已发布：https://www.npmjs.com/package/$NAME/v/$VERSION"
echo "  安装： npm i $NAME    （DSH 侧还要登记进 profile 的 dsh.profile.bundles，见 README）"
echo "  提示：registry 的搜索/页面有缓存，刚发完那几分钟 npm view 可能还看不到 0.3.x。"
