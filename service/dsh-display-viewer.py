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
    GET  /s/<sid>/snapshot    单帧 JPEG（**不阻塞**：没有帧立刻 503；可选 ?quality=&scale=）
    GET  /s/<sid>/stream      MJPEG 长连接（逐帧头 X-DSH-Seq/Time/Size/Cursor，多客户端广播）
    GET  /s/<sid>/state       状态 JSON（cursor / input / realDesktop / tooltip / 抓帧错误）
    GET  /s/<sid>/display     显示号与后端（脚本用）
    GET  /s/<sid>/procs       本会话在本服务里拉起的程序
    GET  /s/<sid>/stats       帧管线指标（fps/带宽/耗时/档位/模式，契约 §5.5）
    GET  /s/<sid>/stream-config  当前档位（quality/fps/scale，只读、不建会话）
    POST /s/<sid>/input       输入事件（见 §1.3；**按到达顺序串行执行**）
    POST /s/<sid>/stream-config  改档（JSON 或查询串；非法值 400）
    POST /s/<sid>/exec        {"argv":[…],"cwd":…,"wait":false} 在会话显示上跑程序
    POST /s/<sid>/kill        {"pid":123}
    POST /s/<sid>/close       回收本会话（等价于 DELETE /s/<sid>）
    DELETE /s/<sid>           回收本会话（停 Xvfb、杀子进程、释放显示号）

## 帧管线（0.4.0，契约 §5.2/§5.3）

抓帧 → 去重 → 编码 → 广播，一条会话一条线程：

* **抓帧**：X11 走进程内 ``ctypes + libX11`` 的 ``XGetImage``（实测 4.9ms/帧，
  旧实现每帧 spawn 一次 ``import`` 要 45ms）；配 ``XDamage`` 事件驱动 ——
  画面不动时**一次唤醒都不需要**（CPU≈0），一动立刻醒。没有 XDamage/没有 libX11
  或像素格式认不出 → 自动退回 ``import``（慢但能用，回退路径不删）。
* **编码**：常驻 ``ffmpeg``（``rawvideo → mjpeg``，``-threads 1``：帧级多线程会把
  头几帧憋在内部缓冲里，实测最坏 3 秒才吐第一帧）。没有 ffmpeg → 退回 ``import``。
* **去重**：内容没变就不编码、不发帧（``/stats.skipped`` 涨、带宽趋近 0）；
  只有指针动了才重发**缓存的那张 JPEG**（上限 5fps，客户端的光标才不会冻住）。
* **自适应**：``quality``(1..100) / ``fps``(1..30) / ``scale``(0.25..1.0)，
  默认 70/20/1（上限留出余量，见下）；``POST /s/<sid>/stream-config`` 可改。
  抓不动或编不过来时
  **先降 fps → 再降 scale → 最后降 quality**，原因写进 ``/stats.reason``；
  闲下来（连续 5 秒远低于预算）再逐档回升。降档只动服务端自己的 ``auto``，
  客户端设的目标档永远保留。

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
import ipaddress
import html as _html
import json
import os
import queue
import re
import select
import shutil
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
import traceback
import zlib
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

SERVICE_NAME = "dsh-display-viewer"


def _package_version() -> str:
    """服务版本 = **包版本**（``<包>/package.json``），读不到才退回常量。

    为什么不再写死：写死的常量在每次发版时都得记得手动改，忘了就会出现
    "面板状态条写着 viewer 0.3.0，而实际装的是 0.3.2"这种对不上的情况
    （真发生过：0.3.1/0.3.2 发布后服务仍然自报 0.3.0，排查时非常误导）。
    这个文件就在 ``<包>/service/`` 下，读 ``../package.json`` 即可。

    读失败（被单独拷出来跑、权限问题、JSON 坏了）**绝不能影响服务启动** ——
    退回常量即可，版本号只是给人看的。
    """
    try:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "package.json"), encoding="utf-8") as fh:
            value = json.load(fh).get("version")
        if isinstance(value, str) and value.strip():
            return value.strip()
    except Exception:                                # noqa: BLE001
        pass
    return "0.3.3"


VERSION = _package_version()

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
#: ⚠️ 0.4.0 起 X11 走进程内 XGetImage + XDamage，**不再按这个间隔轮询**；它现在只用于：
#: ① win32/darwin 的抓帧节奏；② 抓帧失败后的退避。
GRAB_INTERVAL = {"win32": 0.12, "darwin": 0.3}.get(BACKEND, 0.5)

# ---------------------------------------------------------------- 流畅度档位（契约 §5.2/§5.5）
#: 默认档：客户端/宿主不调 /stream-config 时就用它。
#: ⚠️ fps 默认 20 而不是验收线 15：**验收线不该同时是上限** ——
#: 上限=15 意味着调度抖动（实测约 2.3%）直接吃掉余量，稳定跑出 14.6 < 15。
#: 默认 20 时实测 18~19fps，静止/抓不动时自适应会自己降回 12~15。
DEFAULT_QUALITY, DEFAULT_FPS, DEFAULT_SCALE = 70, 20, 1.0
MIN_QUALITY, MAX_QUALITY = 1, 100
MIN_FPS, MAX_FPS = 1, 30
MIN_SCALE, MAX_SCALE = 0.25, 1.0
#: 自适应降档到底线就停：quality 再低画面就没法看了，宁可掉帧也别糊成一片。
ADAPT_MIN_QUALITY = 20
#: 一帧的耗时预算 = 帧周期的这个比例（剩下 40% 留给广播、客户端与调度抖动）。
ADAPT_BUDGET_RATIO = 0.6
#: 两次降档之间的冷却（秒）：降一档要等它生效再看，否则一次抖动就一路降到底。
ADAPT_COOLDOWN = 2.0
#: 连续几次评估都"跟不上"才真降档（一次抖动不算：机器上还有别的活儿在抢 CPU）。
ADAPT_STRIKES = 2
#: 多久没有内容变化就算"空闲"：空闲窗口**不参与自适应**（没有帧不等于处理不过来）。
IDLE_GATE = 2.0
#: 恢复时每次给 fps 加多少、隔多久加一次（有滞回地往上爬，直到客户端设的上限）。
ADAPT_RECOVER_FPS = 2
ADAPT_RECOVER_EVERY = 2.0
#: 恢复的余量判据：一帧总耗时低于帧周期的这个比例才敢往上加。
ADAPT_HEADROOM = 0.6
#: 画面静止时：没有 XDamage 时的兜底轮询节奏。
IDLE_FPS = 4
#: 画面静止时：有 XDamage 时的兜底巡检间隔（秒）—— 万一某次变化没产生 damage
#: （例如扩展在某些 X 服务器上的行为差异），1 秒内也能自己发现。
IDLE_TICK = 1.0
#: 慢速回退（每帧 spawn import，45ms/帧）时的静止巡检节奏：1fps。
IDLE_FPS_SLOW = 1
#: "仅指针移动"的重发上限（每秒）。画面没变但指针动了，就重发**缓存的那张 JPEG**
#: （不重新编码），否则客户端的光标会跟着画面一起冻住。
CURSOR_FPS = 5
#: 内容变化后按目标 fps 抓帧的保持时间（秒）：拖动窗口时不能因为"这一帧没变"就掉回低频。
ACTIVE_HOLD = 1.5
#: /stats 里 fps / bytesPerSec 的滑动窗口（秒）。
STATS_WINDOW = 3.0
#: /stats 里 damageEvents 的窗口（秒）。
DAMAGE_WINDOW = 1.0
#: /snapshot 不带参数时用的质量（AI 截图要看清字，比流里的 70 更清楚）。
SNAPSHOT_QUALITY = 90
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
#: 一个请求最多读多少 body 字节（超出部分由 _drain_request_body 抽干或直接关连接）。
BODY_READ_MAX = 1024 * 1024
#: 收尾抽干 body 的上限：超过就关连接（绝不为恶意巨型 body 陪跑）。
BODY_DRAIN_MAX = 64 * 1024
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


# ---------------------------------------------------------------- Host 白名单
# 令牌是"能不能读"的防线，Host 是"谁能来问"的防线，两道都要有。
#
# 为什么令牌之外还要这一道：服务监听 ``127.0.0.1``，**浏览器里的任意网页都能
# 朝本机回环发请求**（``<img>``/``fetch``/表单提交），而 ``fetch`` 能发
# ``Content-Type: text/plain`` 的"简单请求"——**不触发预检**，于是
# ``POST /s/<sid>/input`` 这种写操作能被跨源页面直接打进来（读不到响应，
# 但事件已经注进显示里了）。更糟的是 DNS rebinding：攻击者把自己的域名解析到
# ``127.0.0.1``，此时请求在**浏览器看来是同源**的，``Host`` 头却是攻击者的域名
# ——不带 ``k=`` 的请求只要命中"没令牌就不设防"的旧行为就能被读走。
#
# 所以照抄 DSH 自己的 ``isTrustedApiRequest``（``@deepseek-ai/dsh-client-connection``）
# 的语义，只多一条"可选扩展"：本服务**永远只监听回环**，正常访问路径是宿主半边
# 的同源代理 —— 代理打上游时 Host 是 ``127.0.0.1:<port>``，天然在白名单里。
TRUSTED_HOSTS: tuple[str, ...] = tuple(
    item.strip().lower()
    for item in os.environ.get("DSH_VIEW_TRUSTED_HOSTS", "").split(",")
    if item.strip()
)


def is_loopback_hostname(hostname: str) -> bool:
    """主机名是否就是本机回环（与 DSH 的 ``isLoopbackHostname`` 同一套判定）。

    ``localhost`` / IPv6 回环 ``[::1]`` / 任意 ``127.0.0.0/8`` 地址。
    **不做 DNS 解析**：解析一次就等于给攻击者一个可控的 TTL 窗口
    （rebinding 正是靠这个），纯字符串判定没有窗口。
    """
    name = (hostname or "").lower()
    if name in ("localhost", "[::1]", "::1"):
        return True
    parts = name.split(".")
    if len(parts) != 4 or parts[0] != "127":
        return False
    return all(part.isdigit() and len(part) <= 3 and int(part) <= 255 for part in parts)


def parse_ipv4_literal(host: str):
    """按 WHATWG URL 的 IPv4 解析器把主机名折成点分十进制；不是 IPv4 写法就回 None。

    为什么非做不可：浏览器与 Node 的 ``new URL()`` 会把 ``0x7f.0.0.1``、
    ``0177.0.0.1``、``2130706433`` 这类写法**全都规范化成** ``127.0.0.1``
    （URL 标准里主机名的 IPv4 解析是"每段按 0x/0/十进制解析、末段按位数补足"）。
    服务端如果只用 ``ipaddress`` 判定，这些形式就会**判不出来是回环**：

    * 拒绝它们 → 正常浏览器根本不会发这种 Host，行为上没差；
    * 但如果哪天有路径把"服务端认定"与"浏览器认定"当成同一个判断
      （例如将来放行别的来源），两边不一致就是一个**静默放宽**的口子。

    所以两端用同一套规则，代价是这二十行。规则与 URL 标准一致：
    每段按前缀决定进制（``0x``/``0X`` = 十六进制，前导 ``0`` = 八进制，否则十进制），
    末段独占剩余字节数（所以 ``127.1`` = ``127.0.0.1``），任何越界即整段作废。
    """
    text = (host or "").strip()
    if not text:
        return None
    if text.endswith("."):
        text = text[:-1]
    parts = text.split(".")
    if parts and parts[-1] == "":                      # 尾随点只允许一个
        parts.pop()
    if not parts or len(parts) > 4:
        return None
    numbers: list[int] = []
    for index, part in enumerate(parts):
        if not part:
            return None
        base = 10
        digits = part
        if len(part) >= 2 and part[:2].lower() == "0x":
            base, digits = 16, part[2:]
            if not digits:
                return None
        elif len(part) >= 2 and part[0] == "0":
            base, digits = 8, part[1:]
            if not digits:
                numbers.append(0)
                continue
        allowed = "0123456789abcdef" if base == 16 else ("01234567" if base == 8 else "0123456789")
        if any(ch.lower() not in allowed for ch in digits):
            return None
        numbers.append(int(digits, base))
    if any(value > 255 for value in numbers[:-1]):
        return None
    if numbers[-1] >= 256 ** (5 - len(numbers)):
        return None
    total = numbers[-1]
    for index, value in enumerate(numbers[:-1]):
        total += value * 256 ** (3 - index)
    return f"{(total >> 24) & 255}.{(total >> 16) & 255}.{(total >> 8) & 255}.{total & 255}"


def ends_in_a_number(host: str) -> bool:
    """主机名是否以"数字段"结尾（URL 标准里 `ends in a number` 的判定）。

    这条规则决定了「本该是 IPv4 却写坏了」的字符串怎么处理：
    ``1.2.3.4.5``（五段）、``0x7f.0.0.256``（越界）、``999999999999``（太大）
    都以数字结尾但**不是**合法 IPv4 —— 浏览器解析这种主机名会**直接报错**，
    不会把它当域名。服务端必须做出同样的判断，否则
    "``1.2.3.4.5`` 是个普通域名"就成了一个只在服务端成立的假设。
    """
    parts = (host or "").split(".")
    if parts and parts[-1] == "":
        parts.pop()
    if not parts or not parts[-1]:
        return False
    last = parts[-1]
    if all(ch.isdigit() for ch in last):
        return True
    if len(last) >= 2 and last[:2].lower() == "0x":
        return all(ch.lower() in "0123456789abcdef" for ch in last[2:])
    return False


def canonical_authority(authority: str):
    """把 ``host[:port]`` 规范化成 ``(host, port)``；不是裸 authority 就回 None。

    与 DSH 的 ``parseAuthority`` + ``canonicalAuthority`` 对齐：

    * 整体形态必须是裸 authority（不允许 ``http://h``、``user@h``、``h/path``、
      ``h?x``、``h#x``）—— 带这些形状的一律判**不**可信，宁可拒绝也别猜；
    * 端口必须是纯数字（零填充的 ``:08099`` 会被浏览器规范化成 ``:8099``，
      两边对不上就会静默放宽，所以直接拒绝这种写法）；
    * 主机名先过 :func:`parse_ipv4_literal`（``0x7f.0.0.1`` / ``2130706433`` /
      ``127.1`` 这类 WHATWG 写法全落到 ``127.0.0.1``），再退到 IPv6，
      最后才当域名（域名只接受小写 ASCII，**不做 punycode 之外的任何猜测**）。
    """
    # ⚠️ 只剥 ASCII 空白（HTTP 的 OWS），**不用 `str.strip()`**：后者会连
    # Unicode 空白（`\x0b`、`\xa0`、`\u2028`…）一起吃掉，于是
    # `Host: 127.0.0.1:8099\x0b` 这种畸形头会被"洗干净"后放行。
    # 剥完还残留任何空白（`.isspace()` 认 Unicode）就直接判不可信。
    raw = (authority or "").strip(" \t")
    if not raw or any(ch in raw for ch in "/?#@") or any(ch.isspace() for ch in raw):
        return None
    if raw.startswith("["):                            # [::1]:8099 / [::1]
        close = raw.find("]")
        if close < 0:
            return None
        host, rest = raw[:close + 1], raw[close + 1:]
        if rest and not rest.startswith(":"):
            return None
        port = rest[1:] if rest else ""
        literal = host[1:-1]
    elif raw.count(":") > 1:                           # 裸 IPv6（无端口）
        host, port, literal = raw, "", raw
    else:
        host, _, port = raw.partition(":")
        literal = host
    if port and (not port.isdigit() or (len(port) > 1 and port.startswith("0"))):
        return None
    canonical = parse_ipv4_literal(literal)
    if canonical is None:
        try:
            canonical = str(ipaddress.ip_address(literal))
            if ":" in canonical:
                canonical = f"[{canonical}]"
        except ValueError:
            canonical = host.lower()
            # 以数字结尾却不是合法 IPv4：浏览器解析这个主机名会失败，
            # 我们也不能退而把它当域名（见 :func:`ends_in_a_number`）。
            if ends_in_a_number(canonical):
                return None
            if not re.fullmatch(
                    r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*",
                    canonical):
                return None
    return canonical, port


def trusted_authority(authority: str) -> bool:
    """authority 是否在"本机 / 显式白名单"里。

    端口语义照抄 DSH：条目**带端口**就要求完全一致（``127.0.0.1:8099`` 只信这一个
    端口），**不带端口**则匹配该主机的任意端口（IP 字面量的端口可能是系统分配的）。
    """
    parsed = canonical_authority(authority)
    if parsed is None:
        return False
    host, port = parsed
    if is_loopback_hostname(host):
        return True
    for entry in TRUSTED_HOSTS:
        entry_parsed = canonical_authority(entry)
        if entry_parsed is None:                      # 配置写错的名字：忽略而不是放宽
            continue
        entry_host, entry_port = entry_parsed
        if entry_host == host and (not entry_port or entry_port == port):
            return True
    return False


def origin_reason(origin: str, raw_host: str) -> str:
    """``Origin`` 头是否与 ``Host`` 同源；同源回 ``""``，否则回原因。

    **刻意不用 ``urlparse``**（真踩过）：``urlparse("http://[")`` 会抛
    ``ValueError: Invalid IPv6 URL``。那个异常发生在令牌校验**之前**，结果是
    "攻击者用一个畸形头就能让服务对本请求一个字节都不回"（栈打到日志里），
    比直接拒绝还糟 —— 拒绝至少是个明确的答复。所以这里全部用字符串判定，
    任何输入都只可能得到"通过"或"一条原因"，不会抛。

    只接受**合法序列化**的 origin（``scheme://host[:port]``）：`Origin` 是浏览器
    生成的，非该形状的一律不可信（``http:host:port`` 这种缺 ``//`` 的、
    带 path/userinfo 的都不是序列化产物）。
    """
    raw = (origin or "").strip(" \t")
    scheme, sep, rest = raw.partition("://")
    if not sep or scheme.lower() not in ("http", "https"):
        return f"Origin {origin!r} 不是 http(s) 来源（要求 scheme://host[:port]）"
    if not rest or any(ch in rest for ch in "/?#@"):
        return f"Origin {origin!r} 带了路径/查询/userinfo —— 不是合法的序列化 Origin"
    parsed = canonical_authority(rest)
    if parsed is None:
        return f"Origin {origin!r} 的 host 部分无法解析"
    origin_host, origin_port = parsed
    host_parsed = canonical_authority(raw_host)
    if host_parsed is None:
        return f"Host {raw_host!r} 无法解析"
    host_name, host_port = host_parsed
    # 端口：Host 是我们自己监听的端口（一定带）；Origin 没写端口时按 scheme 的默认端口比。
    want_port = host_port or origin_port
    if origin_host != host_name or origin_port != want_port:
        return f"Origin {origin!r} 与 Host {raw_host!r} 不同源"
    return ""


def host_reason(headers) -> str:
    """请求头是否来自一个可信来源；可信回 ``""``，否则回**原因**（给人看的一句话）。

    判定顺序与 DSH 的 ``isTrustedApiRequest`` 一致：
    ① ``Host`` 必须存在且可信；② ``Sec-Fetch-Site: cross-site`` 直接拒绝
    （浏览器自己标的跨站，比 Origin 更早、更可靠）；③ 带了 ``Origin`` 就必须与
    ``Host`` 同源。三条都不依赖客户端可以随便伪造的字段组合。
    """
    try:
        raw_host = headers.get("Host") if headers else None
    except Exception:                                 # noqa: BLE001
        return "缺少 Host 头"
    if not raw_host:
        return "缺少 Host 头"
    if not trusted_authority(raw_host):
        return (f"Host {raw_host!r} 不在白名单里（本服务只接受本机回环；"
                "要放行别的名字，设 DSH_VIEW_TRUSTED_HOSTS，逗号分隔）")
    try:
        site = (headers.get("Sec-Fetch-Site") or "").strip().lower()
    except Exception:                                 # noqa: BLE001
        site = ""
    if site == "cross-site":
        return "浏览器标记为 Sec-Fetch-Site: cross-site（跨站请求）"
    try:
        origin = headers.get("Origin")
    except Exception:                                 # noqa: BLE001
        origin = None
    if not origin:
        return ""
    return origin_reason(origin, raw_host)


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
#: 见过的 X 协议错误数（诊断用；0.4.0 起装了处理器，不再让 Xlib 把服务带走）。
X11_ERRORS = 0


def _x11_error_handler(_dpy, _event) -> int:
    """X 协议错误：记一笔就返回 0（Xlib 默认处理器会 print + exit(1)）。

    ctypes 回调里抛异常帮不上忙（ctypes 会把异常吞掉再返回 0），所以只能自己记住。
    """
    global X11_ERRORS
    X11_ERRORS += 1
    if X11_ERRORS <= 3 or X11_ERRORS % 100 == 0:
        print(f"⚠ X11 协议错误（第 {X11_ERRORS} 次，已忽略，服务继续）", flush=True)
    return 0


def _x11_io_error_handler(_dpy) -> int:
    """X 连接断了（Xvfb 没了/显示号被别人顶了）：**绝不返回**，就地 park。

    Xlib 的规矩：``XIOErrorHandler`` 一旦返回，Xlib 立刻 ``exit(1)`` ——
    那就等于"一个会话的显示死了，整个服务（以及所有其它会话）一起死"。
    这条线程本来也已经没救了（它接下来只会拿到同一个坏连接），停在这里最划算：
    其它会话、HTTP、回收线程都不受影响。Session.stop() 只 join 1.5 秒就放手，
    正是为了这种"线程停住但不拖住回收"的情况。
    """
    print("⚠ X11 连接断了（Xvfb 被回收？）：抓帧线程就地停住，服务继续", flush=True)
    while True:
        time.sleep(3600)


_x11_error_handler = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p,
                                      ctypes.c_void_p)(_x11_error_handler)
_x11_io_error_handler = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p)(_x11_io_error_handler)


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
            # --- 0.4.0：抓帧（XGetImage）与指针位置（XQueryPointer）也走同一个 libX11。
            lib.XDefaultDepth.restype = ctypes.c_int
            lib.XDefaultDepth.argtypes = [ctypes.c_void_p, ctypes.c_int]
            lib.XDisplayWidth.restype = ctypes.c_int
            lib.XDisplayWidth.argtypes = [ctypes.c_void_p, ctypes.c_int]
            lib.XDisplayHeight.restype = ctypes.c_int
            lib.XDisplayHeight.argtypes = [ctypes.c_void_p, ctypes.c_int]
            lib.XGetImage.restype = ctypes.c_void_p
            lib.XGetImage.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int,
                                      ctypes.c_int, ctypes.c_uint, ctypes.c_uint,
                                      ctypes.c_ulong, ctypes.c_int]
            lib.XQueryPointer.restype = ctypes.c_int
            lib.XQueryPointer.argtypes = [
                ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong),
                ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_int),
                ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
                ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_uint)]
            lib.XPending.restype = ctypes.c_int
            lib.XPending.argtypes = [ctypes.c_void_p]
            lib.XNextEvent.restype = ctypes.c_int
            lib.XNextEvent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            lib.XConnectionNumber.restype = ctypes.c_int
            lib.XConnectionNumber.argtypes = [ctypes.c_void_p]
            # ⚠️ **不要**想着调 XDestroyImage：它是 Xutil.h 里的宏
            #    （``((*image->f.destroy_image)(image))``），libX11 里**没有这个符号**。
            #    释放 XGetImage 的返回值得自己来两步：先 XFree(image->data) 再 XFree(image)
            #    —— 只 XFree(image) 会漏掉那张 1600×1000×4 = 6.4MB 的像素缓冲（实测内存一路涨）。
            lib.XSetErrorHandler.restype = ctypes.c_void_p
            lib.XSetErrorHandler.argtypes = [ctypes.c_void_p]
            lib.XSetIOErrorHandler.restype = ctypes.c_void_p
            lib.XSetIOErrorHandler.argtypes = [ctypes.c_void_p]
            # Xlib 默认的错误处理器会 **print + exit(1)**：X 服务器一断（Xvfb 被回收、
            # 别的实例顶掉显示号）整个服务就连带所有会话一起死。装上自己的处理器：
            #   * 协议错误（BadWindow 之类）→ 记一笔、返回 0，继续跑；
            #   * IO 错误（连接真的断了）→ **绝不返回**（Xlib 规定返回即 exit(1)），
            #     就地 park 住这条抓帧线程，服务与其它会话照常。
            # 之所以敢 park：Xlib 的连接只有管线线程在用（XCloseDisplay 也在那条线程里），
            # 停住的正是"已经坏掉的那条会话的抓帧线程"，不会牵住 HTTP 线程。
            lib.XSetErrorHandler(ctypes.cast(_x11_error_handler, ctypes.c_void_p))
            lib.XSetIOErrorHandler(ctypes.cast(_x11_io_error_handler, ctypes.c_void_p))
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
    try:
        # SIGKILL 之后还要 wait 一次，否则子进程会留成僵尸（实测 /close 之后
        # ps 里挂着一个 [ffmpeg] <defunct>）。
        proc.wait(timeout=1.0)
    except Exception:                                # noqa: BLE001
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
        # 帧管线（契约 §5.2）：状态挂在会话上（/stats、/snapshot 都读它），
        # 线程由 session() 起、由 stop() 收。
        self.pipe = Pipeline(self)
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
                # ⚠️ ``-noreset`` 必须加：X 服务器在**最后一个客户端断开时会把整个服务器
                # 复位**（销毁 root window 及其全部属性、重跑 xkbcomp）。实测后果：
                # ① 我们打在 root 上的归属标记在"没有客户端连着"的瞬间就没了
                #    （这正是这套归属校验一开始验证不通过的原因）；
                # ② 靶程序一退出，画面就被复位清空 —— 用户看到的是"莫名其妙全黑"。
                ["Xvfb", self.display, "-screen", "0", f"{W}x{H}x24",
                 "-nolisten", "tcp", "-noreset"],
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
        """回收：停管线、停输入 worker、杀 /exec 子进程、停显示服务器、释放显示号。

        幂等；任何一步失败都不抛（回收路径绝不能把服务带崩）。
        """
        self._closed = True
        # ⚠️ 顺序很重要：**先**让帧管线收手，**再**杀 Xvfb。
        #    0.4.0 起抓帧是进程内 XGetImage：管线正在调用 Xlib 时把 Xvfb 杀掉，
        #    Xlib 会走 IO 错误处理器把那条线程 park 住（服务不会死，但会话会留个僵尸线程）。
        #    先 join 就基本不会撞上这个窗口。
        try:
            self.pipe.stop()
        except Exception as exc:                     # noqa: BLE001
            print(f"[{self.sid}] 停帧管线出错（继续回收）：{type(exc).__name__}: {exc}",
                  flush=True)
        try:
            self.events.put_nowait(None)             # 让 worker 退出
        except queue.Full:
            pass
        # ⚠️ 每一步都自己兜住异常：回收路径上任何一步失败都不许拖累后面的步骤
        #    （0.3.4 就是在这里抛出 AttributeError，导致 Xvfb 不回收、显示号不释放）。
        try:
            for proc, _argv, _at in self.procs_live():
                _terminate_proc(proc, timeout=2.0)
            with self.proc_lock:
                self.procs = []
        except Exception as exc:                     # noqa: BLE001
            print(f"[{self.sid}] 回收 /exec 子进程出错（继续回收）："
                  f"{type(exc).__name__}: {exc}", flush=True)
        try:
            proc = self._server_proc
            if proc is not None:
                _unregister_spawn(proc)
                _terminate_proc(proc, timeout=3.0)
                self._server_proc = None
        except Exception as exc:                     # noqa: BLE001
            print(f"[{self.sid}] 停显示服务器出错（继续回收）："
                  f"{type(exc).__name__}: {exc}", flush=True)
        self.started = False
        self._alive_ok = False
        self._alive_at = 0.0
        if release:
            try:
                release_display(self.sid, self.number, forget=forget)
            except Exception as exc:                 # noqa: BLE001
                print(f"[{self.sid}] 释放显示号出错：{type(exc).__name__}: {exc}", flush=True)

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

    def snapshot_jpeg(self, quality=None, scale=None) -> bytes:
        """``/snapshot`` 用的单帧 JPEG。

        * 不带 ``quality`` / ``scale`` → 直接给**流里最新的那帧**（旧的语义与速度）；
        * 带了 → **单独编一帧**（自己的 X 连接 + 一次性编码进程），
          绝不去动常驻编码器：那会为了截一张图把流的节奏打断。
        * 拿不到就返回空 → 调用方回 503（**不阻塞**，这是契约 §1.2 的硬要求）。
        """
        cfg = self.pipe.effective_config()
        q = cfg["quality"] if quality is None else quality
        s = cfg["scale"] if scale is None else scale
        if (int(q), round(float(s), 3)) == (int(cfg["quality"]), round(float(cfg["scale"]), 3)):
            with self.lock:
                if self.latest:
                    return self.latest
        try:
            shot = self.pipe.encode_shot(q, s)
        except Exception as exc:                     # noqa: BLE001
            print(f"[{self.sid}] 高清截图失败（回退到流里那帧）："
                  f"{type(exc).__name__}: {exc}", flush=True)
            shot = b""
        if shot:
            return shot
        with self.lock:
            return self.latest

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

    def procs_live(self) -> list:
        """还活着的 ``(Popen, argv, started)`` 三元组 —— **回收路径用这个**。

        ⚠️ 别拿 :meth:`procs_snapshot` 去做回收：它返回的是给 ``/procs`` 用的
        **dict 列表**，按三元组解包会解出 ``"pid"`` 这种字符串。0.3.4 就是这么写的，
        后果是"只要会话里跑过 ``/exec`` 程序，``/close``/``DELETE`` 就整个崩掉"：
        ``AttributeError: 'str' object has no attribute 'poll'`` ——
        子进程没杀、Xvfb 没停、显示号没释放，用户还拿到一个空响应（实测复现过）。
        """
        with self.proc_lock:
            alive = [(proc, argv, started) for proc, argv, started in self.procs
                     if proc.poll() is None]
            self.procs = alive
            return alive

    def procs_snapshot(self) -> list:
        """``/procs`` 的 JSON 视图（**dict 列表，不是三元组**；见 :meth:`procs_live`）。"""
        return [{"pid": proc.pid, "argv": list(argv), "startedAt": round(started, 3)}
                for proc, argv, started in self.procs_live()]

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
    # 幂等：线程还活着就什么都不做；停了（park 在 Xlib 里 / 抓帧线程异常退出）就重起一条。
    # 放在这里是为了"自愈"——每次 HTTP 请求路过都会顺手检查一次。
    sess.pipe.revive()
    if created:
        print(f"[{sid}] 新会话（显示 {sess.display}）", flush=True)
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

    # ⚠️ xclip 必须给 ``-l``（服务若干次选区请求后再退出）：目标应用读剪贴板是**分几次请求**的
    # （先问 TARGETS、再取数据），默认只服务一次就退出 → 数据还没取到就没主了，
    # 表现为"Ctrl+V 什么都没粘上"。
    #
    # ⚠️⚠️ 但"喂完 stdin + sleep 0.4 就按 Ctrl+V"仍然是个**竞态** ——
    # 高负载机器（CI 的共享 runner、正在编译的笔记本）上 xclip 还没拿到选区所有权，
    # 粘贴就已经发出去了，于是中文"打不进去"。实测：同一份代码前两轮 CI 绿、第三轮红，
    # 而且失败时目标里只有前一条 ASCII 文本 —— 典型竞态。
    # 所以这里改成**确认选区真的可读**（xclip -o 能读回我们要的文本）再粘贴；
    # 读不回来就换更大的 -l 重试一次，仍不行则明确报错（而不是静默粘不上）。
    key = text.encode("utf-8")
    last = "(还没试)"
    for serve in ("20", "200"):
        prev = getattr(sess, "clip_proc", None)
        if prev is not None:
            try:
                prev.kill()                          # 换新文本：老的选区主人让位，别堆积
            except Exception:                        # noqa: BLE001
                pass
        try:
            proc = subprocess.Popen(["xclip", "-selection", "clipboard", "-l", serve],
                                    env=sess.env, stdin=subprocess.PIPE,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            proc.stdin.write(key)
            proc.stdin.close()
        except Exception as exc:                     # noqa: BLE001
            raise RuntimeError(f"xclip 异常：{type(exc).__name__}: {exc}")
        sess.clip_proc = proc

        # 轮询到"读回来就是我们写的"为止（最多 ~2.5 秒；本机通常 1~2 次就命中）
        deadline = time.time() + 2.5
        while time.time() < deadline:
            try:
                got = subprocess.run(["xclip", "-selection", "clipboard", "-o"],
                                     env=sess.env, capture_output=True, timeout=3)
                if got.returncode == 0 and got.stdout == key:
                    run_tool(sess, ["xdotool", "key", "--clearmodifiers", "ctrl+v"])
                    return
                last = repr(got.stdout[:60])
            except subprocess.TimeoutExpired:
                last = "xclip -o 超时"
            except Exception as exc:                 # noqa: BLE001
                last = f"{type(exc).__name__}: {exc}"
            time.sleep(0.1)
    raise RuntimeError(f"剪贴板没能建立（-l {serve}；xclip -o 读回 {last}）→ 中文没粘上")


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


# ---------------------------------------------------------------- 抓帧 / 帧管线
# 契约 §5.2：抓帧（进程内 XGetImage）→ 去重（CRC32）→ 编码（常驻 ffmpeg）→ 广播（MJPEG 长连接）。
#
# 0.3.4 的三个瓶颈（本机 1600×1000 X11 实测）：
#   ① 抓帧：每帧 spawn 一次 `import -window root` = 45ms/帧（还要自带一次 X 抓屏）；
#      进程内 XGetImage 直读 = 4.9ms/帧。
#   ② 节奏：GRAB_INTERVAL=0.5 → 内容只有 2fps；XDamage 事件驱动后变化延迟≈0ms。
#   ③ 编码：每帧无条件重编码（画面静止也照编照发）；常驻 ffmpeg 稳态 16-22ms/帧，
#      内容没变就一帧都不编。
ZPIXMAP = 2

#: 小于这个字节数的帧直接用 CRC32 当去重键（JPEG 几十 KB，CRC 只要几十微秒）；
#: 比它大的（X11 原始 BGRA 是 6.4MB，CRC 要 4.9ms）先用一次内存比较判等。
DEDUP_CRC_ALWAYS = 256 * 1024
#: 编码一帧的超时（秒）：超了就当编码进程坏了，重启它（别把管线卡死在这）。
ENCODE_TIMEOUT = 10.0
#: 抓帧失败、退回 import 之后的连续失败退避上限（秒）。
GRAB_FAIL_MAX_BACKOFF = 5.0

_xdamage_lib = None                                  # None=未加载；False=加载失败


def _xdamage():
    """懒加载 libXdamage —— 用来知道"画面什么时候变的"（没有它就退回轮询）。

    为什么值得引入一个扩展：XDamage 让静止画面**一次唤醒都不需要**（CPU≈0），
    而变化一发生立刻醒（不用等下一个轮询节拍）。实测 root window 上的
    XDamageReportRawRectangles **能收到子窗口重绘**（1 秒 69 个事件），
    这正是"窗口里跑的程序重绘"这条最常见路径。
    扩展不可用（老服务器/非 X11）→ 返回 None，管线自动退回定时轮询。
    """
    global _xdamage_lib
    with _x11_lock:
        if _xdamage_lib is not None:
            return _xdamage_lib or None
        try:
            lib = ctypes.CDLL("libXdamage.so.1")
            lib.XDamageQueryExtension.restype = ctypes.c_int
            lib.XDamageQueryExtension.argtypes = [ctypes.c_void_p,
                                                  ctypes.POINTER(ctypes.c_int),
                                                  ctypes.POINTER(ctypes.c_int)]
            lib.XDamageCreate.restype = ctypes.c_ulong
            lib.XDamageCreate.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int]
            lib.XDamageSubtract.restype = None
            lib.XDamageSubtract.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
                                            ctypes.c_ulong, ctypes.c_ulong]
            lib.XDamageDestroy.restype = None
            lib.XDamageDestroy.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
            _xdamage_lib = lib
        except Exception as exc:                     # noqa: BLE001
            print(f"⚠ 加载 libXdamage 失败（改成定时轮询，功能不受影响）："
                  f"{type(exc).__name__}: {exc}", flush=True)
            _xdamage_lib = False
        return _xdamage_lib or None


class XImage(ctypes.Structure):
    """Xlib 的 ``XImage`` 头字段（只写到 blue_mask）。

    后面还有 obdata 与函数指针表（destroy_image/get_pixel…），我们只通过指针读，
    不需要它们的布局；**但 bytes_per_line / bits_per_pixel 必须读对** ——
    深屏（24/30 位、行对齐补白）下 ``width*4`` 并不等于一行字节数，按紧凑排列解析
    会得到斜掉的画面。
    """

    _fields_ = [("width", ctypes.c_int), ("height", ctypes.c_int),
                ("xoffset", ctypes.c_int), ("format", ctypes.c_int),
                ("data", ctypes.c_void_p), ("byte_order", ctypes.c_int),
                ("bitmap_unit", ctypes.c_int), ("bitmap_bit_order", ctypes.c_int),
                ("bitmap_pad", ctypes.c_int), ("depth", ctypes.c_int),
                ("bytes_per_line", ctypes.c_int), ("bits_per_pixel", ctypes.c_int),
                ("red_mask", ctypes.c_ulong), ("green_mask", ctypes.c_ulong),
                ("blue_mask", ctypes.c_ulong)]


class X11Screen:
    """进程内 X11 抓帧：XGetImage（BGRA）+ XDamage（变化通知）+ XQueryPointer（指针）。

    **每条会话一个连接**，且**只在管线线程里用** —— 一旦显示没了，Xlib 会走
    IO 错误处理器把这条线程 park 住（见 ``_x11_io_error_handler``），
    所以绝不能让 HTTP 线程也共享这个连接。
    """

    def __init__(self, display: str, name: str = "") -> None:
        self.display = display
        self.name = name
        self.dpy = None
        self.root = 0
        self.width = 0
        self.height = 0
        self.depth = 0
        self.bits_per_pixel = 0
        self.bytes_per_line = 0
        self.fd = -1
        self.damage = None                           # Damage id；None = 没有扩展
        self.damage_events = 0
        self.slot = 0
        self._bufs: list = [None, None]
        self._views: list = [None, None]
        self._evbuf = ctypes.create_string_buffer(192)   # XEvent 是 192 字节

    # ---------------------------------------------------------------- 打开 / 关闭
    def open(self) -> bool:
        lib = _x11()
        if not lib:
            return False
        dpy = lib.XOpenDisplay(self.display.encode("utf-8"))
        if not dpy:
            return False
        self.dpy = dpy
        self.fd = lib.XConnectionNumber(dpy)
        self.root = lib.XDefaultRootWindow(dpy)
        self.width = lib.XDisplayWidth(dpy, 0)
        self.height = lib.XDisplayHeight(dpy, 0)
        self.depth = lib.XDefaultDepth(dpy, 0)
        self._init_damage()
        return self.width > 0 and self.height > 0

    def _init_damage(self) -> None:
        xd = _xdamage()
        if not xd:
            return
        ev, er = ctypes.c_int(), ctypes.c_int()
        try:
            if not xd.XDamageQueryExtension(self.dpy, ctypes.byref(ev), ctypes.byref(er)):
                return
            # XDamageReportRawRectangles(=0)：每个矩形一条事件。实测 root 上建 damage
            # 能收到子窗口的重绘（非合成 X11 也一样），这是"damage 驱动"的前提。
            self.damage = xd.XDamageCreate(self.dpy, self.root, 0)
        except Exception:                            # noqa: BLE001
            self.damage = None

    def close(self) -> None:
        lib = _x11()
        if self.dpy is not None and lib:
            xd = _xdamage()
            if self.damage and xd:
                try:
                    xd.XDamageDestroy(self.dpy, self.damage)
                except Exception:                    # noqa: BLE001
                    pass
            self.damage = None
            try:
                lib.XCloseDisplay(self.dpy)
            except Exception:                        # noqa: BLE001
                pass
        self.dpy = None
        self.fd = -1

    # ---------------------------------------------------------------- 抓帧
    def _view(self, index: int):
        size = self.width * self.height * 4
        view = self._views[index]
        if view is None or len(view) != size:
            buf = bytearray(size)
            view = (ctypes.c_char * size).from_buffer(buf)
            self._bufs[index] = buf
            self._views[index] = view
        return self._bufs[index], view

    def grab(self):
        """抓一帧 → 紧凑 BGRA bytearray（认不出的像素格式返回 None）。

        ⚠️ 交出的缓冲是**两块交替复用**的（调用方可以直接拿它跟上一次比内容，
        不必再复制一份 6.4MB）。因此两次连续调用**绝不会返回同一个对象**。
        """
        lib = _x11()
        if not lib or self.dpy is None:
            return None
        img = lib.XGetImage(self.dpy, self.root, 0, 0, self.width, self.height,
                            ctypes.c_ulong(-1), ZPIXMAP)
        if not img:
            return None
        try:
            xi = ctypes.cast(img, ctypes.POINTER(XImage)).contents
            data = xi.data
            bpl = xi.bytes_per_line
            bpp = xi.bits_per_pixel
            self.bits_per_pixel, self.bytes_per_line = bpp, bpl
            if bpp == 32 and self._standard_bgra(xi):
                self.slot ^= 1
                buf, view = self._view(self.slot)
                if bpl == self.width * 4:
                    ctypes.memmove(view, data, len(buf))
                else:                                # 行对齐补白：逐行搬（别假设紧凑）
                    for row in range(self.height):
                        ctypes.memmove(ctypes.byref(view, row * self.width * 4),
                                       data + row * bpl, self.width * 4)
                return buf
            if bpp == 24:                            # 3 字节/像素：扩成 BGRA
                return self._expand(data, bpl, 3)
            if bpp == 16:                            # 5-6-5
                return self._expand(data, bpl, 2)
            return None                              # 认不出的格式：宁慢不乱，交给 import
        finally:
            # ⚠️ 必须 data + image 各 Free 一次（XDestroyImage 是宏，libX11 里没这个符号）
            try:
                lib.XFree(data)
            except Exception:                        # noqa: BLE001
                pass
            lib.XFree(img)

    @staticmethod
    def _standard_bgra(xi) -> bool:
        """B/G/R 掩码与字节序是否就是 ffmpeg 的 ``bgra``（Xvfb 24 位深屏正好是）。"""
        return (xi.byte_order == 0 and xi.red_mask == 0xFF0000
                and xi.green_mask == 0xFF00 and xi.blue_mask == 0xFF)

    def _expand(self, data, bpl, step: int):
        """24bpp / 16bpp 慢路径：逐像素扩成 BGRA（这类深屏很少见，慢一点没关系）。"""
        buf = bytearray(self.width * self.height * 4)
        raw = ctypes.string_at(data, bpl * self.height)
        out = 0
        for row in range(self.height):
            base = row * bpl
            if step == 3:
                for col in range(self.width):
                    i = base + col * 3
                    buf[out] = raw[i]
                    buf[out + 1] = raw[i + 1]
                    buf[out + 2] = raw[i + 2]
                    buf[out + 3] = 0xFF
                    out += 4
            else:
                for col in range(self.width):
                    i = base + col * 2
                    px = raw[i] | (raw[i + 1] << 8)
                    buf[out] = (px & 0x1F) << 3
                    buf[out + 1] = ((px >> 5) & 0x3F) << 2
                    buf[out + 2] = ((px >> 11) & 0x1F) << 3
                    buf[out + 3] = 0xFF
                    out += 4
        return buf

    # ---------------------------------------------------------------- 变化通知 / 指针
    def wait(self, timeout: float, extra=()) -> bool:
        """等"有事发生"：有 damage 就 select 在 X 连接上（静止时 0 CPU），否则睡。

        ``extra`` 是额外的可读 fd（管线用一根自管道打断等待：改档/回收要立刻生效）。
        """
        lib = _x11()
        if lib is None or self.dpy is None:
            time.sleep(max(0.0, timeout))
            return False
        fds = [self.fd]
        for fd in extra:
            if fd is not None and fd >= 0:
                fds.append(fd)
        try:
            ready, _w, _x = select.select(fds, [], [], max(0.0, timeout))
        except (OSError, ValueError):
            return False
        if self.fd not in ready:
            return False
        hit = False
        try:
            # 成串爆发的 damage（一次拖动几十个矩形）在这里一次性排空，
            # 管线那边只在"该出帧的时刻"抓一帧 —— 合并/去抖就是这么做的。
            while lib.XPending(self.dpy):
                lib.XNextEvent(self.dpy, self._evbuf)
                self.damage_events += 1
                hit = True
        except Exception:                            # noqa: BLE001
            return False
        xd = _xdamage()
        if hit and self.damage and xd:
            try:
                xd.XDamageSubtract(self.dpy, self.damage, 0, 0)   # 复位，别让区域无限涨
            except Exception:                        # noqa: BLE001
                pass
        return hit

    def pointer(self):
        """指针位置（**未归一化的像素坐标**）；拿不到返回 None。

        用 XQueryPointer（一次往返，≈50µs），不再为每个 /state 请求 spawn 一个 xdotool。
        """
        lib = _x11()
        if lib is None or self.dpy is None:
            return None
        root_ret, child_ret = ctypes.c_ulong(), ctypes.c_ulong()
        rx, ry, wx, wy = (ctypes.c_int(), ctypes.c_int(), ctypes.c_int(), ctypes.c_int())
        mask = ctypes.c_uint()
        try:
            ok = lib.XQueryPointer(self.dpy, self.root, ctypes.byref(root_ret),
                                   ctypes.byref(child_ret), ctypes.byref(rx),
                                   ctypes.byref(ry), ctypes.byref(wx), ctypes.byref(wy),
                                   ctypes.byref(mask))
        except Exception:                            # noqa: BLE001
            return None
        return (rx.value, ry.value) if ok else None


class FrameDedup:
    """帧去重（契约 §5.2）：内容没变就不编码、不发帧。

    判定顺序按帧大小分两条（1600×1000×4 = 6.4MB 实测）：
      * 小帧（≤ ``DEDUP_CRC_ALWAYS``，即 JPEG 几十 KB）→ 直接算 CRC32 当键（几十µs）；
      * 大帧（X11 原始 BGRA 6.4MB）→ 先跟上一帧比内容（bytes 比较走 C 的 memcmp，
        0.6ms；而 X11Screen 的两块缓冲交替复用，比较不需要额外复制），
        **只有变了才算全帧 CRC32**（4.9ms）。
    为什么不无条件算 CRC32：静止时 1-4 次/秒的抓帧下，它自己就要 2% 以上 CPU，
    而它回答的问题（内容变了没）用一次比较就能精确回答（还没有碰撞风险）。
    算出来的 CRC32 同时是 /stats 里的对外指纹，客户端/测试脚本可以据此判断"这一帧是不是新的"。
    """

    def __init__(self) -> None:
        self.prev = None
        self.crc = 0

    def reset(self) -> None:
        self.prev = None
        self.crc = 0

    def check(self, data):
        """→ ``(changed, crc)``。"""
        prev, crc, changed = self.prev, self.crc, True
        if prev is not None and len(prev) == len(data):
            if len(data) <= DEDUP_CRC_ALWAYS:
                crc = zlib.crc32(data)
                changed = crc != self.crc
            else:
                changed = prev != data
        if changed:
            crc = zlib.crc32(data)
        self.prev, self.crc = data, crc
        return changed, crc


def _ewma(old: float, new: float, alpha: float = 0.3) -> float:
    """指数滑动平均：单帧抖动不该立刻触发降档，但持续变慢要看得出来。"""
    return new if old <= 0 else old * (1 - alpha) + new * alpha


def quality_to_qv(quality) -> int:
    """quality（1..100，越大越好）→ ffmpeg 的 ``-q:v``（2..31，越小越好）。

    70 → 11、90 → 5、100 → 2（mjpeg 的 -q:v 到 2 就到头了，1 与 2 没差别）。
    """
    q = int(round((max(1, min(100, int(quality))) - 1) * 29 / 99))
    return max(2, min(31, 31 - q))


def jpeg_size(data):
    """从 JPEG 字节里读 (宽, 高)；读不出来返回 None。

    为什么要读：``X-DSH-Size`` 必须是 **JPEG 自身的像素尺寸**（客户端按它分配画布），
    而 import / grim / win32 这条路上我们只拿到字节；scale<1 时它跟屏幕尺寸也不一样。
    """
    if not data or data[0] != 0xFF or data[1] != 0xD8:
        return None
    i, n = 2, len(data)
    while i + 9 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seglen = int.from_bytes(data[i + 2:i + 4], "big")
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB,
                      0xCD, 0xCE, 0xCF):
            return (int.from_bytes(data[i + 7:i + 9], "big"),
                    int.from_bytes(data[i + 5:i + 7], "big"))
        if seglen <= 0:
            return None
        i += 2 + seglen
    return None


def mjpeg_part(frame: bytes, seq: int, time_ms: int, size: str, cursor) -> bytes:
    """拼一个 MJPEG part（契约 §5.3 的逐帧头）。

    逐帧头**必须**在这里带上：宿主半边是把上游字节原样透传给浏览器的，
    没有别的地方能补这些信息。所以这四行少一个，客户端就少一样东西：

    * ``X-DSH-Seq``    单调递增帧号（客户端据此丢中间帧、算源帧率）；
    * ``X-DSH-Time``   **抓帧时刻**的 epoch 毫秒（算真实内容 fps 与变化延迟都靠它）；
    * ``X-DSH-Size``   这一帧 JPEG 的像素尺寸（scale<1 时与屏幕尺寸不同）；
    * ``X-DSH-Cursor`` 归一化的指针位置；拿不到指针时是 ``-1,-1``（客户端据此不画光标）。
    """
    if cursor:
        pos = f"{cursor['x']:.4f},{cursor['y']:.4f}"
    else:
        pos = "-1,-1"
    head = ("--frame\r\n"
            "Content-Type: image/jpeg\r\n"
            f"Content-Length: {len(frame)}\r\n"
            f"X-DSH-Seq: {int(seq)}\r\n"
            f"X-DSH-Time: {int(time_ms)}\r\n"
            f"X-DSH-Size: {size}\r\n"
            f"X-DSH-Cursor: {pos}\r\n"
            "\r\n").encode("ascii")
    return head + bytes(frame) + b"\r\n"


class EncoderGone(RuntimeError):
    """常驻编码进程不可用了（退出/管道断了/超时）—— 调用方重启它。"""


class FfmpegEncoder:
    """常驻编码进程：``rawvideo(BGRA)`` → ``mjpeg``（一路喂帧、一路取 JPEG）。

    为什么常驻：0.3.4 每帧 spawn 一次 ``import -window root``（45ms/帧，而且它自己
    还要再抓一遍 X）；常驻 ffmpeg 稳态 16-22ms/帧，首帧 100ms。

    ⚠️ **必须 ``-threads 1``**：帧级多线程（默认按核数）会把**头几帧憋在内部缓冲里**，
    实测连喂 3 帧后最坏 3 秒才吐第一张 JPEG（帧线程要攒够线程数才出帧，我的读循环
    每次都在 3 秒 select 超时里等到 0 字节）。谁要把它"优化"成多线程，
    用户看到的首帧就会慢到 1-3 秒 —— 这是踩过的坑，别踩第二遍。

    编码进程可能死（被 OOM、被误杀、管道断了）：所有 IO 失败都抛 ``EncoderGone``，
    由管线重启；重启仍不行就整体退回 import（慢但能用）。
    """

    def __init__(self, size, quality, scale, log_path: str) -> None:
        self.size = (int(size[0]), int(size[1]))
        self.quality = int(quality)
        self.scale = float(scale)
        self.log_path = log_path
        self.proc = None
        self.last_ms = 0.0
        #: 管线要收手时置上：编码读循环按 0.2 秒切片检查它，能立刻退出 ——
        #: 否则 Session.stop() 的 join 会等满 ENCODE_TIMEOUT，回收线程只能硬来。
        self.stop_event = threading.Event()
        self.out_w, self.out_h = self._out_size()

    def _out_size(self):
        w, h = self.size
        if self.scale >= 0.999:
            return w, h
        return max(2, int(round(w * self.scale)) // 2 * 2), max(2, int(round(h * self.scale)) // 2 * 2)

    @property
    def key(self):
        return (self.size, self.quality, round(self.scale, 3))

    @staticmethod
    def available() -> bool:
        return shutil.which("ffmpeg") is not None

    def _cmd(self):
        w, h = self.size
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
               "-f", "rawvideo", "-pix_fmt", "bgra", "-video_size", f"{w}x{h}", "-i", "-"]
        if (self.out_w, self.out_h) != (w, h):
            cmd += ["-vf", f"scale={self.out_w}:{self.out_h}:flags=bilinear"]
        cmd += ["-f", "mjpeg", "-q:v", str(quality_to_qv(self.quality)), "-threads", "1", "-"]
        return cmd

    def start(self) -> bool:
        if not self.available():
            return False
        try:
            log = open(self.log_path, "ab")
        except OSError:
            log = subprocess.DEVNULL
        try:
            self.proc = subprocess.Popen(self._cmd(), stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=log,
                                         bufsize=0, start_new_session=True)
        except Exception as exc:                     # noqa: BLE001
            print(f"⚠ ffmpeg 起不来：{type(exc).__name__}: {exc}", flush=True)
            return False
        finally:
            if log is not subprocess.DEVNULL:
                try:
                    log.close()
                except OSError:
                    pass
        _register_spawn(self.proc)                   # 服务退出时一并收掉
        return True

    def encode(self, raw) -> bytes:
        """喂一帧原始像素，取回一张 JPEG；失败抛 :class:`EncoderGone`。"""
        proc = self.proc
        if proc is None or proc.poll() is not None:
            raise EncoderGone(self._why("编码进程已经退出"))
        t0 = time.perf_counter()
        try:
            # bufsize=0：bytearray 直接写进管道，不再多拷一份 6.4MB
            proc.stdin.write(raw)
        except (BrokenPipeError, ConnectionResetError, OSError, ValueError) as exc:
            raise EncoderGone(self._why(f"写入失败 {type(exc).__name__}")) from exc
        jpeg = self._read_jpeg()
        self.last_ms = (time.perf_counter() - t0) * 1000.0
        return jpeg

    def _read_jpeg(self) -> bytes:
        fd = self.proc.stdout.fileno()
        data = bytearray()
        deadline = time.monotonic() + ENCODE_TIMEOUT
        while True:
            if len(data) >= 4 and data[-2:] == b"\xff\xd9":
                return bytes(data)
            if self.stop_event.is_set():
                raise EncoderGone("管线正在收手")
            left = min(deadline - time.monotonic(), 0.2)
            if left <= 0:
                if time.monotonic() >= deadline:
                    raise EncoderGone(self._why("取一帧 JPEG 超时"))
                continue
            try:
                ready, _w, _x = select.select([fd], [], [], left)
            except (OSError, ValueError) as exc:
                raise EncoderGone(self._why(f"select 失败 {type(exc).__name__}")) from exc
            if not ready:
                continue
            try:
                chunk = os.read(fd, 262144)
            except OSError as exc:
                raise EncoderGone(self._why(f"读取失败 {type(exc).__name__}")) from exc
            if not chunk:
                raise EncoderGone(self._why("编码进程关掉了输出"))
            data += chunk

    def _why(self, what: str) -> str:
        tail = ""
        try:
            with open(self.log_path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - 400))
                tail = fh.read().decode("utf-8", "replace").strip().splitlines()[-1][:200]
        except (OSError, IndexError):
            tail = ""
        return f"{what}（ffmpeg {self.size[0]}x{self.size[1]} q={self.quality}）" + \
               (f"：{tail}" if tail else "")

    def stop(self) -> None:
        self.stop_event.set()                        # 让正在读 JPEG 的循环立刻退出
        proc, self.proc = self.proc, None
        if proc is None:
            return
        _unregister_spawn(proc)
        _terminate_proc(proc, timeout=1.5)


def encode_once(raw, size, quality, scale, timeout: float = 8.0):
    """一次性编码一帧（``/snapshot?quality=&scale=`` 用）；失败返回 None。

    刻意**不**复用常驻编码器：流的档位是给 /stream 用的，中途换档要重启进程，
    一次 AI 截图就会把流的节奏打断（契约 §5.3 的流畅度不该被截图影响）。
    代价是每次 ~60-100ms（起进程 + 编一帧），而截图本来就是低频操作。
    """
    if not FfmpegEncoder.available():
        return None
    w, h = int(size[0]), int(size[1])
    scale = float(scale)
    ow = max(2, int(round(w * scale)) // 2 * 2)
    oh = max(2, int(round(h * scale)) // 2 * 2)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
           "-f", "rawvideo", "-pix_fmt", "bgra", "-video_size", f"{w}x{h}", "-i", "-"]
    if (ow, oh) != (w, h):
        cmd += ["-vf", f"scale={ow}:{oh}:flags=bilinear"]
    cmd += ["-f", "mjpeg", "-q:v", str(quality_to_qv(quality)), "-threads", "1",
            "-frames:v", "1", "-"]
    try:
        proc = subprocess.run(cmd, input=raw, capture_output=True, timeout=timeout)
    except Exception:                                # noqa: BLE001
        return None
    return proc.stdout if proc.returncode == 0 and proc.stdout[:2] == b"\xff\xd8" else None


def recode_jpeg(jpeg: bytes, quality, scale):
    """把已有 JPEG 改质量/缩放（import 路线给 /snapshot 用）；失败返回 None。"""
    if not jpeg:
        return None
    args = []
    if scale and abs(float(scale) - 1.0) > 0.001:
        args += ["-vf", f"scale=iw*{float(scale)}:ih*{float(scale)}:flags=bilinear"]
    if FfmpegEncoder.available():
        cmd = (["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-f", "mjpeg",
                "-i", "-"] + args + ["-f", "mjpeg", "-q:v", str(quality_to_qv(quality)),
                                     "-threads", "1", "-frames:v", "1", "-"])
        try:
            proc = subprocess.run(cmd, input=jpeg, capture_output=True, timeout=8)
            if proc.returncode == 0 and proc.stdout[:2] == b"\xff\xd8":
                return proc.stdout
        except Exception:                            # noqa: BLE001
            pass
    if shutil.which("convert") is not None:
        cmd = ["convert", "-"]
        if scale and abs(float(scale) - 1.0) > 0.001:
            cmd += ["-resize", f"{max(1, int(round(float(scale) * 100)))}%"]
        cmd += ["-quality", str(int(quality)), "JPEG:-"]
        try:
            proc = subprocess.run(cmd, input=jpeg, capture_output=True, timeout=8)
            if proc.returncode == 0:
                return proc.stdout
        except Exception:                            # noqa: BLE001
            pass
    return None


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


def parse_stream_config(params, current) -> tuple:
    """校验 ``/stream-config`` 的参数 → ``(新档, 错误原因)``。

    * 只认 ``quality`` / ``fps`` / ``scale`` 三个键，**其余键一律忽略**：
      客户端的 ``profile`` / ``reason``、查询串里的 ``k=``（令牌）都会从这里路过，
      把未知键当错误会让"顺手打个标记"的调用直接 400。
    * 非法值（0 / 999 / abc / null / 布尔）→ 错误 → 调用方回 400。
      **不做"静默夹到边界"**：那会让调用方以为改成功了，而实际档位完全不是他要的。
    """
    cfg = dict(current or {"quality": DEFAULT_QUALITY, "fps": DEFAULT_FPS,
                           "scale": DEFAULT_SCALE})
    if params is None:
        return cfg, ""
    if not isinstance(params, dict):
        return None, "body 必须是 JSON 对象"
    for key, caster, lo, hi in (("quality", _cfg_int, MIN_QUALITY, MAX_QUALITY),
                                ("fps", _cfg_int, MIN_FPS, MAX_FPS),
                                ("scale", _cfg_float, MIN_SCALE, MAX_SCALE)):
        if key not in params or params[key] is None:
            continue
        value = caster(params[key], lo, hi)
        if value is None:
            return None, f"{key} 非法：期望 {lo}..{hi}，收到 {params[key]!r}"
        cfg[key] = value
    return cfg, ""


def _cfg_int(value, lo, hi):
    num = _cfg_num(value)
    if num is None or num != int(num) or not lo <= num <= hi:
        return None
    return int(num)


def _cfg_float(value, lo, hi):
    num = _cfg_num(value)
    if num is None or not lo <= num <= hi:
        return None
    return round(float(num), 3)


def _cfg_num(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if num != num or num in (float("inf"), float("-inf")):
        return None
    return num


class Pipeline:
    """一个会话的帧管线：抓帧 → 去重 → 编码 → 广播（契约 §5.2/§5.3）。

        X11Screen.grab() ──BGRA──▶ FrameDedup ──变了──▶ FfmpegEncoder ──JPEG──▶ publish
                                     │                                            │
                                     └─ 没变：skipped++，只可能发"仅光标"帧        └─▶ cond.notify_all()
                                                                                     （每个 /stream 客户端一条线程）

    线程模型：**一个会话一条管线线程**（它就是唯一碰 X11 连接的线程），
    /stream 的每个客户端各自一条 HTTP 线程，靠 ``cond`` 广播拿"最新一帧"。
    客户端慢就自然丢中间帧（永远只取最新），不会拖住管线。
    """

    def __init__(self, sess: Session) -> None:
        self.sess = sess
        self.cond = threading.Condition()
        self.config = {"quality": DEFAULT_QUALITY, "fps": DEFAULT_FPS, "scale": DEFAULT_SCALE}
        #: 自适应降档只写这里（**只降不升用户设的档**，所以 POST 的目标档不会被悄悄改掉）
        self.auto = {"quality": None, "fps": None, "scale": None}
        self.reason = ""
        self.encoder = None
        self.screen = None
        self.dedup = FrameDedup()
        self.seq = 0
        self.frame = b""
        self.frame_meta = {"seq": 0, "time": 0, "size": f"{W}x{H}", "cursor": None}
        self.content_time_ms = 0
        self.content_size = f"{W}x{H}"
        self.cursor = None
        self.cursor_at = 0.0
        self.next_cursor = 0.0
        self.cursor_period = 1.0 / CURSOR_FPS           # 源定了之后按源调整（见 _mode）
        self.clients = 0
        self.mode = "idle"
        self.idle_fps = IDLE_FPS_SLOW
        self.captured = self.encoded = self.skipped = self.cursor_only = 0
        self.capture_ms = self.encode_ms = 0.0
        #: 最近若干帧的耗时（用来暴露"平均很好看、偶尔卡 400ms"的情况）
        self.recent_cap = deque(maxlen=32)
        self.recent_enc = deque(maxlen=32)
        self.crc = 0
        self.sent = deque()                          # 出帧时刻（fps 窗口）
        self.rate = deque()                          # 出帧时刻（自适应判据，2s 窗口）
        self.sent_bytes = deque()                    # (时刻, 字节数)（bytesPerSec 窗口）
        self.damage_times = deque()                  # damage 事件时刻（damageEvents 窗口）
        self.last_sent_at = 0.0
        self.last_change = 0.0
        self.last_capture_at = 0.0
        self.last_watch = time.time()                # 最近一次"有人在看"
        self.last_cursor_only = 0.0
        self.next_capture = 0.0
        self.last_adapt = 0.0
        self.calm_since = 0.0
        self.over_strikes = 0
        self.idle = True
        self._damage_seen = 0
        self._warm = 0
        self._no_encoder = False
        self._stop = threading.Event()
        # 自管道：用来**立刻**打断管线线程里最长 1 秒的等待（改档/回收/新客户端）。
        # ⚠️ 两头都必须非阻塞：读端如果是阻塞的，`while os.read(...)` 会在管道空了之后
        #    永久阻塞在第二次读上（实测：管线抓完第一帧就再也不动了，而日志里什么都看不到）。
        self._rfd, self._wfd = os.pipe()
        os.set_blocking(self._rfd, False)
        os.set_blocking(self._wfd, False)
        self.thread = None
        self._closed = False

    # ---------------------------------------------------------------- 生命周期
    def start(self) -> bool:
        """起（或重起）管线线程。**同一个对象**：Seq、订阅者、档位都不受影响。"""
        self.revive()
        return self.thread is not None

    def revive(self) -> None:
        """线程停住了（多半是 park 在 Xlib 的 IO 错误处理器里）就再起一条。

        Xvfb 被回收/被别的实例顶掉时这是唯一的自愈路径：旧线程永远停在 Xlib 里，
        而服务不能因此把这个会话变成"永远黑屏"。**千万不要**在重起前 XCloseDisplay
        那条旧连接 —— 对新线程来说它是"死连接上的调用"，会把新线程也 park 掉。
        """
        if self._closed:
            return
        if self.thread is not None and self.thread.is_alive():
            return
        if self.screen is not None:
            self.screen = None                       # 旧连接不要了（见上面那条注释）
            self.dedup.reset()                       # 新连接的首帧一定要发出去
        self.thread = threading.Thread(target=_grab_loop, args=(self.sess,), daemon=True,
                                       name=f"grab-{self.sess.sid}")
        self.thread.start()

    def stop(self, timeout: float = 1.5) -> None:
        """让管线收手（会话回收路径）。**必须在杀 Xvfb 之前调**：
        进程内抓帧正在跑的时候把 Xvfb 杀掉 = Xlib IO 错误 = 抓帧线程就地停住。
        """
        self._closed = True
        self._stop.set()
        self._notify()
        thread = self.thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self._drop_encoder(None)
        self._close_pipe()

    def _close_pipe(self) -> None:
        for fd in (self._rfd, self._wfd):
            try:
                if fd is not None and fd >= 0:
                    os.close(fd)
            except OSError:
                pass
        self._rfd = self._wfd = -1

    def _notify(self) -> None:
        """打断管线线程的等待（改档 / 回收 / 有新客户端）。"""
        fd = self._wfd
        if fd is None or fd < 0:
            return
        try:
            os.write(fd, b"\x01")
        except (OSError, ValueError):
            pass

    def _drain_notify(self) -> None:
        """排空自管道（非阻塞读：管道空时立刻返回，绝不在这里等）。"""
        fd = self._rfd
        if fd is None or fd < 0:
            return
        try:
            while os.read(fd, 64):
                pass
        except (BlockingIOError, OSError, ValueError):
            pass

    # ---------------------------------------------------------------- 对外接口
    def watch(self) -> None:
        """记一笔"有人在看"（/stream 订阅、/stats、/snapshot、/state）。

        没人看时（也没有流客户端）画面再动也不按目标 fps 跑：一个没人看的会话
        不该把 CPU 烧在编码上；`/stats` 一被采样就立刻恢复——所以观测口径不会因此变低。
        """
        self.last_watch = time.time()

    def watched(self) -> bool:
        return self.clients > 0 or (time.time() - self.last_watch) < 5.0

    def subscribe(self) -> None:
        with self.cond:
            self.clients += 1
        self.watch()
        self._notify()

    def unsubscribe(self) -> None:
        with self.cond:
            self.clients = max(0, self.clients - 1)

    def set_config(self, cfg: dict) -> dict:
        """用户改档：清掉自适应降档（新档位重新评估），立刻生效。"""
        with self.cond:
            self.config = dict(cfg)
            for key in self.auto:
                self.auto[key] = None
            self.reason = ""
            self.last_adapt = time.monotonic()       # 给新档一个冷却期再评判
            self.calm_since = 0.0
            self.over_strikes = 0
            self.cond.notify_all()
        self._notify()
        return dict(self.config)

    def effective_config(self) -> dict:
        cfg = dict(self.config)
        for key in ("quality", "fps", "scale"):
            auto = self.auto[key]
            if auto is not None:
                cfg[key] = min(cfg[key], auto)
        return cfg

    def wait_frame(self, last_seq: int, timeout: float = 0.5):
        """等一帧（阻塞 ≤timeout）→ ``(jpeg, meta, seq)``；没有新帧时 jpeg 为 None。

        新客户端（last_seq=0）会**立刻**拿到当前最新一帧，然后才是后续增量。
        """
        with self.cond:
            if self.seq <= last_seq:
                self.cond.wait(timeout)
            if self.seq > last_seq and self.frame:
                return self.frame, dict(self.frame_meta), self.seq
            return None, None, last_seq

    def note_sent(self, nbytes: int) -> None:
        self.sent_bytes.append((time.time(), int(nbytes)))
        while len(self.sent_bytes) > 4096:
            self.sent_bytes.popleft()

    def cached_cursor(self, max_age: float = 1.0):
        """缓存里的归一化指针位置（管线每轮都刷新）；太旧返回 None。"""
        if self.cursor is not None and (time.time() - self.cursor_at) <= max_age:
            return dict(self.cursor)
        return None

    def stats(self) -> dict:
        """``/stats`` 的主体（契约 §5.5 + 0.4.0 扩展字段）。"""
        now = time.time()
        while self.sent and now - self.sent[0] > STATS_WINDOW:
            self.sent.popleft()
        while self.sent_bytes and now - self.sent_bytes[0][0] > STATS_WINDOW:
            self.sent_bytes.popleft()
        while self.damage_times and now - self.damage_times[0] > DAMAGE_WINDOW:
            self.damage_times.popleft()
        cfg = self.effective_config()
        fps = round(len(self.sent) / STATS_WINDOW, 2)
        byps = round(sum(n for _t, n in self.sent_bytes) / STATS_WINDOW, 1)
        return {
            "fps": fps,
            # fpsCap 是**控制器压着的上限**（客户端设的档经过自适应降档之后），
            # fpsActual 是实测出来的：裁判脚本靠这两个数区分"管线达不到"与"控制器压着"。
            "fpsCap": cfg["fps"],
            "fpsActual": fps,
            "userFps": self.config["fps"],
            "idle": bool(self.idle),
            "captured": self.captured,
            "encoded": self.encoded,
            "skipped": self.skipped,
            "bytesPerSec": byps,
            "quality": cfg["quality"],
            "scale": cfg["scale"],
            "mode": self.mode,
            "captureMs": round(self.capture_ms, 2),
            "encodeMs": round(self.encode_ms, 2),
            "captureMaxMs": round(max(self.recent_cap), 1) if self.recent_cap else 0.0,
            "encodeMaxMs": round(max(self.recent_enc), 1) if self.recent_enc else 0.0,
            "cursor": dict(self.cursor) if self.cursor else None,
            # reason 只在空闲时补一句"空闲（无变化）"：不能把"没有帧"写成"跟不上"，
            # 那正是旧版在静止时把自己降档、用户一动只剩 9fps 的原因。
            "reason": self.reason_text(),
            # ---- 0.4.0 扩展（perf 脚本与客户端状态条会用；契约 §5.5 的字段一个不少）
            "encoder": self.encoder_name(),
            "cursorOnly": self.cursor_only,
            "clients": self.clients,
            "seq": self.seq,
            "crc": self.crc,
            "damageEvents": len(self.damage_times),
            "lastFrameAgeMs": (int((now - self.last_sent_at) * 1000)
                               if self.last_sent_at else None),
            "targetFps": cfg["fps"],
            "idleFps": self.idle_fps,
            "damageAgoMs": (int((time.time() - self.damage_times[-1]) * 1000)
                            if self.damage_times else None),
            "costMs": round(self.capture_ms + self.encode_ms, 2),
            "stream": {"fps": fps, "fpsCap": cfg["fps"], "quality": cfg["quality"],
                       "scale": cfg["scale"], "mode": self.mode},
        }

    def reason_text(self) -> str:
        """``/stats.reason``：自适应说明；空闲时明说"空闲（无变化）"。"""
        if not self.idle:
            return self.reason
        if self.reason and any(self.auto.values()):
            return f"{self.reason}（当前空闲/无变化）"
        return "空闲（无变化）"

    def encoder_name(self) -> str:
        if self.encoder is not None:
            return "ffmpeg"
        if BACKEND in ("win32", "darwin"):
            return "in-process"
        if BACKEND == "wayland":
            return "grim"
        return "import"

    def encode_shot(self, quality: float, scale: float):
        """单独编一帧给 ``/snapshot``（自己的 X 连接 + 一次性编码进程）。"""
        quality = int(quality)
        scale = float(scale)
        if BACKEND == "x11" and self._no_encoder is False:
            scr = X11Screen(self.sess.display, self.sess.sid)
            try:
                if scr.open():
                    raw = scr.grab()
                    if raw is not None:
                        jpeg = encode_once(raw, (scr.width, scr.height), quality, scale)
                        if jpeg:
                            return jpeg
            except Exception:                        # noqa: BLE001
                pass
            finally:
                scr.close()
        try:
            jpeg = _grab_once(self.sess)
        except Exception:                            # noqa: BLE001
            return b""
        cfg = self.effective_config()
        if (quality, scale) == (int(cfg["quality"]), float(cfg["scale"])):
            return jpeg
        return recode_jpeg(jpeg, quality, scale) or jpeg

    # ---------------------------------------------------------------- 线程主体
    def _is_idle(self) -> bool:
        """最近有没有内容变化（空闲 = 没有帧可出，**不等于**处理不过来）。"""
        return (time.monotonic() - self.last_change) > IDLE_GATE

    def _pump(self) -> None:
        """一轮：等变化 → 抓帧 → 去重 → 编码 → 广播（异常交给 run() 兜）。

        ⚠️ **先判"到点没有"，再干别的**：XDamage 每秒能醒 60 次，如果每次都顺手做一次
        XQueryPointer（0.3-3ms 往返），抓帧的节拍就会被一路往后推 —— 实测只有 11fps
        （目标 15fps，而每帧实际只要 25ms）。所以指针刷新自己按 CURSOR_FPS 节流。
        """
        sess = self.sess
        cfg = self.effective_config()
        now = time.monotonic()
        if now < self.next_capture:
            # 还没到出帧的点：等 damage / 通知（也会按指针的节拍醒来）
            hit = self._wake(min(self.next_capture - now, self._cursor_wait()))
            self._cursor_tick()
            self._cursor_frame()
            if hit and self._is_idle():
                # 空闲巡检（1fps）中来了变化：**立刻**抓这一帧，别等巡检节拍 ——
                # 否则"上一秒还很安静、这一刻刚动"的画面最多要等 1 秒才出去。
                self.next_capture = 0.0
            return
        self._ensure_sources(cfg)
        cfg = self.effective_config()                # _ensure_sources 可能改了档（没 ffmpeg）
        idle = self._is_idle()
        if self.idle and not idle:
            # 空闲结束：上一段空闲里的耗时样本（尤其是编码进程冷启动那一帧）
            # 不该拿来评判"现在跟不跟得上" —— 清了重测，第一帧就按原上限出。
            self.capture_ms = self.encode_ms = 0.0
            self.over_strikes = 0
            self.calm_since = 0.0
            self.last_adapt = time.monotonic()
        self.idle = idle
        epoch_ms = int(time.time() * 1000)
        t0 = time.perf_counter()
        self.last_capture_at = time.monotonic()
        data, kind = self._capture()
        cap_ms = (time.perf_counter() - t0) * 1000.0
        self.captured += 1
        changed, crc = self.dedup.check(data)
        self.crc = crc
        enc_ms = 0.0
        if not changed:
            # 内容没变：不编码、不发帧（静止带宽趋近 0）
            self.skipped += 1
        else:
            jpeg, enc_ms, size = self._encode(data, kind, cfg)
            if jpeg:
                self.last_change = time.monotonic()
                self._publish(jpeg, epoch_ms, size, cursor_only=False)
        # α 取小一点：单帧抖动不该在 EWMA 里留很久（自适应另有一道 strikes 防线）。
        # _warm > 0 的帧（编码进程刚起/刚重启）不采样：那一帧带着进程冷启动。
        if self._warm <= 0:
            self.capture_ms = _ewma(self.capture_ms, cap_ms, 0.2)
            self.recent_cap.append(cap_ms)
            if enc_ms:
                self.encode_ms = _ewma(self.encode_ms, enc_ms, 0.2)
                self.recent_enc.append(enc_ms)
        self._cursor_tick()
        self._cursor_frame()
        self._schedule(cfg)
        if not idle:
            # 空闲窗口不参与自适应（没有帧 ≠ 处理不过来）。
            # 旧行为就是在静止时把自己一路降档，等用户真动起来只剩 9fps。
            self._adapt(cfg, cap_ms, enc_ms)

    def run(self) -> None:
        sess = self.sess
        fails = 0
        print(f"[{sess.sid}] 帧管线启动（抓帧优先 XGetImage+XDamage，编码优先常驻 ffmpeg）",
              flush=True)
        try:
            while not sess.closed and not self._stop.is_set():
                try:
                    if not sess.ensure():
                        self._fail("显示服务器没起来")
                        if self._stop.wait(1.0):
                            break
                        continue
                    self._pump()
                    fails = 0
                except Exception as exc:             # noqa: BLE001
                    fails += 1
                    self._fail(f"{type(exc).__name__}: {exc}")
                    self._drop_screen("抓帧失败")
                    if self._stop.wait(min(GRAB_FAIL_MAX_BACKOFF, 0.2 * fails)):
                        break
        finally:
            self._drop_encoder(None)
            self._drop_screen(None)
            self._close_pipe()

    def _fail(self, note: str) -> None:
        """抓帧失败**必须可见**（/state 的 frameError、日志），不再静默。"""
        with self.sess.lock:
            self.sess.frame_error = note
        if note != getattr(self, "_last_fail", None):
            self._last_fail = note
            print(f"[{self.sess.sid}] 抓帧失败：{note}（缺 import/grim？）", flush=True)

    # ---------------------------------------------------------------- 源与编码器
    def _ensure_sources(self, cfg) -> None:
        """让"抓帧源 + 编码器"处于一致状态：raw 抓帧必须配 ffmpeg，否则整体退回 import。"""
        self._ensure_screen()
        if self.screen is not None:
            self._ensure_encoder(cfg)
        self._mode()

    def _ensure_screen(self) -> None:
        if BACKEND != "x11" or self._no_encoder:
            return
        sess = self.sess
        scr = self.screen
        if scr is not None and scr.display != sess.display:
            self._drop_screen("显示号变了")
            scr = None
        if scr is not None:
            return
        scr = X11Screen(sess.display, sess.sid)
        if scr.open():
            self.screen = scr
            self._damage_seen = 0
            # 空抓一帧：既把 bits_per_pixel / bytes_per_line 学到手（深屏解析要看它），
            # 也顺便验证"这台显示的像素格式我们认得出"，认不出就当场退回 import。
            warm = scr.grab()
            if warm is None:
                self._drop_screen(f"XGetImage 回来的像素格式认不出"
                                  f"（depth={scr.depth} bpp={scr.bits_per_pixel}）"
                                  f"→ 抓帧退回 import")
                return
            print(f"[{sess.sid}] 抓帧：进程内 XGetImage {scr.width}x{scr.height} "
                  f"depth={scr.depth} bpp={scr.bits_per_pixel} "
                  f"行跨距={scr.bytes_per_line}"
                  + ("+XDamage 事件驱动" if scr.damage else "+定时轮询（没有 XDamage）"),
                  flush=True)
        else:
            self.screen = None
            print(f"[{sess.sid}] 抓帧退回 import（libX11 用不了；每帧一个进程，慢但能用）",
                  flush=True)

    def _ensure_encoder(self, cfg) -> None:
        size = (self.screen.width, self.screen.height)
        key = (size, int(cfg["quality"]), round(float(cfg["scale"]), 3))
        enc = self.encoder
        if enc is not None and enc.key == key:
            return
        if enc is not None:
            self._drop_encoder(f"档位变了（{enc.quality}→{cfg['quality']}、"
                               f"{enc.scale}→{cfg['scale']}）")
        enc = FfmpegEncoder(size, cfg["quality"], cfg["scale"],
                            os.path.join(self.sess.dir, "ffmpeg.log"))
        if enc.start():
            self.encoder = enc
            # 头一两帧带着进程冷启动（实测首帧 60-100ms，稳态 20ms 上下）：
            # 既不算进自适应判据，也把旧样本清掉 —— 否则"冷启动→误判跟不上→降档→
            # 又重启编码器→又冷启动"会自己转成一个死循环（实测真的转过）。
            self._warm = 2
            self.capture_ms = self.encode_ms = 0.0
        else:
            self._no_encoder = True
            self._drop_screen("没装 ffmpeg：抓帧退回 import（每帧一个进程，慢但能用）")

    def _drop_screen(self, note) -> None:
        scr, self.screen = self.screen, None
        if scr is not None:
            if self._display_alive():
                scr.close()
            else:
                # 那头已经没了（Xvfb 被回收/被顶掉）：XCloseDisplay 会走 Xlib 的 IO
                # 错误处理器把**本线程** park 住，宁可漏一个 fd（罕见），
                # 也不要把抓帧线程赔进去 —— 留着的连接由 revive() 另起线程接管。
                print(f"[{self.sess.sid}] 显示已经没了：放弃这条 X 连接（不 close，免得"
                      f"抓帧线程卡在 Xlib 里）", flush=True)
        if note:
            print(f"[{self.sess.sid}] {note}", flush=True)

    def _display_alive(self) -> bool:
        """显示还活着吗（**不碰 Xlib**，所以在"连接可能已断"时也能安全调用）。"""
        proc = self.sess._server_proc
        if proc is not None:
            return proc.poll() is None
        return True                                  # 孤儿显示：没有进程可查，当作活着

    def _drop_encoder(self, note) -> None:
        enc, self.encoder = self.encoder, None
        if enc is not None:
            enc.stop()
        if note:
            print(f"[{self.sess.sid}] {note}", flush=True)

    def _mode(self) -> None:
        """``/stats.mode``：一眼看出走的哪条路（perf 脚本按它断言）。"""
        if self.screen is not None:
            self.mode = "xgetimage+XDamage" if self.screen.damage else "xgetimage+poll"
        elif BACKEND == "x11":
            self.mode = "import"
        elif BACKEND == "wayland":
            self.mode = "grim"
        else:
            self.mode = BACKEND
        if self.screen is None:
            self.idle_fps = IDLE_FPS_SLOW
            # 没有进程内指针（import/grim/win32/darwin）：xdotool 每次都要 fork/exec，
            # 别按 5Hz 去问，1 秒一次足够（页面自己还会按需问 /state）。
            self.cursor_period = 1.0
        elif self.screen.damage:
            self.idle_fps = max(1, int(round(1.0 / IDLE_TICK)))
            self.cursor_period = 1.0 / CURSOR_FPS
        else:
            self.idle_fps = IDLE_FPS
            self.cursor_period = 1.0 / CURSOR_FPS

    def _capture(self):
        """抓一帧 → ``(data, kind)``：kind=``raw``（BGRA，要编码）/``jpeg``（已经是 JPEG）。"""
        scr = self.screen
        if scr is not None:
            buf = scr.grab()
            if buf is None:
                raise RuntimeError("XGetImage 交不出这一帧（像素格式认不出？）")
            return buf, "raw"
        return _grab_once(self.sess), "jpeg"

    def _encode(self, data, kind, cfg):
        """编码一帧 → ``(jpeg, encodeMs, "WxH")``。"""
        if kind == "jpeg":
            size = jpeg_size(data)
            return data, 0.0, (f"{size[0]}x{size[1]}" if size else self.content_size)
        enc = self.encoder
        if enc is None:
            # 走到这里说明 ffmpeg 刚刚失效：这一帧用 import 顶上，下一轮整体退回 import
            jpeg = _grab_once(self.sess)
            size = jpeg_size(jpeg)
            return jpeg, 0.0, (f"{size[0]}x{size[1]}" if size else f"{W}x{H}")
        try:
            jpeg = enc.encode(data)
        except EncoderGone as exc:
            self._drop_encoder(f"编码进程异常（{exc}）→ 重启")
            self._no_encoder = not FfmpegEncoder.available()
            jpeg = _grab_once(self.sess)             # 这一帧先用 import 顶上
            size = jpeg_size(jpeg)
            return jpeg, 0.0, (f"{size[0]}x{size[1]}" if size else f"{W}x{H}")
        self.encoded += 1
        return jpeg, enc.last_ms, f"{enc.out_w}x{enc.out_h}"

    # ---------------------------------------------------------------- 出帧
    def _wake(self, timeout: float) -> bool:
        """睡到"有事发生"：damage 到达 / 改档 / 回收 / 超时。

        返回"这次醒来是不是因为画面真的变了"（没有 XDamage 的轮询路线上恒为 False：
        它靠定时抓帧发现变化，所以空转时也按 IDLE_FPS 的节拍抓）。
        """
        if timeout <= 0:
            self._drain_notify()
            return False
        if self._stop.is_set():
            return False
        hit = False
        scr = self.screen
        if scr is not None:
            hit = bool(scr.wait(timeout, extra=(self._rfd,)))
            self._count_damage(scr)
        else:
            self._stop.wait(timeout)
        self._drain_notify()
        return hit

    def _count_damage(self, scr: X11Screen) -> None:
        total = scr.damage_events
        if total > self._damage_seen:
            now = time.time()
            for _ in range(min(total - self._damage_seen, 4096)):
                self.damage_times.append(now)
            self._damage_seen = total

    def _read_cursor(self):
        """归一化指针位置（0..1）；拿不到返回 None。"""
        scr = self.screen
        if scr is None:
            return cursor_pos(self.sess)             # import/grim/win32/darwin：走老路
        pos = scr.pointer()
        if pos is None:
            return self.cursor
        x, y = pos
        return {"x": round(min(1.0, max(0.0, x / max(1, scr.width))), 4),
                "y": round(min(1.0, max(0.0, y / max(1, scr.height))), 4)}

    def _cursor_wait(self) -> float:
        """距下一次指针刷新还有多久（用来决定等待时长）。"""
        return max(0.0, self.next_cursor - time.monotonic())

    def _cursor_tick(self) -> None:
        """刷新指针位置（按 ``cursor_period`` 节流）。

        进程内 XQueryPointer 是往返调用：跟着 damage 的 60 次/秒一起做会很贵；
        xdotool 那条回退路更贵（每次 fork/exec），所以它单独用 1 秒的节拍。
        """
        now = time.monotonic()
        if now < self.next_cursor:
            return
        cur = self._read_cursor()
        self.next_cursor = now + self.cursor_period
        if cur is not None:
            self.cursor, self.cursor_at = cur, time.time()

    def _cursor_frame(self) -> None:
        """画面没变、但指针动了：重发**缓存的那张 JPEG**（不重新编码），Seq 递增。

        为什么不干脆不发：游标位置是随帧下发的（契约 §5.3），不发就等于指针冻住。
        为什么复用缓存：重编一张一模一样的图纯属浪费 CPU（静止时那点带宽是可接受的代价，
        而且只在指针真的动时才发，上限 CURSOR_FPS）。
        """
        cur = self.cursor
        prev = self.frame_meta.get("cursor")
        if cur is None or not self.frame or prev is None:
            if cur is not None:
                self.frame_meta["cursor"] = cur
            return
        if cur["x"] == prev["x"] and cur["y"] == prev["y"]:
            return
        now = time.time()
        if now - self.last_cursor_only < 1.0 / max(1, CURSOR_FPS):
            return
        self.last_cursor_only = now
        self.cursor_only += 1
        self._publish(self.frame, self.content_time_ms, self.content_size,
                      cursor_only=True, cursor=cur)

    def _publish(self, jpeg: bytes, time_ms: int, size: str, cursor_only: bool = False,
                 cursor=None) -> None:
        """把一帧交给所有 /stream 客户端（广播：每个客户端线程各拿一份最新帧）。"""
        sess = self.sess
        if not cursor_only:
            self.content_time_ms = int(time_ms)
            self.content_size = size
        if cursor is None:
            cursor = self.cursor
        now = time.time()
        with self.cond:
            self.seq += 1
            self.frame = jpeg
            self.frame_meta = {"seq": self.seq, "time": int(self.content_time_ms),
                               "size": self.content_size,
                               "cursor": dict(cursor) if cursor else None}
            self.cond.notify_all()
        self.last_sent_at = now
        self.sent.append(now)
        if not cursor_only:
            self.rate.append(now)                    # 自适应只看真正的内容帧
        # bytesPerSec = **上游出帧带宽**（每帧算一次，不按客户端数翻倍）：
        # 画面静止时它是 0，这正是契约 §5.1 那条"静止带宽 ≤5KB/s"要看的数。
        self.note_sent(len(jpeg))
        with sess.lock:
            sess.latest = jpeg
            sess.frame_count += 1
            sess.last_frame_at = now
            sess.frame_error = None

    def _schedule(self, cfg) -> None:
        """下一帧的时间点：有人看且画面在动 → 目标 fps；没人看/静止 → 低频巡检。

        ⚠️ 从**上一次抓帧的开始时刻**算周期，不是从"现在"算：抓帧 + 编码本身要
        25-30ms，从"现在"算就等于每帧都白送这 30ms（实测 15fps 目标只能跑到 6.7fps，
        因为 66ms 的等待叠上 30ms 的干活）。超时了就把时间点设在过去 → 立刻抓下一帧。
        """
        active = (time.monotonic() - self.last_change) < ACTIVE_HOLD
        fps = cfg["fps"] if (active and self.watched()) else min(self.idle_fps, cfg["fps"])
        self.next_capture = self.last_capture_at + 1.0 / max(1, fps)

    # ---------------------------------------------------------------- 自适应
    def achieved_fps(self, window: float = 3.0) -> float:
        """最近 ``window`` 秒里真正**出帧**的速率（数据不够时返回 0 = 还不知道）。

        为什么自适应判据要带上它：只看"一帧耗时"会被单帧抖动骗到
        （实测 41ms > 40ms 预算就降了一档，而当时 15fps 跑得好好的、CPU 才 15%）。
        "出不了目标帧数"才是"抓不动/编不过来"的直接证据。
        """
        now = time.time()
        while self.rate and now - self.rate[0] > window:
            self.rate.popleft()
        if len(self.rate) < 4 or now - self.rate[0] < 1.0:
            return 0.0
        return len(self.rate) / max(0.5, now - self.rate[0])

    def _adapt(self, cfg, cap_ms: float, enc_ms: float) -> None:
        """自适应降档（契约 §5.2）：**先 fps → 再 scale → 最后 quality**；闲了再逐档回升。

        判据是"一帧的实测耗时超出帧周期的预算" **且** "实际出帧数明显低于目标" ——
        两者同时成立才算跟不上（只看耗时会被单帧抖动误伤）。而且必须**连续两次评估**
        都成立才真降档：这台机器上别的活儿（编译器、别的会话、别的测试）会让某一帧忽然
        慢两三倍，单帧抖动就降档的话画面会在 15fps↔9fps 之间来回跳，比降档本身更难看。
        降档只写进 ``self.auto``（只降不升），用户 POST 的目标档永远原样保留。
        """
        cost = self.capture_ms + self.encode_ms
        budget = max(8.0, 1000.0 * ADAPT_BUDGET_RATIO / max(1, cfg["fps"]))
        achieved = self.achieved_fps()
        behind = achieved == 0.0 or achieved < 0.75 * cfg["fps"]
        now = time.monotonic()
        if self._warm > 0:                           # 编码进程刚起：这几帧不作数
            self._warm -= 1
            self.last_adapt = now
            return
        if now - self.last_adapt < ADAPT_COOLDOWN:
            return
        if cost > budget and behind:
            self.over_strikes += 1
            if self.over_strikes < ADAPT_STRIKES:    # 一次抖动不算数，等下一次评估
                self.last_adapt = now
                return
            self.over_strikes = 0
            self.last_adapt = now
            shown = f"{cost:.0f}ms/帧（目标 {cfg['fps']}fps，实际 {achieved:.1f}fps）"
            if cfg["fps"] > MIN_FPS:
                value = max(MIN_FPS, int(cfg["fps"] * 0.6))
                self.auto["fps"] = value
                self.reason = f"跟不上：{shown} → fps 降到 {value}"
            elif cfg["scale"] > MIN_SCALE:
                value = max(MIN_SCALE, round(cfg["scale"] - 0.25, 2))
                self.auto["scale"] = value
                self.reason = f"仍跟不上：{shown} → scale 降到 {value}"
            elif cfg["quality"] > ADAPT_MIN_QUALITY:
                value = max(ADAPT_MIN_QUALITY, cfg["quality"] - 15)
                self.auto["quality"] = value
                self.reason = f"仍跟不上：{shown} → quality 降到 {value}"
            else:
                self.reason = f"已经是最低档还是跟不上：{shown}"
            print(f"[{self.sess.sid}] 自适应降档：{self.reason}", flush=True)
            self._notify()                           # 档变了下轮重启编码进程
        elif cost < budget * ADAPT_HEADROOM and achieved > 0.0:
            # 有余量就往上爬（有滞回：每 ADAPT_RECOVER_EVERY 秒动一小步）。
            # 只降档不回升的话，一次瞬时卡顿会把这个会话**永久**按在低帧率上。
            self.over_strikes = 0
            if not self.calm_since:
                self.calm_since = now
            elif now - self.calm_since >= ADAPT_RECOVER_EVERY:
                self.calm_since = now
                step = self._recover_step()
                if step:
                    self.reason = f"有余量（{cost:.0f}ms/帧 < {budget * ADAPT_HEADROOM:.0f}ms）→ {step}"
                    print(f"[{self.sess.sid}] 自适应回升：{self.reason}", flush=True)
                    self._notify()
        else:
            self.calm_since = 0.0

    def _recover_step(self) -> str:
        """回升一小步（返回说明；没有可回升的返回空串）。

        顺序与降档相反（quality → scale → fps）：降档时 quality 是最后的保命手段，
        回升时先把它还回去；fps 是**一步步 +2 爬**的，避免一恢复就又踩到过载线。
        """
        user = self.config
        if self.auto["quality"] is not None:
            value = min(user["quality"], self.auto["quality"] + 15)
            self.auto["quality"] = None if value >= user["quality"] else value
            return f"quality 回升到 {value}"
        if self.auto["scale"] is not None:
            value = min(user["scale"], round(self.auto["scale"] + 0.25, 2))
            self.auto["scale"] = None if value >= user["scale"] else value
            return f"scale 回升到 {value}"
        if self.auto["fps"] is not None:
            value = min(user["fps"], self.auto["fps"] + ADAPT_RECOVER_FPS)
            self.auto["fps"] = None if value >= user["fps"] else value
            return f"fps 回升到 {value}"
        return ""


def _grab_loop(sess: Session) -> None:
    """每会话一条抓帧/编码线程（没有会话就不会有它，不会白发抓帧进程）。"""
    sess.pipe.run()


# ---------------------------------------------------------------- 状态
def cursor_pos(sess: Session):
    """指针位置（**归一化 0..1**）；拿不到返回 None。

    0.4.0 起优先读帧管线缓存的位置（XQueryPointer，管线每轮刷新，≈50µs）——
    旧实现每次 /state 都 spawn 一个 xdotool（+一次 X 往返），而 /state 是 2 秒一次的轮询，
    面板开着就一直在花这个进程钱。缓存太旧（>1s，比如 import 路线）才退回 xdotool。
    """
    cached = sess.pipe.cached_cursor(max_age=1.0) if getattr(sess, "pipe", None) else None
    if cached is not None:
        return cached
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
    sess.pipe.watch()                                # 有人在看面板 → 别降到低频巡检
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


def stats_payload(sid: str, sess) -> dict:
    """``/stats``（契约 §5.5）。``sess`` 为 None 时给默认档的零计数，**绝不创建会话**。"""
    if sess is None:
        return {
            "session": sid, "ok": True, "started": False,
            "fps": 0.0, "captured": 0, "encoded": 0, "skipped": 0, "bytesPerSec": 0.0,
            "quality": DEFAULT_QUALITY, "scale": DEFAULT_SCALE, "mode": "idle",
            "captureMs": 0.0, "encodeMs": 0.0, "cursor": None, "reason": "",
            "encoder": "none", "cursorOnly": 0, "clients": 0, "seq": 0, "crc": 0,
            "damageEvents": 0, "damageAgoMs": None, "lastFrameAgeMs": None,
            "targetFps": DEFAULT_FPS, "fpsCap": DEFAULT_FPS, "fpsActual": 0.0,
            "userFps": DEFAULT_FPS, "idle": True,
            "idleFps": IDLE_FPS_SLOW, "costMs": 0.0,
            "stream": {"fps": 0.0, "fpsCap": DEFAULT_FPS, "quality": DEFAULT_QUALITY,
                       "scale": DEFAULT_SCALE, "mode": "idle"},
        }
    sess.pipe.watch()                                # 采样即"有人在看"（别在观测时降频）
    out = sess.pipe.stats()
    out.update({"session": sid, "ok": True, "started": bool(sess.started),
                "frameError": sess.frame_error})
    return out


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
            note = ("只读观看 —— 未开启输入注入"
                    "（DSH 里「设置 → 插件 → 显示器面板」可开，或设 DSH_VIEW_INPUT=1 后重启服务）")
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
                                if MAC_INPUT
                                else "关闭（只读观看；在 DSH 设置里开，或设 DSH_VIEW_INPUT=1）")
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

    #: 当前请求的 body（懒读一次；读了多少也要记下来，见 _body_bytes / _drain_request_body）
    _body = None
    _body_read = 0
    #: 令牌是否来自查询串（决定要不要回种 Cookie）。
    #:
    #: ⚠️ 只在 `_auth()` 里赋值是不够的：`do_OPTIONS` / `do_unsupported` /
    #: `do_HEAD` 走的是 `_host_ok()` → `_send()` → `_head()`，**不经过 `_auth()`**，
    #: 于是 `_head` 读它会抛 `AttributeError` —— 表现是"同源 OPTIONS/PUT 一个字节
    #: 都不回"（连接直接断），而契约承诺的是 204/405。所以给它一个**类级默认值**，
    #: 任何入口都不会再踩空。
    _cookie_from_query = False

    # ------------------------------------------------------------ 基础设施
    def log_message(self, *args) -> None:            # 静音：日志留给真正的错误
        pass

    def log_error(self, fmt, *args) -> None:
        print(f"[http] {self.address_string()} {fmt % args}", flush=True)

    def handle_one_request(self) -> None:
        """把 handler 里的任何异常挡住 —— BaseHTTPRequestHandler 默认会**打整条栈**。

        ⚠️ 收尾还要**抽干本次请求没读完的 body**：``protocol_version`` 是 HTTP/1.1
        （keep-alive），而 Python 的 http.server **不会**替你读 body。实测后果：
        同一条连接上先发 ``POST /s/<sid>/stream-config``（旧版服务 → 404，body 没人读），
        紧接着的 ``GET /health`` 会拿到 **501**，而且 501 里的"方法名"就是上一个 POST 的
        JSON body。``curl`` 不复用被污染的连接所以复现不出来，但 **Node/undici 与浏览器
        fetch 会** —— 宿主"旧版服务上探 /stream"就会因此误判"上游没有流"，
        客户端静默退回轮询，0.4.0 的流畅度整条路作废。
        """
        self._body = None
        self._body_read = 0
        try:
            BaseHTTPRequestHandler.handle_one_request(self)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as exc:                     # noqa: BLE001
            self.close_connection = True
            # 带上一行出错位置：这类异常以前只有一句消息，排查时得靠猜
            # （实测就是靠这一行定位到"回收会话时 _server_proc 是字符串"的）。
            where = traceback.extract_tb(sys.exc_info()[2])[-1]
            print(f"[http] 请求处理异常（已隔离，服务继续）：{type(exc).__name__}: {exc}"
                  f" @ {where.filename.split('/')[-1]}:{where.lineno} {where.name}", flush=True)
        finally:
            self._drain_request_body()

    def _drain_request_body(self) -> None:
        """把本请求剩下的 body 读掉（读不完就直接关连接，绝不污染下一条请求）。"""
        try:
            if self.close_connection:
                return
            length = int(self.headers.get("Content-Length") or 0)
        except (AttributeError, TypeError, ValueError):
            return
        left = max(0, length) - self._body_read
        if left <= 0:
            if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
                # 分块 body 找不到长度、也没法安全跳到结尾：关连接，别让残留字节变成下一条请求
                self.close_connection = True
            return
        if left > BODY_DRAIN_MAX:                    # 恶意/异常巨大的 body：不为它陪跑
            self.close_connection = True
            return
        try:
            while left > 0:
                chunk = self.rfile.read(min(left, 16384))
                if not chunk:
                    break
                left -= len(chunk)
            self._body_read = max(0, length - left)
        except (OSError, ValueError):
            self.close_connection = True

    def _head(self, code: int, ctype: str, length=None, extra=()) -> None:
        """发响应头。**不设 CORS 头**（见上方 :func:`host_reason` 的说明）。

        删掉 ``Access-Control-Allow-Origin: *`` 是刻意的：面板只走 DSH 自己的
        同源代理，独立页面也是同源访问，**跨源读本服务从来不是受支持的用法**；
        通配 CORS 只会让"任意网页读本机回环"这件危险的事变容易。

        Host 校验**不在这一层**：它在每个请求的入口（``_auth`` / :meth:`do_OPTIONS`
        / :meth:`do_unsupported`）就做完了，这样"没通过来源校验的请求"不会先跑一段
        业务逻辑（例如把 Xvfb 拉起来）再被拒。这一层只管写头。
        """
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        if self.close_connection:
            # 明确告诉客户端"用完就关"：否则它会按 HTTP/1.1 的默认语义把这条连接放回池子，
            # 下一次请求才发现服务端已经关了（undici 会报 socket hang up）。
            self.send_header("Connection", "close")
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

    def _host_403(self, reason: str) -> None:
        """来源不可信：写 403 并**关连接**。

        关连接是必要的：不可信的请求体我们不会去读，留着这条 keep-alive 连接
        只会让残留字节变成下一条请求的"起始行"。
        """
        # 面板里看不到这条 403 的正文（跨源响应读不了），所以同时落一行日志：
        # 用户配错代理/白名单时，`viewer.log` 才是唯一能说清原因的地方。
        try:
            print(f"[host] 拒绝请求：{reason}（Host={self.headers.get('Host')!r} "
                  f"Origin={self.headers.get('Origin')!r}）", file=sys.stderr, flush=True)
        except Exception:                                 # noqa: BLE001
            pass
        body = json.dumps(
            {"ok": False, "error": "forbidden host/origin", "reason": reason},
            ensure_ascii=False).encode("utf-8")
        self.send_response(403)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self._write(body)

    def _host_ok(self) -> bool:
        """来源校验的统一入口：通过了回 True，否则写 403 并回 False。"""
        reason = host_reason(self.headers)
        if not reason:
            return True
        self._host_403(reason)
        return False

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
        """校验来源（Host/Origin）与令牌；成功时记下"令牌来自查询串"，用于回种 Cookie。

        两道防线**都要过**，顺序是"来源 → 令牌"：来源不对的请求连令牌都不该被
        拿去做比较（少一次未知输入的路径）。来源不过时 :meth:`_host_ok` 已经把
        403 写出去了。
        """
        if not self._host_ok():
            return False
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

    def _precheck_body(self) -> None:
        """看到"大到我们不会读"的 body，先把连接标成"用完就关"。

        这样响应头里会带上 ``Connection: close``，客户端不会把一条**即将被我们关掉**的
        连接放回池子（否则它下一次请求会撞上 socket hang up）。
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (AttributeError, TypeError, ValueError):
            return
        if length > BODY_DRAIN_MAX:
            self.close_connection = True

    def _body_bytes(self) -> bytes:
        """本请求的 body（只读一次，最多 ``BODY_READ_MAX`` 字节）。

        ⚠️ 一定要走这里读，别自己 ``rfile.read``：读了多少要记在 ``_body_read`` 上，
        收尾的 :meth:`_drain_request_body` 才知道还剩多少要抽干（keep-alive 下
        残留字节会被当成下一条请求的起始行，实测会变成 501）。
        """
        if self._body is None:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                length = 0
            length = max(0, min(length, BODY_READ_MAX))
            self._body = self.rfile.read(length) if length > 0 else b""
            self._body_read = len(self._body)
        return self._body

    def _read_json(self):
        raw = self._body_bytes()
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8", "replace"))

    def _query(self) -> dict:
        """查询串 → ``{键: 值}``（同键取最后一个；``k=`` 是令牌，不参与业务）。"""
        out = {}
        for key, values in parse_qs(urlparse(self.path).query).items():
            if key == "k" or not values:
                continue
            out[key] = values[-1]
        return out

    # ------------------------------------------------------------ GET
    def do_GET(self) -> None:                        # noqa: N802 - BaseHTTPRequestHandler
        self._precheck_body()
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
            sess.pipe.watch()
            # ?quality=/&scale= 是**锦上添花**的参数：非法就当没给（不 400），
            # 因为旧调用方（页面、宿主、AI 工具）不该因为多写一个参数就拿到错误。
            params = self._query()
            quality = _cfg_int(params.get("quality"), MIN_QUALITY, MAX_QUALITY)
            scale = _cfg_float(params.get("scale"), MIN_SCALE, MAX_SCALE)
            frame = sess.snapshot_jpeg(quality, scale)
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
        if rest == "/stats":
            # 探测类：**不创建会话**（没有会话就给默认档的零计数）
            self._json(200, stats_payload(sid, peek_session(sid)))
            return
        if rest == "/stream-config":
            sess = peek_session(sid)
            cfg = sess.pipe.effective_config() if sess else {
                "quality": DEFAULT_QUALITY, "fps": DEFAULT_FPS, "scale": DEFAULT_SCALE}
            self._json(200, {"ok": True, "session": sid, "config": cfg,
                             "default": sess is None})
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
        """MJPEG 长连接（契约 §5.3）。

        每个客户端一条 HTTP 线程，但**永远只发最新一帧**（``wait_frame`` 拿的是
        "当前最新"，不是队列）：客户端慢就自然丢中间帧，不会把延迟堆起来。
        多个客户端同时连是**广播**（各自从同一份最新帧取），不是第一个独占。

        断开要干净：写失败/对端关了立刻收场（旧实现会抛 BrokenPipeError 刷栈、
        线程永不退出）。
        """
        sess = session(sid)
        if not sess.ensure():
            self._error(502, "显示未就绪", f"看 {sess.dir}/xvfb.log")
            return
        pipe = sess.pipe
        pipe.subscribe()
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("X-Accel-Buffering", "no")   # 反向代理别攒着这批字节
        self.end_headers()
        seq = 0
        try:
            while not sess.closed:
                sess.touch()
                frame, meta, seq = pipe.wait_frame(seq, timeout=0.5)
                if frame is None:
                    # 没新帧也要定期看一眼对端还在不在（静止画面可能几分钟不发一帧）
                    if self._peer_gone():
                        return
                    continue
                chunk = mjpeg_part(frame, meta["seq"], meta["time"], meta["size"],
                                  meta["cursor"])
                if not self._write(chunk):
                    return
                try:
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return
                if self._peer_gone():
                    return
        finally:
            pipe.unsubscribe()
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
        self._precheck_body()
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
        elif rest == "/stream-config":
            self._post_stream_config(sid)
        elif rest == "/kill":
            self._post_kill(sid)
        elif rest == "/close":
            self._post_close(sid)
        else:
            self._error(404, f"未知路径 /s/{sid}{rest}")

    def do_DELETE(self) -> None:                     # noqa: N802
        self._precheck_body()
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

    # ------------------------------------------------------------ 其它方法
    def do_OPTIONS(self) -> None:                    # noqa: N802
        """预检请求：**明确拒绝**，不再回"允许一切"。

        本服务的跨源读从来不是受支持的用法（面板走宿主同源代理），所以预检的
        正确答复是"不行"。这里**只做来源校验、不做令牌校验**：预检请求按规范
        不带凭据（Cookie），拿令牌要求它等于认定所有合法跨源都失败——而合法
        跨源本来就不存在。同源访问不触发预检，不受影响。
        """
        if not self._host_ok():
            return
        self._send(204, b"", "text/plain; charset=utf-8")

    def do_PUT(self) -> None:                        # noqa: N802
        self.do_unsupported()

    def do_HEAD(self) -> None:                       # noqa: N802
        """HEAD：只回"服务活着"，不回任何业务量。

        探活脚本（``selfcheck.py``、``/health`` 的调用方）用它最省事；这里**不落
        ``_head`` 的缓存/``Set-Cookie`` 逻辑**是有意的：HEAD 不该有副作用。
        """
        if not self._host_ok():
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()

    def do_PATCH(self) -> None:                      # noqa: N802
        self.do_unsupported()

    def do_unsupported(self) -> None:
        """未实现的方法统一回 405（默认实现回 501）。

        501 会让客户端以为"服务端坏了"，而事实是"这个方法本来就不该用"。
        先过来源校验：不信任的请求连"有哪些方法"都不必告诉它。
        """
        if not self._host_ok():
            return
        self._send(405, b"", "text/plain; charset=utf-8", extra=(("Allow", "GET, POST, DELETE, OPTIONS"),))

    def _post_stream_config(self, sid: str) -> None:
        """改档（契约 §5.2）：``POST /s/<sid>/stream-config``。

        JSON body 与查询串都认（``?quality=90&fps=10``），查询串优先；
        只认 quality/fps/scale 三个键，其余键（客户端的 profile/reason、令牌 k）忽略。
        **非法值回 400**：静默夹到边界会让调用方以为改成功了。
        """
        try:
            body = self._read_json()
        except ValueError as exc:
            self._error(400, f"body 不是合法 JSON：{exc}")
            return
        params = dict(body) if isinstance(body, dict) else {}
        query = self._query()
        for key in ("quality", "fps", "scale"):
            if key in query:
                params[key] = query[key]
        # ⚠️ 先校验、再创建会话：非法参数不该把 Xvfb 拉起来（与 /input 的校验顺序同理）。
        sess = peek_session(sid)
        base = sess.pipe.config if sess else {"quality": DEFAULT_QUALITY,
                                              "fps": DEFAULT_FPS, "scale": DEFAULT_SCALE}
        cfg, err = parse_stream_config(params, base)
        if err:
            self._error(400, err, "quality 1..100、fps 1..30、scale 0.25..1.0；"
                                  "其余键一律忽略")
            return
        sess = session(sid)
        applied = sess.pipe.set_config(cfg)
        sess.touch()
        self._json(200, {"ok": True, "session": sid, "config": applied,
                         "effective": sess.pipe.effective_config()})

    def _post_input(self, sid: str) -> None:
        # ⚠️ 注入关闭时要**明确拒绝**，不能"收下但不做"。
        #
        # 这条路径不只服务浏览器：宿主的 `display_panel_input` 工具（AI 直接调）也走它。
        # 早先的做法是照收、`enqueue` 成功、回 `{"ok":true}` —— 真实的注入失败只在
        # `sess.last_input_error` 里、由 `/state` 事后暴露。对"真实桌面只读观看"这个
        # 场景来说，"回 ok 但什么都没发生"比报错更糟：调用方以为点到了。
        # 客户端已经在入队前拦了一层（弹确认框），这一层是给**绕过客户端**的调用方
        # （AI 工具、curl 脚本）兜底的 —— 两道都要有。
        if not input_enabled():
            self._error(
                403, "输入注入未开启（当前只读观看）",
                "真实桌面后端默认关闭注入；在 DSH 的「设置 → 插件 → 显示器面板」里打开，"
                "或设 DSH_VIEW_INPUT=1 后重启服务")
            return
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


class ViewerServer(ThreadingHTTPServer):
    """HTTP 服务本体：**唯一区别是 bind 时不反查域名**。

    ``http.server.HTTPServer.server_bind()`` 会调用 ``socket.getfqdn(host)`` 做反向解析。
    在没有反向 DNS 的环境（GitHub Actions 的 macOS runner、部分容器/隔离网络）这一步会
    卡十几秒甚至更久 —— 后果是**端口已经绑好、端口文件却迟迟不写**，
    于是宿主/CI 脚本/``scripts/install-service.sh`` 全都以为"服务没起来"
    （实测：把 ``socket.getfqdn`` 人为拖慢 20 秒，``<home>/port`` 15 秒内都不出现；
    macOS CI 就是这么红的）。

    这里改成只做真正的 bind/listen，``server_name`` 直接用绑定地址填 ——
    我们从不依赖 FQDN，少一次可能挂死的系统调用。
    """

    daemon_threads = True
    allow_reuse_address = True

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)     # 只 bind + listen，不碰 DNS
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)


def _bind(preferred: int, tries: int = 12) -> "ThreadingHTTPServer":
    """从 preferred 开始找一个能绑的端口，并把最终端口写进 ``<HOME_DIR>/port``。

    为什么：同一台机器上可能有多个实例（多个用户、或手工起了两次）。原来的行为是
    端口被占就**直接崩** —— 用户只会看到"面板连不上"，完全看不出原因。
    现在自动往后找，插件侧则在 8099..8111 范围内探测第一个应答的服务。

    为什么用 :class:`ViewerServer` 而不是 ``ThreadingHTTPServer``：见前者的注释
    （``server_bind`` 里的 FQDN 反查会拖到端口文件写不出来）。
    """
    last = None
    for port in range(preferred, preferred + tries):
        try:
            srv = ViewerServer(("127.0.0.1", port), Handler)
        except OSError as exc:                       # 端口被占：换下一个
            last = exc
            continue
        global PORT
        PORT = port
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
