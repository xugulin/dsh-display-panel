#!/usr/bin/env python3
"""实时会话墙：把某个 DSH 会话的**实时记录**动画演示到它自己的显示器上。

用途：把"这个会话正在干什么"投到那台会话的显示上 —— 适合演示、挂机看进度、
或者让旁边的人（或另一块屏）知道 AI 正在忙什么。

数据源：会话记录 ``<home>/sessions/<项目编码>/<sid>/session.v4.jsonl.zstd``
（zstd 压缩的 JSONL，边写边追加）。整个文件一读一解压只要 ~20ms，所以"文件变了就重读"
就是最省事也最可靠的做法 —— 不需要碰 DSH 的内部 RPC。

画面（默认 1600x1000）：
   头部  ● LIVE + 标题 + 会话 id + 事件计数 + 时钟（红点每秒呼吸）
   主体  最近 N 条事件，按类型配色 + 左侧色条（用户/助手/工具/结果/任务/成员/传讯/步骤）
   底部  记录文件大小 + 最后更新时刻 + 一条来回扫的进度线；新事件到达时底部高亮闪一下

性能：整帧合成只在**内容变化**时做（文字行用 ImageMagick 渲染一次就缓存到磁盘）；
每帧只重画头/尾两条小带和扫描线，所以 30fps 的开销很小。

依赖：X11（libX11 + 会话显示）、ImageMagick（magick）、中文字体（默认 Noto-Sans-CJK-HK）、
Python 3.14 的 compression.zstd（或系统 zstdcat）。

用法：
   # 直接在某台显示上跑（DISPLAY 由 display_panel_run / 宿主 exec 提供）
   python3 tools/session-wall.py --file <session.v4.jsonl.zstd> --title 内存卡检测 --sid session-xxxx

   # 只解析记录、不画（自检/调试用）
   python3 tools/session-wall.py --file <...> --dry-run

   在 DSH 里挂到某个会话的显示上（跨会话，走宿主 HTTP 接口）：
   curl -X POST -H 'content-type: application/json' \
     -d '{"argv":["python3","tools/session-wall.py","--file","<记录>","--title","内存卡检测"],"wait":false}' \
     "http://127.0.0.1:<GUI端口>/api/dsh-display-panel/exec?session=<sid>"

⚠️ 三个实测踩过的坑（改这份代码前先读）：
   1. XCreateImage 传 data=NULL 时本机 libX11 **不会**替你分配缓冲（image->data 是 NULL），
      直接 memmove 进去就是段错误 → 自己 malloc 再交给 X；XDestroyImage 是**宏**，
      libX11 没导出，得手动 free。
   2. ImageMagick 的 label:/caption: 会换行、还继承外层 -size，配合 -extent 的垂直居中
      裁剪会把两行"叠"进一条行带（看着像文字被复制）；而且 % 是转义引导符，
      `date '+%H:%M:%S'` 会被吃掉 → 用 -annotate（单行）+ %% 转义。
   3. 别在普通 shell 里直接跑：桌面环境下 DISPLAY 往往指向**用户真实桌面**，
      脚本会画到用户屏幕上；要用会话自己的 DISPLAY。

"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import subprocess
import sys
import time

FONT = "Noto-Sans-CJK-HK"
BG_TOP = (0x0A, 0x14, 0x22)
BG_BOT = (0x0F, 0x22, 0x33)

#: 事件类型 → (中文标签, 文字颜色, 左侧色条颜色)
KINDS = {
    "user": ("用户", "#ffe6a8", "#f5a524"),
    "assistant": ("助手", "#e8f1ff", "#4c8dff"),
    "tool": ("工具", "#cfe3ff", "#22b8cf"),
    "result": ("结果", "#9fb4c8", "#3b5b78"),
    "task": ("任务", "#d8ffe8", "#22c55e"),
    "member": ("成员", "#ffe0f2", "#e64980"),
    "message": ("传讯", "#ffe9d6", "#f76707"),
    "title": ("标题", "#ffffff", "#8b5cf6"),
    "step": ("步骤", "#8fa3b8", "#31465c"),
    "other": ("事件", "#9fb4c8", "#3b5b78"),
}


# --------------------------------------------------------------------------- 记录读取


def _text_of(content) -> str:
    """把 content 数组里的 text 拼起来（跳过 reasoning：那是模型的内心戏）。"""
    out = []
    for item in content or []:
        if isinstance(item, dict) and item.get("type") == "text":
            out.append(str(item.get("text") or ""))
    return "\n".join(x for x in out if x).strip()


def _clip(s: str, n: int) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def read_records(path: str) -> list[dict]:
    """整份解压 + 解析成记录列表（团队看板这类"要原始字段"的用法读它）。"""
    if path.endswith(".zstd"):
        try:
            from compression import zstd  # Python 3.14 stdlib
            with open(path, "rb") as fh:
                raw = zstd.decompress(fh.read())
        except ImportError:          # 老 Python：退回系统 zstdcat
            with open(path, "rb") as fh:
                raw = subprocess.run(["zstdcat"], stdin=fh, capture_output=True).stdout
    else:
        with open(path, "rb") as fh:
            raw = fh.read()
    out = []
    for line in raw.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:            # noqa: BLE001
            continue
    return out


def read_events(path: str) -> tuple[list[dict], dict]:
    """整份解压 + 解析 → 事件列表（时间升序）。返回 (events, meta)。"""
    if path.endswith(".zstd"):
        try:
            from compression import zstd  # Python 3.14 stdlib
            with open(path, "rb") as fh:
                raw = zstd.decompress(fh.read())
        except ImportError:          # 老 Python：退回系统 zstdcat（一样是整份解压）
            with open(path, "rb") as fh:
                raw = subprocess.run(["zstdcat"], stdin=fh, capture_output=True).stdout
    else:                            # 未压缩的 JSONL：自检与跨平台测试用得上
        with open(path, "rb") as fh:
            raw = fh.read()
    text = raw.decode("utf-8", "replace")
    events: list[dict] = []
    title = ""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        kind = rec.get("type") or ""
        data = rec.get("data") or {}
        ts = rec.get("time") or 0
        if kind == "user/message":
            body = _text_of(data.get("content"))
            if body:
                events.append({"kind": "user", "text": _clip(body, 150), "ts": ts})
        elif kind == "assistant/message":
            msg = data.get("message") or {}
            body = _text_of(msg.get("content"))
            if body:
                events.append({"kind": "assistant", "text": _clip(body, 150), "ts": ts})
        elif kind == "tool/call":
            name = data.get("name") or "?"
            args = data.get("arguments") or ""
            try:
                parsed = json.loads(args) if isinstance(args, str) else args
                if isinstance(parsed, dict):
                    args = next(iter(parsed.values()), "") if parsed else ""
            except Exception:  # noqa: BLE001
                pass
            events.append({"kind": "tool", "text": f"{name} · {_clip(args, 100)}", "ts": ts})
        elif kind == "tool/result":
            msg = data.get("message") or {}
            body = _text_of(msg.get("content"))
            events.append({"kind": "result", "text": _clip(body or "（返回）", 110), "ts": ts})
        elif kind == "team/task":
            task = data.get("task") or {}
            sub = task.get("subject") or task.get("status") or ""
            events.append({"kind": "task", "text": f"{task.get('id', '')} {_clip(sub, 90)}".strip(), "ts": ts})
        elif kind == "team/member":
            member = data.get("member") or {}
            events.append({"kind": "member", "text": f"{member.get('name', '')} · {_clip(member.get('description', ''), 80)}".strip(" ·"), "ts": ts})
        elif kind.startswith("team/message/"):
            events.append({"kind": "message", "text": f"{kind.split('/')[-1]} → {_clip(data.get('targetId', ''), 40)}", "ts": ts})
        elif kind == "session/title":
            title = str(data.get("title") or "")
        elif kind == "step/start":
            events.append({"kind": "step", "text": f"第 {data.get('step', '?')} 步", "ts": ts})
    meta = {"events": len(events), "title": title, "bytes": os.path.getsize(path), "mtime": os.path.getmtime(path)}
    return events, meta


def fit(text: str, width_px: int, pointsize: int) -> str:
    """按像素宽度预算把一行文本截断（label: 是单行，不截就会横向溢出窗口）。

    字宽估算：CJK 约 1.0 em、ASCII 约 0.55 em —— 够用，宁可留点余量。
    """
    budget = width_px / float(pointsize)
    used = 0.0
    out = []
    for ch in text:
        cw = 1.0 if ord(ch) > 0x2E80 else 0.55
        if used + cw > budget:
            return "".join(out) + "…"
        used += cw
        out.append(ch)
    return text


# --------------------------------------------------------------------------- 文字渲染


class Strips:
    """把一行文字渲染成 BGRA 像素（ImageMagick 一次调用；按内容哈希缓存到磁盘）。"""

    def __init__(self, cache_dir: str, width: int) -> None:
        self.dir = cache_dir
        self.width = width
        os.makedirs(cache_dir, exist_ok=True)
        self.mem: dict[str, tuple[bytes, int, int]] = {}

    def get(self, text: str, fg: str, bar: str, height: int, pointsize: int) -> tuple[bytes, int, int]:
        key = hashlib.sha1(f"{self.width}|{height}|{pointsize}|{fg}|{bar}|{text}".encode()).hexdigest()
        if key in self.mem:
            return self.mem[key]
        raw_path = os.path.join(self.dir, key + ".bgra")
        if os.path.exists(raw_path):
            with open(raw_path, "rb") as fh:
                buf = fh.read()
        else:
            # ⚠️ 这里必须用 **-annotate**（单行、不换行、不参与 -size 排版）：
            #    · `caption:` 会在宽度不够时换行，超出固定行高被裁掉、残余压到下一行；
            #    · `label:` 更阴 —— 它会**继承外层的 -size**，于是同样换行成两行，而后面
            #      `-extent`（gravity west = 左+垂直居中）会把两行的"上半截"叠在同一条行带里，
            #      看起来像文字被复制了一遍（实测：一行里出现两次同样的前缀）。
            #    · 文本里的 `%` 是 ImageMagick 的转义引导符（`date '+%H:%M:%S'` 会被吃掉），
            #      所以要写成 `%%`。
            safe = text.replace("%", "%%")
            cmd = [
                "magick",
                "-size", f"{self.width}x{height}", "xc:#0b1220",
                "-font", FONT, "-pointsize", str(pointsize), "-fill", fg,
                "-gravity", "west", "-annotate", "+20+0", safe,
                "-fill", bar, "-draw", f"rectangle 0,0 6,{height}",
                "-alpha", "remove", "-depth", "8", "bgra:-",
            ]
            done = subprocess.run(cmd, capture_output=True)
            if done.returncode != 0:
                # 渲染失败（字体/参数问题）不能让整面墙挂掉：退回纯色条
                buf = bytes(self.width * height * 4)
            else:
                buf = done.stdout
            want = self.width * height * 4
            if len(buf) != want:
                sys.stderr.write(f"[wall] strip 尺寸异常 {len(buf)} != {want}（text={text[:40]!r}）\n")
                try:
                    os.unlink(raw_path)
                except OSError:
                    pass
                buf = (buf + bytes(want))[:want]
            with open(raw_path, "wb") as fh:
                fh.write(buf)
        self.mem[key] = (buf, self.width, height)
        return self.mem[key]


# --------------------------------------------------------------------------- X11


class XImage(ctypes.Structure):
    _fields_ = [
        ("width", ctypes.c_int), ("height", ctypes.c_int), ("xoffset", ctypes.c_int),
        ("format", ctypes.c_int), ("data", ctypes.c_void_p), ("byte_order", ctypes.c_int),
        ("bitmap_unit", ctypes.c_int), ("bitmap_bit_order", ctypes.c_int), ("bitmap_pad", ctypes.c_int),
        ("depth", ctypes.c_int), ("bytes_per_line", ctypes.c_int), ("bits_per_pixel", ctypes.c_int),
        ("red_mask", ctypes.c_ulong), ("green_mask", ctypes.c_ulong), ("blue_mask", ctypes.c_ulong),
    ]


class X:
    """够用的 Xlib 封装：开窗 + 把 BGRA 缓冲整块贴上去 + 画色块。"""

    def __init__(self, width: int, height: int, title: str) -> None:
        self.lib = ctypes.CDLL("libX11.so.6")
        L = self.lib
        L.XOpenDisplay.restype = ctypes.c_void_p
        L.XOpenDisplay.argtypes = [ctypes.c_char_p]
        L.XDefaultScreen.argtypes = [ctypes.c_void_p]
        L.XDefaultScreen.restype = ctypes.c_int
        L.XRootWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        L.XRootWindow.restype = ctypes.c_ulong
        L.XDefaultVisual.argtypes = [ctypes.c_void_p, ctypes.c_int]
        L.XDefaultVisual.restype = ctypes.c_void_p
        L.XDefaultDepth.argtypes = [ctypes.c_void_p, ctypes.c_int]
        L.XDefaultDepth.restype = ctypes.c_int
        L.XCreateSimpleWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_int,
                                          ctypes.c_uint, ctypes.c_uint, ctypes.c_uint, ctypes.c_ulong, ctypes.c_ulong]
        L.XCreateSimpleWindow.restype = ctypes.c_ulong
        L.XStoreName.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_char_p]
        L.XMapWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        L.XCreateGC.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p]
        L.XCreateGC.restype = ctypes.c_void_p
        L.XCreateImage.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_char_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_int, ctypes.c_int]
        L.XCreateImage.restype = ctypes.c_void_p
        L.XPutImage.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_void_p,
                                ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint, ctypes.c_uint]
        L.XSetForeground.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
        L.XFillRectangle.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p,
                                     ctypes.c_int, ctypes.c_int, ctypes.c_uint, ctypes.c_uint]
        L.XFlush.argtypes = [ctypes.c_void_p]
        L.XDestroyImage.argtypes = [ctypes.c_void_p]
        L.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
        L.XClearWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]

        self.dpy = L.XOpenDisplay(None)
        if not self.dpy:
            raise RuntimeError("开不了 X 显示（DISPLAY 对不对？）")
        self.screen = L.XDefaultScreen(self.dpy)
        self.root = L.XRootWindow(self.dpy, self.screen)
        self.visual = L.XDefaultVisual(self.dpy, self.screen)
        self.depth = L.XDefaultDepth(self.dpy, self.screen)
        self.w = width
        self.h = height
        self.win = L.XCreateSimpleWindow(self.dpy, self.root, 0, 0, width, height, 0, 0, 0)
        L.XStoreName(self.dpy, self.win, title.encode("utf-8"))
        L.XMapWindow(self.dpy, self.win)
        self.gc = L.XCreateGC(self.dpy, self.win, 0, None)
        L.XFlush(self.dpy)
        #: 图像缓冲要自己 malloc 再交给 X（见 image() 的注释）；XDestroyImage 是宏，手动等价实现。
        self.libc = ctypes.CDLL("libc.so.6")
        self.libc.malloc.restype = ctypes.c_void_p
        self.libc.malloc.argtypes = [ctypes.c_size_t]
        self.libc.free.argtypes = [ctypes.c_void_p]

    def image(self, buf: bytes, width: int, height: int) -> ctypes.c_void_p:
        """建一个 ZPixmap 的 XImage 并把 BGRA 拷进去。

        ⚠️ 两个坑（都是实测踩出来的）：
        1. **不能**让 X 分配数据（`data=NULL`）：本机 libX11 下 XCreateImage 回来的
           `image->data` 是 NULL —— 直接 memmove 进去就是段错误（核心转储，没有 Python 栈，
           只能用 faulthandler 看到是 ffi_call 崩的）。所以要自己 malloc 一块交给它，
           XDestroyImage 时再由 Xfree 回收（Xfree 就是 free）。
        2. `XDestroyImage` 在 Xlib 里是**宏**，libX11 没导出这个符号 —— 手动实现 destroy()。
        """
        bpl = width * 4
        need = bpl * height
        mem = self.libc.malloc(need)
        if not mem:
            raise RuntimeError("malloc 失败")
        ptr = self.lib.XCreateImage(self.dpy, self.visual, self.depth, 2, 0,
                                    ctypes.cast(mem, ctypes.c_char_p), width, height, 32, bpl)
        if not ptr:
            self.libc.free(mem)
            raise RuntimeError("XCreateImage 失败")
        data = bytes(buf)                       # memmove 不收 bytearray
        if len(data) < need:
            data += bytes(need - len(data))
        ctypes.memmove(ctypes.c_void_p(mem), data, need)
        return ptr

    def destroy(self, ptr) -> None:
        """XDestroyImage 的等价物（它是宏，libX11 里没有这个符号）。"""
        if not ptr:
            return
        img = ctypes.cast(ptr, ctypes.POINTER(XImage)).contents
        if img.data:
            self.libc.free(img.data)
        self.libc.free(ptr)

    def put(self, ptr: ctypes.c_void_p, w: int, h: int, x: int = 0, y: int = 0) -> None:
        self.lib.XPutImage(self.dpy, self.win, self.gc, ptr, 0, 0, x, y, w, h)

    def put_rect(self, ptr: ctypes.c_void_p, x: int, y: int, w: int, h: int) -> None:
        """从整幅画布图像里把一个小矩形**复位**回窗口（动画覆盖前的擦除；XPutImage 支持源偏移）。"""
        self.lib.XPutImage(self.dpy, self.win, self.gc, ptr, x, y, x, y, w, h)

    def rect(self, x: int, y: int, w: int, h: int, rgb: int) -> None:
        self.lib.XSetForeground(self.dpy, self.gc, rgb)
        self.lib.XFillRectangle(self.dpy, self.win, self.gc, x, y, w, h)

    def flush(self) -> None:
        self.lib.XFlush(self.dpy)


# --------------------------------------------------------------------------- 合成


def gradient(w: int, h: int, top=BG_TOP, bot=BG_BOT) -> bytearray:
    buf = bytearray(w * h * 4)
    for y in range(h):
        t = y / max(1, h - 1)
        b = int(bot[2] + (top[2] - bot[2]) * (1 - t))
        g = int(bot[1] + (top[1] - bot[1]) * (1 - t))
        r = int(bot[0] + (top[0] - bot[0]) * (1 - t))
        row = bytes((b, g, r, 255)) * w
        buf[y * w * 4:(y + 1) * w * 4] = row
    return buf


def blit(dst: bytearray, dw: int, src: bytes, sw: int, sh: int, x: int, y: int) -> None:
    """把一个 BGRA 的小块贴到大缓冲里（行与行之间只拷有效宽度）。"""
    for row in range(sh):
        dy = y + row
        if dy < 0 or dy >= (len(dst) // (dw * 4)):
            continue
        sx = max(0, -x)
        ex = min(sw, dw - x)
        if ex <= sx:
            continue
        off_d = (dy * dw + x + sx) * 4
        off_s = (row * sw + sx) * 4
        dst[off_d:off_d + (ex - sx) * 4] = src[off_s:off_s + (ex - sx) * 4]


# --------------------------------------------------------------------------- 主循环


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True)
    ap.add_argument("--title", default="DSH")
    ap.add_argument("--sid", default="")
    ap.add_argument("--width", type=int, default=1600)
    ap.add_argument("--height", type=int, default=1000)
    ap.add_argument("--fps", type=float, default=30.0)
    ap.add_argument("--cache", default="/tmp/dsh-session-wall")
    ap.add_argument("--seconds", type=float, default=0, help="跑多久（0=一直跑）")
    ap.add_argument("--dry-run", action="store_true", help="只解析记录并打印摘要，不画（不需要 X）")
    args = ap.parse_args()

    if args.dry_run:
        evs, meta = read_events(args.file)
        print(f"记录 {args.file}")
        print(f"  事件 {len(evs)} 条｜{meta['bytes']} 字节｜标题 {meta['title'] or '（无）'}")
        for e in evs[-8:]:
            label = KINDS.get(e["kind"], KINDS["other"])[0]
            print(f"  [{label}] {time.strftime('%H:%M:%S', time.localtime(e['ts'] / 1000))}  {e['text'][:90]}")
        return 0

    W, H = args.width, args.height
    HEAD, FOOT = 84, 66
    BODY_Y, BODY_H = HEAD, H - HEAD - FOOT
    LINE_H = 52
    ROWS = BODY_H // LINE_H

    strips = Strips(args.cache, W)
    x = X(W, H, "DSH_SESSION_WALL")
    bg_img = x.image(gradient(W, H), W, H)

    events: list[dict] = []
    meta: dict = {}
    body_ptr = None
    body_sig = None
    head_ptr = head_sig = None
    foot_ptr = foot_sig = None
    last_mtime = 0.0
    last_size = -1
    new_flash_until = 0.0
    scan_phase = 0.0
    t0 = time.time()
    rendered_rows: list = []

    def compose_body(rows: list[dict]) -> tuple[bytes, int, int]:
        buf = bytearray(BODY_H * W * 4)
        for i, ev in enumerate(rows):
            png = strips.get(ev["strip_text"], ev["fg"], ev["bar"], LINE_H, ev["pt"])
            blit(buf, W, png[0], png[1], png[2], 0, i * LINE_H)
        return bytes(buf), W, BODY_H

    while True:
        now = time.time()
        if args.seconds and now - t0 > args.seconds:
            break

        # ---- 记录变了就重读（真·实时：整份解压很快，790KB 约 20ms）
        try:
            st = os.stat(args.file)
            if st.st_mtime != last_mtime or st.st_size != last_size:
                last_mtime, last_size = st.st_mtime, st.st_size
                fresh, meta = read_events(args.file)
                if len(fresh) > len(events):
                    new_flash_until = now + 2.0
                events = fresh
        except Exception as exc:  # noqa: BLE001
            meta["error"] = f"{type(exc).__name__}: {exc}"

        # ---- 取最后 ROWS 条（新的在下面），每行渲染成 strip
        rows = []
        for ev in events[-ROWS:]:
            label, fg, bar = KINDS.get(ev["kind"], KINDS["other"])
            head = f"{label}   {time.strftime('%H:%M:%S', time.localtime((ev['ts'] or 0) / 1000))}   "
            rows.append({
                "strip_text": head + fit(str(ev["text"]), W - 380, 21),
                "fg": fg, "bar": bar, "pt": 21,
            })
        sig = hashlib.sha1(json.dumps([r["strip_text"] for r in rows], ensure_ascii=False).encode()).hexdigest()
        if sig != body_sig:
            body_sig = sig
            if body_ptr:
                x.destroy(body_ptr)
            buf, bw, bh = compose_body(rows)
            body_ptr = x.image(buf, bw, bh)
            rendered_rows = rows

        # ---- 头部（每秒才需要重画时钟）
        head_text = (f"● LIVE   {args.title}   ·   {args.sid or 'session'}   ·   "
                     f"事件 {meta.get('events', 0)}   ·   {time.strftime('%H:%M:%S')}")
        if head_text != head_sig:
            head_sig = head_text
            if head_ptr:
                x.destroy(head_ptr)
            b, bw, bh = strips.get(fit(head_text, W - 40, 30), "#eaf3ff", "#ff3b30", HEAD, 30)
            head_ptr = x.image(b, bw, bh)
        foot_text = (f"记录 {meta.get('bytes', 0) // 1024} KB   ·   "
                     f"更新 {time.strftime('%H:%M:%S', time.localtime(meta.get('mtime', now)))}   ·   "
                     f"最近事件 {time.strftime('%H:%M:%S', time.localtime((events[-1]['ts'] if events else 0) / 1000))}"
                     f"{'   ·   ' + meta['error'] if meta.get('error') else ''}")
        if foot_text != foot_sig:
            foot_sig = foot_text
            if foot_ptr:
                x.destroy(foot_ptr)
            b, bw, bh = strips.get(fit(foot_text, W - 40, 22), "#a9c0d6", "#22b8cf", FOOT, 22)
            foot_ptr = x.image(b, bw, bh)

        # ---- 每帧只重画头/体/尾三块 + 两个动画元素
        x.put(bg_img, W, H)
        x.put(head_ptr, W, HEAD, 0, 0)
        x.put(body_ptr, W, BODY_H, 0, BODY_Y)
        x.put(foot_ptr, W, FOOT, 0, H - FOOT)
        # 新事件提示：底部一条呼吸的高亮
        if now < new_flash_until:
            k = 1.0 - (new_flash_until - now) / 2.0
            c = int(0x20 + 0x60 * (1 - abs(0.5 - k) * 2))
            x.rect(0, BODY_Y + BODY_H - 4, W, 4, (c << 16) | (0x9c << 8) | 0x30)
        # 扫描条：来回扫的一小段（内存卡检测的意象）
        scan_phase = (scan_phase + 0.02) % 2.0
        pos = int((scan_phase if scan_phase <= 1 else 2 - scan_phase) * (W - 220))
        x.rect(pos, H - 6, 220, 3, 0x2FD0E0)
        x.flush()

        time.sleep(max(0.0, 1.0 / args.fps))

    return 0


if __name__ == "__main__":
    sys.exit(main())
