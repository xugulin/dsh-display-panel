# shellcheck shell=bash
# install-service.sh / uninstall-service.sh 共用的路径计算与探测函数。
#
# 本文件**不被直接执行**，只被 source —— 所以没有 shebang，也不要在这里做任何有副作用的动作。
#
# 约定：所有可调项都走环境变量，默认值与 service/dsh-display-viewer.py 一致。
# 文档里只写默认值，需要改就设环境变量（README「安装 → 服务怎么跑」一节有表）。

# 调用方（入口脚本）会先算好 SCRIPT_DIR；这里只是兜底，方便单独 source 来交互式排查。
: "${SCRIPT_DIR:=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

SVC_NAME="${DSH_VIEW_UNIT_NAME:-dsh-display-panel-viewer}"  # 单元名 / 兜底模式的标识
LEGACY_SVC_NAMES=("dsh-display-viewer")     # 早期版本与别的 checkout 用过的名字（**绝不碰**）
UNIT_MARK='X-DSH-Display-Panel=1'           # 我们写的单元带的标记（systemd 忽略 X- 开头的键）
VIEWER="${DSH_VIEW_SCRIPT:-$REPO_DIR/service/dsh-display-viewer.py}"
TEMPLATE="$REPO_DIR/service/dsh-display-viewer.service"
PY="${PYTHON:-$(command -v python3 2>/dev/null || true)}"
PORT="${DSH_VIEW_PORT:-8099}"
RUN_HOME="${DSH_DISPLAY_HOME:-$HOME/.cache/dsh-display}"
PORT_FILE="$RUN_HOME/port"                # 服务把**最终**端口写在这里（被占用时会往后找）
PID_FILE="$RUN_HOME/viewer.pid"           # 兜底模式自己记的 PID（服务本身不写 pid 文件）
LOG_FILE="${DSH_VIEW_LOG:-$RUN_HOME/viewer.log}"
UNIT_DIR="${DSH_VIEW_UNIT_DIR:-$HOME/.config/systemd/user}"
UNIT_FILE="$UNIT_DIR/$SVC_NAME.service"
SYSTEMCTL="${DSH_VIEW_SYSTEMCTL:-systemctl}"            # 测试/排查可指向桩脚本

# probe_ready 会把结果填在这里（"服务到底在哪个端口上"以它为准，不要假设就是 $PORT）
ACTIVE_PORT=""
HEALTH_JSON=""

log()  { printf '%s\n' "$*"; }
warn() { printf '⚠ %s\n' "$*" >&2; }
die()  { printf '✗ %s\n' "$*" >&2; exit 1; }

read_token() {
  # 令牌文件可能还不存在（服务从没起过）—— 缺失就返回空串，接口仍会以 403 明确拒绝
  [ -r "$RUN_HOME/token" ] && cat "$RUN_HOME/token" 2>/dev/null || true
}

json_field() {
  # 从 stdin 的 JSON 里取一个顶层字段（打印空串表示没有）。用 python3 是因为它必然存在
  # ——本脚本要跑的就是 Python 服务；不为了少一个依赖去写容易出错的 grep/sed 解析。
  python3 -c 'import json,sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
v = d.get(sys.argv[1]) if isinstance(d, dict) else None
print("" if v is None else v)' "$1" 2>/dev/null || true
}

pid_alive() {
  [ -n "${1:-}" ] && kill -0 "$1" 2>/dev/null
}

pid_belongs_to_home() {
  # 关键防呆：这台机器上可能**同时**跑着别的实例（别的用户、或手工起的一份）。
  # 光看"进程名像"会认错人，所以核对进程环境里的 DSH_DISPLAY_HOME 是不是本脚本这一个。
  # （/proc/<pid>/environ 只有同一用户 + 同 uid 才读得到，读不到就当"不是我们的"。）
  local pid="$1"
  [ -r "/proc/$pid/environ" ] || return 1
  tr '\0' '\n' < "/proc/$pid/environ" | grep -qx "DSH_DISPLAY_HOME=$RUN_HOME"
}

http_code() {
  # 任何 HTTP 应答（含 403/404）都算"端口上有人"，000/空 表示连不上
  curl -s -o /dev/null -m 2 -w '%{http_code}' "$1" 2>/dev/null || true
}

health_at() {
  # 打 /health（契约保证：极快、无副作用，不创建会话、不启动 Xvfb）。
  # 老版本服务没有这个路由 —— 返回空串，由 probe_ready 的兜底分支处理。
  curl -fsS -m 2 "http://127.0.0.1:$1/health?k=$(read_token)" 2>/dev/null || true
}

probe_once() {
  # 探测一次：能应答就填 ACTIVE_PORT/HEALTH_JSON 并返回 0。
  # 先按 <home>/port 里服务自报的端口找，再试 $PORT。
  local p body pid code
  for p in "$(cat "$PORT_FILE" 2>/dev/null || true)" "$PORT"; do
    [ -n "$p" ] || continue
    body="$(health_at "$p")"
    if [ -n "$body" ] && printf '%s' "$body" | grep -q '"ok"'; then
      pid="$(printf '%s' "$body" | json_field pid)"
      # pid 必须属于本 RUN_HOME；拿不到 pid（老版本）就只看端口。
      # 认亲失败 = 这是别人家实例的端口，继续找下一个。
      if [ -n "$pid" ] && ! pid_belongs_to_home "$pid"; then continue; fi
      ACTIVE_PORT="$p"; HEALTH_JSON="$body"; return 0
    fi
  done
  for p in "$PORT" "$(cat "$PORT_FILE" 2>/dev/null || true)"; do
    [ -n "$p" ] || continue
    code="$(http_code "http://127.0.0.1:$p/")"
    if [ -n "$code" ] && [ "$code" != "000" ]; then
      ACTIVE_PORT="$p"; HEALTH_JSON=""; return 0   # 老版本服务：没有 /health 也算起来了
    fi
  done
  return 1
}

probe_ready() {
  # 轮询到服务应答（最多 $1 秒，默认 8）。服务起 Xvfb 之前就该能应答，所以 8 秒足够。
  local timeout="${1:-8}" i=0
  while [ "$i" -lt $((timeout * 2)) ]; do
    probe_once && return 0
    sleep 0.5
    i=$((i + 1))
  done
  return 1
}

resolve_pid() {
  # 找出"本 RUN_HOME 的服务进程 pid"，优先级：/health 自报 > 自己记的 PID 文件 > 按脚本名找。
  # 每一条都用 pid_belongs_to_home 验亲，绝不接受别人家实例的 pid（否则 --stop 会误杀）。
  local pid
  if [ -n "$HEALTH_JSON" ]; then
    pid="$(printf '%s' "$HEALTH_JSON" | json_field pid)"
    [ -n "$pid" ] && printf '%s' "$pid" && return 0
  fi
  pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  if pid_alive "$pid" && pid_belongs_to_home "$pid"; then printf '%s' "$pid"; return 0; fi
  for pid in $(pgrep -f "$VIEWER" 2>/dev/null || true); do
    if pid_belongs_to_home "$pid"; then printf '%s' "$pid"; return 0; fi
  done
  return 1
}

have_systemd_user() {
  # **systemd 用户单元是主路径**；这里只判断"这台机器的当前环境有没有可用的用户总线"。
  # 没有的情况真实存在（容器 / headless / WSL / 某些沙箱 shell —— 沙箱里实测就报过
  # "Failed to connect to user scope bus via local transport: $DBUS_SESSION_BUS_ADDRESS
  #  and $XDG_RUNTIME_DIR not defined"），那种环境下自动退化成 setsid+nohup 兜底。
  # **必须以实际能否连上为准，不能只看 systemctl 存在**。
  #   DSH_VIEW_SYSTEMD=1 强制走 systemd；=0 强制走兜底（两条路都要能人工触发，便于排查）。
  case "${DSH_VIEW_SYSTEMD:-auto}" in
    0 | no | false | off) return 1 ;;
    1 | yes | true | on)  return 0 ;;
  esac
  command -v "$SYSTEMCTL" >/dev/null 2>&1 || return 1
  [ -d /run/systemd/system ] || return 1
  "$SYSTEMCTL" --user show-environment >/dev/null 2>&1
}

unit_installed() { [ -f "$UNIT_FILE" ]; }
unit_active()    { "$SYSTEMCTL" --user is-active --quiet "$SVC_NAME" >/dev/null 2>&1; }

unit_exec_start() {
  # 取单元的 ExecStart（给"这到底是谁的单元"判断与提示用）。
  # 优先问 systemd（它认的是真正加载的那份），问不到就读文件。
  local name="${1:-$SVC_NAME}" out=""
  out="$("$SYSTEMCTL" --user cat "$name.service" 2>/dev/null | grep -m1 '^ExecStart=' || true)"
  if [ -z "$out" ]; then
    out="$(grep -m1 '^ExecStart=' "$UNIT_DIR/$name.service" 2>/dev/null || true)"
  fi
  printf '%s' "$out"
}

unit_is_ours() {
  # 是不是**本包**装出来的单元。两道判据，任意一条成立即可：
  #   ① 带我们的标记（scripts 写单元时会插 $UNIT_MARK，systemd 忽略 X- 开头的键）；
  #   ② ExecStart 指向本包的 service/dsh-display-viewer.py。
  # 这条判据是**安全阀**：用户的 ~/.config/systemd/user 下可能已经有一个同名单元
  # （例如指向另一个 checkout 的那份 viewer）—— 覆盖它 = 悄悄改掉别人正在用的服务。
  [ -f "$UNIT_FILE" ] || return 1
  grep -qx "$UNIT_MARK" "$UNIT_FILE" 2>/dev/null && return 0
  grep -qF "$VIEWER" "$UNIT_FILE" 2>/dev/null
}

unit_belongs_to_home() {
  # "同名单元"还可能是**同一个包、另一个运行目录**（DSH_DISPLAY_HOME 不同）装的。
  # 停/删之前也要确认是本脚本这一个，否则 `--stop` 会顺手停掉另一个实例。
  # 规则：单元里写了 DSH_DISPLAY_HOME → 必须与本脚本一致；没写 → 只认默认运行目录。
  [ -f "$UNIT_FILE" ] || return 1
  if grep -q '^Environment=DSH_DISPLAY_HOME=' "$UNIT_FILE" 2>/dev/null; then
    grep -qx "Environment=DSH_DISPLAY_HOME=$RUN_HOME" "$UNIT_FILE"
    return
  fi
  [ "$RUN_HOME" = "$HOME/.cache/dsh-display" ]
}

legacy_unit_note() {
  # 只**报告**，绝不动手：别的名字的老单元（dsh-display-viewer）可能是另一份 checkout
  # 的服务，正占着 8099。用户需要知道"面板为什么可能连到别的服务上去"。
  local name file exec_line
  for name in "${LEGACY_SVC_NAMES[@]}"; do
    [ "$name" = "$SVC_NAME" ] && continue
    file="$UNIT_DIR/$name.service"
    [ -f "$file" ] || continue
    exec_line="$(unit_exec_start "$name")"
    log "· 另有旧单元 $name（$file）"
    [ -n "$exec_line" ] && log "    $exec_line"
    if [ "$("$SYSTEMCTL" --user is-active "$name" 2>/dev/null || true)" = "active" ]; then
      log "    它正在运行 —— 本脚本**不会**碰它；两个服务会各占一个端口（以 <home>/port 与 /health 为准）"
    fi
  done
}

missing_deps() {
  # 缺依赖**不再让安装失败**：新架构下面板会把缺什么、装哪个包直接显示给用户，
  # 硬失败只会把人挡在门外（而且 win32/darwin 后端本来就不需要这几个命令）。
  local tools=(Xvfb xdotool xclip import) t
  for t in "${tools[@]}"; do
    command -v "$t" >/dev/null 2>&1 || printf '%s\n' "$t"
  done
}

warn_missing_deps() {
  local miss
  miss="$(missing_deps)"
  [ -n "$miss" ] || return 0
  warn "本机缺少：$(printf '%s' "$miss" | tr '\n' ' ')"
  warn "  面板仍能打开，但会显示「缺少依赖」。按发行版装："
  warn "    Arch:   sudo pacman -S xorg-server-xvfb xdotool xclip imagemagick"
  warn "    Ubuntu: sudo apt install xvfb xdotool xclip imagemagick"
  warn "    Fedora: sudo dnf install xorg-x11-server-Xvfb xdotool xclip ImageMagick"
}

print_paths() {
  log "  服务脚本： $VIEWER"
  log "  运行目录： $RUN_HOME   （token / port / viewer.pid / 日志都在这里）"
  log "  日志：     $LOG_FILE"
  log "  单元文件： $UNIT_FILE"
}
