#!/usr/bin/env python3
"""极简 X11 测试靶程序 —— 验证「显示器服务」把鼠标/键盘注入到了哪里、落点准不准。

**纯标准库 + ctypes 加载 libX11**：不需要 gcc、不需要编译、不需要 python-xlib，
CI 上装了 Xvfb 就能跑（对照用的 C 版原型见 .verify/xtarget.c，两者功能等价）。

它开一个**铺满整屏**的窗口（这样归一化坐标换算最直接：px = x * 屏宽），
把收到的事件按行追加写进日志文件（每行 flush），并在窗口上画出收到的文字。

日志格式（**空格分隔的 key=value，值一律用 JSON 转义**，便于脚本抓取，
例如 text="中" 或 text="\\n"）：

    HH:MM:SS.mmm READY window=<id> display=<str> screen=<W>x<H> pid=<pid>
    HH:MM:SS.mmm FOCUS in=1
    HH:MM:SS.mmm BUTTON x=<int> y=<int> button=<int>          # 按下
    HH:MM:SS.mmm BUTTONUP x=<int> y=<int> button=<int>        # 抬起
    HH:MM:SS.mmm MOTION x=<int> y=<int> state=<hex>           # 移动（pointer 在窗口内）
    HH:MM:SS.mmm KEY keysym=<name> text=<json> state=<hex>    # 单个按键（含中文直投）
    HH:MM:SS.mmm PASTE text=<json>                            # Ctrl+V：从剪贴板读回
    HH:MM:SS.mmm LINE text=<json>                             # 缓冲区（累积）内容
    HH:MM:SS.mmm ERROR <why>                                  # 起不来（无显示等）

为什么坐标要记 `x` / `y`：注入是「归一化坐标 × 屏宽高」，靶程序记的是**窗口坐标**，
窗口铺满整屏时两者可直接相减 —— 落点误差 ≤2px 就成了可断言的硬指标，不必人眼看图。

为什么 PASTE 要自己实现：服务端对含非 ASCII 的文本走「剪贴板 + Ctrl+V」（`xdotool type`
打不进中文）。普通 X11 窗口不会粘贴，所以这里自己响应 Ctrl+V：XConvertSelection 取
CLIPBOARD 的 UTF8_STRING 再 XGetWindowProperty 读回来 —— 这样「中文到底到没到」
才是**端到端**断言，而不是只看剪贴板里有没有东西。

用法：
    python3 tools/xtarget.py <日志路径> [--title T] [--label L]
                             [--geometry WxH+X+Y] [--timeout 秒] [--fg 0xRRGGBB]
    # 例：DISPLAY=:190 python3 tools/xtarget.py /tmp/xt.log --label SESSION-A --timeout 60
"""

from __future__ import annotations

import ctypes
import ctypes.util
import json
import os
import select
import sys
import time

# --------------------------------------------------------------------- X11 常量
KeyPress = 2
KeyRelease = 3
ButtonPress = 4
ButtonRelease = 5
MotionNotify = 6
Expose = 12
FocusIn = 9
SelectionNotify = 31
ClientMessage = 33

ExposureMask = 1 << 15
PointerMotionMask = 1 << 6
ButtonMotionMask = 1 << 13
StructureNotifyMask = 1 << 17
KeyPressMask = 1 << 0
ButtonPressMask = 1 << 2
ButtonReleaseMask = 1 << 3
FocusChangeMask = 1 << 21

RevertToParent = 2
CurrentTime = 0
AnyPropertyType = 0
XK_BackSpace = 0xFF08
XK_Return = 0xFF0D
XK_v = 0x76
ControlMask = 1 << 2
XK_VoidSymbol = 0xFFFFFF


class XKeyEvent(ctypes.Structure):
    """XKeyEvent / XButtonEvent / XMotionEvent 的公共前缀部分。

    三者的 x/y/state 偏移完全一致（前 84 字节），keycode 与 button 也共用同一偏移，
    所以一个结构就能读全部三种事件 —— 少写三份容易写错的布局。
    """

    _fields_ = [
        ("type", ctypes.c_int),
        ("serial", ctypes.c_ulong),
        ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("window", ctypes.c_ulong),
        ("root", ctypes.c_ulong),
        ("subwindow", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("x", ctypes.c_int),
        ("y", ctypes.c_int),
        ("x_root", ctypes.c_int),
        ("y_root", ctypes.c_int),
        ("state", ctypes.c_uint),
        ("keycode_or_button_or_is_hint", ctypes.c_uint),
        ("same_screen", ctypes.c_int),
    ]


class XSelectionEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int),
        ("serial", ctypes.c_ulong),
        ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("requestor", ctypes.c_ulong),
        ("selection", ctypes.c_ulong),
        ("target", ctypes.c_ulong),
        ("property", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
    ]


class XEvent(ctypes.Union):
    """XEvent 是 24 个 long 的联合体（192 字节，Xlib 1.8+）。"""

    _fields_ = [
        ("type", ctypes.c_int),
        ("xkey", XKeyEvent),
        ("xselection", XSelectionEvent),
        ("pad", ctypes.c_long * 24),
    ]


class XErrorEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("resourceid", ctypes.c_ulong),
        ("serial", ctypes.c_ulong),
        ("error_code", ctypes.c_ubyte),
        ("request_code", ctypes.c_ubyte),
        ("minor_code", ctypes.c_ubyte),
    ]


_XERROR_HANDLER = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
_ACTIVE_TARGET: "Target | None" = None
_HANDLER_REF = None                    # 必须常驻引用，否则回调被 GC 后进程直接崩


def _install_error_handler(target: "Target") -> None:
    """把 Xlib 的默认错误处理器（= 直接 exit）换成「写一行日志、继续跑」。

    测试工具最怕的就是**静默死掉**：服务端看到的是"注入没反应"，看不出靶程序早没了。
    """
    global _ACTIVE_TARGET, _HANDLER_REF

    _ACTIVE_TARGET = target

    def _callback(_dpy, event_ptr):
        try:
            ev = ctypes.cast(event_ptr, ctypes.POINTER(XErrorEvent)).contents
            if _ACTIVE_TARGET is not None:
                _ACTIVE_TARGET.log(f"XERROR code={ev.error_code} "
                                   f"request={ev.request_code} minor={ev.minor_code}")
        except Exception:                                     # noqa: BLE001
            pass
        return 0

    _HANDLER_REF = _XERROR_HANDLER(_callback)
    target.lib.XSetErrorHandler(ctypes.cast(_HANDLER_REF, ctypes.c_void_p))


def load_x11() -> ctypes.CDLL:
    """加载 libX11（ctypes.util.find_library 在部分发行版上找不到，故按名字兜底）。"""
    tried = []
    for name in (ctypes.util.find_library("X11"), "libX11.so.6", "libX11.so",
                 "libX11.so.5", "libX11.dylib",
                 "/usr/lib/x86_64-linux-gnu/libX11.so.6", "/opt/X11/lib/libX11.dylib"):
        if not name:
            continue
        tried.append(name)
        try:
            return ctypes.CDLL(name)
        except OSError:
            continue
    raise OSError("加载 libX11 失败，试过：" + ", ".join(tried))


class Target:
    def __init__(self, log_path: str, *, title: str, label: str,
                 geometry: str | None, fg: int, bg: int) -> None:
        self.log_path = log_path
        self._logf = open(log_path, "a", encoding="utf-8", buffering=1)
        self.title = title
        self.label = label
        self.geometry = geometry
        self.fg, self.bg = fg, bg
        self.line = ""                      # 收到的文字（累积）
        self.clipboard_atom = 0
        self.utf8_atom = 0
        self.prop_atom = 0
        self.lib = load_x11()
        self._declare()
        self.dpy = None
        self.win = 0
        self.gc = None
        self.screen = 0
        self.width = self.height = 0

    # ------------------------------------------------------------------ 打通 ctypes
    def _declare(self) -> None:
        lib = self.lib
        lib.XOpenDisplay.restype = ctypes.c_void_p
        lib.XOpenDisplay.argtypes = [ctypes.c_char_p]
        lib.XDefaultScreen.restype = ctypes.c_int
        lib.XDefaultScreen.argtypes = [ctypes.c_void_p]
        lib.XRootWindow.restype = ctypes.c_ulong
        lib.XRootWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.XDisplayWidth.restype = ctypes.c_int
        lib.XDisplayWidth.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.XDisplayHeight.restype = ctypes.c_int
        lib.XDisplayHeight.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.XBlackPixel.restype = ctypes.c_ulong
        lib.XBlackPixel.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.XWhitePixel.restype = ctypes.c_ulong
        lib.XWhitePixel.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.XCreateSimpleWindow.restype = ctypes.c_ulong
        lib.XCreateSimpleWindow.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_int,
            ctypes.c_uint, ctypes.c_uint, ctypes.c_uint,
            ctypes.c_ulong, ctypes.c_ulong]
        lib.XStoreName.restype = ctypes.c_int
        lib.XStoreName.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_char_p]
        lib.XSelectInput.restype = ctypes.c_int
        lib.XSelectInput.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_long]
        lib.XMapWindow.restype = ctypes.c_int
        lib.XMapWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        lib.XFlush.restype = ctypes.c_int
        lib.XFlush.argtypes = [ctypes.c_void_p]
        lib.XSync.restype = ctypes.c_int
        lib.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.XSetInputFocus.restype = ctypes.c_int
        lib.XSetInputFocus.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
                                       ctypes.c_int, ctypes.c_ulong]
        lib.XNextEvent.restype = ctypes.c_int
        lib.XNextEvent.argtypes = [ctypes.c_void_p, ctypes.POINTER(XEvent)]
        lib.XPending.restype = ctypes.c_int
        lib.XPending.argtypes = [ctypes.c_void_p]
        lib.XConnectionNumber.restype = ctypes.c_int
        lib.XConnectionNumber.argtypes = [ctypes.c_void_p]
        lib.XCreateGC.restype = ctypes.c_void_p
        lib.XCreateGC.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
                                  ctypes.c_ulong, ctypes.c_void_p]
        lib.XSetForeground.restype = ctypes.c_int
        lib.XSetForeground.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
        lib.XSetBackground.restype = ctypes.c_int
        lib.XSetBackground.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
        lib.XClearWindow.restype = ctypes.c_int
        lib.XClearWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        lib.XDrawString.restype = ctypes.c_int
        lib.XDrawString.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p,
                                    ctypes.c_int, ctypes.c_int, ctypes.c_char_p,
                                    ctypes.c_int]
        # X 错误默认会**直接终止进程**（Xlib 的默认错误处理器就是 exit）。
        # 靶程序是测试工具，绝不能因为一次画字失败就悄无声息地死掉：换成记录并继续。
        lib.XSetErrorHandler.restype = ctypes.c_void_p
        lib.XSetErrorHandler.argtypes = [ctypes.c_void_p]
        lib.XLookupString.restype = ctypes.c_int
        lib.XLookupString.argtypes = [ctypes.POINTER(XKeyEvent), ctypes.c_char_p,
                                      ctypes.c_int, ctypes.POINTER(ctypes.c_ulong),
                                      ctypes.c_void_p]
        lib.XKeysymToString.restype = ctypes.c_char_p
        lib.XKeysymToString.argtypes = [ctypes.c_ulong]
        lib.XInternAtom.restype = ctypes.c_ulong
        lib.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
        lib.XConvertSelection.restype = ctypes.c_int
        lib.XConvertSelection.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong,
                                          ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
        lib.XGetWindowProperty.restype = ctypes.c_int
        lib.XGetWindowProperty.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_long,
            ctypes.c_long, ctypes.c_int, ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte))]
        lib.XFree.restype = ctypes.c_int
        lib.XFree.argtypes = [ctypes.c_void_p]

    # ------------------------------------------------------------------ 日志
    def log(self, msg: str) -> None:
        now = time.time()
        stamp = time.strftime("%H:%M:%S", time.localtime(now)) + f".{int(now % 1 * 1000):03d}"
        self._logf.write(f"{stamp} {msg}\n")
        self._logf.flush()

    @staticmethod
    def _j(value: str) -> str:
        """值一律 JSON 转义（含空格/换行/引号也不会破坏 key=value 的行格式）。"""
        return json.dumps(value, ensure_ascii=False)

    # ------------------------------------------------------------------ 启动
    def open(self) -> None:
        self.dpy = self.lib.XOpenDisplay(None)
        if not self.dpy:
            raise RuntimeError("cannot open display " + repr(os.environ.get("DISPLAY")))
        self.screen = self.lib.XDefaultScreen(self.dpy)
        self.width = self.lib.XDisplayWidth(self.dpy, self.screen)
        self.height = self.lib.XDisplayHeight(self.dpy, self.screen)
        x = y = 0
        w, h = self.width, self.height
        if self.geometry:                    # WxH+X+Y
            geo = self.geometry
            plus = geo.find("+", 1)
            size, pos = (geo[:plus], geo[plus:]) if plus > 0 else (geo, "")
            if "x" in size:
                w, h = (int(v) for v in size.split("x", 1))
            if pos:
                parts = [p for p in pos.split("+") if p]
                if len(parts) >= 2:
                    x, y = int(parts[0]), int(parts[1])
        self.win = self.lib.XCreateSimpleWindow(
            self.dpy, self.lib.XRootWindow(self.dpy, self.screen), x, y, w, h, 0,
            self.bg, self.bg)
        self.lib.XStoreName(self.dpy, self.win, self.title.encode())
        self.lib.XSelectInput(self.dpy, self.win,
                              KeyPressMask | ButtonPressMask | ButtonReleaseMask
                              | ExposureMask | PointerMotionMask | ButtonMotionMask
                              | StructureNotifyMask | FocusChangeMask)
        self.lib.XMapWindow(self.dpy, self.win)
        self.gc = self.lib.XCreateGC(self.dpy, self.win, 0, None)
        self.lib.XSetForeground(self.dpy, self.gc, self.fg)
        self.lib.XSetBackground(self.dpy, self.gc, self.bg)
        # 不再显式 XLoadQueryFont + XSetFont：新 GC 的字体就是屏幕默认字体，
        # 而 XSetFont 要的是 XFontStruct.fid（不是结构体指针）—— 传错就是 BadFont，
        # 且 Xlib 默认会因 X 错误直接退出（实测踩过）。
        _install_error_handler(self)
        self.lib.XFlush(self.dpy)
        # 无窗口管理器时默认焦点是 PointerRoot：显式把焦点拿过来，键盘注入才有着落
        for _ in range(20):
            self.lib.XSetInputFocus(self.dpy, self.win, RevertToParent, CurrentTime)
            self.lib.XSync(self.dpy, False)
            time.sleep(0.05)
        # ⚠️ 必须 only_if_exists=False：靶程序启动时剪贴板属主（xclip）往往**还没起**，
        #    只查不建会拿到 None(0)，于是 Ctrl+V 永远不请求 —— 实测踩过（PASTE 一直不出现）。
        self.clipboard_atom = self.lib.XInternAtom(self.dpy, b"CLIPBOARD", False)
        self.utf8_atom = self.lib.XInternAtom(self.dpy, b"UTF8_STRING", False)
        self.prop_atom = self.lib.XInternAtom(self.dpy, b"DSH_XTARGET_PASTE", False)
        self.log(f"READY window={self.win} display="
                 f"{self._j(os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY') or '?')} "
                 f"screen={self.width}x{self.height} pid={os.getpid()}")
        self.redraw()

    def redraw(self) -> None:
        try:
            self.lib.XClearWindow(self.dpy, self.win)
            if self.label:
                self.lib.XDrawString(self.dpy, self.win, self.gc, 20, 30,
                                     self.label.encode("utf-8", "replace"),
                                     len(self.label.encode("utf-8", "replace")))
            text = self.line.replace("\n", "\\n")
            data = text.encode("utf-8", "replace")
            self.lib.XDrawString(self.dpy, self.win, self.gc, 20, 60, data, len(data))
            self.lib.XFlush(self.dpy)
        except Exception:                                     # noqa: BLE001
            pass

    # ------------------------------------------------------------------ 事件
    def keysym_text(self, ks: int, raw: bytes) -> str:
        """键 → 文本。XLookupString 对带 Unicode 位的 keysym（中文直投）返回空，需兜底。"""
        if raw:
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError:
                return raw.decode("latin-1")
        if 0x01000000 <= ks <= 0x0110FFFF:          # Unicode keysym（xdotool type 中文走这条）
            return chr(ks & 0x1FFFFF)
        if 0x20 <= ks <= 0x7E or 0xA0 <= ks <= 0xFF:
            return chr(ks)
        return ""

    def on_key(self, ev: XEvent) -> None:
        key = ev.xkey
        buf = ctypes.create_string_buffer(32)
        ks = ctypes.c_ulong(0)
        n = self.lib.XLookupString(ctypes.byref(key), buf, 31, ctypes.byref(ks), None)
        raw = buf.raw[:n]
        name_ptr = self.lib.XKeysymToString(ks.value)
        name = name_ptr.decode() if name_ptr else "?"
        text = self.keysym_text(ks.value, raw)
        if text == "\r":
            text = "\n"
        self.log(f"KEY keysym={name} text={self._j(text)} state={key.state:#x}")
        if text and text.isprintable() or text == "\n":
            self.line += text
        if ks.value == XK_BackSpace and self.line:
            self.line = self.line[:-1]
        # Ctrl+V：XLookupString 在 Ctrl 下给的是控制字符 \x16，keysym 仍是 v —— 两条都认
        if (key.state & ControlMask) and (ks.value in (XK_v, 0x56) or raw == b"\x16"):
            self.request_paste()
        self.log(f"LINE text={self._j(self.line)}")
        self.redraw()

    def request_paste(self) -> None:
        """Ctrl+V：主动向剪贴板属主（xclip）索取 UTF8_STRING。"""
        if not self.clipboard_atom or not self.utf8_atom:
            return
        try:
            self.lib.XConvertSelection(self.dpy, self.clipboard_atom, self.utf8_atom,
                                       self.prop_atom, self.win, CurrentTime)
            self.lib.XFlush(self.dpy)
        except Exception:                                     # noqa: BLE001
            pass

    def on_selection(self, ev: XEvent) -> None:
        sel = ev.xselection
        if sel.property == 0:                     # 属主拒绝或无此 target
            self.log("PASTE text=\"\" note=selection-refused")
            return
        actual_type = ctypes.c_ulong(0)
        actual_format = ctypes.c_int(0)
        nitems = ctypes.c_ulong(0)
        after = ctypes.c_ulong(0)
        prop = ctypes.POINTER(ctypes.c_ubyte)()
        status = self.lib.XGetWindowProperty(
            self.dpy, self.win, sel.property, 0, 1 << 20, 1, AnyPropertyType,
            ctypes.byref(actual_type), ctypes.byref(actual_format),
            ctypes.byref(nitems), ctypes.byref(after), ctypes.byref(prop))
        if status != 0 or not prop:
            self.log("PASTE text=\"\" note=getproperty-failed")
            return
        try:
            data = ctypes.string_at(prop, nitems.value)
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                text = data.decode("latin-1")
        finally:
            self.lib.XFree(prop)
        self.log(f"PASTE text={self._j(text)}")
        if text:
            self.line += text
            self.log(f"LINE text={self._j(self.line)}")
            self.redraw()

    def run(self, timeout: float | None) -> int:
        deadline = (time.time() + timeout) if timeout else None
        while True:
            if deadline and time.time() > deadline:
                self.log("EXIT reason=timeout")
                return 0
            fd = self.lib.XConnectionNumber(self.dpy)
            try:
                select.select([fd], [], [], 0.2)
            except Exception:                                 # noqa: BLE001
                time.sleep(0.2)
            while self.lib.XPending(self.dpy):
                ev = XEvent()
                self.lib.XNextEvent(self.dpy, ctypes.byref(ev))
                if ev.type == Expose:
                    self.redraw()
                elif ev.type == KeyPress:
                    self.on_key(ev)
                elif ev.type == ButtonPress:
                    self.log(f"BUTTON x={ev.xkey.x} y={ev.xkey.y} "
                             f"button={ev.xkey.keycode_or_button_or_is_hint}")
                elif ev.type == ButtonRelease:
                    self.log(f"BUTTONUP x={ev.xkey.x} y={ev.xkey.y} "
                             f"button={ev.xkey.keycode_or_button_or_is_hint}")
                elif ev.type == MotionNotify:
                    self.log(f"MOTION x={ev.xkey.x} y={ev.xkey.y} state={ev.xkey.state:#x}")
                elif ev.type == FocusIn:
                    self.log("FOCUS in=1")
                elif ev.type == SelectionNotify:
                    self.on_selection(ev)


def parse_args(argv: list[str]) -> dict:
    opts: dict = {"log": "xtarget.log", "title": "DSH X target", "label": "",
                  "geometry": None, "timeout": 0.0, "fg": 0x000000, "bg": 0xFFFFFF}
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg == "--title" and i + 1 < len(argv):
            opts["title"] = argv[i + 1]; i += 2
        elif arg == "--label" and i + 1 < len(argv):
            opts["label"] = argv[i + 1]; i += 2
        elif arg == "--geometry" and i + 1 < len(argv):
            opts["geometry"] = argv[i + 1]; i += 2
        elif arg == "--timeout" and i + 1 < len(argv):
            opts["timeout"] = float(argv[i + 1]); i += 2
        elif arg == "--fg" and i + 1 < len(argv):
            opts["fg"] = int(argv[i + 1], 0); i += 2
        elif arg == "--bg" and i + 1 < len(argv):
            opts["bg"] = int(argv[i + 1], 0); i += 2
        elif arg in ("-h", "--help"):
            print(__doc__)
            raise SystemExit(0)
        elif not arg.startswith("--"):
            opts["log"] = arg; i += 1
        else:
            print(f"未知参数：{arg}", file=sys.stderr)
            raise SystemExit(2)
    return opts


def main(argv: list[str]) -> int:
    opts = parse_args(argv)
    try:
        target = Target(opts["log"], title=opts["title"], label=opts["label"],
                        geometry=opts["geometry"], fg=opts["fg"], bg=opts["bg"])
    except OSError as exc:
        print(f"xtarget: {exc}", file=sys.stderr)
        return 3
    try:
        target.open()
    except Exception as exc:                                  # noqa: BLE001
        # 打不开显示是最常见的失败：**必须写进日志**，否则调用方只看到"没有输出"
        target.log(f"ERROR {type(exc).__name__}: {exc}")
        print(f"xtarget: {exc}", file=sys.stderr)
        return 2
    return target.run(opts["timeout"] or None)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
