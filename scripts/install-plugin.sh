#!/bin/bash
# 把本插件装进某个 DSH profile —— **不需要在命令行里写版本号**。
#
# ── 为什么需要这个脚本 ────────────────────────────────────────────────────────
# pnpm 11 默认开着 24 小时的"发布冷静期"（`minimumReleaseAge=1440`）。于是：
#
#   dsh plugin --profile web add dsh-display-panel        # → 装到 0.6.1（旧版！）
#   dsh plugin --profile web add dsh-display-panel@latest # → 一样是 0.6.1
#   dsh plugin --profile web add dsh-display-panel@0.7.0  # → 0.7.0，而且 pnpm 会
#                                                        #   自动把这一版写进
#                                                        #   minimumReleaseAgeExclude
#
# `latest` 这个 dist-tag **不**绕过冷静期（实测：pnpm 11.7.0 上两者都解析成 ^0.6.1），
# 所以"不带版本号就装不到最新版"不是错觉，而是 pnpm 的默认行为。本脚本做的就是
# 把那句"带版本号"自动做掉：查最新版 → 需要时加白名单 → 带版本号安装。
#
# ── 用法 ──────────────────────────────────────────────────────────────────────
#   bash scripts/install-plugin.sh                    # 用默认 profile（web）
#   bash scripts/install-plugin.sh tui                # 指定 profile
#   DSH_PROFILE=web bash scripts/install-plugin.sh
#   bash scripts/install-plugin.sh --print            # 只打印它会执行的命令，不动手
#
# 装的是 **npm 上的正式版**。要从本地目录/打包文件装（开发用），用 README 里的
# `add link:` / `add file:` 那两条，本脚本不掺和。
#
# 其他可用环境变量：
#   DSH_HOME           DSH 状态目录（默认 ~/.dsh；桌面版是它自己的目录）
#   DSH_BIN            dsh 可执行文件（默认从 PATH 找，再退到 ~/.npm-global/bin/dsh）
#   PLUGIN_REGISTRY    查询"最新版"用的 registry（默认跟随 profile 配置，再退官方）
#   NPM_BIN            npm（默认 PATH 里的 npm；只用来查版本）
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PKG_NAME="$(node -p "require('$REPO_DIR/package.json').name" 2>/dev/null || echo dsh-display-panel)"

# ⚠️ 顺序无关地解析：`--print demo` 与 `demo --print` 都要认。
# （第一版用 `PROFILE="${1:-...}"` 起头，于是 `--print` 在前面时 profile 被重置成
#   默认值 —— 实测过：`install-plugin.sh demo --print` 打印的是 web。）
PROFILE="${DSH_PROFILE:-}"
PRINT_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --print|-n) PRINT_ONLY=1 ;;
    --help|-h) sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*) echo "✗ 未知参数：$arg（--print 只打印，profile 名作为位置参数）" >&2; exit 2 ;;
    *) PROFILE="$arg" ;;
  esac
done
PROFILE="${PROFILE:-web}"

DSH_HOME_DIR="${DSH_HOME:-$HOME/.dsh}"
PROFILE_DIR="$DSH_HOME_DIR/profiles/$PROFILE"

# ---- 找 dsh ----------------------------------------------------------------
DSH_BIN="${DSH_BIN:-}"
if [ -z "$DSH_BIN" ]; then
  DSH_BIN="$(command -v dsh 2>/dev/null || true)"
fi
if [ -z "$DSH_BIN" ] && [ -x "$HOME/.npm-global/bin/dsh" ]; then
  DSH_BIN="$HOME/.npm-global/bin/dsh"
fi
if [ -z "$DSH_BIN" ]; then
  echo "✗ 找不到 dsh。请把它加进 PATH，或用 DSH_BIN=/绝对路径/dsh 指定。" >&2
  exit 1
fi

NPM_BIN="${NPM_BIN:-$(command -v npm 2>/dev/null || true)}"
if [ -z "$NPM_BIN" ]; then
  echo "✗ 找不到 npm（本脚本用它查最新版；也可以用 NPM_BIN 指定）。" >&2
  exit 1
fi

# ---- 1) 查最新版 -----------------------------------------------------------
# 为什么用 npm 而不是 pnpm：`pnpm view` 会**套用冷静期过滤**，问出来的"最新"
# 正好就是被过滤后的那个旧版本 —— 用它等于白问。npm 不受 pnpm 的配置影响。
# 查找顺序与 pnpm 的 .npmrc 链一致：profile → DSH_HOME → 用户主目录 → 官方。
# （profile 自己的配置优先，否则"同一个包在不同 profile 里查到不同版本"会很难解释。）
read_registry() {
  [ -f "$1" ] || return 0
  # 去掉行尾注释、再剥掉一层成对的引号：ini 里 `registry="https://…"` 完全合法，
  # 而带引号的串直接喂给 `npm view --registry=` 时 npm **只警告一下就不认了** ——
  # 于是查询悄悄走到别的 registry 上（实测：读到的版本来自回退源，而 profile
  # 自己的 registry 一个请求都没收到）。注释同理：`registry=x # 说明` 里的注释
  # 会被当成 URL 的一部分。
  sed -n 's/^[[:space:]]*registry[[:space:]]*=[[:space:]]*//p' "$1" | tail -1 \
    | sed 's/[[:space:]]*[#;].*$//' \
    | sed -E 's/^"(.*)"$/\1/; s/^'"'"'(.*)'"'"'$/\1/' \
    | sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//'
}
REGISTRY="${PLUGIN_REGISTRY:-}"
[ -z "$REGISTRY" ] && REGISTRY="$(read_registry "$PROFILE_DIR/.npmrc")"
[ -z "$REGISTRY" ] && REGISTRY="$(read_registry "$DSH_HOME_DIR/.npmrc")"
[ -z "$REGISTRY" ] && REGISTRY="$(read_registry "$HOME/.npmrc")"
REGISTRY="${REGISTRY:-https://registry.npmjs.org}"

echo "插件：$PKG_NAME"
echo "profile：$PROFILE（$PROFILE_DIR）"
echo "registry：$REGISTRY"

# PLUGIN_VERSION 是**逃生门**（registry 不通 / 离线 / 想钉版本）：给了它就完全跳过查询 ——
# 早先写成"先查、查不到就 exit 1"，于是这个逃生门在最需要它的场合（没有网）反而失效，
# 而 README 和报错信息都在教用户用它。
if [ -n "${PLUGIN_VERSION:-}" ]; then
  LATEST="$PLUGIN_VERSION"
  # 版本号会被拼进 `pkg@ver`（pypi/pnpm 的参数）并最终出现在配置文件里，格式必须钉死。
  #
  # ⚠️ `grep -q` 是**按行**匹配的：`PLUGIN_VERSION=$'1.0.0\nminimumReleaseAge: 0'` 里的
  # 第一行完全匹配，于是整串被放行 —— 实测真的能穿过去（多出来的那行跟进了 pnpm 命令行）。
  # 所以这里三道一起上：① 只允许版本号字符集（连换行都进不来）；
  # ② 单行检查（值里不许有换行/回车）；③ 正则做**整串**锚定。
  case "$LATEST" in
    *[!0-9A-Za-z.+-]*)
      echo "✗ PLUGIN_VERSION 含版本号不允许的字符：$LATEST" >&2
      exit 2 ;;
  esac
  if [ "$(printf '%s' "$LATEST" | wc -l | tr -d ' ')" != "0" ] \
     || ! printf '%s' "$LATEST" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+([-+][0-9A-Za-z.-]+)?$'; then
    echo "✗ PLUGIN_VERSION 不是合法的语义化版本号：$LATEST" >&2
    echo "  形如 0.8.0 或 0.8.0-rc.1（单行，不含其它字符）" >&2
    exit 2
  fi
  echo "版本：$LATEST（来自 PLUGIN_VERSION，跳过 registry 查询）"
else
  LATEST="$("$NPM_BIN" view "$PKG_NAME" version --registry="$REGISTRY" 2>/dev/null | tail -1 | tr -d '\r\n' || true)"
  if [ -z "$LATEST" ]; then
    echo "✗ 查不到 $PKG_NAME 的最新版（registry 不通？包名写错？）。" >&2
    echo "  离线/不通时可以直接指定版本：PLUGIN_VERSION=1.2.3 bash scripts/install-plugin.sh $PROFILE" >&2
    exit 1
  fi
  # registry 回的内容是**远端数据**，进命令行前必须过滤：只接受纯版本号字符集 + 单行。
  case "$LATEST" in
    *[!0-9A-Za-z.+-]*)
      echo "✗ registry 回的内容含版本号不允许的字符：$LATEST" >&2
      exit 1 ;;
  esac
  if [ "$(printf '%s' "$LATEST" | wc -l | tr -d ' ')" != "0" ] \
     || ! printf '%s' "$LATEST" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+([-+][0-9A-Za-z.-]+)?$'; then
    echo "✗ registry 回的不是版本号：$LATEST" >&2
    exit 1
  fi
  echo "最新版：$LATEST"
fi

# ---- 2) 需要时把这一版加进冷静期白名单 -------------------------------------
# 位置与 pnpm 自己加的一样：<profile>/pnpm-workspace.yaml 的 minimumReleaseAgeExclude。
#
# ⚠️ 判定要小心：`pnpm config get minimumReleaseAge` 在**没显式配过**时回
# `undefined`，而 pnpm 11 的**默认值就是 1440**（冷静期是开着的！）。实测：
#
#     配置为空              → undefined   ← 冷静期仍然生效
#     minimumReleaseAge: 0  → 0           ← 明确关掉了
#
# 所以只有"明确读到 0/false"才算关，其余一律加白名单条目 —— 它**幂等**，
# 而且对"带版本号安装"这一形式本来就无害（pnpm 自己也会加上去）。
PNPM_BIN=""
if [ -x "$HOME/.npm-global/bin/pnpm" ]; then PNPM_BIN="$HOME/.npm-global/bin/pnpm"
elif command -v pnpm >/dev/null 2>&1; then PNPM_BIN="$(command -v pnpm)"; fi

# ⚠️ `dsh plugin add` 是**转发给 pnpm** 的，pnpm 必须在**子进程的 PATH** 里。
# 实测：宿主进程的 PATH 常常只有 `/usr/local/bin:/usr/bin`（pnpm 装在
# `~/.npm-global/bin`），于是 `dsh plugin add` 直接 `pnpm was not found`（127）——
# 而那时我们已经改过 pnpm-workspace.yaml 了，留下一个"改了配置却没装成"的中间态。
# 所以：① 动文件之前先确认 pnpm 找得到；② 把它的目录前置进 PATH 交下去。
if [ -n "$PNPM_BIN" ]; then
  export PATH="$(dirname "$PNPM_BIN"):$PATH"
fi

NEED_EXCLUDE=1
if [ -d "$PROFILE_DIR" ]; then
  if [ -n "$PNPM_BIN" ]; then
    AGE="$(cd "$PROFILE_DIR" && "$PNPM_BIN" config get minimumReleaseAge 2>/dev/null | tail -1 | tr -d '\r\n' || true)"
    case "$AGE" in
      0|false|off|no) NEED_EXCLUDE="" ;;              # 明确关掉了冷静期：不用动白名单
      *) NEED_EXCLUDE=1 ;;
    esac
  fi
fi

WORKSPACE="$PROFILE_DIR/pnpm-workspace.yaml"
ENTRY="$PKG_NAME@$LATEST"

# ---------------------------------------------------------------------------
# 把一条 `pkg@version` 加进 minimumReleaseAgeExclude（结构化改写 + 读回验证）
# ---------------------------------------------------------------------------
#
# 为什么不用 awk 拼字符串（第一版就是那么写的，被对抗性审查打回来）：
# `minimumReleaseAgeExclude:` 在 YAML 里有**多种完全合法的写法**，纯文本定位
# 一定会漏，而漏的后果是"写出重复键 / 混缩进" → **整个 profile 的 pnpm 从此读不了
# 这个文件**（之后每次 `dsh plugin add` 都报 duplicated mapping key）：
#
#     minimumReleaseAgeExclude:   # 白名单      ← 行内注释
#     minimumReleaseAgeExclude: [a@1.0.0]       ← flow 序列
#     minimumReleaseAgeExclude:
#     - a@1.0.0                                 ← 零缩进列表项
#
# 所以改成让 YAML 解析器来做：解析 → 追加 → 序列化 → **读回断言新条目在列表里**。
# 解析不了（文件本身有问题、或用了锚点/合并键等我们不认的写法）就**不写**，
# 把人工指令打出来 —— "拒绝并有话说"永远优于"猜着写坏它"。
exclude_entry() {
  # 具体规则见那个脚本自己的文档（解析定位 + 最小文本插入，不动用户注释与缩进）
  python3 "$SCRIPT_DIR/add-release-age-exclude.py" "$WORKSPACE" "$ENTRY"
}

manual_hint() {
  echo "  ✗ 没有自动改这个文件。请手动加一条（等价效果）：" >&2
  echo "      $WORKSPACE 里 minimumReleaseAgeExclude 下面加  - $ENTRY" >&2
  echo "    或者关掉冷静期：在同一个文件里设 minimumReleaseAge: 0" >&2
}

if [ -n "$NEED_EXCLUDE" ] && [ -f "$WORKSPACE" ] && ! grep -qF "$ENTRY" "$WORKSPACE"; then
  echo "冷静期（minimumReleaseAge）开着：把 $ENTRY 加进 $WORKSPACE"
  if [ "$PRINT_ONLY" = "1" ]; then
    echo "  [print] 在白名单里加一条：$ENTRY"
  else
    BAK="$WORKSPACE.bak.$(date +%Y%m%d-%H%M%S)"
    cp "$WORKSPACE" "$BAK"
    # ⚠️ `VAR="$(cmd)"` 里 cmd 返回非零时，**bash 5.3 在 `set -e` 下会直接退出脚本**
    # （实测：`set -e; R="$(f)"` 而 `f` 返回 4 → 整个脚本 exit 4，后面一行都不执行）。
    # 所以退出码要单独取，并且用 `|| true` 把这条赋值语句本身"免疫"掉 —— 否则
    # "文件形态不认识"这条路径会变成静默退出（用户既看不到原因，也拿不到人工指令）。
    RESULT="$(exclude_entry || true)"
    STATUS="${PIPESTATUS[0]:-$?}"
    case "$STATUS:$RESULT" in
      0:added)
        echo "  ✓ 已加入白名单（原文件备份在 $BAK）" ;;
      0:already)
        echo "  ✓ 白名单里已经有了" ;;
      3:no-yaml)
        echo "  ⚠ 缺 PyYAML，无法安全改写这个文件（不猜着写）" >&2
        echo "    装一下：python3 -m pip install pyyaml" >&2
        manual_hint
        exit 1 ;;
      *)
        echo "  ⚠ 没认出来这个文件的写法：$RESULT" >&2
        manual_hint
        exit 1 ;;
    esac
    # 最后一道：让 pnpm 自己读一遍。读不了就回滚 —— 绝不留下读不动的 workspace。
    if [ -n "$PNPM_BIN" ] && ! (cd "$PROFILE_DIR" && "$PNPM_BIN" config get registry >/dev/null 2>&1); then
      echo "  ✗ 写完之后 pnpm 读不了这个 workspace，已回滚" >&2
      cp "$BAK" "$WORKSPACE"
      exit 1
    fi
  fi
fi

# ---- 3) 安装（**带版本号**，这是唯一能拿到最新版的形式） --------------------
CMD=("$DSH_BIN" plugin --profile "$PROFILE" add "$ENTRY")
echo
echo "执行：${CMD[*]}"
if [ "$PRINT_ONLY" = "1" ]; then
  echo "（--print：没有真的执行）"
  exit 0
fi

if [ ! -d "$PROFILE_DIR" ]; then
  echo "提示：profile 目录还不存在，dsh 会先初始化它（这一步是正常的）。"
fi
"${CMD[@]}"

echo
echo "✓ 装好了：$ENTRY"
echo "  重启 DSH 让宿主半边生效；面板（浏览器半边）改动刷新页面即可。"
echo "  验证：dsh plugin --profile $PROFILE why $PKG_NAME"
