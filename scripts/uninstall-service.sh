#!/bin/bash
# 卸载「显示器服务」：停进程 / 停并移除 systemd 用户单元。
#
# 用法：
#   bash scripts/uninstall-service.sh            # 卸载（**默认保留**运行目录里的数据）
#   bash scripts/uninstall-service.sh --purge    # 连运行目录一起删（token / 会话 / 日志）
#   bash scripts/uninstall-service.sh --status   # 只看状态，什么都不改（= install-service.sh --status）
#   bash scripts/uninstall-service.sh --help
#
# 为什么默认保留数据：运行目录里有**会话与显示的映射**（displays.json）、令牌、日志 ——
# 卸载/重装插件时把它们删掉，用户会看到"显示号全变了、程序全没了"，
# 而这些并不是卸载该管的事。要清干净得显式 --purge。
#
# 卸载只动两处，别的什么都不碰：
#   $UNIT_FILE   （**只删本包装的那个**：默认单元名 dsh-display-panel-viewer，且要求带本包标记
#                 X-DSH-Display-Panel=1 或 ExecStart 指向本包的服务脚本；不是本包的、
#                 或属于另一个 DSH_DISPLAY_HOME 的单元一律不动）
#   $RUN_HOME    （默认 ~/.cache/dsh-display；只有 --purge 才删）
# 它**不会**去动正在跑的 Xvfb：那是服务自己 SIGTERM 时清理的（脚本先 TERM 再等 5 秒）。
#
# 可用环境变量：与 install-service.sh 相同（DSH_DISPLAY_HOME / DSH_VIEW_UNIT_NAME /
# DSH_VIEW_UNIT_DIR / DSH_VIEW_SYSTEMCTL / DSH_VIEW_PORT / PYTHON），见 --help。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/service-common.sh
. "$SCRIPT_DIR/service-common.sh"

usage() {
  # 只打印文件头的注释块（到 set -... 为止），避免把代码当帮助打出来
  sed -n '2,/^set -/{/^#/p;}' "$0" | sed -e 's/^# \{0,1\}//'
}

cmd_uninstall() {
  local purge="${1:-no}" stopped=0 pid

  log "卸载显示器服务（运行目录 $RUN_HOME 默认保留）"
  print_paths
  log ""
  legacy_unit_note

  # ① systemd 用户单元：有总线就正常 disable（避免下次登录又起来）；没总线也要把文件删掉，
  #    否则用户以后一旦有了总线，这个单元还会阴魂不散地冒出来。
  #    但**只动本包装的、且属于本运行目录的那个** —— 同名单元完全可能是别人的服务
  #    （真实场景：~/.config/systemd/user/dsh-display-viewer.service 指向另一份 checkout）。
  if unit_installed && ! unit_is_ours; then
    warn "单元 $UNIT_FILE 不是本包装的（无标记、ExecStart 也不指向本包）—— 本次不动它"
    warn "  $(unit_exec_start)"
    warn "  要卸载它请用它的安装方提供的方式；本脚本绝不替别人停服务。"
  elif unit_installed && ! unit_belongs_to_home; then
    warn "单元 $UNIT_FILE 是本包装的，但属于另一个运行目录（DSH_DISPLAY_HOME 不同）—— 本次不动它"
    warn "  要卸载它：用它自己的 DSH_DISPLAY_HOME 跑本脚本"
  elif unit_installed; then
    if have_systemd_user; then
      if "$SYSTEMCTL" --user disable --now "$SVC_NAME.service" >/dev/null 2>&1; then
        log "✓ 已停止并禁用 systemd 用户单元 $SVC_NAME"
      else
        warn "systemctl --user disable --now 失败（可能本来就没在跑），继续删单元文件"
      fi
    else
      warn "systemd 用户总线不可用 —— 跳过 disable，直接删单元文件"
      warn "  （本机若以后恢复总线，建议手动跑一次：systemctl --user daemon-reload）"
    fi
    rm -f "$UNIT_FILE"
    log "✓ 已删除单元文件 $UNIT_FILE"
    if have_systemd_user; then
      "$SYSTEMCTL" --user daemon-reload >/dev/null 2>&1 || true
      "$SYSTEMCTL" --user reset-failed "$SVC_NAME.service" >/dev/null 2>&1 || true
    fi
    stopped=1
  fi

  # ② 后台兜底进程（PID 文件 + 按脚本名认亲；认亲失败绝不会误杀别的实例）
  pid="$(resolve_pid || true)"
  if [ -n "$pid" ]; then
    kill "$pid" 2>/dev/null || true
    local i=0
    while [ "$i" -lt 20 ] && pid_alive "$pid"; do sleep 0.25; i=$((i + 1)); done
    if pid_alive "$pid"; then
      warn "PID $pid 没在 5 秒内退出，改用 SIGKILL"
      kill -9 "$pid" 2>/dev/null || true
    fi
    log "✓ 已停止后台兜底进程 PID $pid"
    stopped=1
  fi
  rm -f "$PID_FILE"

  # ③ 数据目录
  if [ "$purge" = "yes" ]; then
    if [ -d "$RUN_HOME" ]; then
      rm -rf "$RUN_HOME"
      log "✓ 已删除运行目录 $RUN_HOME（--purge：token / 会话 / 日志全清）"
    fi
  else
    [ -d "$RUN_HOME" ] && log "· 保留运行目录 $RUN_HOME（令牌、会话/显示映射、日志；要清掉加 --purge）"
  fi

  [ "$stopped" = 1 ] || log "· 没有可停止的单元或进程（本来就干净）"
  log ""
  log "插件本体（DSH profile 里的依赖）不在本脚本范围内，按 README「安装 → 移除」一节处理。"
}

case "${1:---uninstall}" in
  --uninstall | uninstall | "") cmd_uninstall no ;;
  --purge | purge)              cmd_uninstall yes ;;
  --status | status)            exec bash "$SCRIPT_DIR/install-service.sh" --status ;;
  -h | --help | help)           usage ;;
  *) die "未知参数：$1（用 --help 看用法）" ;;
esac
