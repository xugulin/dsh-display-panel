#!/usr/bin/env python3
"""DSH 测试显示器：**每个 harness 会话一台完全独立的显示**，能看画面，也能把
浏览器里的鼠标/键盘操作注入回去。

## 为什么要每会话独立

早先所有会话共用一台显示，结果**别的项目的窗口混了进来**（用户实测：网盘管理项目的
登录窗口出现在他的「显示器」标签画面里），既串扰又不安全。现在每个 sessionId 有自己
的一台显示、自己的帧缓冲与输入通道 —— 一个会话里跑什么都不可能出现在另一个会话的画面上。

## 为什么底座选 Xvfb（而不是 Wayland）

三条路都实测过（详见 tools/dsh-display-BACKENDS.md）：

* **Wayland 单台**：可用，但指针注入前提苛刻 —— 必须同时满足
  ``WLR_BACKENDS=headless,libinput`` + ``LIBSEAT_BACKEND=seatd`` + 进程带 ``seat`` 组
  + 先起 ``ydotoold``；缺任何一个 ``seat capabilities`` 就是 0，**任何**指针注入都
  无处投递（客户端连指针对象都不会创建）。
* **Wayland 每会话一台**：**不可行** —— seatd 的 seat 是 VT-bound，同一时刻只允许
  一个客户端（实测 ``seat is VT-bound and has an active client``），logind 的会话
  又被用户自己的桌面占着。
* **Xvfb + X11（本文件默认）**：X11 **没有 seat 概念**，可以同时跑任意多个 display；
  输入用 ``xdotool`` 走 XTest，**不需要权限、不占 seat、不碰 uinput**。
  实测两个 display 的画面互不相同（完全独立），点击注入生效。
  代价：里面的程序跑在 X11/XWayland 下，不覆盖「原生 Wayland 渲染」。

## 路由（对外契约见 docs/CONTRACT.md §1）

全局（**绝不创建会话、绝不启动 Xvfb**，供宿主探测）：

    GET  /health          探测专用：极快、无副作用
    GET  /                会话索引页（HTML，人用）
    GET  /state           全局概览（兼容旧探测，无副作用）

每会话（``/s/<sessionId>/…``；sessionId 必须匹配 ``^[A-Za-z0-9._-]{1,64}$``，否则 400）：

    GET  /s/<sid>/            独立页面（人用兜底；显示缺失依赖与光标位置）
    GET  /s/<sid>/snapshot    单帧 JPEG（**不阻塞**：没有帧立刻 503）
    GET  /s/<sid>/stream      MJPEG 流（能感知客户端断开）
    GET  /s/<sid>/state       状态 JSON（cursor / input / realDesktop / tooltip / 抓帧错误）
    GET  /s/<sid>/display     显示号与后端（脚本用）
    GET  /s/<sid>/procs       本会话在本服务里拉起的程序
    POST /s/<sid>/input       输入事件（见 §1.3；**按到达顺序串行执行**）
    POST /s/<sid>/exec        {"argv":[…],"cwd":…,"wait":false} 在会话显示上跑程序
    POST /s/<sid>/kill        {"pid":123}
    POST /s/<sid>/close       回收本会话（等价于 DELETE /s/<sid>）
    DELETE /s/<sid>           回收本会话（停 Xvfb、杀子进程、释放显示号）

## 怎么在某个会话的显示上跑程序

**推荐 ``POST /s/<sid>/exec``** —— 由本服务在自己进程里拉起，天然用对 ``DISPLAY``、
能拿到退出码与输出、能列/杀进程，而且**不依赖客户端那边"沙箱不隔离网络"这一偶然条件**
（DSH 的 bash 工具确实能连上 abstract socket ``@/tmp/.X11-unix/X<n>``，但它的 ``/tmp``
是私有 tmpfs，给沙箱加 ``--unshare-net`` 就立刻连不上；详见 docs/CONTRACT.md §4.1）。

想手工调试时也可以：先 ``GET /s/<sid>/display`` 拿显示号，再 ``DISPLAY=:<号> 程序``
（今天的 DSH 里能用）；或者用 ``/exec`` 起一个 shell：``{"argv":["bash","-lc","xterm &"]}``。

## 会话与显示号的生命周期

* 显示号：先看 ``<HOME>/displays.json`` 里该 sid 的持久映射（**同一 sid 重启服务后仍拿同一号**），
  再在 100..399 里探测**空闲**号并用 ``<HOME>/locks/X<n>.lock``（内容=sid）原子占用
  —— 旧实现是 ``100 + crc32(sid) % 300``，两个会话撞号时会共用一台显示（隔离失效）。
* **归属校验（ownership proof）**：光"这个号有人应答"是不够的 —— X11 的 abstract socket
  （``@/tmp/.X11-unix/X<n>``）属于**网络命名空间**，而 ``/tmp`` 是各命名空间私有的，
  所以别的沙箱/别的实例遗留的 Xvfb 可能占着同一个号：它能应答，却是**别人的画面**。
  因此我们在自己的 X 服务器 root window 上打标记 ``DSH_DISPLAY_SESSION=<sid>``，
  "可用" = 有人应答 **且** （我们自己拉起的 Xvfb 进程还活着 **或** 标记等于本会话 sid）。
  标记不匹配/缺失 → 视为**不可用** → 换号重试（并把它当成"死号"，绝不静默复用别人的屏幕）。
* 空闲回收：``DSH_VIEW_IDLE_MINUTES``（默认 30，**0=不回收**）分钟没有请求就回收会话、
  停掉它的 Xvfb；``/health``、``/``、``/state`` 这类探测**永远不会**创建会话。
* 进程退出（SIGTERM/SIGINT/atexit）会把自己拉起的显示服务器与 /exec 子进程一并收掉。
  失败原因（例如 ``Server is already active for display N``）会写进日志与
  ``/state`` 的 ``startError``，不再只有一句"Xvfb 没起来"。

## Windows（win32 后端）

Windows 没有 Xvfb 这类可以任意多开的 headless 显示服务器（虚拟显示器要装内核
驱动、要管理员），所以 win32 后端**直接抓真实桌面**：GDI ``BitBlt`` 取像素 +
GDI+ 编码 JPEG，纯 ``ctypes``，不依赖 Pillow / ffmpeg / ImageMagick。

* 代价一：**所有会话看到的是同一块屏**，per-session 隔离在这条路上不存在
  （Windows 上本来也只有一块桌面可以看）。
* 代价二：注入的是**真实**鼠标键盘。因此 win32 下注入**默认关闭**（只读观看），
  要开就设 ``DSH_VIEW_INPUT=1``；页面抬头会写明当前是哪种模式。

## 环境变量

``DSH_DISPLAY_HOME``（默认 ~/.cache/dsh-display）、``DSH_VIEW_PORT``（默认 8099）、
``DSH_VIEW_SIZE``（默认 1600x1000；win32 下默认取真实屏幕）、
``DSH_VIEW_BACKEND=x11|wayland|win32|darwin``（Windows 默认 win32，macOS 默认 darwin，
其余默认 x11；macOS 后端**尚未真机验证**，属实验性）、
``DSH_VIEW_INPUT=1``（仅 win32/darwin：允许把点击/按键注入真实桌面，默认关）、
``DSH_VIEW_LOG``（把日志写到文件）、
``DSH_VIEW_IDLE_MINUTES``（空闲回收，默认 30；0=关）。
"""

from __future__ import annotations

import ctypes
import hmac
import html as _html
import json
import os
import queue
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

SERVICE_NAME = "dsh-display-viewer"
VERSION = "0.3.0"

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
#: 抓的是**本机真实桌面**的后端（win32 / darwin）：没有"每会话一台显示"这回事，
#: 所有会话看的是同一块屏，而且注入进去就是真的动用户的鼠标键盘。
REAL_DESKTOP_BACKENDS = ("win32", "darwin")
HOME_DIR = os.environ.get("DSH_DISPLAY_HOME") or os.path.expanduser("~/.cache/dsh-display")
PORT = int(os.environ.get("DSH_VIEW_PORT", "8099"))
W, H = (int(x) for x in (os.environ.get("DSH_VIEW_SIZE") or "1600x1000").split("x"))
BACKEND = (os.environ.get("DSH_VIEW_BACKEND")
           or ("win32" if IS_WIN else ("darwin" if IS_MAC else "x11"))).strip().lower()
#: win32 / darwin 下是否允许把输入注入真实桌面（默认只读观看）。
#: 注意：这两个后端抓的是**本机真实桌面**，注入进去就是真的动用户的鼠标键盘，
#: 所以默认关闭 —— 想开显式设 DSH_VIEW_INPUT=1。
WIN_INPUT = (os.environ.get("DSH_VIEW_INPUT") or "").strip().lower() in ("1", "true", "yes", "on")
MAC_INPUT = WIN_INPUT
#: 抓帧间隔。win32 是纯本地调用（实测约 34ms/帧），可以贴着页面 130ms 的拉帧节奏来；
#: X11/Wayland 每次都要起一个外部进程，间隔给大一点，别把 CPU 烧在抓屏上。
#: darwin 每次要起一个 screencapture 进程，同样给大一点。
GRAB_INTERVAL = {"win32": 0.12, "darwin": 0.3}.get(BACKEND, 0.5)
#: 协议注入工具（tools/virtual-pointer 编译产物），只在 wayland 后端用得到。
VPTR = os.environ.get("DSH_VIEW_VPTR") or os.path.join(HOME_DIR, "vptr", "vptr")


def parse_idle_minutes(raw, default: float = 30.0) -> float:
    """解析空闲回收分钟数。``0``（或负数/非法值里的 0）表示**关闭回收**。

    单独抽成函数是为了能被自检断言：非法输入必须退回默认值而不是让服务崩掉。
    """
    if raw is None or str(raw).strip() == "":
        return float(default)
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return float(default)
    return value if value > 0 else 0.0


#: 会话空闲多少分钟后回收（0 = 不回收）。回收会停掉该会话的 Xvfb 与它的 /exec 子进程。
IDLE_MINUTES = parse_idle_minutes(os.environ.get("DSH_VIEW_IDLE_MINUTES"))
IDLE_SECONDS = IDLE_MINUTES * 60.0
#: 回收线程的检查间隔：空闲阈值很小时（自测/调试用）也要及时生效。
REAP_INTERVAL = min(30.0, max(2.0, IDLE_SECONDS / 4.0)) if IDLE_SECONDS > 0 else 30.0

#: 会话 id 白名单（安全：**绝不能**把未校验的 sid 拼进文件路径）。
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
#: 输入事件类型（契约 §1.3）。
INPUT_TYPES = ("click", "down", "up", "move", "wheel", "text", "key")
#: 每会话输入队列上限：满了宁可报错，也不要无限堆积。
INPUT_QUEUE_MAX = 512
#: 显示号范围。
DISPLAY_BASE = 100
DISPLAY_SPAN = 300
#: 一个"滚轮刻度"折算多少 DOM deltaY 像素；DOM 语义：dy>0 = 向下滚。
WHEEL_UNIT = 60.0
WHEEL_MAX_TICKS = 10
#: /exec wait:true 的默认超时（秒）。
EXEC_WAIT_TIMEOUT = 30.0
EXEC_WAIT_MAX = 600.0
#: 令牌 Cookie 名：让独立页面在第一次带 ?k= 访问之后不必再把令牌放在链接里。
COOKIE_NAME = "dsh_display_token"

#: 各后端需要的命令：(命令, 用途, 常见包名)。win32 是纯 ctypes，**零外部依赖**。
REQUIRED_TOOLS: dict[str, tuple[tuple[str, str, str], ...]] = {
    "x11": (
        ("Xvfb", "虚拟显示服务器", "xorg-server-xvfb / xvfb"),
        ("xdotool", "鼠标与键盘注入", "xdotool"),
        ("import", "抓帧（ImageMagick）", "imagemagick"),
        ("xclip", "中文输入（剪贴板）", "xclip"),
    ),
    "wayland": (
        ("sway", "显示合成器", "sway"),
        ("grim", "抓帧", "grim"),
        ("wtype", "键盘注入", "wtype"),
    ),
    "win32": (),          # 纯 ctypes，不需要外部命令
    "darwin": (
        ("screencapture", "抓帧（macOS 自带）", "系统自带，无需安装"),
    ),
}


def ensure_token() -> str:
    """确保本用户的访问令牌存在（``<HOME_DIR>/token``，权限 600）。

    为什么要令牌：服务监听 127.0.0.1，**同一台机器上的其他用户**也能连上来 ——
    不设防的话，另一个用户的 DSH 面板会看到你的桌面（插件按端口探测，先应答者胜）。
    令牌放在本用户的 ``~/.cache/dsh-display/``（目录 700），别的用户读不到，
    于是只有本用户的面板能连上。令牌由插件的**宿主半边**（以该用户身份运行）读出、
    经宿主自己的接口转交给浏览器 —— 浏览器不需要文件系统权限。
    """
    import secrets

    path = os.path.join(HOME_DIR, "token")
    try:
        with open(path, encoding="utf-8") as fh:
            token = fh.read().strip()
        if token:
            return token
    except OSError:
        pass
    token = secrets.token_hex(16)
    try:
        os.makedirs(HOME_DIR, exist_ok=True)
        os.chmod(HOME_DIR, 0o700)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(token)
        os.chmod(path, 0o600)
    except OSError as exc:
        print(f"⚠ 写令牌文件失败（本次不强制校验）：{exc}", flush=True)
    return token


TOKEN = ensure_token()


def query_token(path: str) -> str:
    """从 ``?k=<token>`` 里取令牌（没有就返回空串）。"""
    try:
        return parse_qs(urlparse(path).query).get("k", [""])[0]
    except ValueError:                               # 乱七八糟的查询串
        return ""


def cookie_token(headers) -> str:
    """从 Cookie 里取令牌（独立页面第二次访问就不必再带 ``?k=``）。"""
    if not headers:
        return ""
    raw = ""
    try:
        raw = headers.get("Cookie") or ""
    except Exception:                                # noqa: BLE001
        return ""
    for part in raw.split(";"):
        name, _, value = part.strip().partition("=")
        if name == COOKIE_NAME:
            return value
    return ""


def token_ok(path: str, headers=None) -> bool:
    """请求是否带对令牌。

    * 查询串里给了 ``k`` 就以它为准（错了不再回退到 Cookie，避免"看起来通过了"）；
    * 没有 ``k`` 才看 Cookie；
    * 比较一律用 :func:`hmac.compare_digest`（``==`` 是计时侧信道）；
    * 没有令牌（写文件失败）时不设防 —— 与旧行为一致。
    """
    if not TOKEN:
        return True
    supplied = query_token(path)
    if not supplied:
        supplied = cookie_token(headers)
    if not supplied:
        return False
    return hmac.compare_digest(supplied, TOKEN)


# ---------------------------------------------------------------- 依赖自检
_missing_cache: list = []
_missing_cache_at = 0.0
_missing_lock = threading.Lock()
_MISSING_TTL = 5.0


def _scan_missing() -> list:
    missing = []
    for name, why, pkg in REQUIRED_TOOLS.get(BACKEND, ()):
        if shutil.which(name) is None:
            missing.append({"tool": name, "why": why, "package": pkg})
    if BACKEND == "x11" and _x11() is None:
        # 没 libX11 就打不了归属标记 → 跨重启认领退化为"绝不认领"（保守但安全）。
        missing.append({"tool": "libX11.so.6", "why": "显示归属标记（防止复用到别人的显示）",
                        "package": "libx11-6 / libX11"})
    return missing


def missing_tools(force: bool = False) -> list:
    """返回缺失的依赖（命令 / 用途 / 常见包名）。

    为什么要自检：缺工具时的表现**非常隐蔽** —— 缺 ``xclip`` 只是"中文打不进去"、
    缺 ``import`` 只是"画面一直黑"，用户根本看不出是缺东西（真实反馈过这类问题）。
    所以启动时查一遍，并且通过 ``/health``、``/state`` 与独立页面把那句话说清楚。

    ``shutil.which`` 本身很便宜，这里仍然加 5 秒缓存：``/health`` 是宿主每次探测都
    要打的接口，必须**极快**（探测超时会让宿主以为服务没起）。
    """
    global _missing_cache, _missing_cache_at
    now = time.time()
    with _missing_lock:
        if force or not _missing_cache_at or (now - _missing_cache_at) > _MISSING_TTL:
            _missing_cache = _scan_missing()
            _missing_cache_at = now
        return [dict(item) for item in _missing_cache]


def input_enabled() -> bool:
    """当前后端是否允许注入（真实桌面后端默认只读）。"""
    if BACKEND in REAL_DESKTOP_BACKENDS:
        return bool(WIN_INPUT if BACKEND == "win32" else MAC_INPUT)
    return True


def real_desktop() -> bool:
    return BACKEND in REAL_DESKTOP_BACKENDS


# ================================================================ 滚轮方向（DOM 语义）
# 契约 §1.3：``dy`` 是 DOM 的 ``deltaY``，**dy>0 = 向下滚**。
# 四个后端的原生方向各不相同，全部在这里统一，避免又出现"Windows 上方向反了"这种 bug。
def wheel_ticks(dy, unit: float = WHEEL_UNIT, limit: int = WHEEL_MAX_TICKS) -> int:
    """DOM ``deltaY`` → 滚轮刻度数：**正数=向下滚**，负数=向上滚，0=不动。"""
    try:
        value = float(dy)
    except (TypeError, ValueError):
        return 0
    if value == 0:
        return 0
    ticks = int(abs(value) / unit) or 1                 # 一格滚轮（deltaMode=1 的行数很小）
    ticks = min(limit, ticks)
    return ticks if value > 0 else -ticks


#: X11 按钮号：4=上滚、5=下滚。DOM dy>0（向下）→ 5。
_X11_WHEEL = {1: "5", -1: "4"}


def x11_wheel_button(ticks: int) -> str:
    return _X11_WHEEL[1 if ticks > 0 else -1]


#: Windows 的 ``mouseData``：**正值 = 向上滚**（与 DOM 相反），所以向下滚要发负值。
WIN_WHEEL_DELTA = 120


def win_wheel_data(ticks: int) -> int:
    """滚轮刻度 → ``MOUSEEVENTF_WHEEL`` 的 ``mouseData``（已按 DOM 语义换算）。"""
    return -WIN_WHEEL_DELTA if ticks > 0 else WIN_WHEEL_DELTA


def mac_scroll_units(ticks: int) -> int:
    """滚轮刻度 → ``CGEventCreateScrollWheelEvent`` 的滚动量（正值=向上滚，故取负）。"""
    return -abs(int(ticks)) if ticks > 0 else abs(int(ticks))


def wayland_wheel_button(ticks: int) -> str:
    """evdev 按钮：``0x4``=上滚、``0x5``=下滚。"""
    return "0x5" if ticks > 0 else "0x4"


def _num(obj, key):
    """从 JSON 事件里取一个数值（bool 不算，字符串数字也接受）。"""
    if not isinstance(obj, dict):
        return None
    value = obj.get(key)
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _unit(value) -> float:
    """把归一化坐标夹到 0..1。"""
    return 0.0 if value is None else min(1.0, max(0.0, float(value)))


def valid_sid(sid) -> bool:
    """会话 id 是否合法（契约 §1.2 的白名单）。

    额外拒绝 ``.`` / ``..``：它们虽然落在字符集里，但拼进路径就是"上一级目录"。
    """
    if not isinstance(sid, str) or not SESSION_ID_RE.match(sid):
        return False
    return sid not in (".", "..")


def js_str(value) -> str:
    """把任意值转成**安全的 JS 字面量**（连 ``</script>`` 都进不来）。

    独立页面的 XSS 就是从这里进来的：旧实现把 URL 里的 ``sid`` / ``k`` 原样插进
    ``<script>`` 里的单引号字符串，构造 ``/s/"><script>…`` 就能在该源上执行脚本。
    """
    text = json.dumps(str(value), ensure_ascii=False)
    return (text.replace("<", "\\u003c").replace(">", "\\u003e")
            .replace("&", "\\u0026").replace("\u2028", "\\u2028")
            .replace("\u2029", "\\u2029"))


def html_escape(value) -> str:
    return _html.escape(str(value), quote=True)


# ================================================================ darwin 后端（实验性）
# ⚠️ 这一段**没有在真机上验证过**（作者手上没有 Mac）—— 逻辑按官方文档写，
#    每一步都包了异常，失败只记日志、不影响别的后端。欢迎 macOS 用户回报结果。
#    抓帧用系统自带的 screencapture（无需安装任何东西）；注入用 Quartz 的 CGEvent。
if IS_MAC:
    import tempfile

    _cg = ctypes.cdll.LoadLibrary(
        "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
    _cf = ctypes.cdll.LoadLibrary(
        "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")

    class CGPoint(ctypes.Structure):
        _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]

    class CGSize(ctypes.Structure):
        _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]

    class CGRect(ctypes.Structure):
        _fields_ = [("origin", CGPoint), ("size", CGSize)]

    _cg.CGMainDisplayID.restype = ctypes.c_uint32
    _cg.CGDisplayBounds.restype = CGRect
    _cg.CGDisplayBounds.argtypes = [ctypes.c_uint32]
    _cg.CGEventCreateMouseEvent.restype = ctypes.c_void_p
    _cg.CGEventCreateMouseEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint32, CGPoint, ctypes.c_uint32]
    _cg.CGEventCreateKeyboardEvent.restype = ctypes.c_void_p
    _cg.CGEventCreateKeyboardEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_bool]
    _cg.CGEventKeyboardSetUnicodeString.restype = None
    _cg.CGEventKeyboardSetUnicodeString.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
                                                    ctypes.POINTER(ctypes.c_uint16)]
    _cg.CGEventCreateScrollWheelEvent.restype = ctypes.c_void_p
    _cg.CGEventCreateScrollWheelEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                                  ctypes.c_uint32, ctypes.c_int32]
    _cg.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
    _cf.CFRelease.argtypes = [ctypes.c_void_p]

    _MOVE, _LDOWN, _LUP, _RDOWN, _RUP = 5, 1, 2, 3, 4
    _BTN_LEFT, _BTN_RIGHT = 0, 1
    _HID = 0                      # kCGHIDEventTap
    _PIXEL_UNITS = 0

    def _mac_screen() -> tuple:
        """主屏尺寸（**点**，不是像素 —— Retina 上两者不同，事件坐标用点）。"""
        r = _cg.CGDisplayBounds(_cg.CGMainDisplayID())
        return int(r.size.width), int(r.size.height)

    def _mac_mouse(x: int, y: int, kind: int, button: int = _BTN_LEFT) -> None:
        ev = _cg.CGEventCreateMouseEvent(None, kind, CGPoint(float(x), float(y)), button)
        if ev:
            _cg.CGEventPost(_HID, ev)
            _cf.CFRelease(ev)

    #: 控制键在 macOS 上的等价字符（用 Unicode 送单字符即可被 App 当成对应按键）。
    _MAC_KEY_ALIASES = {"Enter": "\r", "Backspace": "\x7f", "Tab": "\t", "Escape": "\x1b"}

    def _mac_key_text(text: str) -> None:
        """按 **Unicode 字符串**送字（不用键码表）—— 中文、符号都能过。"""
        for ch in text:
            code = ord(ch)
            ev = _cg.CGEventCreateKeyboardEvent(None, 0, True)
            if not ev:
                continue
            buf = (ctypes.c_uint16 * 1)(code)
            _cg.CGEventKeyboardSetUnicodeString(ev, 1, buf)
            _cg.CGEventPost(_HID, ev)
            _cf.CFRelease(ev)

    def _grab_darwin() -> bytes:
        """抓一帧：系统自带的 screencapture 只能写文件，所以落到临时文件再读回。"""
        fd, path = tempfile.mkstemp(suffix=".jpg")
        os.close(fd)
        try:
            subprocess.run(["screencapture", "-x", "-t", "jpeg", path],
                           capture_output=True, timeout=15)
            with open(path, "rb") as fh:
                return fh.read()
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass


# ================================================================ win32 后端
# 只放平台原语：抓一帧、注入一个事件。取舍见文件头「Windows（win32 后端）」。
if IS_WIN:
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    _gdiplus = ctypes.WinDLL("gdiplus", use_last_error=True)

    class _BMIH(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                    ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                    ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD)]

    class _BMI(ctypes.Structure):
        _fields_ = [("bmiHeader", _BMIH), ("bmiColors", wintypes.DWORD * 3)]

    class _GdipStartup(ctypes.Structure):
        _fields_ = [("GdiplusVersion", ctypes.c_uint32), ("DebugEventCallback", ctypes.c_void_p),
                    ("SuppressBackgroundThread", ctypes.c_int), ("SuppressExternalCodecs", ctypes.c_int)]

    class _GUID(ctypes.Structure):
        _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                    ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]

    class _MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.c_void_p)]

    class _KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_void_p)]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]

    class _INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]

    # 参数类型必须显式声明：64 位下不声明会被按 int 截断成野指针。
    _user32.GetDC.restype = wintypes.HDC
    _user32.GetDC.argtypes = [wintypes.HWND]
    _user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    _user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    _user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]
    _user32.SendInput.restype = wintypes.UINT
    _gdi32.CreateCompatibleDC.restype = wintypes.HDC
    _gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    _gdi32.CreateDIBSection.restype = wintypes.HBITMAP
    _gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.POINTER(_BMI), wintypes.UINT,
                                        ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
    _gdi32.SelectObject.restype = wintypes.HGDIOBJ
    _gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    _gdi32.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                              wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
    _gdiplus.GdiplusStartup.argtypes = [ctypes.POINTER(ctypes.c_void_p),
                                        ctypes.POINTER(_GdipStartup), ctypes.c_void_p]
    _gdiplus.GdipCreateBitmapFromScan0.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                                   ctypes.c_int, ctypes.c_void_p,
                                                   ctypes.POINTER(ctypes.c_void_p)]
    _gdiplus.GdipSaveImageToFile.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
                                             ctypes.POINTER(_GUID), ctypes.c_void_p]
    _gdiplus.GdipDisposeImage.argtypes = [ctypes.c_void_p]

    _SRCCOPY = 0x00CC0020
    _DIB_RGB_COLORS = 0
    _BI_RGB = 0
    _PIXELFORMAT_32BPP_RGB = 0x00022009
    _SM_CXSCREEN, _SM_CYSCREEN = 0, 1
    # GDI+ 内置编码器的固定 CLSID
    _JPEG_CLSID = _GUID(0x557CF401, 0x1A04, 0x11D3,
                        (ctypes.c_ubyte * 8)(0x9A, 0x73, 0x00, 0x00, 0xF8, 0x1E, 0xF3, 0x2E))

    def _dpi_aware() -> str:
        """让 GetSystemMetrics/BitBlt 走物理像素；否则缩放屏上抓到的是被拉伸的画面。"""
        try:
            if _user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):  # PER_MONITOR_AWARE_V2
                return "per-monitor-v2"
        except Exception:                            # noqa: BLE001
            pass
        try:
            if _user32.SetProcessDPIAware():
                return "system"
        except Exception:                            # noqa: BLE001
            pass
        return "none"

    _DPI_MODE = _dpi_aware()

    # 没显式给尺寸就按真实屏幕来：W/H 是页面抬头和注入坐标换算的共同基准，
    # 抓多大就写多大，否则点击会落偏。
    if not os.environ.get("DSH_VIEW_SIZE"):
        W = int(_user32.GetSystemMetrics(_SM_CXSCREEN))
        H = int(_user32.GetSystemMetrics(_SM_CYSCREEN))


    class WinScreen:
        """真实桌面的抓帧器：GDI ``BitBlt`` → DIB → GDI+ 编码 JPEG。"""

        def __init__(self) -> None:
            self.lock = threading.Lock()             # 所有会话共用一块屏，一把锁够了
            os.makedirs(HOME_DIR, exist_ok=True)
            self.path = os.path.join(HOME_DIR, "win-frame.jpg")
            self.token = ctypes.c_void_p()
            startup = _GdipStartup(1, None, 0, 0)
            if _gdiplus.GdiplusStartup(ctypes.byref(self.token), ctypes.byref(startup), None) != 0:
                raise RuntimeError("GdiplusStartup 失败")
            self.w, self.h = self.size()

        def size(self) -> tuple:
            return (int(_user32.GetSystemMetrics(_SM_CXSCREEN)),
                    int(_user32.GetSystemMetrics(_SM_CYSCREEN)))

        def grab(self) -> bytes:
            w, h = self.w, self.h
            with self.lock:
                screen_dc = _user32.GetDC(None)
                mem_dc = _gdi32.CreateCompatibleDC(screen_dc)
                bmi = _BMI()
                bmi.bmiHeader.biSize = ctypes.sizeof(_BMIH)
                bmi.bmiHeader.biWidth = w
                bmi.bmiHeader.biHeight = -h          # 负数 = 自上而下，与 PNG/JPEG 行序一致
                bmi.bmiHeader.biPlanes = 1
                bmi.bmiHeader.biBitCount = 32
                bmi.bmiHeader.biCompression = _BI_RGB
                bits = ctypes.c_void_p()
                hbmp = _gdi32.CreateDIBSection(screen_dc, ctypes.byref(bmi), _DIB_RGB_COLORS,
                                               ctypes.byref(bits), None, 0)
                old = _gdi32.SelectObject(mem_dc, hbmp)
                try:
                    if not _gdi32.BitBlt(mem_dc, 0, 0, w, h, screen_dc, 0, 0, _SRCCOPY):
                        return b""
                    bmp = ctypes.c_void_p()
                    st = _gdiplus.GdipCreateBitmapFromScan0(w, h, w * 4, _PIXELFORMAT_32BPP_RGB,
                                                            bits, ctypes.byref(bmp))
                    if st != 0:
                        print(f"[win32] GdipCreateBitmapFromScan0 失败：{st}", flush=True)
                        return b""
                    try:
                        st = _gdiplus.GdipSaveImageToFile(bmp, self.path,
                                                          ctypes.byref(_JPEG_CLSID), None)
                        if st != 0:
                            print(f"[win32] GdipSaveImageToFile 失败：{st}", flush=True)
                            return b""
                    finally:
                        _gdiplus.GdipDisposeImage(bmp)
                    with open(self.path, "rb") as fh:
                        return fh.read()
                finally:
                    _gdi32.SelectObject(mem_dc, old)
                    _gdi32.DeleteObject(hbmp)
                    _gdi32.DeleteDC(mem_dc)
                    _user32.ReleaseDC(None, screen_dc)

    # ------------------------------------------------------------ 输入注入
    _INPUT_MOUSE, _INPUT_KEYBOARD = 0, 1
    _MEF_MOVE, _MEF_ABSOLUTE = 0x0001, 0x8000
    _MEF_LEFTDOWN, _MEF_LEFTUP = 0x0002, 0x0004
    _MEF_RIGHTDOWN, _MEF_RIGHTUP = 0x0008, 0x0010
    _MEF_MIDDLEDOWN, _MEF_MIDDLEUP = 0x0020, 0x0040
    _MEF_WHEEL = 0x0800
    _KEF_EXTENDED, _KEF_KEYUP, _KEF_UNICODE = 0x0001, 0x0002, 0x0004

    #: DOM 键名 → Windows 虚拟键码（VK）。
    _VK = {
        "Enter": 0x0D, "Backspace": 0x08, "Delete": 0x2E, "Tab": 0x09, "Escape": 0x1B,
        " ": 0x20, "ArrowUp": 0x26, "ArrowDown": 0x28, "ArrowLeft": 0x25, "ArrowRight": 0x27,
        "Home": 0x24, "End": 0x23, "PageUp": 0x21, "PageDown": 0x22,
        "ctrl+": 0x11, "shift+": 0x10, "alt+": 0x12, "super+": 0x5B,
    }

    def _send_inputs(items: list) -> int:
        if not items:
            return 0
        arr = (_INPUT * len(items))(*items)
        return int(_user32.SendInput(len(items), arr, ctypes.sizeof(_INPUT)))

    def _win_mouse(px: int, py: int, flags: int, data: int = 0) -> int:
        """绝对坐标要走 0..65535 归一化；先把光标定位再按键，避免点错位置。"""
        sw, sh = max(1, W - 1), max(1, H - 1)
        nx, ny = int(px * 65535 / sw), int(py * 65535 / sh)
        items = []
        if flags & (_MEF_LEFTDOWN | _MEF_RIGHTDOWN | _MEF_MIDDLEDOWN):
            items.append(_INPUT(_INPUT_MOUSE, _INPUTUNION(mi=_MOUSEINPUT(
                nx, ny, 0, _MEF_MOVE | _MEF_ABSOLUTE, 0, None))))
        items.append(_INPUT(_INPUT_MOUSE, _INPUTUNION(mi=_MOUSEINPUT(
            nx, ny, data, flags, 0, None))))
        return _send_inputs(items)

    def _win_text(text: str) -> int:
        """任意 Unicode 直接走 KEYEVENTF_UNICODE —— 中文不用剪贴板，比 X11 那条路省事。"""
        items = []
        for ch in text:
            code = ord(ch)
            if code > 0xFFFF:                        # 非 BMP 走代理对
                code -= 0x10000
                pairs = [0xD800 + (code >> 10), 0xDC00 + (code & 0x3FF)]
            else:
                pairs = [code]
            for unit in pairs:
                items.append(_INPUT(_INPUT_KEYBOARD, _INPUTUNION(ki=_KEYBDINPUT(
                    0, unit, _KEF_UNICODE, 0, None))))
                items.append(_INPUT(_INPUT_KEYBOARD, _INPUTUNION(ki=_KEYBDINPUT(
                    0, unit, _KEF_UNICODE | _KEF_KEYUP, 0, None))))
        return _send_inputs(items)

    def _win_key(key: str) -> int:
        mods, base = [], key
        for prefix in ("ctrl+", "super+", "alt+", "shift+"):
            while base.startswith(prefix):
                mods.append(_VK[prefix])
                base = base[len(prefix):]
        vk = _VK.get(base)
        if vk is None and len(base) == 1:
            vk = _user32.VkKeyScanW(ctypes.c_wchar(base)) & 0xFF
        if not vk:
            return 0
        items = [_INPUT(_INPUT_KEYBOARD, _INPUTUNION(ki=_KEYBDINPUT(m, 0, 0, 0, None))) for m in mods]
        items.append(_INPUT(_INPUT_KEYBOARD, _INPUTUNION(ki=_KEYBDINPUT(vk, 0, 0, 0, None))))
        items.append(_INPUT(_INPUT_KEYBOARD, _INPUTUNION(ki=_KEYBDINPUT(vk, 0, _KEF_KEYUP, 0, None))))
        items += [_INPUT(_INPUT_KEYBOARD, _INPUTUNION(ki=_KEYBDINPUT(m, 0, _KEF_KEYUP, 0, None)))
                  for m in reversed(mods)]
        return _send_inputs(items)

    def _win_windows() -> int:
        """可见的顶层窗口数（用于页面的"空闲"提示）。"""
        count = 0
        enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def callback(hwnd, _lparam):
            nonlocal count
            if _user32.IsWindowVisible(hwnd):
                length = _user32.GetWindowTextLengthW(hwnd)
                if length > 0:
                    count += 1
            return True

        try:
            _user32.EnumWindows(enum_proc(callback), 0)
        except Exception:                            # noqa: BLE001
            return -1
        return count

    def _win_cursor() -> tuple:
        pt = wintypes.POINT()
        try:
            if _user32.GetCursorPos(ctypes.byref(pt)):
                return int(pt.x), int(pt.y)
        except Exception:                            # noqa: BLE001
            pass
        return -1, -1

    _WIN_SCREEN = None
    _WIN_SCREEN_LOCK = threading.Lock()

    def win_screen() -> "WinScreen":
        global _WIN_SCREEN
        with _WIN_SCREEN_LOCK:
            if _WIN_SCREEN is None:
                _WIN_SCREEN = WinScreen()
            return _WIN_SCREEN


# ================================================================ 显示号分配
# 旧实现是 ``100 + crc32(sid) % 300``：**没有占用检测**。两个 sessionId 撞到同一个号时，
# 第二个会话的 ``_alive()`` 会对**别人的** Xvfb 返回 True → 两个会话共用一台显示，
# 正是这个插件宣称已经解决的"串扰"。现在改成"持久映射 + 探测空闲号 + 锁文件原子占用"。
_display_lock = threading.Lock()


def _displays_map_path() -> str:
    return os.path.join(HOME_DIR, "displays.json")


def _locks_dir() -> str:
    return os.path.join(HOME_DIR, "locks")


def _lock_path(number: int) -> str:
    return os.path.join(_locks_dir(), f"X{number}.lock")


def _ensure_dir(path: str) -> None:
    try:
        os.makedirs(path, exist_ok=True)
        os.chmod(path, 0o700)
    except OSError:
        pass


def _read_display_map() -> dict:
    path = _displays_map_path()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for key, value in data.items():
        try:
            out[str(key)] = int(value)
        except (TypeError, ValueError):
            continue
    return out


def _write_display_map(mapping: dict) -> None:
    path = _displays_map_path()
    _ensure_dir(HOME_DIR)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(mapping, fh, ensure_ascii=False, sort_keys=True)
        os.replace(tmp, path)
        os.chmod(path, 0o600)                        # 里面有会话 id，别让别的用户读到
    except OSError as exc:
        print(f"⚠ 写 {path} 失败（重启后显示号可能变）：{exc}", flush=True)


def _lock_owner(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _socket_in_use(number: int) -> bool:
    return (os.path.exists(f"/tmp/.X11-unix/X{number}")
            or os.path.exists(f"/tmp/.X{number}-lock"))


def _xserver_displays() -> set:
    """扫 ``/proc`` 找出正在跑的 X 服务器占了哪些显示号。

    为什么要这一手：X11 客户端优先走 **abstract unix socket**
    （``@/tmp/.X11-unix/X<n>``），而它属于**网络命名空间**。DSH 的 bash 沙箱里
    ``/tmp`` 是私有 tmpfs（看不到 socket 文件），网络命名空间却是共享的 ——
    于是"文件系统里没这个号、实际上已经有人占了"，我们的 Xvfb 会起不来。
    ``/proc`` 在本机是共享的，扫它最可靠（不依赖 xauth 能不能连上）。
    """
    found = set()
    names = ("Xvfb", "Xorg", "Xwayland", "X")
    try:
        entries = os.listdir("/proc")
    except OSError:
        return found
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as fh:
                argv = [a.decode("utf-8", "replace") for a in fh.read().split(b"\0") if a]
        except OSError:
            continue
        for index, arg in enumerate(argv[:3]):
            if os.path.basename(arg) not in names:
                continue
            for nxt in argv[index + 1:index + 3]:    # 后面一两个参数里通常是 :<号>
                if nxt.startswith(":") and nxt[1:].split(".")[0].isdigit():
                    found.add(int(nxt[1:].split(".")[0]))
            break
    return found


#: 我们在**自己的** X 服务器 root window 上打的归属标记（属性名）。
#: 分辨"这台显示到底是不是本会话的"靠它 —— 光看"有没有人应答"是不够的，
#: 见 Session._owns_display 的说明。
OWNER_PROP = "DSH_DISPLAY_SESSION"

#: XGetWindowProperty 的 AnyPropertyType。
_X11_ANY_TYPE = 0
_x11_lib = None                                      # None=未加载；False=加载失败
_x11_lock = threading.Lock()


def _x11():
    """懒加载 libX11（ctypes）—— 归属标记用它，**不用 xprop**。

    为什么不用 ``xprop -root -set``：实测本机（xprop 1.2 系）它**返回 0 却什么
    都没写进去**，属性读回来永远是 ``not found`` —— 拿它做归属标记等于没做。
    自己调 XChangeProperty/XGetWindowProperty 反而更短、更可控，也少一个依赖。
    另外这里**必须**逐一声明 argtypes：64 位下不声明会被按 int 截断成野指针
    （实测直接段错误）。
    """
    global _x11_lib
    with _x11_lock:
        if _x11_lib is not None:
            return _x11_lib or None
        try:
            lib = ctypes.CDLL("libX11.so.6")
            lib.XOpenDisplay.restype = ctypes.c_void_p
            lib.XOpenDisplay.argtypes = [ctypes.c_char_p]
            lib.XCloseDisplay.restype = ctypes.c_int
            lib.XCloseDisplay.argtypes = [ctypes.c_void_p]
            lib.XDefaultRootWindow.restype = ctypes.c_ulong
            lib.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
            lib.XInternAtom.restype = ctypes.c_ulong
            lib.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
            lib.XChangeProperty.restype = ctypes.c_int
            lib.XChangeProperty.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong,
                                            ctypes.c_ulong, ctypes.c_int, ctypes.c_int,
                                            ctypes.c_char_p, ctypes.c_int]
            lib.XGetWindowProperty.restype = ctypes.c_int
            lib.XGetWindowProperty.argtypes = [
                ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_long, ctypes.c_long,
                ctypes.c_int, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong),
                ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_ulong),
                ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_void_p)]
            lib.XSync.restype = ctypes.c_int
            lib.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
            lib.XFree.restype = ctypes.c_int
            lib.XFree.argtypes = [ctypes.c_void_p]
            _x11_lib = lib
        except Exception as exc:                     # noqa: BLE001
            print(f"⚠ 加载 libX11 失败（显示归属校验会退化）：{type(exc).__name__}: {exc}",
                  flush=True)
            _x11_lib = False
        return _x11_lib or None


def _with_x11(display: str, fn):
    """在 ``display`` 上做一次 X 调用（打开→调用→关闭，全程串行化）。

    连不上、或 libX11 不可用 → 返回 None。
    """
    lib = _x11()
    if not lib:
        return None
    with _x11_lock:
        dpy = lib.XOpenDisplay(str(display).encode("utf-8"))
        if not dpy:
            return None
        try:
            return fn(lib, dpy, lib.XDefaultRootWindow(dpy))
        except Exception:                            # noqa: BLE001
            return None
        finally:
            lib.XCloseDisplay(dpy)


def display_owner(display: str):
    """读 root window 上的归属标记。

    返回 sid 字符串；``""`` 表示**属性不存在**（即不是我们打的标记 = 别人的显示）；
    ``None`` 表示根本问不到（连不上这个显示、或 libX11 不可用）。
    """
    def read(lib, dpy, root):
        prop = lib.XInternAtom(dpy, OWNER_PROP.encode(), True)   # only_if_exists
        if not prop:
            return ""
        actual_type = ctypes.c_ulong()
        actual_format = ctypes.c_int()
        nitems = ctypes.c_ulong()
        bytes_after = ctypes.c_ulong()
        data = ctypes.c_void_p()
        status = lib.XGetWindowProperty(
            dpy, root, prop, 0, 1024, False, _X11_ANY_TYPE,
            ctypes.byref(actual_type), ctypes.byref(actual_format),
            ctypes.byref(nitems), ctypes.byref(bytes_after), ctypes.byref(data))
        if status != 0:
            return None
        try:
            if not data or not nitems.value:
                return ""
            raw = ctypes.string_at(data, nitems.value)
        finally:
            if data:
                lib.XFree(data)
        return raw.decode("utf-8", "replace").strip("\x00")

    return _with_x11(display, read)


def mark_display_owner(display: str, sid: str) -> bool:
    """在我们自己的 X 服务器上打归属标记（root window 的 ``DSH_DISPLAY_SESSION``）。

    标记打在 **X 服务器**里，所以即使本服务被 ``kill -9``、Xvfb 变成孤儿进程，
    重启后的服务仍然能认出"这台显示是本会话的"，从而安全复用它（同号）。
    """
    def write(lib, dpy, root):
        prop = lib.XInternAtom(dpy, OWNER_PROP.encode(), False)
        string = lib.XInternAtom(dpy, b"STRING", False)
        if not prop or not string:
            return False
        value = str(sid).encode("utf-8")
        lib.XChangeProperty(dpy, root, prop, string, 8, 0, value, len(value))
        lib.XSync(dpy, False)
        return True

    return bool(_with_x11(display, write))


def _display_available(number: int, sid: str, busy=frozenset(), mapped: bool = False,
                       owner_check=None) -> bool:
    """号是否可用：不是别人的锁、也不是**别人的** X 服务器在跑。

    ``mapped=True`` 表示"持久映射说这个号原本就是本会话的"：
    * 残留的 socket 文件（Xvfb 被杀后没清干净）不算占用 —— 让 Xvfb 自己去清理，
      否则服务重启后同一 sid 就拿不回同一个号了；
    * 如果这个号上真有一台 X 服务器在跑，还要用 ``owner_check``（读 root 上的
      归属标记）确认那是**我们自己的孤儿 Xvfb** —— 是的话就认领复用（同号）。
      光看"在跑"就复用会让别的命名空间/别人的显示被当成自己的（串扰）。
    """
    lock = _lock_path(number)
    if os.path.exists(lock):
        owner = _lock_owner(lock)
        if owner == sid:
            return True                              # 本会话的（可能正在跑，重启后复用）
        if owner:
            return False                             # 别人的锁
        # 空锁文件 = 上次崩了留下的：继续按占用情况判断
    if number in busy:
        if not mapped or owner_check is None:
            return False                             # 真有 X 服务器跑着这个号
        return bool(owner_check(number))             # 只有"标记对得上"才认领
    return mapped or not _socket_in_use(number)


def _claim_lock(number: int, sid: str) -> bool:
    """用一个 ``O_EXCL`` 锁文件把号占住（跨进程也安全）。"""
    _ensure_dir(_locks_dir())
    path = _lock_path(number)
    for _attempt in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            owner = _lock_owner(path)
            if owner == sid:
                return True
            if owner == "":                          # 空锁：上次崩溃留下的，清掉重来
                try:
                    os.unlink(path)
                except OSError:
                    return False
                continue
            return False
        except OSError:
            return False
        try:
            os.write(fd, sid.encode("utf-8"))
        except OSError:
            pass
        finally:
            os.close(fd)
        return True
    return False


def candidate_display(sid: str) -> int:
    """稳定推导的首选号（crc32 而不是 hash()：后者每进程加盐，重启后会变）。"""
    return DISPLAY_BASE + (zlib.crc32(sid.encode("utf-8")) % DISPLAY_SPAN)


def mark_owner_check(sid: str):
    """给 :func:`claim_display` 用的归属回调：这个号上的 X 服务器是不是本会话的。

    判据就是 root window 上的归属标记（没有标记、或标记是别的 sid → 不是我们的）。
    """
    def check(number: int) -> bool:
        owner = display_owner(f":{number}")
        return owner is not None and owner == sid
    return check


def claim_display(sid: str, exclude=()) -> int:
    """给会话要一个**独占**的显示号；同一 sid 重启服务后仍拿到同一号。"""
    with _display_lock:
        mapping = _read_display_map()
        busy = _xserver_displays()
        current = mapping.get(sid)
        if (current is not None and current not in exclude
                and _display_available(current, sid, busy, mapped=True,
                                       owner_check=mark_owner_check(sid))
                and _claim_lock(current, sid)):
            return current
        start = candidate_display(sid)
        for offset in range(DISPLAY_SPAN):
            number = DISPLAY_BASE + ((start - DISPLAY_BASE + offset) % DISPLAY_SPAN)
            if number in exclude:
                continue
            if not _display_available(number, sid, busy):
                continue
            if not _claim_lock(number, sid):
                continue
            mapping[sid] = number
            _write_display_map(mapping)
            return number
    raise RuntimeError(f"{DISPLAY_BASE}..{DISPLAY_BASE + DISPLAY_SPAN - 1} 没有空闲显示号")


def peek_display(sid: str):
    """只看映射、不占用（``/state`` 探测用；没有映射返回 None）。"""
    return _read_display_map().get(sid)


def release_display(sid: str, number, forget: bool = False) -> None:
    """释放显示号锁（``forget=True`` 时连持久映射一起删掉）。"""
    if number is None:
        return
    with _display_lock:
        lock = _lock_path(int(number))
        if _lock_owner(lock) == sid:
            try:
                os.unlink(lock)
            except OSError:
                pass
        if forget:
            mapping = _read_display_map()
            if mapping.pop(sid, None) is not None:
                _write_display_map(mapping)


# ================================================================ 进程管理
def _register_spawn(proc: subprocess.Popen) -> None:
    with _spawned_lock:
        _spawned.append(proc)


def _unregister_spawn(proc: subprocess.Popen) -> None:
    with _spawned_lock:
        try:
            _spawned.remove(proc)
        except ValueError:
            pass


def _prune_spawned() -> None:
    """把已经退出的进程从 ``_spawned`` 里摘掉（旧实现只增不减 = Popen 泄漏）。"""
    with _spawned_lock:
        for proc in list(_spawned):
            if proc.poll() is not None:
                _spawned.remove(proc)


def _terminate_proc(proc: subprocess.Popen, timeout: float = 3.0) -> None:
    """先 SIGTERM 整个进程组、再 SIGKILL —— /exec 拉起的程序可能自己带子孙。"""
    if proc is None or proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:                                # noqa: BLE001
        try:
            proc.terminate()
        except Exception:                            # noqa: BLE001
            pass
    try:
        proc.wait(timeout=timeout)
        return
    except Exception:                                # noqa: BLE001
        pass
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:                                # noqa: BLE001
        try:
            proc.kill()
        except Exception:                            # noqa: BLE001
            pass


# ================================================================ 会话
class Session:
    """一个 harness 会话独占的显示（X11 默认；wayland / win32 / darwin 为备用后端）。"""

    #: 显示"活着且属于我们"的探测结果缓存时长（秒）。
    #: 探测要起一个 xdotool 进程，而 ensure() 在每个输入事件上都会被调用。
    _ALIVE_TTL = 2.0

    def __init__(self, sid: str) -> None:
        self.sid = sid
        self.dir = os.path.join(HOME_DIR, "sessions", sid)
        self.runtime = os.path.join(self.dir, "run")
        self.latest = b""
        self.lock = threading.Lock()
        self.started = False
        self._boot_lock = threading.Lock()
        self._worker_lock = threading.Lock()
        self._worker = None
        self._closed = False
        self._server_proc = None
        self.created = time.time()
        self.last_used = self.created
        # 显示归属/可用性的探测缓存（见 _alive / _owns_display）。
        self._alive_ok = False
        self._alive_at = 0.0
        self.start_error = None
        self.owner_marked = None
        #: 换过号的话记下原来的号（诊断用：说明那个号被别人占了）。
        self.relocated_from = None
        # 抓帧可见性：失败不再静默（旧实现 except: pass，全黑时谁也看不出原因）。
        self.frame_error = None
        self.frame_count = 0
        self.last_frame_at = 0.0
        # /exec 拉起的程序。
        self.procs = []
        self.proc_lock = threading.Lock()
        # 输入：**每会话一个串行队列**（旧实现每个事件起一个线程 → 打字顺序会乱）。
        self.events = queue.Queue(maxsize=INPUT_QUEUE_MAX)
        self.input_count = 0
        self.last_input_error = None
        if BACKEND in REAL_DESKTOP_BACKENDS:
            # win32 / darwin 抓的是真实桌面，没有"这一路的显示号"。
            self.number = None
            self.display = "真实桌面"
        else:
            self.number = claim_display(sid)
            self.display = f":{self.number}"

    # ------------------------------------------------------------------ 生命周期
    def touch(self) -> None:
        self.last_used = time.time()

    @property
    def closed(self) -> bool:
        return self._closed

    def idle_seconds(self) -> float:
        return max(0.0, time.time() - self.last_used)

    def has_live_procs(self) -> bool:
        with self.proc_lock:
            return any(proc.poll() is None for proc, _argv, _at in self.procs)

    def ensure(self) -> bool:
        if BACKEND in REAL_DESKTOP_BACKENDS:
            self.started = True                      # 真实桌面，没有要拉起的显示服务器
            return True
        if self.started and self._alive():
            return True
        with self._boot_lock:
            if self._closed:
                return False
            if self.started and self._alive():
                return True
            _ensure_dir(os.path.dirname(self.dir))   # .../sessions 也要 700
            _ensure_dir(self.dir)
            _ensure_dir(self.runtime)
            ok = self._start_wayland() if BACKEND == "wayland" else self._start_xvfb()
            # 起不来最常见的原因就是"这个显示号被别的命名空间的 X 服务器占着"：
            # 换号重试几次（每次失败的原因都会写进 self.start_error 与日志）。
            for _attempt in range(3):
                if ok or BACKEND != "x11" or not self.relocate():
                    break
                ok = self._start_xvfb()
            self.started = ok
            return ok

    def relocate(self) -> bool:
        """换一个显示号：原号可能被**别的命名空间**的 X 服务器占着。

        ``@/tmp/.X11-unix/X<n>`` 是网络命名空间共享的，而沙箱里 ``/tmp`` 是私有
        tmpfs —— 号探测有可能漏掉这种占用，表现就是 Xvfb 起不来。这里换号重试，
        而不是把"起不来"直接扔给用户。
        """
        if BACKEND in REAL_DESKTOP_BACKENDS or self.number is None:
            return False
        old = self.number
        release_display(self.sid, old, forget=False)
        try:
            new = claim_display(self.sid, exclude=(old,))
        except RuntimeError:
            return False
        if new == old:
            return False
        print(f"[{self.sid}] 显示号 {old} 起不来（可能被别的命名空间占了），改用 :{new}",
              flush=True)
        self.relocated_from = old
        self.number = new
        self.display = f":{new}"
        return True

    def _display_answers(self) -> bool:
        """这个显示号上**有人应答**吗（不区分是谁）。"""
        if shutil.which("xdotool") is None:
            return display_owner(self.display) is not None
        try:
            proc = subprocess.run(["xdotool", "getdisplaygeometry"], env=self.env,
                                  capture_output=True, timeout=6)
            return proc.returncode == 0 and bool(proc.stdout.strip())
        except Exception:                            # noqa: BLE001
            return False

    def _alive_probe(self) -> bool:
        """真实探测：这个号上有人应答，**并且那台 X 服务器是我们的**。

        ⚠️ 三条都必须查：

        1. 不能只看 socket 文件 —— 进程被杀后 socket 会残留（"文件在、服务没了"）。
        2. 更不能只看"有人应答" —— X11 的 abstract socket（``@/tmp/.X11-unix/X<n>``）
           属于**网络命名空间**，而 ``/tmp`` 是各命名空间私有的：别的沙箱/别的实例
           遗留的 Xvfb 可能占着同一个号。它**能应答**，但那是**别人的画面** ——
           当成自己的用就退回到最初被投诉的"串扰"；反过来，等它退出后我们又记着
           这个号，``/exec`` 里的程序就报 ``cannot open display``。
           （实测：同名号的 Xvfb 在共享网络命名空间里**起不来** ——
           ``Cannot establish any listening sockets``，所以"我们自己的 Xvfb 活着"
           就意味着两个 socket 都是我们的。）
        3. 归属证明见 :meth:`_owns_display`：要么我们自己的 Xvfb 进程活着，
           要么 root window 上带着本会话的标记。跨命名空间认领（服务重启后
           Xvfb 变成孤儿、socket 文件留在旧命名空间的 ``/tmp`` 里）正是靠标记。
        """
        if BACKEND in REAL_DESKTOP_BACKENDS:
            return True                              # 真实桌面永远"在"
        if BACKEND == "wayland":
            return os.path.exists(os.path.join(self.runtime, "wayland-1"))
        if not self._display_answers():
            return False
        if _socket_in_use(self.number) or peek_display(self.sid) == self.number:
            return self._owns_display()
        return False                                 # 号上有东西应答，但跟本会话无关

    def _owns_display(self) -> bool:
        """归属校验：这个号上的 X 服务器是不是本会话的。

        判据（满足其一即可）：

        * 我们**自己拉起**的 Xvfb 进程还活着（``self._server_proc``）；
        * 它的 root window 上带着我们打的标记 ``DSH_DISPLAY_SESSION=<sid>``
          （服务被 kill -9、Xvfb 变成孤儿之后，重启的服务靠这条还能认出自己的显示）。

        两条都不成立 → **不算我们的**：宁可换号，也不静默复用到别人的画面上。
        """
        proc = self._server_proc
        if proc is not None and proc.poll() is None:
            return True
        owner = display_owner(self.display)
        if owner is None:                            # 问不到（连不上 / libX11 不可用）
            return False
        return owner == self.sid

    def _alive(self) -> bool:
        """带短缓存的 :meth:`_alive_probe`。

        缓存是为了性能：``ensure()`` 在每个输入事件、每次 ``/state``、``/snapshot``
        上都会被调用，而探测要起一个 ``xdotool`` 进程（+ 一次 X 调用）。
        """
        now = time.time()
        if self._alive_ok and (now - self._alive_at) < self._ALIVE_TTL:
            return True
        ok = self._alive_probe()
        self._alive_at = now
        self._alive_ok = ok
        return ok

    def _startup_error(self, log_path: str) -> str:
        """从启动日志里挑出真正的原因（例如 "Server is already active for display N"）。"""
        try:
            with open(log_path, encoding="utf-8", errors="replace") as fh:
                lines = [ln.strip() for ln in fh.read().splitlines() if ln.strip()]
        except OSError:
            return ""
        keys = ("already active", "fatal", "error", "failed", "cannot", "no such")
        hits = [ln for ln in lines if any(k in ln.lower() for k in keys)]
        chosen = hits[-1] if hits else (lines[-1] if lines else "")
        return chosen[:300]

    def _start_xvfb(self) -> bool:
        # ⚠️ 这里的 _alive() 现在带**归属校验**：别人的显示不会被当成我们的
        #    （旧实现直接 `if self._alive(): return True` = 静默复用别人的画面）。
        if self._alive():
            return True
        log = os.path.join(self.dir, "xvfb.log")
        try:
            logfh = open(log, "ab")
        except OSError:
            logfh = subprocess.DEVNULL
        try:
            proc = subprocess.Popen(
                ["Xvfb", self.display, "-screen", "0", f"{W}x{H}x24", "-nolisten", "tcp"],
                stdout=logfh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                start_new_session=True)
        except Exception as exc:                     # noqa: BLE001
            self.start_error = f"Xvfb 启动失败：{type(exc).__name__}: {exc}"
            print(f"[{self.sid}] {self.start_error}", flush=True)
            return False
        finally:
            if logfh not in (subprocess.DEVNULL, None):
                try:
                    logfh.close()
                except OSError:
                    pass
        self._server_proc = proc
        _register_spawn(proc)
        ok = False
        for _ in range(25):
            time.sleep(0.2)
            if self._alive():
                ok = True
                break
            if proc.poll() is not None:              # 已经死了，不必再等
                break
        if ok:
            if self.relocated_from is None:
                self.start_error = None              # 原地起来了，没有要报的启动故障
            self._alive_ok = True
            self._alive_at = time.time()
            # 打归属标记：这样即使服务被 kill -9、Xvfb 变成孤儿，重启后也认得出。
            self.owner_marked = mark_display_owner(self.display, self.sid)
            if not self.owner_marked:
                print(f"[{self.sid}] ⚠ 归属标记写入失败（{OWNER_PROP}）：跨重启认领会退化为"
                      f"换号（不会复用到别人的显示）", flush=True)
            print(f"[{self.sid}] 显示就绪 {self.display}（{W}x{H}）", flush=True)
            return True
        # 失败：**不要**把死进程留在 _spawned（旧实现就是那样泄漏的）。
        _unregister_spawn(proc)
        self._server_proc = None
        self._alive_ok = False
        _terminate_proc(proc, timeout=1.0)
        detail = self._startup_error(log)
        self.start_error = (f"显示 {self.display} 起不来：{detail}" if detail
                            else f"显示 {self.display} 起不来（看 {log}）")
        if self._display_answers():
            # 号上有人应答却不是我们的：这正是"别的命名空间/别的实例占着同一个号"。
            self.start_error += "；该显示号上有别的 X 服务器在应答（不是本会话的）"
        print(f"[{self.sid}] {self.start_error}", flush=True)
        return False

    def _start_wayland(self) -> bool:
        """备用后端：sway + seatd + seat 组 + headless,libinput（见 dsh-display-reset.sh）。"""
        if self._alive():
            return True
        conf = os.path.join(self.dir, "sway.conf")
        with open(conf, "w", encoding="utf-8") as fh:
            fh.write(f"output HEADLESS-1 resolution {W}x{H}\n")
        cmd = (f"setsid env XDG_RUNTIME_DIR={self.runtime} LIBSEAT_BACKEND=seatd "
               f"WLR_BACKENDS=headless,libinput WLR_RENDERER_ALLOW_SOFTWARE=1 "
               f"LIBGL_ALWAYS_SOFTWARE=1 sway -c {conf} >{self.dir}/sway.log 2>&1 &")
        try:
            subprocess.run(["sudo", "-n", "-u", os.environ.get("USER") or "root", "-g", "seat",
                            "sh", "-c", cmd], capture_output=True, timeout=20)
        except Exception as exc:                     # noqa: BLE001
            print(f"[{self.sid}] 合成器启动失败：{exc}", flush=True)
            return False
        for _ in range(24):
            time.sleep(0.5)
            if self._alive():
                print(f"[{self.sid}] 显示就绪（Wayland）", flush=True)
                return True
        detail = self._startup_error(os.path.join(self.dir, "sway.log"))
        self.start_error = (f"合成器（sway）起不来：{detail}" if detail
                            else f"合成器（sway）起不来（看 {self.dir}/sway.log）")
        print(f"[{self.sid}] {self.start_error}", flush=True)
        return False

    def stop(self, release: bool = False, forget: bool = False) -> None:
        """回收：停输入 worker、杀 /exec 子进程、停显示服务器、释放显示号。

        幂等；任何一步失败都不抛（回收路径绝不能把服务带崩）。
        """
        self._closed = True
        try:
            self.events.put_nowait(None)             # 让 worker 退出
        except queue.Full:
            pass
        for proc, _argv, _at in self.procs_snapshot():
            _terminate_proc(proc, timeout=2.0)
        with self.proc_lock:
            self.procs = []
        proc = self._server_proc
        if proc is not None:
            _unregister_spawn(proc)
            _terminate_proc(proc, timeout=3.0)
            self._server_proc = None
        self.started = False
        self._alive_ok = False
        self._alive_at = 0.0
        if release:
            release_display(self.sid, self.number, forget=forget)

    @property
    def env(self) -> dict:
        """在该会话显示上跑程序时应使用的环境。"""
        if BACKEND in REAL_DESKTOP_BACKENDS:
            return dict(os.environ)                  # 真实桌面：原样用当前环境
        if BACKEND == "wayland":
            return {**os.environ, "XDG_RUNTIME_DIR": self.runtime,
                    "WAYLAND_DISPLAY": "wayland-1"}
        return {**os.environ, "DISPLAY": self.display, "QT_QPA_PLATFORM": "xcb",
                "WAYLAND_DISPLAY": ""}

    # ------------------------------------------------------------------ 输入队列
    def enqueue(self, event: dict) -> int:
        """把事件追加到本会话的串行队列；返回当前排队长度。

        满队列抛 :class:`queue.Full`（调用方转成 429），不阻塞 HTTP 线程。
        """
        with self._worker_lock:
            if self._worker is None and not self._closed:
                self._worker = threading.Thread(target=self._input_worker, daemon=True,
                                                name=f"input-{self.sid}")
                self._worker.start()
        self.events.put_nowait(event)
        return self.events.qsize()

    def _input_worker(self) -> None:
        """单 worker：**严格按到达顺序**串行执行，失败只记录、不让线程死掉。"""
        while True:
            event = self.events.get()
            try:
                if event is None:
                    return
                self.touch()
                try:
                    inject(self, event)
                except Exception as exc:             # noqa: BLE001
                    self.last_input_error = f"{type(exc).__name__}: {exc}"
                    print(f"[{self.sid}] 注入失败：{self.last_input_error}", flush=True)
                else:
                    self.input_count += 1
                    self.last_input_error = None
            finally:
                self.events.task_done()

    def queue_size(self) -> int:
        return self.events.qsize()

    # ------------------------------------------------------------------ 子进程
    def spawn(self, argv: list, cwd=None, timeout_note: str = "") -> subprocess.Popen:
        """在**本会话的显示**上拉起程序（由服务自己 fork，天然在同一命名空间）。"""
        proc = subprocess.Popen(argv, cwd=cwd, env=self.env,
                                stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                start_new_session=True)
        with self.proc_lock:
            self.procs.append((proc, list(argv), time.time()))
        self.touch()
        return proc

    def procs_snapshot(self) -> list:
        with self.proc_lock:
            alive, out = [], []
            for proc, argv, started in self.procs:
                if proc.poll() is None:
                    alive.append((proc, argv, started))
                    out.append({"pid": proc.pid, "argv": list(argv),
                                "startedAt": round(started, 3)})
            self.procs = alive
            return out

    def kill_pid(self, pid: int) -> bool:
        with self.proc_lock:
            for proc, _argv, _at in self.procs:
                if proc.pid == int(pid):
                    break
            else:
                return False
        _terminate_proc(proc)
        return True


_sessions: dict = {}
_sessions_lock = threading.Lock()

#: 本服务拉起的显示服务器进程 —— 服务退出时要**自己收拾干净**。
#: 早先用 start_new_session=True 起 Xvfb 又配了 KillMode=process，结果停服务时只杀主进程，
#: Xvfb 全变成孤儿（systemd 日志里 "remains running after unit stopped"，
#: 既泄漏显示又让 systemd 认为服务实现有缺陷）。现在显式 killpg + atexit + 信号处理。
_spawned: list = []
_spawned_lock = threading.Lock()


def session(sid: str) -> Session:
    """取（或创建）会话 —— **只有 ``/s/<sid>/…`` 接口才该调它**。"""
    with _sessions_lock:
        sess = _sessions.get(sid)
        created = sess is None
        if sess is None:
            sess = _sessions[sid] = Session(sid)
        sess.touch()
    if created:
        threading.Thread(target=_grab_loop, args=(sess,), daemon=True,
                         name=f"grab-{sid}").start()
    return sess


def peek_session(sid: str):
    """只查不建（``/state``、``/health`` 这类探测走这里，绝不产生副作用）。"""
    with _sessions_lock:
        return _sessions.get(sid)


def session_count() -> int:
    with _sessions_lock:
        return len(_sessions)


def drop_session(sid: str, release: bool = True, forget: bool = False) -> bool:
    with _sessions_lock:
        sess = _sessions.pop(sid, None)
    if sess is None:
        return False
    sess.stop(release=release, forget=forget)
    return True


def _reap_idle_once() -> list:
    """回收空闲会话；返回被回收的 sid 列表（供日志/自检观察）。"""
    if IDLE_SECONDS <= 0:
        return []
    reaped = []
    now = time.time()
    with _sessions_lock:
        candidates = [(sid, sess) for sid, sess in _sessions.items()
                      if (now - sess.last_used) > IDLE_SECONDS]
    for sid, sess in candidates:
        if sess.has_live_procs():
            sess.touch()                             # 显示上还有程序在跑 = 还在用
            continue
        if sess.queue_size() > 0:
            continue
        # 回收显示与 Xvfb，但**保留 displays.json 映射**：同一 sid 回来还是同一个号。
        if drop_session(sid, release=True, forget=False):
            reaped.append(sid)
            print(f"[{sid}] 空闲 {int(sess.idle_seconds())}s，已回收（显示 {sess.display}）",
                  flush=True)
    return reaped


def _reaper_loop() -> None:
    while True:
        time.sleep(REAP_INTERVAL)
        try:
            _prune_spawned()
            _reap_idle_once()
        except Exception as exc:                     # noqa: BLE001
            print(f"⚠ 回收线程异常（已忽略）：{type(exc).__name__}: {exc}", flush=True)


def _cleanup_spawned() -> None:
    """退出前把自己拉起的显示服务器与 /exec 子进程一并终止。幂等。"""
    with _sessions_lock:
        sessions = list(_sessions.values())
    for sess in sessions:
        try:
            sess.stop(release=True, forget=False)
        except Exception:                            # noqa: BLE001
            pass
    with _sessions_lock:
        _sessions.clear()
    with _spawned_lock:
        procs = list(_spawned)
        _spawned.clear()
    for proc in procs:
        try:
            if proc.poll() is None:
                _terminate_proc(proc, timeout=1.0)
        except Exception:                            # noqa: BLE001
            pass


def _install_signal_handlers() -> None:
    def handler(_signum, _frame):
        _cleanup_spawned()
        raise SystemExit(0)

    # Windows 补丁（原写法把 SIGHUP 放进元组字面量，求值发生在 try 之外，
    # Windows 没有 signal.SIGHUP -> 服务在绑端口之前就 AttributeError 崩掉）。
    for _name in ("SIGTERM", "SIGINT", "SIGHUP"):
        sig = getattr(signal, _name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, handler)
        except Exception:                            # noqa: BLE001
            pass


# ---------------------------------------------------------------- 输入注入
def run_tool(sess: Session, argv: list, timeout: float = 15.0):
    """跑一个注入命令；非 0 退出抛错（失败要能浮到 /state，不能静默吞掉）。"""
    proc = subprocess.run(argv, env=sess.env, capture_output=True, timeout=timeout)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()[:200]
        raise RuntimeError(f"{argv[0]} 退出码 {proc.returncode}：{err or '（无输出）'}")
    return proc


#: DOM 的标准键名 → xdotool（X keysym 名）。两套名字并不一样，
#: 例如 DOM 叫 Backspace / Enter / ArrowUp，而 X11 叫 BackSpace / Return / Up。
_XDOTOOL_KEY = {
    "Enter": "Return", "Backspace": "BackSpace", "Delete": "Delete",
    "Tab": "Tab", "Escape": "Escape", " ": "space",
    "ArrowUp": "Up", "ArrowDown": "Down", "ArrowLeft": "Left", "ArrowRight": "Right",
    "Home": "Home", "End": "End", "PageUp": "Prior", "PageDown": "Next",
}

#: DOM 鼠标键号（1 左 2 右 3 中）→ xdotool 的按键号（1 左 2 中 3 右）。
_X11_BUTTON = {1: "1", 2: "3", 3: "2"}


def _xdotool_key(key: str) -> str:
    """把页面报上来的键（可能带 ctrl+/super+ 前缀）翻成 xdotool 认的名字。"""
    mods = ""
    base = key
    for prefix in ("ctrl+", "super+", "alt+", "shift+"):
        while base.startswith(prefix):
            mods += prefix
            base = base[len(prefix):]
    return mods + _XDOTOOL_KEY.get(base, base)


def _x11_button(value) -> str:
    try:
        return _X11_BUTTON.get(int(value or 1), "1")
    except (TypeError, ValueError):
        return "1"


def inject(sess: Session, obj: dict) -> None:
    """把一个输入事件注入到该会话自己的显示（由会话的输入 worker 串行调用）。"""
    if not sess.ensure():
        raise RuntimeError(f"显示未就绪（看 {sess.dir}/xvfb.log）")
    if BACKEND == "wayland":
        _inject_wayland(sess, obj)
    elif BACKEND == "win32":
        _inject_win32(sess, obj)
    elif BACKEND == "darwin":
        _inject_darwin(sess, obj)
    else:
        _inject_x11(sess, obj)


def _inject_x11(sess: Session, obj: dict) -> None:
    kind = obj.get("t")
    x, y = _num(obj, "x"), _num(obj, "y")
    if kind in ("click", "down", "up", "move"):
        if x is None or y is None:
            raise ValueError(f"{kind} 缺少坐标 x/y")
        px, py = int(_unit(x) * W), int(_unit(y) * H)
        run_tool(sess, ["xdotool", "mousemove", str(px), str(py)])
        if kind == "move":
            return
        button = _x11_button(obj.get("b"))
        if kind == "click":
            run_tool(sess, ["xdotool", "click", button])
        elif kind == "down":
            run_tool(sess, ["xdotool", "mousedown", button])
        else:
            run_tool(sess, ["xdotool", "mouseup", button])
    elif kind == "wheel":
        ticks = wheel_ticks(_num(obj, "dy"))
        if not ticks:
            return
        if x is not None and y is not None:
            run_tool(sess, ["xdotool", "mousemove",
                            str(int(_unit(x) * W)), str(int(_unit(y) * H))])
        button = x11_wheel_button(ticks)
        for _ in range(abs(ticks)):
            run_tool(sess, ["xdotool", "click", button])
    elif kind == "text":
        text = str(obj.get("s") or "")
        if text:
            # 先等一下：焦点刚落定时立刻注入，开头几个字符会掉（Wayland 那边实测过，
            # X11 同样给一点余量更稳）
            time.sleep(0.08)
            _type_text(sess, text)
    elif kind == "key":
        key = str(obj.get("k") or "")
        if key:
            run_tool(sess, ["xdotool", "key", _xdotool_key(key)])


def _chunks(text: str, size: int):
    for i in range(0, len(text), size):
        yield text[i:i + size]


def _type_text(sess: Session, text: str) -> None:
    """把一段文本送进该会话显示上**有焦点**的那个控件。

    ASCII 直接 ``xdotool type``（长文本切块，避免一次喂太多丢字）；
    **含非 ASCII（中文等）必须走剪贴板 + Ctrl+V**：
    ``xdotool type`` 是靠临时映射 keysym 打字符的，CJK 上不可靠 —— 实测
    "中文显示器" 一个字都进不去（自测第 9 项就是这条）。剪贴板路线对任意 Unicode 都稳。
    """
    if all(ord(ch) < 128 for ch in text):
        for chunk in _chunks(text, 200):
            run_tool(sess, ["xdotool", "type", "--clearmodifiers", "--delay", "12", chunk])
        return
    if shutil.which("xclip") is None:
        raise RuntimeError("缺 xclip，中文等非 ASCII 文本打不进去（安装：xclip）")
    # ⚠️ xclip 必须给 ``-l``（服务若干次选区请求后再退出）：Qt 读剪贴板是**分几次请求**的
    # （先问 TARGETS、再取数据），默认只服务一次就退出 → 数据还没取到就没主了，
    # 表现为"Ctrl+V 什么都没粘上"（这就是我上一版失败的原因）。
    # 又因为它会驻留，不能用 subprocess.run 等它 —— 用 Popen 喂完 stdin 就走。
    try:
        proc = subprocess.Popen(["xclip", "-selection", "clipboard", "-l", "20"],
                                env=sess.env, stdin=subprocess.PIPE,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        proc.stdin.write(text.encode("utf-8"))
        proc.stdin.close()
    except Exception as exc:                         # noqa: BLE001
        raise RuntimeError(f"xclip 异常：{type(exc).__name__}: {exc}")
    time.sleep(0.4)                                  # 等选区真正建立
    run_tool(sess, ["xdotool", "key", "--clearmodifiers", "ctrl+v"])


def _inject_darwin(sess: Session, obj: dict) -> None:
    """macOS 注入：Quartz CGEvent（纯 ctypes）。

    ⚠️ 与 win32 同理：注入的是**真实**鼠标键盘，所以由 DSH_VIEW_INPUT 控制，
    **默认关闭**（只读观看）。这段也**没有在真机上验证过**（作者没有 Mac）。
    """
    if not MAC_INPUT:
        return
    kind = obj.get("t")
    x, y = _num(obj, "x"), _num(obj, "y")
    sw, sh = _mac_screen()
    if kind in ("click", "down", "up", "move") and x is not None and y is not None:
        px, py = int(_unit(x) * sw), int(_unit(y) * sh)
        right = int(obj.get("b") or 1) == 2
        down = _RDOWN if right else _LDOWN
        up = _RUP if right else _LUP
        button = _BTN_RIGHT if right else _BTN_LEFT
        _mac_mouse(px, py, _MOVE)
        if kind == "move":
            return
        if kind in ("click", "down"):
            _mac_mouse(px, py, down, button)
        if kind in ("click", "up"):
            if kind == "click":
                time.sleep(0.02)
            _mac_mouse(px, py, up, button)
    elif kind == "wheel":
        ticks = wheel_ticks(_num(obj, "dy"))
        if not ticks:
            return
        ev = _cg.CGEventCreateScrollWheelEvent(None, _PIXEL_UNITS, 1,
                                               mac_scroll_units(ticks))
        if ev:
            _cg.CGEventPost(_HID, ev)
            _cf.CFRelease(ev)
    elif kind == "text":
        text = str(obj.get("s") or "")
        if text:
            time.sleep(0.1)                      # 给焦点一点时间（同 Linux 侧的经验）
            _mac_key_text(text)
    elif kind == "key":
        key = str(obj.get("k") or "")
        if key:
            _mac_key_text(_MAC_KEY_ALIASES.get(key, ""))


def _inject_win32(sess: Session, obj: dict) -> None:
    """把事件注入**真实桌面**。

    ⚠️ 默认关闭（``DSH_VIEW_INPUT=1`` 才开）：Windows 上没有独立的虚拟显示，
    注入的就是用户本人的鼠标键盘，误开会在用户正在用的桌面上真点下去。
    """
    if not WIN_INPUT:
        return
    kind = obj.get("t")
    x, y = _num(obj, "x"), _num(obj, "y")
    if kind in ("click", "down", "up", "move") and x is not None and y is not None:
        px, py = int(_unit(x) * W), int(_unit(y) * H)
        down, up = {1: (_MEF_LEFTDOWN, _MEF_LEFTUP),
                    2: (_MEF_MIDDLEDOWN, _MEF_MIDDLEUP),
                    3: (_MEF_RIGHTDOWN, _MEF_RIGHTUP)}.get(
                        int(obj.get("b") or 1), (_MEF_LEFTDOWN, _MEF_LEFTUP))
        if kind == "move":
            if _win_mouse(px, py, _MEF_MOVE | _MEF_ABSOLUTE) == 0:
                raise RuntimeError(f"SendInput 失败：{ctypes.get_last_error()}")
            return
        if kind in ("click", "down"):
            if _win_mouse(px, py, down) == 0:
                raise RuntimeError(f"SendInput 失败：{ctypes.get_last_error()}")
        if kind in ("click", "up"):
            if kind == "click":
                time.sleep(0.02)
            if _win_mouse(px, py, up) == 0:
                raise RuntimeError(f"SendInput 失败：{ctypes.get_last_error()}")
    elif kind == "wheel":
        ticks = wheel_ticks(_num(obj, "dy"))
        if not ticks:
            return
        # ⚠️ 这里以前是 ``+WHEEL_DELTA if dy > 0``：Windows 的**正值 = 向上滚**，
        #    与 DOM 相反 → Windows 上滚轮方向是反的。现在统一走 win_wheel_data()。
        data = win_wheel_data(ticks)
        for _ in range(abs(ticks)):
            _send_inputs([_INPUT(_INPUT_MOUSE, _INPUTUNION(
                mi=_MOUSEINPUT(0, 0, data, _MEF_WHEEL, 0, None)))])
    elif kind == "text":
        text = str(obj.get("s") or "")
        if text:
            time.sleep(0.1)                          # 与 X11 那条路同样的余量
            _win_text(text)
    elif kind == "key":
        key = str(obj.get("k") or "")
        if key:
            _win_key(key)


def _inject_wayland(sess: Session, obj: dict) -> None:
    """备用后端：Wayland 下的注入（协议工具优先，ydotool 兜底）。

    ``down`` / ``up`` 需要 ``vptr``（ydotool 没有分离的 press/release），
    缺 vptr 时**明确报错**而不是假装成功。
    """
    kind = obj.get("t")
    x, y = _num(obj, "x"), _num(obj, "y")
    if kind in ("click", "down", "up", "move") and x is not None and y is not None:
        px, py = int(_unit(x) * W), int(_unit(y) * H)
        if os.path.exists(VPTR):
            run_tool(sess, [VPTR, "absolute", str(px), str(py), str(W), str(H)])
        else:
            run_tool(sess, ["ydotool", "mousemove", "--absolute", "-x", str(px), "-y", str(py)])
        if kind == "move":
            return
        btn = {1: 272, 2: 273, 3: 274}.get(int(obj.get("b") or 1), 272)  # evdev BTN_*
        if kind == "click":
            if os.path.exists(VPTR):
                run_tool(sess, [VPTR, "button", str(btn), "press"])
                time.sleep(0.05)
                run_tool(sess, [VPTR, "button", str(btn), "release"])
            else:
                run_tool(sess, ["ydotool", "click", hex(btn)])
        elif os.path.exists(VPTR):
            run_tool(sess, [VPTR, "button", str(btn), "press" if kind == "down" else "release"])
        else:
            raise RuntimeError("wayland 的 down/up 需要 vptr（ydotool 不支持分离的按下/抬起）")
    elif kind == "wheel":
        ticks = wheel_ticks(_num(obj, "dy"))
        if not ticks:
            return
        button = wayland_wheel_button(ticks)
        for _ in range(abs(ticks)):
            run_tool(sess, ["ydotool", "click", button])
    elif kind == "text":
        text = str(obj.get("s") or "")
        if text:
            time.sleep(0.15)
            run_tool(sess, ["wtype", "-s", "30", "--", text])
    elif kind == "key":
        key = str(obj.get("k") or "")
        if key:
            run_tool(sess, ["wtype", "-k", key])


# ---------------------------------------------------------------- 抓帧
def _grab_once(sess: Session) -> bytes:
    if BACKEND == "win32":
        return win_screen().grab()
    if BACKEND == "darwin":
        return _grab_darwin()
    if BACKEND == "wayland":
        cmd = ["grim", "-t", "jpeg", "-q", "80", "-"]
    else:
        # ImageMagick 的 import 直接抓成 JPEG 到 stdout
        cmd = ["import", "-window", "root", "-quality", "80", "JPEG:-"]
    proc = subprocess.run(cmd, env=sess.env, capture_output=True, timeout=15)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError(f"{cmd[0]} 退出码 {proc.returncode}：{err[-1][:160] if err else ''}")
    return proc.stdout


def _grab_loop(sess: Session) -> None:
    """每个会话一个抓帧线程；**没有会话就不会有它**（不会白发抓帧进程）。

    失败**必须可见**：写日志，并把最后一次错误放进 ``sess.frame_error``，
    由 ``/state`` 与独立页面显示出来 —— 旧实现 ``except Exception: pass``，
    画面全黑时用户和日志都无从判断是"没程序"还是"抓不到"。
    """
    fails = 0
    while not sess.closed:
        if not sess.ensure():
            with sess.lock:
                sess.frame_error = "显示服务器没起来"
            time.sleep(1.0)
            continue
        try:
            frame = _grab_once(sess)
            if not frame:
                raise RuntimeError(f"{BACKEND} 抓帧返回空数据")
        except Exception as exc:                     # noqa: BLE001
            fails += 1
            with sess.lock:
                sess.frame_error = f"{type(exc).__name__}: {exc}"
            if fails <= 3 or fails % 20 == 0:
                print(f"[{sess.sid}] 抓帧失败（第 {fails} 次）：{sess.frame_error}"
                      f"（缺 import/grim？）", flush=True)
            time.sleep(min(5.0, max(GRAB_INTERVAL, GRAB_INTERVAL * fails)))
            continue
        fails = 0
        with sess.lock:
            sess.latest = frame
            sess.frame_count += 1
            sess.last_frame_at = time.time()
            sess.frame_error = None
        time.sleep(GRAB_INTERVAL)


# ---------------------------------------------------------------- 状态
def cursor_pos(sess: Session):
    """指针位置（**归一化 0..1**）；拿不到返回 None。"""
    if BACKEND == "win32":
        x, y = _win_cursor()
        if x < 0 or y < 0:
            return None
        return {"x": round(min(1.0, x / max(1, W)), 4), "y": round(min(1.0, y / max(1, H)), 4)}
    if BACKEND in ("darwin", "wayland"):
        # darwin 要 CGEventGetLocation + 真机验证；wayland 拿不到全局指针。宁可不报。
        return None
    try:
        proc = subprocess.run(["xdotool", "getmouselocation", "--shell"],
                              env=sess.env, capture_output=True, timeout=5)
        if proc.returncode != 0:
            return None
        pos = {}
        for line in proc.stdout.decode("utf-8", "replace").splitlines():
            key, _, value = line.partition("=")
            if key in ("X", "Y"):
                pos[key] = float(value.strip())
        if "X" not in pos or "Y" not in pos:
            return None
        return {"x": round(min(1.0, max(0.0, pos["X"] / max(1, W))), 4),
                "y": round(min(1.0, max(0.0, pos["Y"] / max(1, H))), 4)}
    except Exception:                                # noqa: BLE001
        return None


def window_count(sess: Session) -> int:
    """该显示上的可见窗口数（-1 = 查不到）。用于区分"空闲"与"坏了"。"""
    if BACKEND == "win32":
        return _win_windows()
    if BACKEND == "wayland":
        return -1
    try:
        proc = subprocess.run(["xdotool", "search", "--name", "."],
                              env=sess.env, capture_output=True, timeout=6)
        if proc.returncode not in (0, 1):            # 1 = 没有匹配（= 空闲）
            return -1
        return len([ln for ln in proc.stdout.decode().splitlines() if ln.strip()])
    except Exception:                                # noqa: BLE001
        return -1


def _tooltip(started: bool, count: int, idle: bool, frame_error, start_error=None) -> str:
    if real_desktop():
        if not input_enabled():
            return "真实桌面 · 只读观看（未开启输入注入）"
        return "真实桌面 · 点击/按键会落在本机真实桌面上"
    if not started:
        if start_error:
            return f"显示器起不来：{start_error}"
        return "显示器尚未启动（调用 /display 或 /snapshot 会拉起）"
    if frame_error:
        return f"抓不到画面：{frame_error}"
    if idle:
        return "显示器空闲 —— 这台显示上还没有程序在运行"
    if count < 0:
        return "窗口数未知（xdotool 不可用？）"
    return f"运行中（{count} 个窗口）"


def _session_state(sid: str, sess) -> dict:
    """``/state`` 的响应体。``sess`` 为 None 时**只读**，绝不创建会话/启动 Xvfb。"""
    number = peek_display(sid)
    if number is None:
        number = candidate_display(sid)
        claimed = False
    else:
        claimed = True
    display = "真实桌面" if real_desktop() else f":{number}"
    base = {
        "session": sid,
        "display": display,
        "backend": BACKEND,
        "size": f"{W}x{H}",
        "missing": missing_tools(),
        "input": input_enabled(),
        "realDesktop": real_desktop(),
        "idleMinutes": IDLE_MINUTES,
    }
    if sess is None:
        base.update({
            "windows": -1,
            "idle": False,
            "cursor": None,
            "started": False,
            "claimed": claimed,
            "tooltip": _tooltip(False, -1, False, None),
            "frame": {"ok": False, "error": None, "count": 0, "lastAt": 0},
            "frameError": None,
            "ownDisplay": False,
            "ownerMarked": None,
            "startError": None,
        })
        return base
    started = sess.ensure()
    sess.touch()                                     # 轮询 /state 也算"在用"
    count = window_count(sess) if started else -1
    with sess.lock:
        frame_ok = bool(sess.latest) and sess.frame_error is None
        frame_error = sess.frame_error
        frame_count = sess.frame_count
        last_frame_at = sess.last_frame_at
    base.update({
        "windows": count,
        "idle": count == 0,
        "cursor": cursor_pos(sess) if started else None,
        "started": bool(started),
        "claimed": claimed,
        "tooltip": _tooltip(bool(started), count, count == 0, frame_error,
                            sess.start_error),
        "frame": {"ok": frame_ok, "error": frame_error,
                  "count": frame_count, "lastAt": round(last_frame_at, 3)},
        "frameError": frame_error,
        # 归属/启动诊断：显示号是不是真被别的 X 服务器占了、我们有没有打上标记，
        # 全在这里能看到（旧实现只有一句"Xvfb 没起来"）。
        "ownDisplay": bool(sess._owns_display()) if started else False,
        "ownerMarked": sess.owner_marked,
        "startError": sess.start_error,
        "relocatedFrom": sess.relocated_from,
        "inputCount": sess.input_count,
        "inputError": sess.last_input_error,
        "queue": sess.queue_size(),
        "procs": len(sess.procs_snapshot()),
    })
    return base


def display_payload(sid: str, sess) -> dict:
    return {"session": sid, "display": sess.display, "backend": BACKEND,
            "size": f"{W}x{H}", "input": input_enabled(),
            "realDesktop": real_desktop()}


def health_payload() -> dict:
    """``/health``：**探测专用**（宿主每次探测都打它），必须极快、无副作用。"""
    return {
        "ok": True,
        "service": SERVICE_NAME,
        "version": VERSION,
        "backend": BACKEND,
        "size": f"{W}x{H}",
        "input": input_enabled(),
        "realDesktop": real_desktop(),
        "port": PORT,
        "pid": os.getpid(),
        "sessions": session_count(),
        "missing": missing_tools(),
        "idleMinutes": IDLE_MINUTES,
        "home": HOME_DIR,
    }


# ---------------------------------------------------------------- 页面
def missing_banner(missing) -> str:
    if not missing:
        return ""
    items = "；".join(f"{html_escape(m.get('tool'))}（{html_escape(m.get('why'))}；"
                      f"安装：{html_escape(m.get('package'))}）" for m in missing)
    return ('<div style="padding:6px 8px;background:#3a1d1d;color:#ffd9d9">'
            f"⚠ 缺少依赖，部分功能不可用：{items}</div>")


#: 独立页面的模板源码：**单花括号**（JS/CSS 原样），占位符写成 ``__X__``。
#: 渲染前由 :func:`_make_page_template` 把花括号转义成 ``{{ }}``（这样 ``PAGE.format``
#: 仍然可用），再把 ``__X__`` 换成 ``{x}``。
_PAGE_SRC = r"""<!doctype html><meta charset=utf-8><title>DSH 显示器 · __SID__</title>
<body style="margin:0;background:#0b0b0c;color:#ddd;font:12px system-ui">
<div style="padding:4px 8px;opacity:.75">
  会话 __SID__ 的显示器 · __DISP__ · __W__x__H__ · __NOTE__
</div>
__MISSING__
<div id="cursor" style="padding:2px 8px;opacity:.6;font-variant-numeric:tabular-nums">光标 —</div>
<div id="warn" style="padding:0 8px 2px;color:#ffb3b3;opacity:.9"></div>
<canvas id="screen" style="width:100%;display:block;cursor:crosshair"></canvas>
<div id="offline" style="display:none;padding:10px;opacity:.7">正在连接显示器…</div>
<div id="idle" style="display:none;position:fixed;left:0;right:0;bottom:12px;text-align:center;
     font-size:12px;opacity:.45;pointer-events:none">
  显示器空闲 —— 这台显示上还没有程序在运行（让 AI 帮你拉起来即可）
</div>
__EXTRA__
<textarea id="sink" aria-label="keyboard sink"
  style="position:fixed;left:-1000px;top:0;width:10px;height:10px;opacity:0"></textarea>
<script>
(function () {
  var BASE = __BASE__;
  var K = __K__;            // 访问令牌；通常是空串（宿主代理不带它，靠同源 Cookie）
  function withToken(url) {
    return K ? url + (url.indexOf('?') < 0 ? '?' : '&') + 'k=' + encodeURIComponent(K) : url;
  }
  var canvas = document.getElementById('screen');
  var ctx = canvas.getContext('2d');
  var offline = document.getElementById('offline');
  var idleBox = document.getElementById('idle');
  var cursorLine = document.getElementById('cursor');
  var warnLine = document.getElementById('warn');
  var sink = document.getElementById('sink');
  var dragging = false, lastHover = 0;
  function norm(ev) {
    var r = canvas.getBoundingClientRect();
    var x = (ev.clientX - r.left) / (r.width || 1);
    var y = (ev.clientY - r.top) / (r.height || 1);
    return { x: Math.min(1, Math.max(0, x)), y: Math.min(1, Math.max(0, y)) };
  }
  function send(o) {
    try {
      fetch(withToken(BASE + '/input'), { method: 'POST', credentials: 'same-origin',
        cache: 'no-store', body: JSON.stringify(o) });
    } catch (e) {}
  }
  // 画面自愈：**主动拉单帧画到 canvas**，而不是把 MJPEG 塞进 <img>。
  // 原因：<img> 上的 MJPEG 一旦断开（例如 viewer 重启）不会重连，画面就永远黑着
  // —— 用户实测到的"一片漆黑"有一部分就是这个。canvas 方案断了会自动续上。
  (function pull() {
    var im = new Image();
    im.onload = function () {
      offline.style.display = 'none';
      if (canvas.width !== im.naturalWidth || canvas.height !== im.naturalHeight) {
        canvas.width = im.naturalWidth; canvas.height = im.naturalHeight;
      }
      ctx.drawImage(im, 0, 0);
      setTimeout(pull, 130);            // 约 7~8 帧/秒，看操作足够
    };
    im.onerror = function () {
      offline.style.display = 'block';
      setTimeout(pull, 1000);           // 失败就重试
    };
    im.src = withToken(BASE + '/snapshot?t=' + Date.now());
  })();
  // 状态轮询：空闲提示 + **光标位置** + 抓帧/依赖/只读告警。
  (function pollState() {
    fetch(withToken(BASE + '/state?t=' + Date.now()),
          { cache: 'no-store', credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        d = d || {};
        idleBox.style.display = (d.idle === true) ? 'block' : 'none';
        var c = d.cursor;
        var where = d.display ? (' · ' + d.display) : '';
        if (c && typeof c.x === 'number') {
          cursorLine.textContent = '光标 ' + (c.x * 100).toFixed(1) + '% , '
            + (c.y * 100).toFixed(1) + '%' + where;
        } else {
          cursorLine.textContent = '光标 —（' + (d.backend || '?') + ' 后端拿不到指针位置）' + where;
        }
        var msgs = [];
        if (d.frameError) msgs.push('抓不到画面：' + d.frameError);
        if (d.realDesktop && d.input === false) msgs.push('这是本机真实桌面，当前只读（未开启输入注入）');
        if (d.missing && d.missing.length) {
          msgs.push('缺少依赖：' + d.missing.map(function (m) { return m.tool; }).join('、'));
        }
        warnLine.textContent = msgs.join(' · ');
        document.title = 'DSH 显示器 · ' + (d.session || '');
      })
      .catch(function () {})
      .then(function () { setTimeout(pollState, 2000); });
  })();
  // 指针：按下/抬起分开送（拖拽、长按），拖动中的移动也送。
  canvas.addEventListener('mousedown', function (ev) {
    ev.preventDefault();
    try { sink.focus(); } catch (e) {}
    dragging = true;
    var p = norm(ev); p.t = 'down'; p.b = ev.button + 1; send(p);
  });
  window.addEventListener('mousemove', function (ev) {
    var p = norm(ev);
    if (dragging) { p.t = 'move'; send(p); return; }
    var now = Date.now();
    if (now - lastHover < 60) return;   // 悬停的移动节流，别把队列灌满
    lastHover = now; p.t = 'move'; send(p);
  });
  window.addEventListener('mouseup', function (ev) {
    if (!dragging) return;
    dragging = false;
    var p = norm(ev); p.t = 'up'; p.b = ev.button + 1; send(p);
  });
  canvas.addEventListener('contextmenu', function (ev) {
    ev.preventDefault(); var p = norm(ev); p.t = 'click'; p.b = 2; send(p);
  });
  canvas.addEventListener('wheel', function (ev) {
    ev.preventDefault();
    var p = norm(ev);
    // dy 直接送 DOM 的 deltaY：**dy>0 = 向下滚**（方向换算在服务端按后端做）
    send({ t: 'wheel', dy: ev.deltaY, x: p.x, y: p.y });
  }, { passive: false });
  // 输入法（中文）合成：**合成期间绝不能发送**。
  //
  // 踩过的坑：打拼音时隐藏输入框会不断触发 input 事件，值是**未上屏的拼音**
  // （如 "xianshiqi"）→ 早先直接发出去，等上屏后又发一次中文 → 远端同时收到
  // 拼音和中文（用户实测："我只想输入中文的显示器，结果字母也输进去了"）。
  // 正确做法：compositionstart 到 compositionend 之间一律不发，上屏时只发最终文本。
  var composing = false;
  sink.addEventListener('compositionstart', function () { composing = true; });
  sink.addEventListener('compositionend', function () {
    composing = false;
    if (sink.value) { send({ t: 'text', s: sink.value }); sink.value = ''; }
  });
  // 键盘：普通字符走 input（含输入法合成结果），控制键与组合键走 keydown。
  //
  // ⚠️ 键名必须用 **DOM 的标准名**：退格是 'Backspace'（小写 s）、回车是 'Enter'。
  // 早先写成 'BackSpace' / 'Return'（X11 的 keysym 名）→ indexOf 永远不命中，
  // 于是退格和回车完全没反应（用户实测："打错了删不掉"）。X11 名字的换算在服务端。
  var NAMED = { Enter: 1, Backspace: 1, Delete: 1, Tab: 1, Escape: 1,
                ArrowUp: 1, ArrowDown: 1, ArrowLeft: 1, ArrowRight: 1,
                Home: 1, End: 1, PageUp: 1, PageDown: 1 };
  sink.addEventListener('keydown', function (ev) {
    // 合成中的按键交给输入法处理（例如回车用于选词），不要转发
    if (ev.isComposing || composing) { return; }
    var mod = '';
    if (ev.ctrlKey) { mod += 'ctrl+'; }
    if (ev.altKey) { mod += 'alt+'; }
    if (ev.metaKey) { mod += 'super+'; }
    // 组合键（Ctrl+C/V/A…）也走 key 通道；单个可打印字符仍交给 input，避免重复输入
    if (NAMED[ev.key] || (mod && ev.key.length === 1)) {
      ev.preventDefault();
      send({ t: 'key', k: mod + ev.key });
    }
  });
  sink.addEventListener('input', function (ev) {
    // 合成中（含 isComposing 标记）一律不发，避免把拼音当正文送出去
    if (composing || (ev && ev.isComposing)) { return; }
    if (sink.value) { send({ t: 'text', s: sink.value }); sink.value = ''; }
  });
})();
</script>
"""


def _make_page_template(src: str) -> str:
    """把 ``__X__`` 风格的源码变成 ``str.format`` 模板（花括号自动转义）。"""
    out = src.replace("{", "{{").replace("}", "}}")
    for token in ("SID", "DISP", "W", "H", "NOTE", "MISSING", "EXTRA", "BASE", "K"):
        out = out.replace("__" + token + "__", "{" + token.lower() + "}")
    return out


class _PageTemplate(str):
    """``PAGE`` 模板：缺省占位符有默认值，``PAGE.format(base=…, sid=…, k=…)`` 老调用不会 KeyError。"""

    _DEFAULTS = {"sid": "", "disp": "", "w": 0, "h": 0, "note": "", "missing": "",
                 "extra": "", "base": '""', "k": '""'}

    def format(self, **kwargs) -> str:               # noqa: A003 - 有意覆盖 str.format
        merged = dict(self._DEFAULTS)
        merged.update(kwargs)
        return str.format(self, **merged)


PAGE = _PageTemplate(_make_page_template(_PAGE_SRC))


def page_html(sid: str, disp: str = "", note: str = "", base=None, token: str = "",
              missing=None, extra: str = "", w: int = None, h: int = None) -> str:
    """渲染独立页面：**HTML 与 JS 双重转义**（旧实现直接把 sid/k 插进去 = XSS）。"""
    return PAGE.format(
        sid=html_escape(sid),
        disp=html_escape(disp),
        w=int(W if w is None else w),
        h=int(H if h is None else h),
        note=html_escape(note),
        missing=missing_banner(missing if missing is not None else []),
        extra=extra,
        base=js_str(base if base is not None else f"/s/{sid}"),
        k=js_str(token),
    )


def page_labels(sid: str, display: str):
    """页面抬头的 (disp, note)：**按真实后端写**（darwin 也是真实桌面）。"""
    if real_desktop():
        disp = "真实桌面（所有会话共用）"
        if input_enabled():
            note = "点击画面即可操作（会注入到本机真实的鼠标键盘）"
        else:
            note = "只读观看 —— 未开启输入注入（设 DSH_VIEW_INPUT=1 再重启可开）"
        if BACKEND == "darwin":
            note += " · darwin 后端尚未真机验证"
    else:
        disp = f"独立显示 {display}"
        note = "点击画面即可操作（鼠标/键盘都会注入回去）"
        if BACKEND == "wayland":
            note += " · wayland 后端实验性"
    return disp, note


def index_html() -> str:
    """会话索引页（人用）。**不创建会话**。"""
    with _sessions_lock:
        items = sorted(_sessions.items())
    rows = "".join(
        f'<li><a href="/s/{html_escape(sid)}/">{html_escape(sid)}</a> · '
        f'{html_escape(str(sess.display))} '
        f'{"（就绪）" if sess.started else "（未启动）"} · '
        f'空闲 {int(sess.idle_seconds())}s</li>'
        for sid, sess in items)
    if BACKEND == "darwin":
        title = "DSH 显示器（macOS · 真实桌面）"
        howto = ("<p style='opacity:.65'>本机是 macOS：没有可多开的 headless 显示，"
                 "darwin 后端抓的是<strong>真实桌面</strong> —— 所有会话看到的是"
                 "同一块屏。<br>"
                 "输入注入：" + ("<strong>已开启</strong>（DSH_VIEW_INPUT=1）"
                                "，页面上的点击/按键会落在真实桌面上。"
                                if MAC_INPUT else "关闭（只读观看；要开设 DSH_VIEW_INPUT=1）")
                 + "<br><em>darwin 后端尚未在真机验证过，欢迎回报结果。</em></p>")
    elif BACKEND == "win32":
        title = "DSH 显示器（Windows · 真实桌面）"
        howto = ("<p style='opacity:.65'>本机是 Windows：没有 Xvfb 这类可多开的 "
                 "headless 显示，win32 后端抓的是<strong>真实桌面</strong> —— "
                 "所有会话看到的是同一块屏。<br>"
                 "输入注入：" + ("<strong>已开启</strong>（DSH_VIEW_INPUT=1）"
                                "，页面上的点击/按键会落在真实桌面上。"
                                if WIN_INPUT else
                                "默认<strong>关闭</strong>（只读观看），"
                                "要开就带 <code>DSH_VIEW_INPUT=1</code> 重启本服务。")
                 + "</p>")
    elif BACKEND == "wayland":
        title = "DSH 测试显示器（wayland · 实验性）"
        howto = ("<p style='opacity:.65'>wayland 后端需要 sway + seatd + <code>seat</code> 组"
                 "+ vptr/ydotool 才能注入，属实验性后端。<br>"
                 "在该会话显示上跑程序：<code>POST /s/&lt;sessionId&gt;/exec</code></p>")
    else:
        title = "DSH 测试显示器（每会话独立）"
        howto = ("<p style='opacity:.65'>每个 harness 会话有自己的显示："
                 "<code>/s/&lt;sessionId&gt;/</code> —— 互不可见、互不污染。<br>"
                 "在该会话显示上跑程序：推荐 "
                 "<code>POST /s/&lt;sessionId&gt;/exec</code>（能拿输出/退出码、能列进程，"
                 "不依赖客户端沙箱的网络隔离）；"
                 "也可以先取显示号再 <code>DISPLAY=:&lt;号&gt; 程序</code>。</p>")
    missing = missing_tools()
    return ("<!doctype html><meta charset=utf-8><title>DSH 显示器</title>"
            "<body style='background:#0b0b0c;color:#ddd;font:13px system-ui;padding:16px'>"
            f"<h3>{html_escape(title)}</h3>"
            f"{missing_banner(missing)}"
            f"<ul>{rows or '<li>（暂无会话）</li>'}</ul>"
            f"<p style='opacity:.5'>服务 {html_escape(SERVICE_NAME)} {html_escape(VERSION)} · "
            f"端口 {PORT} · pid {os.getpid()} · 后端 {html_escape(BACKEND)} · "
            f"{W}x{H} · 会话空闲回收 "
            f"{'关闭' if IDLE_MINUTES <= 0 else str(int(IDLE_MINUTES)) + ' 分钟'}</p>"
            f"{howto}")


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"                    # 旧实现是 HTTP/1.0：每请求一条连接
    server_version = f"{SERVICE_NAME}/{VERSION}"
    sys_version = ""
    timeout = 60

    # ------------------------------------------------------------ 基础设施
    def log_message(self, *args) -> None:            # 静音：日志留给真正的错误
        pass

    def log_error(self, fmt, *args) -> None:
        print(f"[http] {self.address_string()} {fmt % args}", flush=True)

    def handle_one_request(self) -> None:
        """把 handler 里的任何异常挡住 —— BaseHTTPRequestHandler 默认会**打整条栈**。"""
        try:
            BaseHTTPRequestHandler.handle_one_request(self)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as exc:                     # noqa: BLE001
            self.close_connection = True
            print(f"[http] 请求处理异常（已隔离，服务继续）：{type(exc).__name__}: {exc}",
                  flush=True)

    def _head(self, code: int, ctype: str, length=None, extra=()) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        # ⚠️ 必须禁用缓存：页面里写着"本会话的显示号"，而显示号/服务状态会变。
        # 早先没设这些头，Chromium 缓存了旧页面 —— 用户刷新后仍看到旧显示号
        # （页头写着 :233、而实际已是 :265），旧显示又已被清理 → 满屏漆黑。
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        if self._cookie_from_query and TOKEN:
            # 带 ?k= 访问过一次之后就把令牌记进 Cookie：独立页面不必再依赖链接里的令牌
            # （链接里的令牌仍完全兼容）。
            self.send_header("Set-Cookie",
                             f"{COOKIE_NAME}={TOKEN}; Path=/; SameSite=Strict; HttpOnly")
        for name, value in extra:
            self.send_header(name, value)
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.end_headers()

    def _write(self, body: bytes) -> bool:
        try:
            self.wfile.write(body)
            return True
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.close_connection = True             # 客户端走了：安静收场，不打栈
            return False

    def _send(self, code: int, body: bytes, ctype: str, extra=()) -> bool:
        if code == 204 or not body:
            self._head(code, ctype, length=0, extra=extra)
            return True
        self._head(code, ctype, length=len(body), extra=extra)
        return self._write(body)

    def _json(self, code: int, obj) -> bool:
        return self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                          "application/json; charset=utf-8")

    def _error(self, code: int, message: str, hint=None) -> bool:
        body = {"ok": False, "error": message}
        if hint:
            body["hint"] = hint
        return self._json(code, body)

    def _deny(self) -> bool:
        return self._error(
            403, "missing or bad token",
            "本机其它用户访问不了本服务；请用 DSH 的「显示器」面板，"
            f"或带上 ?k=<{os.path.join(HOME_DIR, 'token')} 的内容>")

    def _auth(self) -> bool:
        """校验令牌；成功时记下"令牌来自查询串"，用于回种 Cookie。"""
        self._cookie_from_query = bool(query_token(self.path))
        if token_ok(self.path, self.headers):
            return True
        self._deny()
        return False

    @staticmethod
    def _split(path: str):
        """把 ``/s/<sid>/<rest>`` 拆成 (sid, rest)。

        ⚠️ **必须先剥掉查询串**：页面为了防缓存会带 ``?t=<时间戳>`` / ``?v=…``，
        早先拿整条 path（含 ?…）去比对 → 所有带参数的请求统统 404，
        表现为"页面一直在连接显示器…"（实测：连新开的浏览器里也是黑的）。
        sid 先做百分号解码再校验（``%2e%2e`` 这类躲不过白名单）。
        """
        raw = path.split("?", 1)[0]
        parts = [p for p in raw.split("/") if p]
        if len(parts) >= 2 and parts[0] == "s":
            return unquote(parts[1]), "/" + "/".join(parts[2:])
        return None, raw

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8", "replace"))

    # ------------------------------------------------------------ GET
    def do_GET(self) -> None:                        # noqa: N802 - BaseHTTPRequestHandler
        if not self._auth():
            return
        sid, rest = self._split(self.path)
        if sid is None:
            if rest in ("/health", "/health/"):
                self._json(200, health_payload())
            elif rest in ("", "/"):
                self._send(200, index_html().encode("utf-8"), "text/html; charset=utf-8")
            elif rest in ("/state", "/state/"):
                # 兼容旧版宿主/脚本的探测：**无副作用**，不建会话、不拉 Xvfb。
                self._json(200, {"ok": True, "service": SERVICE_NAME, "version": VERSION,
                                 "backend": BACKEND, "size": f"{W}x{H}",
                                 "sessions": session_count(), "port": PORT,
                                 "pid": os.getpid(), "missing": missing_tools(),
                                 "input": input_enabled(), "realDesktop": real_desktop()})
            else:
                self._error(404, f"未知路径 {rest or '/'}",
                            "可用：/health、/、/s/<sessionId>/{snapshot,stream,state,display,procs}")
            return

        if not valid_sid(sid):
            self._error(400, "非法会话 id",
                        "只允许 ^[A-Za-z0-9._-]{1,64}$（会用来拼文件路径，必须严格校验）")
            return

        rest = rest.rstrip("/") or "/"
        if rest == "/":
            sess = session(sid)
            disp, note = page_labels(sid, sess.display)
            extra = ""
            if real_desktop():
                extra = ("<div style='padding:0 8px 4px;color:#ffcc66'>"
                         "⚠ 这是本机真实桌面，不是虚拟显示</div>")
                if not input_enabled():
                    extra += ("<div style='padding:0 8px 4px;color:#ffcc66'>"
                              "只读观看：未开启输入注入（DSH_VIEW_INPUT=1 可开）</div>")
            html = page_html(sid, disp=disp, note=note, base=f"/s/{sid}",
                             token=query_token(self.path), missing=missing_tools(),
                             extra=extra)
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            return
        if rest == "/snapshot":
            sess = session(sid)
            sess.ensure()
            with sess.lock:
                frame = sess.latest
            if not frame:
                # ⚠️ **不阻塞**：旧实现最多等 5 秒（20×0.25s），而页面 130ms 拉一次，
                #    会把宿主侧连接堆起来。没有帧就立刻 503，调用方自己重试。
                self._error(503, "还没有帧",
                            "抓帧线程刚开始工作（或抓帧失败，看 /state 的 frameError）")
                return
            self._send(200, frame, "image/jpeg")
            return
        if rest == "/stream":
            self._stream(sid)
            return
        if rest == "/state":
            self._json(200, _session_state(sid, peek_session(sid)))
            return
        if rest == "/display":
            sess = session(sid)
            if not sess.ensure():
                self._error(502, "显示未就绪", f"看 {sess.dir}/xvfb.log")
                return
            self._json(200, display_payload(sid, sess))
            return
        if rest == "/procs":
            sess = peek_session(sid)
            self._json(200, {"session": sid,
                             "procs": sess.procs_snapshot() if sess else []})
            return
        self._error(404, f"未知路径 /s/{sid}{rest}")

    def _stream(self, sid: str) -> None:
        """MJPEG 流。

        客户端断开时旧实现会抛 ``BrokenPipeError`` 刷栈、线程永不退出；
        这里每次写前检查连接、写失败立刻收场。
        """
        sess = session(sid)
        if not sess.ensure():
            self._error(502, "显示未就绪", f"看 {sess.dir}/xvfb.log")
            return
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.end_headers()
        try:
            while not sess.closed:
                sess.touch()
                with sess.lock:
                    frame = sess.latest
                if frame:
                    chunk = (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                             + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n")
                    if not self._write(chunk):
                        return
                    try:
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        self.close_connection = True
                        return
                else:
                    try:
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        self.close_connection = True
                        return
                if self._peer_gone():
                    self.close_connection = True
                    return
                time.sleep(0.2)
        finally:
            self.close_connection = True

    def _peer_gone(self) -> bool:
        """对端是否已经关掉连接（保持连接时不阻塞）。"""
        try:
            ready, _w, _x = select.select([self.connection], [], [], 0)
            if not ready:
                return False
            return self.connection.recv(1, socket.MSG_PEEK) == b""
        except (OSError, ValueError):
            return True

    # ------------------------------------------------------------ POST / DELETE
    def do_POST(self) -> None:                       # noqa: N802
        if not self._auth():
            return
        sid, rest = self._split(self.path)
        if sid is None or not valid_sid(sid):
            self._error(400 if sid is not None else 404, "非法会话 id",
                        "只允许 ^[A-Za-z0-9._-]{1,64}$")
            return
        rest = rest.rstrip("/") or "/"
        if rest == "/input":
            self._post_input(sid)
        elif rest == "/exec":
            self._post_exec(sid)
        elif rest == "/kill":
            self._post_kill(sid)
        elif rest == "/close":
            self._post_close(sid)
        else:
            self._error(404, f"未知路径 /s/{sid}{rest}")

    def do_DELETE(self) -> None:                     # noqa: N802
        if not self._auth():
            return
        sid, rest = self._split(self.path)
        if sid is None or not valid_sid(sid):
            self._error(400 if sid is not None else 404, "非法会话 id")
            return
        if rest.rstrip("/") not in ("", "/", "/close"):
            self._error(404, f"未知路径 /s/{sid}{rest}")
            return
        self._post_close(sid)

    def _post_input(self, sid: str) -> None:
        try:
            payload = self._read_json()
        except ValueError as exc:
            self._error(400, f"body 不是合法 JSON：{exc}")
            return
        problem = validate_input(payload)
        if problem:
            self._error(400, problem)
            return
        sess = session(sid)
        try:
            queued = sess.enqueue(payload)
        except queue.Full:
            self._error(429, "输入队列已满（服务跟不上）", "稍后重试，或减少事件频率")
            return
        # 事件是**异步串行执行**的，这里只能保证"已按到达顺序入队"。
        # 真正的失败会记录在 sess.last_input_error 并由 /state 暴露（旧实现静默丢）。
        self._json(200, {"ok": True, "queued": queued})

    def _post_exec(self, sid: str) -> None:
        try:
            payload = self._read_json()
        except ValueError as exc:
            self._error(400, f"body 不是合法 JSON：{exc}")
            return
        if not isinstance(payload, dict):
            self._error(400, "body 必须是 JSON 对象")
            return
        argv = payload.get("argv")
        if (not isinstance(argv, list) or not argv
                or any(not isinstance(a, str) or a == "" for a in argv)):
            self._error(400, "argv 必须是非空字符串数组",
                        '例如 {"argv":["xterm","-e","bash"],"cwd":"/tmp","wait":false}')
            return
        cwd = payload.get("cwd") or None
        if cwd is not None:
            if not isinstance(cwd, str) or not os.path.isdir(cwd):
                self._error(400, f"cwd 不存在或不是目录：{cwd!r}")
                return
        wait = bool(payload.get("wait"))
        try:
            timeout = float(payload.get("timeout") or EXEC_WAIT_TIMEOUT)
        except (TypeError, ValueError):
            timeout = EXEC_WAIT_TIMEOUT
        timeout = min(EXEC_WAIT_MAX, max(1.0, timeout))

        sess = session(sid)
        if not sess.ensure():
            self._error(502, "显示未就绪", f"看 {sess.dir}/xvfb.log")
            return
        try:
            # 由**本服务**拉起：环境里的 DISPLAY / QT_QPA_PLATFORM / WAYLAND_DISPLAY
            # 都是我们显式设好的（见 Session.env），所以无论客户端在什么命名空间、
            # 走 abstract socket 还是文件 socket，程序都落在**该会话自己的显示**上；
            # 顺带还能拿退出码/输出、列进程、杀进程组（CONTRACT §4.1）。
            proc = sess.spawn(argv, cwd=cwd)
        except FileNotFoundError:
            self._error(400, f"找不到可执行文件：{argv[0]}")
            return
        except Exception as exc:                     # noqa: BLE001
            self._error(500, f"拉起失败：{type(exc).__name__}: {exc}")
            return
        if not wait:
            self._json(200, {"ok": True, "pid": proc.pid, "display": sess.display})
            return
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _terminate_proc(proc, timeout=2.0)
            try:
                out, err = proc.communicate(timeout=2)
            except Exception:                        # noqa: BLE001
                out, err = b"", b""
            self._json(200, {"ok": False, "error": f"超时（{timeout:g}s）已终止",
                             "code": None, "display": sess.display,
                             "stdout": _decode(out), "stderr": _decode(err),
                             "pid": proc.pid})
            return
        self._json(200, {"ok": True, "code": proc.returncode,
                         "stdout": _decode(out), "stderr": _decode(err),
                         "pid": proc.pid, "display": sess.display})

    def _post_kill(self, sid: str) -> None:
        try:
            payload = self._read_json()
        except ValueError as exc:
            self._error(400, f"body 不是合法 JSON：{exc}")
            return
        pid = payload.get("pid") if isinstance(payload, dict) else None
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            self._error(400, "需要整数 pid")
            return
        sess = peek_session(sid)
        if sess is None or not sess.kill_pid(pid):
            self._error(404, f"pid {pid} 不属于会话 {sid}（看 /procs）")
            return
        self._json(200, {"ok": True, "pid": pid})

    def _post_close(self, sid: str) -> None:
        """回收会话：停 Xvfb、杀 /exec 子进程、释放显示号并删掉持久映射。"""
        removed = drop_session(sid, release=True, forget=True)
        self._json(200, {"ok": True, "session": sid, "removed": removed})


def _decode(raw: bytes, limit: int = 200000) -> str:
    text = (raw or b"").decode("utf-8", "replace")
    if len(text) > limit:
        return text[:limit] + f"\n…（截断，共 {len(text)} 字符）"
    return text


def validate_input(payload) -> str:
    """输入事件的**同步**校验（异步执行的那部分错误由 /state 暴露）。

    返回错误说明，合法则返回空串。坐标一律 0..1 归一化（相对画面宽高）。
    """
    if not isinstance(payload, dict):
        return "body 必须是 JSON 对象"
    kind = payload.get("t")
    if kind not in INPUT_TYPES:
        return f"未知事件类型 t={kind!r}（支持：{'/'.join(INPUT_TYPES)}）"
    if kind in ("click", "down", "up", "move"):
        for axis in ("x", "y"):
            if _num(payload, axis) is None:
                return f"{kind} 需要 {axis}（0..1 归一化坐标）"
    elif kind == "wheel":
        if _num(payload, "dy") is None:
            return "wheel 需要 dy（DOM deltaY：dy>0 = 向下滚）"
    elif kind == "text":
        if not isinstance(payload.get("s"), str):
            return "text 需要字符串 s"
    elif kind == "key":
        key = payload.get("k")
        if not isinstance(key, str) or not key.strip():
            return "key 需要非空字符串 k（DOM 键名，可带 ctrl+/alt+/super+ 前缀）"
    return ""


def _redirect_log() -> None:
    """把输出落到文件（``DSH_VIEW_LOG``）。

    无控制台启动时用得上：Windows 上便携包用 ``pythonw.exe`` 起服务（不留黑框），
    那样 stdout 是空设备，启动失败就什么线索都没有。systemd 那边本来就是 journal，
    不受影响。打开失败就算了 —— 日志绝不该拦住服务本身。
    """
    path = os.environ.get("DSH_VIEW_LOG")
    if not path:
        return
    try:
        stream = open(path, "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = stream
    except Exception:                                # noqa: BLE001
        pass


def _bind(preferred: int, tries: int = 12) -> "ThreadingHTTPServer":
    """从 preferred 开始找一个能绑的端口，并把最终端口写进 ``<HOME_DIR>/port``。

    为什么：同一台机器上可能有多个实例（多个用户、或手工起了两次）。原来的行为是
    端口被占就**直接崩** —— 用户只会看到"面板连不上"，完全看不出原因。
    现在自动往后找，插件侧则在 8099..8111 范围内探测第一个应答的服务。
    """
    last = None
    for port in range(preferred, preferred + tries):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        except OSError as exc:                       # 端口被占：换下一个
            last = exc
            continue
        global PORT
        PORT = port
        srv.daemon_threads = True
        srv.allow_reuse_address = True
        _ensure_dir(HOME_DIR)
        try:
            with open(os.path.join(HOME_DIR, "port"), "w", encoding="utf-8") as fh:
                fh.write(str(port))
        except OSError:
            pass
        if port != preferred:
            print(f"  （{preferred} 被占用，改用 {port}）", flush=True)
        return srv
    raise SystemExit(f"{preferred}..{preferred + tries - 1} 全部被占用：{last}")


def main() -> None:
    import atexit

    _redirect_log()
    _install_signal_handlers()
    atexit.register(_cleanup_spawned)
    if BACKEND == "darwin":
        print(f"viewer on http://127.0.0.1:{PORT}/ · 后端 darwin（真实桌面，实验性）· "
              f"输入注入{'已开启' if MAC_INPUT else '关闭（只读）'}", flush=True)
    elif BACKEND == "win32":
        print(f"viewer on http://127.0.0.1:{PORT}/ · 后端 win32（真实桌面 {W}x{H}，"
              f"DPI {_DPI_MODE}）· 所有会话共用这块屏 · "
              f"输入注入{'已开启' if WIN_INPUT else '关闭（只读）'}", flush=True)
    else:
        print(f"viewer on http://127.0.0.1:{PORT}/ · 后端 {BACKEND} · 每会话独立 "
              f"（/s/<sessionId>/）· {W}x{H} · 支持双向注入 · "
              f"空闲回收 {'关闭' if IDLE_MINUTES <= 0 else str(int(IDLE_MINUTES)) + ' 分钟'}",
              flush=True)
    print(f"访问令牌：{os.path.join(HOME_DIR, 'token')}（600；本机其它用户读不到）", flush=True)
    miss = missing_tools(force=True)
    if miss:
        print("⚠ 缺少依赖，部分功能不可用：", flush=True)
        for m in miss:
            print(f"    {m['tool']} —— {m['why']}（安装：{m['package']}）", flush=True)
    srv = _bind(PORT, tries=12)
    threading.Thread(target=_reaper_loop, daemon=True, name="reaper").start()
    try:
        srv.serve_forever()
    finally:
        _cleanup_spawned()


if __name__ == "__main__":
    main()
