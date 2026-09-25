#!/usr/bin/env python3
"""会动的 X11 靶画面 —— 性能测量的**内容发生器**（不是断言工具）。

为什么要有它：`tools/perf-panel.mjs` 要量「内容 fps / 变化延迟 / 静止带宽」，
前提是**有一个我们知道"它什么时候变了"的画面源**。

* 靠"看着像在动"不行 —— 延迟是「X 侧真的变了 → 客户端画出来」，
  所以每一次画面变化都必须留下**带 epoch 毫秒的时间戳**。
* 靠截图比对也不行 —— 帧是 JPEG，有损压缩下"两张图不完全一样"和"画面真的变了"
  是两件事，必须由**画的一方**声明。

它开一个铺满整屏的窗口，每画一帧就写一行 JSONL（每行 flush，含 `t`/`seq`/`src`），
于是测量脚本可以精确配对：

    变化时间戳(本文件)  →  帧到达时间(流客户端)  →  像素变化时间(浏览器)

三种"变化源"（可叠加，都记进日志）：

| 源 | 开关 | 用途 |
|---|---|---|
| 定时翻页 | `--fps 20` | 内容 fps、服务端 CPU（15fps 时的开销） |
| 定时脉冲 | `--pulse-interval 1.5 --pulses 12` | 变化延迟（脉冲之间**完全静止**，便于定位"第一帧新内容"） |
| 收到按键 | `--on-key`（默认开） | 浏览器里 `POST /input` 触发变化 → 量端到端延迟（含输入链路） |

画面刻意做成**大色块网格**（而不是小字）：JPEG 有损压缩下小字会被磨掉，
"去了重却没变化"会让 fps 统计说谎。右下角的进度条保证相邻两帧**一定**不同
（即使调色板周期正好对上）。

用法：
    DISPLAY=:N python3 tools/perf-anim.py --log /path/anim.jsonl --fps 20 --duration 30
    DISPLAY=:N python3 tools/perf-anim.py --log /path/anim.jsonl --static
    DISPLAY=:N python3 tools/perf-anim.py --log /path/anim.jsonl --pulse-interval 1.5 --pulses 12

日志（JSONL，**每行一个对象**）：
    {"event":"READY","t":…,"pid":…,"display":":148","size":"1600x1000","depth":24,
     "mode":"anim|static","fps":20,"window":…}
    {"t":1774…123.456,"seq":7,"src":"tick|pulse|key|expose","ms":…}   # 每次画面变化
    {"event":"DONE","t":…,"seq":7,"flips":7}

退出码：0=正常结束（含 SIGTERM）；2=打不开显示；3=参数错误。
"""

from __future__ import annotations

import ctypes
import ctypes.util
import json
import os
import select
import signal
import sys
import time

# --------------------------------------------------------------------- X11 常量
KeyPress = 2
Expose = 12
ConfigureNotify = 22

KeyPressMask = 1 << 0
ExposureMask = 1 << 15
StructureNotifyMask = 1 << 17

RevertToParent = 2
CurrentTime = 0

#: 16 个高对比色（大色块用；相邻格子的颜色差得足够远，JPEG 压缩磨不掉）
PALETTE = [
    0x101820, 0xE03030, 0x30C060, 0x3060E0, 0xE0C030, 0xC030C0, 0x30D0D0, 0xF0F0F0,
    0x802020, 0x208040, 0x203080, 0x808020, 0x602060, 0x206060, 0x404040, 0xFF8000,
]


class XKeyEvent(ctypes.Structure):
    """只用到 type/keycode 两个字段，但仍按完整布局声明 —— 少一份容易写错的偏移。"""

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
        ("keycode", ctypes.c_uint),
        ("same_screen", ctypes.c_int),
    ]


class XEvent(ctypes.Union):
    _fields_ = [("type", ctypes.c_int), ("xkey", XKeyEvent), ("pad", ctypes.c_long * 24)]


def load_x11() -> ctypes.CDLL:
    for name in (ctypes.util.find_library("X11"), "libX11.so.6", "libX11.so",
                 "/usr/lib/x86_64-linux-gnu/libX11.so.6"):
        if not name:
            continue
        try:
            return ctypes.CDLL(name)
        except OSError:
            continue
    raise OSError("加载 libX11 失败（装了 Xvfb 就有）")


def parse_args(argv: list[str]) -> dict:
    opts: dict = {"log": "anim.jsonl", "fps": 0.0, "duration": 0.0, "geometry": None,
                  "static": False, "pulse_interval": 0.0, "pulses": 0, "on_key": True,
                  "cols": 8, "rows": 5, "title": "DSH perf anim", "label": "PERF"}
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "--log" and i + 1 < len(argv):
            opts["log"] = argv[i + 1]; i += 2
        elif a == "--fps" and i + 1 < len(argv):
            opts["fps"] = float(argv[i + 1]); i += 2
        elif a == "--duration" and i + 1 < len(argv):
            opts["duration"] = float(argv[i + 1]); i += 2
        elif a == "--geometry" and i + 1 < len(argv):
            opts["geometry"] = argv[i + 1]; i += 2
        elif a == "--pulse-interval" and i + 1 < len(argv):
            opts["pulse_interval"] = float(argv[i + 1]); i += 2
        elif a == "--pulses" and i + 1 < len(argv):
            opts["pulses"] = int(argv[i + 1]); i += 2
        elif a == "--cols" and i + 1 < len(argv):
            opts["cols"] = max(1, int(argv[i + 1])); i += 2
        elif a == "--rows" and i + 1 < len(argv):
            opts["rows"] = max(1, int(argv[i + 1])); i += 2
        elif a == "--title" and i + 1 < len(argv):
            opts["title"] = argv[i + 1]; i += 2
        elif a == "--label" and i + 1 < len(argv):
            opts["label"] = argv[i + 1]; i += 2
        elif a == "--static":
            opts["static"] = True; i += 1
        elif a == "--no-key":
            opts["on_key"] = False; i += 1
        elif a in ("-h", "--help"):
            print(__doc__)
            raise SystemExit(0)
        else:
            print(f"未知参数：{a}", file=sys.stderr)
            raise SystemExit(3)
    if opts["static"] and opts["fps"]:
        print("--static 与 --fps 互斥", file=sys.stderr)
        raise SystemExit(3)
    return opts


class Anim:
    def __init__(self, opts: dict) -> None:
        self.opts = opts
        self.lib = load_x11()
        self._declare()
        self.dpy = None
        self.win = 0
        self.gc = None
        self.seq = 0
        self.flips = 0
        self.width = self.height = 0
        self.depth = 24
        self.color_px: list[int] = []
        self.stopping = False
        self.stop_reason = None
        self._logf = open(opts["log"], "a", encoding="utf-8", buffering=1)

    # ------------------------------------------------------------------ ctypes 声明
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
        lib.XDefaultDepth.restype = ctypes.c_int
        lib.XDefaultDepth.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.XBlackPixel.restype = ctypes.c_ulong
        lib.XBlackPixel.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.XCreateSimpleWindow.restype = ctypes.c_ulong
        lib.XCreateSimpleWindow.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_int,
            ctypes.c_uint, ctypes.c_uint, ctypes.c_uint, ctypes.c_ulong, ctypes.c_ulong]
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
        lib.XCreateGC.restype = ctypes.c_void_p
        lib.XCreateGC.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong,
                                  ctypes.c_void_p]
        lib.XSetForeground.restype = ctypes.c_int
        lib.XSetForeground.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
        lib.XFillRectangle.restype = ctypes.c_int
        lib.XFillRectangle.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p,
                                       ctypes.c_int, ctypes.c_int, ctypes.c_uint,
                                       ctypes.c_uint]
        lib.XDrawString.restype = ctypes.c_int
        lib.XDrawString.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p,
                                    ctypes.c_int, ctypes.c_int, ctypes.c_char_p,
                                    ctypes.c_int]
        lib.XSetInputFocus.restype = ctypes.c_int
        lib.XSetInputFocus.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int,
                                       ctypes.c_ulong]
        lib.XNextEvent.restype = ctypes.c_int
        lib.XNextEvent.argtypes = [ctypes.c_void_p, ctypes.POINTER(XEvent)]
        lib.XPending.restype = ctypes.c_int
        lib.XPending.argtypes = [ctypes.c_void_p]
        lib.XConnectionNumber.restype = ctypes.c_int
        lib.XConnectionNumber.argtypes = [ctypes.c_void_p]

    # ------------------------------------------------------------------ 日志
    def log(self, obj: dict) -> None:
        self._logf.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self._logf.flush()

    # ------------------------------------------------------------------ 启动
    def open(self) -> None:
        self.dpy = self.lib.XOpenDisplay(None)
        if not self.dpy:
            raise RuntimeError("cannot open display " + repr(os.environ.get("DISPLAY")))
        scr = self.lib.XDefaultScreen(self.dpy)
        self.width = self.lib.XDisplayWidth(self.dpy, scr)
        self.height = self.lib.XDisplayHeight(self.dpy, scr)
        self.depth = self.lib.XDefaultDepth(self.dpy, scr)
        x = y = 0
        w, h = self.width, self.height
        geo = self.opts["geometry"]
        if geo:                                     # WxH+X+Y
            plus = geo.find("+", 1)
            size, pos = (geo[:plus], geo[plus:]) if plus > 0 else (geo, "")
            if "x" in size:
                w, h = (int(v) for v in size.split("x", 1))
            if pos:
                parts = [p for p in pos.split("+") if p]
                if len(parts) >= 2:
                    x, y = int(parts[0]), int(parts[1])
        self.win = self.lib.XCreateSimpleWindow(
            self.dpy, self.lib.XRootWindow(self.dpy, scr), x, y, w, h, 0,
            self.lib.XBlackPixel(self.dpy, scr), self.lib.XBlackPixel(self.dpy, scr))
        self.lib.XStoreName(self.dpy, self.win, self.opts["title"].encode())
        self.lib.XSelectInput(self.dpy, self.win,
                              KeyPressMask | ExposureMask | StructureNotifyMask)
        self.lib.XMapWindow(self.dpy, self.win)
        self.gc = self.lib.XCreateGC(self.dpy, self.win, 0, None)
        # 24 位 TrueColor 下 pixel 就是 0xRRGGBB（Xvfb 默认 24 位）；
        # 其它深度退化成黑白交替 —— 画面仍然会变，只是不好看，量的是变化不是审美。
        if self.depth >= 24:
            self.color_px = list(PALETTE)
        else:
            self.color_px = [0x000000, 0xFFFFFF]
        self.lib.XFlush(self.dpy)
        # 无窗口管理器时焦点是 PointerRoot：显式拿过来，按键注入才有着落（同 xtarget）
        for _ in range(20):
            self.lib.XSetInputFocus(self.dpy, self.win, RevertToParent, CurrentTime)
            self.lib.XSync(self.dpy, False)
            time.sleep(0.05)
        mode = "static" if self.opts["static"] else (
            f"anim@{self.opts['fps']:g}" if self.opts["fps"] else "pulse")
        self.log({"event": "READY", "t": time.time(), "pid": os.getpid(),
                  "display": os.environ.get("DISPLAY", "?"),
                  "size": f"{self.width}x{self.height}", "depth": self.depth,
                  "mode": mode, "fps": self.opts["fps"],
                  "pulseInterval": self.opts["pulse_interval"] or None,
                  "window": self.win, "label": self.opts["label"]})
        self.draw(src="init")

    # ------------------------------------------------------------------ 画面
    def draw(self, src: str) -> None:
        """画一帧**一定与上一帧不同**的大色块图，并记下发画时刻。

        时间戳写在 `XFlush` **之后**：写进日志的时刻必须晚于像素真的进 X 服务器的时刻，
        否则延迟会被系统性低估（虽然只有百微秒级，但方向必须是保守的那一边）。
        """
        try:
            cols, rows = self.opts["cols"], self.opts["rows"]
            cw = max(1, self.width // cols)
            ch = max(1, self.height // rows)
            lib, dpy, win, gc = self.lib, self.dpy, self.win, self.gc
            for j in range(rows):
                for i in range(cols):
                    lib.XSetForeground(
                        dpy, gc, self.color_px[(self.seq * 7 + i * 3 + j * 5)
                                               % len(self.color_px)])
                    lib.XFillRectangle(dpy, win, gc, i * cw, j * ch, cw, ch)
            # 进度条：即使调色板周期正好重复，这一条也保证相邻帧不同
            bar = int((self.seq % 40) / 40 * (self.width - 8)) + 4
            lib.XSetForeground(dpy, gc, 0xFFFFFF)
            lib.XFillRectangle(dpy, win, gc, 0, self.height - 60, bar, 40)
            lib.XSetForeground(dpy, gc, 0x000000)
            lib.XFillRectangle(dpy, win, gc, bar, self.height - 60,
                               max(1, self.width - bar), 40)
            label = f"{self.opts['label']} seq={self.seq} {src}".encode()
            try:
                lib.XDrawString(dpy, win, gc, 12, 28, label, len(label))
            except Exception:                             # noqa: BLE001
                pass
            lib.XFlush(dpy)
        except Exception as exc:                              # noqa: BLE001
            self.log({"event": "DRAWERR", "t": time.time(), "error": repr(exc)})
            return
        self.flips += 1
        self.log({"t": time.time(), "seq": self.seq, "src": src})

    def flip(self, src: str) -> None:
        self.seq += 1
        self.draw(src=src)

    # ------------------------------------------------------------------ 主循环
    def run(self) -> int:
        o = self.opts
        deadline = (time.time() + o["duration"]) if o["duration"] else None
        tick = (1.0 / o["fps"]) if o["fps"] else 0.0
        next_tick = time.time() + tick if tick else 0.0
        pulses_left = o["pulses"]
        next_pulse = (time.time() + o["pulse_interval"]
                      if (o["pulse_interval"] and pulses_left) else 0.0)

        def _stop(sig=None, _frm=None):
            # 退出原因必须记下来：'deadline' = 自己到点收工；'signal' = **被别人杀了**。
            # 测量时"靶画面比预期早死"会让延迟样本全变 null —— 没有这一行就只能猜（踩过）。
            self.stopping = True
            self.stop_reason = f"signal:{signal.Signals(sig).name if sig else '?'}"
        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)

        while not self.stopping:
            now = time.time()
            if deadline and now >= deadline:
                self.stop_reason = self.stop_reason or "deadline"
                break
            if tick and now >= next_tick:
                self.flip("tick")
                next_tick += tick
                if next_tick < now:                     # 落后了就丢拍，不追（追赶会造假 fps）
                    next_tick = now + tick
            if next_pulse and now >= next_pulse:
                self.flip("pulse")
                pulses_left -= 1
                next_pulse = (next_pulse + o["pulse_interval"]) if pulses_left else 0.0
            waits = [w for w in (next_tick, next_pulse, deadline) if w]
            wait = min(0.05, max(0.0, min(waits) - time.time())) if waits else 0.05
            fd = self.lib.XConnectionNumber(self.dpy)
            try:
                select.select([fd], [], [], wait)
            except Exception:                                # noqa: BLE001
                time.sleep(wait)
            while self.lib.XPending(self.dpy):
                ev = XEvent()
                self.lib.XNextEvent(self.dpy, ctypes.byref(ev))
                if ev.type == Expose:
                    self.draw(src="expose")
                elif ev.type == KeyPress and o["on_key"]:
                    self.flip("key")
        self.log({"event": "DONE", "t": time.time(), "seq": self.seq,
                  "flips": self.flips, "reason": self.stop_reason or "loop-exit",
                  "parent": os.getppid(), "pgid": os.getpgid(0)})
        return 0


def main(argv: list[str]) -> int:
    opts = parse_args(argv)
    try:
        anim = Anim(opts)
        anim.open()
    except OSError as exc:
        print(f"perf-anim: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:                                  # noqa: BLE001
        print(f"perf-anim: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return anim.run()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
