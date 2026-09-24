#!/bin/bash
# 安装 / 启动 / 管理「显示器服务」（每个会话一台独立显示；面板经宿主同源接口取画面、注入输入）。
#
# 用法：
#   bash scripts/install-service.sh                 # 安装并启动（默认动作）
#   bash scripts/install-service.sh --status        # 看状态：端口 / PID / 日志 / 缺什么依赖
#   bash scripts/install-service.sh --stop          # 停止（systemd 单元或后台兜底进程，自动识别）
#   bash scripts/install-service.sh --restart       # 重启（显示会重建，上面跑的程序需要重新拉起）
#   bash scripts/install-service.sh --foreground    # 前台跑（排查用，Ctrl-C 退出）
#   bash scripts/install-service.sh --force         # 允许覆盖"不是本包装的"同名单元（默认拒绝）
#   bash scripts/install-service.sh --help
#
# **它其实可以不装**：宿主半边（lib/index.js）在面板打开、服务没起时会**自动拉起**服务
# （契约 §2「自动拉起」；DSH_VIEW_MANAGED=0 可关）。本脚本的意义是「让它常驻」——
# 常驻之后没有冷启动延迟，也不会因为宿主重启而重建显示。
#
# 两条安装路径（自动选，也可用 DSH_VIEW_SYSTEMD 强制）：
#   ① **主路径**：有 systemd 用户总线 → 装用户单元 $UNIT_FILE 并 enable --now
#      （随登录常驻、开机自启）；
#   ② **兜底**：没有用户总线（容器 / headless / WSL / 某些沙箱 shell）→
#      setsid + nohup 后台化，PID 写 $RUN_HOME/viewer.pid。
#      为什么要有②：这类环境里 `systemctl --user` 会直接报
#      "Failed to connect to user scope bus via local transport:
#       $DBUS_SESSION_BUS_ADDRESS and $XDG_RUNTIME_DIR not defined"，旧脚本一步都走不了，
#      用户只能看到面板里一句"显示器还没有打开"。两条路的**功能完全一样**，
#      区别只是②不随登录自启。
#
# 单元名默认 `dsh-display-panel-viewer`（**不是** `dsh-display-viewer`）：
# 后者可能已经被同一个用户、另一份 checkout 的服务占着 —— 我们**不覆盖、不停、不删**别人的单元。
# 若发现同名单元不是本包装的，脚本会拒绝并给对策；确实要覆盖再加 --force。
#
# 可用环境变量（都可不设）：
#   DSH_VIEW_PORT       服务端口，默认 8099（被占用时服务会自己往后找，脚本以 /health 为准）
#   DSH_DISPLAY_HOME    运行目录（token/port/pid/日志），默认 ~/.cache/dsh-display
#   DSH_VIEW_SYSTEMD    1=强制用 systemd 用户单元；0=强制用后台兜底（排查/测试用）
#   DSH_VIEW_UNIT_NAME  systemd 单元名，默认 dsh-display-panel-viewer（同机多实例时改它）
#   DSH_VIEW_UNIT_DIR   单元目录，默认 ~/.config/systemd/user（测试时指到临时目录）
#   DSH_VIEW_SYSTEMCTL  systemctl 路径（测试时可指向桩脚本）
#   DSH_VIEW_FORCE      1 = 等价于 --force
#   PYTHON              python3 路径
# 还有几个透传给服务本身的：DSH_VIEW_SIZE / DSH_VIEW_BACKEND / DSH_VIEW_INPUT /
# DSH_VIEW_IDLE_MINUTES / DSH_VIEW_LOG（设了就会被写进 systemd 单元的 Environment=）。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/service-common.sh
. "$SCRIPT_DIR/service-common.sh"

usage() {
  # 只打印文件头的注释块（到 set -... 为止），避免把代码当帮助打出来
  sed -n '2,/^set -/{/^#/p;}' "$0" | sed -e 's/^# \{0,1\}//'
}

# ---------------------------------------------------------------- systemd 分支

unit_env_lines() {
  # 把当前环境里"用户显式设了"的配置写进单元，**排在模板的 Environment= 之后**
  # （systemd 里同名后者胜），这样模板的默认值不会被绕过，用户设置也不会丢。
  local v
  printf 'Environment=DSH_VIEW_PORT=%s\n' "$PORT"
  printf 'Environment=DSH_DISPLAY_HOME=%s\n' "$RUN_HOME"
  for v in DSH_VIEW_SIZE DSH_VIEW_BACKEND DSH_VIEW_INPUT DSH_VIEW_IDLE_MINUTES DSH_VIEW_LOG; do
    if [ -n "${!v:-}" ]; then printf 'Environment=%s=%s\n' "$v" "${!v}"; fi
  done
}

render_unit() {
  # 优先用仓库里的模板（service/dsh-display-viewer.service，改一处即可）；
  # 模板不在就内联一份等价的（打包/裁剪过的目录也能用）。
  # 最后插入 ① 用户设的环境变量（排在模板的 Environment= 之后 → 同名后者胜）
  # ② 本包标记 $UNIT_MARK（systemd 忽略 X- 开头的键）——
  #    uninstall/--stop 靠它认出"这是本包装的单元"，绝不误停别人的同名单元。
  local envs
  envs="$(unit_env_lines)"
  {
    if [ -f "$TEMPLATE" ]; then
      sed -e "s|@PYTHON@|$PY|g" -e "s|@HERE@|$REPO_DIR|g" "$TEMPLATE"
    else
      cat <<EOF
[Unit]
Description=DSH display panel service (per-session isolated Xvfb + canvas stream)
Documentation=https://github.com/xugulin/dsh-display-panel#readme

[Service]
Type=simple
WorkingDirectory=$REPO_DIR
Environment=DSH_VIEW_PORT=$PORT
ExecStart=$PY $VIEWER
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
EOF
    fi
  } | awk -v envs="$envs" -v mark="$UNIT_MARK" '
      /^\[Unit\]$/ { print; if (!marked) { print mark; marked = 1 } ; next }
      /^ExecStart=/ && !done {
        n = split(envs, a, "\n")
        for (i = 1; i <= n; i++) if (a[i] != "") print a[i]
        done = 1
      }
      { print }'
}

check_unit_conflict() {
  # 安全阀：同名单元已存在、但**不是本包**装的 → 拒绝，不覆盖。
  # 真实场景：用户的 ~/.config/systemd/user/dsh-display-viewer.service 早就装着另一份
  # checkout 的 viewer（还在跑、占着 8099）。旧脚本会**静默覆盖**它，重启后用户的面板
  # 就指向另一份代码甚至起不来 —— 这种"帮忙帮成事故"必须挡住。
  # 确实想覆盖：--force 或 DSH_VIEW_FORCE=1。
  unit_installed || return 0
  unit_is_ours && return 0
  local exec_line force="${DSH_VIEW_FORCE:-0}"
  [ "${1:-}" = "force" ] && force=1
  exec_line="$(unit_exec_start)"
  if [ "$force" = "1" ]; then
    warn "同名单元不是本包装的，但已指定 --force → 覆盖它："
    warn "  $UNIT_FILE"
    [ -n "$exec_line" ] && warn "  $exec_line"
    return 0
  fi
  warn "已存在同名单元，且它**不是本包**装的 —— 拒绝覆盖："
  warn "  $UNIT_FILE"
  [ -n "$exec_line" ] && warn "  $exec_line"
  warn "对策（任选）："
  warn "  ① 换个单元名，两份服务并存："
  warn "     DSH_VIEW_UNIT_NAME=dsh-display-panel-viewer bash scripts/install-service.sh"
  warn "  ② 先确认旧单元没用再停掉/删掉它："
  warn "     systemctl --user disable --now $SVC_NAME && rm -f '$UNIT_FILE'"
  warn "  ③ 确认就要覆盖它：bash scripts/install-service.sh --force"
  warn "（注意：不改名而覆盖，会让原来用那个单元的服务从此跑本包的代码。）"
  exit 1
}

install_unit() {
  mkdir -p "$UNIT_DIR"
  render_unit > "$UNIT_FILE" || return 1
  # daemon-reload 之后才认识新单元。任何一步失败都返回非 0，让调用方回退到后台兜底
  # —— "装了但没起来"是最糟的结果：用户以为装好了，面板却一直空着。
  "$SYSTEMCTL" --user daemon-reload >/dev/null 2>&1 || return 1
  if ! "$SYSTEMCTL" --user enable --now "$SVC_NAME.service" >/dev/null 2>&1; then
    "$SYSTEMCTL" --user reset-failed "$SVC_NAME.service" >/dev/null 2>&1 || true
    return 1
  fi
  probe_ready 8
}

# ---------------------------------------------------------------- 后台兜底分支

start_background() {
  mkdir -p "$RUN_HOME"
  chmod 700 "$RUN_HOME" 2>/dev/null || true
  [ -n "$PY" ] || die "找不到 python3（可用 PYTHON=/path/to/python3 指定）"
  [ -f "$VIEWER" ] || die "找不到服务脚本：$VIEWER"

  log "后台启动（setsid + nohup，不依赖 systemd）…"
  # setsid：新会话/新进程组，父 shell 退出后不收到 SIGHUP 类连带信号；
  # nohup + </dev/null：不留控制终端，关掉终端也不受影响；
  # 环境里显式带上 PORT/RUN_HOME —— 服务据此选端口，脚本据此"认亲"（见 pid_belongs_to_home）。
  DSH_VIEW_PORT="$PORT" DSH_DISPLAY_HOME="$RUN_HOME" \
    setsid nohup "$PY" "$VIEWER" >>"$LOG_FILE" 2>&1 </dev/null &
  local pid=$!
  # 先用 $! 占位；服务起来后用 /health 自报的 pid 覆盖（setsid 在个别实现下会先 fork，
  # $! 可能不是最终的服务进程）。
  printf '%s\n' "$pid" > "$PID_FILE"

  if ! probe_ready 10; then
    warn "服务 10 秒内没有应答 —— 日志尾部（$LOG_FILE）："
    tail -n 15 "$LOG_FILE" 2>/dev/null >&2 || true
    kill "$pid" 2>/dev/null || true
    rm -f "$PID_FILE"
    die "启动失败，请按上面的日志排查（也见 README「故障排查」）"
  fi

  local real
  if real="$(resolve_pid)"; then
    printf '%s\n' "$real" > "$PID_FILE"
    log "已启动：PID $real，端口 $ACTIVE_PORT"
  else
    log "已启动（端口 $ACTIVE_PORT；服务自己没报 pid，PID 文件里是先记的 $pid）"
  fi
}

# ---------------------------------------------------------------- 子命令

cmd_install() {
  local force="${1:-}"
  [ -n "$PY" ] || die "找不到 python3（可用 PYTHON=/path/to/python3 指定）"
  [ -f "$VIEWER" ] || die "找不到服务脚本：$VIEWER"
  warn_missing_deps
  legacy_unit_note

  if probe_once; then
    log "端口 $ACTIVE_PORT 上已经有本运行目录的服务在跑（PID $(resolve_pid || echo '?'))，不重复启动。"
    return 0
  fi

  if have_systemd_user; then
    check_unit_conflict "$force"          # 别人的同名单元：拒绝覆盖（除非 --force）
    log "检测到 systemd 用户总线可用 → 安装用户单元 $UNIT_FILE"
    if install_unit; then
      log "✓ 已安装并启动 systemd 用户单元 $SVC_NAME（随登录常驻）"
      return 0
    fi
    warn "systemd 用户单元安装/启动失败 → 回退到后台兜底（功能一样，只是不随登录自启）"
    "$SYSTEMCTL" --user reset-failed "$SVC_NAME.service" >/dev/null 2>&1 || true
  else
    log "systemd 用户总线不可用（或 DSH_VIEW_SYSTEMD=0）→ 用 setsid/nohup 后台兜底"
  fi

  start_background
  log "✓ 已启动（后台常驻；不会随登录自启，重启后重跑一次本脚本即可）"
}

cmd_status() {
  print_paths
  log ""
  log "── 进程 ──"
  local pid
  if unit_installed && ! unit_is_ours; then
    log "  systemd 用户单元：$UNIT_FILE 存在，但**不是本包装的**（无标记、ExecStart 也不指向本包）"
    log "    $(unit_exec_start)"
    log "    → 本脚本不动它；要装本包的常驻服务请换名字或先处理它（见 --help / README）"
  elif unit_installed && ! unit_belongs_to_home; then
    log "  systemd 用户单元：$UNIT_FILE 是本包装的，但 DSH_DISPLAY_HOME 不是 $RUN_HOME"
    log "    → 那是另一个运行目录的服务，本次不认它（要管它就用它自己的 DSH_DISPLAY_HOME 跑本脚本）"
  elif unit_active; then
    log "  systemd 用户单元：active（$SVC_NAME）"
    "$SYSTEMCTL" --user --no-pager status "$SVC_NAME" 2>/dev/null | head -n 6 | sed 's/^/  /' || true
  elif unit_installed; then
    log "  systemd 用户单元：已安装但**没在跑**（$UNIT_FILE）"
  else
    log "  systemd 用户单元：未安装（没关系：宿主半边会自动拉起，或用本脚本装）"
  fi
  pid="$(resolve_pid || true)"
  if [ -n "$pid" ]; then
    log "  服务进程 PID：$pid（$(ps -o lstart=,args= -p "$pid" 2>/dev/null | cut -c1-100 || true)）"
  else
    log "  服务进程：没找到（PID 文件 $PID_FILE 里是 $(cat "$PID_FILE" 2>/dev/null || echo 无)）"
  fi

  log ""
  log "── 接口 ──"
  if probe_once; then
    log "  端口 $ACTIVE_PORT 应答正常：http://127.0.0.1:$ACTIVE_PORT/"
    if [ -n "$HEALTH_JSON" ]; then
      log "  /health: $HEALTH_JSON"
    else
      log "  ⚠ 该服务没有 /health（0.3.0 之前的老版本）—— 面板仍可用，但宿主无法自动拉起/诊断"
    fi
  else
    log "  ✗ 没有服务在应答（端口 $PORT 与 $PORT_FILE 都试过了）"
    log "    起服务： bash scripts/install-service.sh"
    log "    ⚠ 其实不用手动起：宿主半边在面板打开时会自动拉起（DSH_VIEW_MANAGED=0 可关）"
    log "    看日志： tail -n 50 $LOG_FILE"
  fi

  log ""
  log "── 依赖 ──"
  local miss
  miss="$(missing_deps)"
  if [ -n "$miss" ]; then
    log "  缺少：$(printf '%s' "$miss" | tr '\n' ' ')（面板里也会明说；见 README「安装依赖」）"
  else
    log "  Xvfb / xdotool / xclip / import 都在"
  fi
}

stop_one() {
  # 先 TERM 给它收拾 Xvfb 的机会（服务退出时会清理自己建的显示），超时才 KILL
  local pid="$1" i=0
  kill "$pid" 2>/dev/null || return 0
  while [ "$i" -lt 20 ]; do
    pid_alive "$pid" || return 0
    sleep 0.25
    i=$((i + 1))
  done
  warn "PID $pid 没在 5 秒内退出，改用 SIGKILL"
  kill -9 "$pid" 2>/dev/null || true
}

cmd_stop() {
  local stopped=0 pid
  if unit_installed && ! unit_is_ours; then
    log "· 单元 $UNIT_FILE 不是本包装的 —— 本次不动它（别人的服务不能替你停）"
  elif unit_installed && ! unit_belongs_to_home; then
    log "· 单元 $UNIT_FILE 属于别的运行目录（DSH_DISPLAY_HOME 不同），本次不动它"
  elif unit_installed && have_systemd_user; then
    if unit_active; then
      if "$SYSTEMCTL" --user stop "$SVC_NAME.service"; then
        log "已停止 systemd 单元 $SVC_NAME（单元仍装着，下次登录还会起）"
      else
        warn "systemctl --user stop $SVC_NAME 失败 —— 见 systemctl --user status 输出"
      fi
      stopped=1
    fi
  fi
  pid="$(resolve_pid || true)"
  if [ -n "$pid" ]; then
    stop_one "$pid"
    log "已停止服务进程 PID $pid"
    stopped=1
  fi
  rm -f "$PID_FILE"
  [ "$stopped" = 1 ] || log "没有正在跑的服务（无需停止）"
  log "提示：宿主半边在面板下次打开时**还会自动拉起**服务；要让面板彻底不起服务，"
  log "      在 DSH 的环境里设 DSH_VIEW_MANAGED=0。要连单元一起清掉：bash scripts/uninstall-service.sh"
}

cmd_foreground() {
  [ -f "$VIEWER" ] || die "找不到服务脚本：$VIEWER"
  log "前台运行（Ctrl-C 退出）：$PY $VIEWER"
  exec env DSH_VIEW_PORT="$PORT" DSH_DISPLAY_HOME="$RUN_HOME" "$PY" "$VIEWER"
}

case "${1:---install}" in
  --install | install | "") cmd_install ;;
  --force | force)          cmd_install force ;;
  --status | status)        cmd_status ;;
  --stop | stop)            cmd_stop ;;
  --restart | restart)
    cmd_stop
    sleep 1
    cmd_install "${2:-}"
    ;;
  --foreground | foreground | -f) cmd_foreground ;;
  -h | --help | help) usage ;;
  *) die "未知参数：$1（用 --help 看用法）" ;;
esac
