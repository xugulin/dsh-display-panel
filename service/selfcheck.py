#!/usr/bin/env python3
"""显示器服务自检：不产生副作用，只加载模块并核对平台相关的关键事实 + 无 GUI 的接口冒烟。

用法： python3 service/selfcheck.py [dsh-display-viewer.py 的路径]

三种结果：

* ``PASS`` —— 断言成立；
* ``FAIL`` —— 断言不成立（**退出码 1**，CI 该红）；
* ``SKIP`` —— 本机条件不足（例如没装 Xvfb/xdotool），**不算失败**：
  旧版把这些做成 FAIL，于是"没有 GUI 的 CI"上必然一片红，谁也不会再看它。

Windows 那几项来自社区用户写的 check_inject.py（把那台机器上验过的断言搬过来），
Linux 这边补上等价的 x11 断言 —— 两边共用同一个脚本，避免各测一半。

**为什么值得单独一个自检**：这些点一旦错了都不会报错，只会"表现不对"——
    INPUT 结构大小写错 → SendInput 读错内存（可能崩、可能乱动鼠标）
    DOM 键名漏映射     → 那个键在面板里就是没反应（退格/回车都踩过）
    滚轮方向映射反了   → Windows 上滚轮反着走（dy>0 应该是向下滚）
    会话 id 不校验     → 路径穿越
    探测接口建会话     → 宿主一次探测就把 Xvfb 拉起来
    页面模板占位符没替换 / sid 没转义 → 页面显示 "{disp}"、甚至 XSS

**自检本身绝不允许碰用户的真实状态目录**：除非外部已经设了 ``DSH_DISPLAY_HOME``，
这里一律把它指到临时目录 —— 旧版会在 `~/.cache/dsh-display` 里写令牌文件。
"""

from __future__ import annotations

import ctypes
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
import zlib
from pathlib import Path

RESULTS: list = []          # (名称, "PASS"/"FAIL"/"SKIP", 详情)
_TEMP_HOME = None


def check(name: str, cond: bool, detail: object = "") -> None:
    RESULTS.append((name, "PASS" if cond else "FAIL", str(detail)))
    print(("PASS  " if cond else "FAIL  ") + name + (("  — " + str(detail)) if detail else ""))


def skip(name: str, detail: object = "") -> None:
    RESULTS.append((name, "SKIP", str(detail)))
    print("SKIP  " + name + (("  — " + str(detail)) if detail else ""))


def _isolate_home() -> None:
    """把状态目录隔离到临时目录，绝不动用户的 ~/.cache/dsh-display。"""
    global _TEMP_HOME
    if not os.environ.get("DSH_DISPLAY_HOME"):
        _TEMP_HOME = tempfile.mkdtemp(prefix="dsh-display-selfcheck-")
        os.environ["DSH_DISPLAY_HOME"] = _TEMP_HOME
    # 自检从不 bind 这个端口（它自己绑随机端口），设 0 只是为了不落回 8099 的语义。
    os.environ.setdefault("DSH_VIEW_PORT", "0")


def load_viewer(path: Path):
    spec = importlib.util.spec_from_file_location("viewer", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["viewer"] = mod
    spec.loader.exec_module(mod)          # __name__ != "__main__" → 不会真的起服务
    return mod


# ---------------------------------------------------------------- 无 GUI 的接口冒烟
class _Client:
    """极简 HTTP 客户端：(状态码, body, 响应头, HTTP 版本)，连不上也不抛异常。"""

    def __init__(self, port: int) -> None:
        self.port = port
        self.base = f"http://127.0.0.1:{port}"

    @staticmethod
    def _fetch(req) -> tuple:
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, resp.read(), dict(resp.headers), getattr(resp, "version", 0)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), dict(exc.headers), getattr(exc, "version", 0)
        except Exception as exc:                         # noqa: BLE001 - 连接被关/超时
            return 0, str(exc).encode(), {}, 0

    def get(self, path: str, token: str = "") -> tuple:
        url = self.base + path + (("&" if "?" in path else "?") + "k=" + token if token else "")
        return self._fetch(urllib.request.Request(url, method="GET"))

    def post(self, path: str, body, token: str = "") -> tuple:
        url = self.base + path + ("?k=" + token if token else "")
        req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        return self._fetch(req)


def interface_checks(mod) -> None:
    """起一个**进程内**的 HTTP 服务打接口：不需要 GUI、不需要 Xvfb。

    最有价值的一条：把 ``mod.session`` 换成会抛异常的桩 —— 探测类接口
    （``/health``、``/``、``/state``、未创建会话的 ``/s/<sid>/state``）**只要碰一下
    会话工厂就会立刻炸出来**，比事后数 sessions 更硬。
    """
    from http.server import ThreadingHTTPServer

    try:
        srv = ThreadingHTTPServer(("127.0.0.1", 0), mod.Handler)
    except OSError as exc:                             # 理论上不会（绑随机端口）
        skip("接口冒烟（进程内 HTTP 服务）", f"绑端口失败：{exc}")
        return
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    mod.PORT = port
    client = _Client(port)
    token = mod.TOKEN
    original_session = mod.session
    created: list = []

    def poisoned(sid):
        created.append(sid)
        raise AssertionError(f"探测接口竟然创建了会话：{sid}")

    mod.session = poisoned
    try:
        status, body, headers, version = client.get("/health", token)
        data = json.loads(body or b"{}")
        check("/health 返回 200 + JSON ok", status == 200 and data.get("ok") is True,
              f"HTTP {status}")
        check("/health 字段齐全（契约 §1.1）",
              not [k for k in ("ok", "service", "version", "backend", "size", "input",
                               "port", "pid", "sessions", "missing") if k not in data],
              "缺: " + str([k for k in ("ok", "service", "version", "backend", "size", "input",
                                        "port", "pid", "sessions", "missing") if k not in data]))
        check("/health 报的 port 就是实际端口", data.get("port") == port, f"{data.get('port')}")
        check("/health 报的 pid 就是本进程", data.get("pid") == os.getpid(), f"{data.get('pid')}")
        check("响应头有 no-store（所有响应统一）",
              "no-store" in (headers.get("Cache-Control") or ""), headers.get("Cache-Control"))
        check("说的是 HTTP/1.1（旧实现是 1.0，每请求一条连接）",
              version == 11 and mod.Handler.protocol_version == "HTTP/1.1",
              f"响应版本 {version} · protocol_version={mod.Handler.protocol_version}")

        status, body, _h, _v = client.get("/", token)
        check("索引页可打开", status == 200 and b"DSH" in body, f"HTTP {status}")
        status, body, _h, _v = client.get("/state", token)
        check("/state（旧探测路径）无副作用且 200",
              status == 200 and json.loads(body).get("ok") is True, f"HTTP {status}")

        fresh = "selfcheck-no-side-effect"
        status, body, _h, _v = client.get(f"/s/{fresh}/state", token)
        data = json.loads(body or b"{}")
        check("未创建会话的 /state 返回 200 且 started=false",
              status == 200 and data.get("started") is False, f"HTTP {status} {data.get('started')}")
        need = ("session", "display", "backend", "size", "windows", "idle", "cursor",
                "missing", "input", "realDesktop", "tooltip",
                "startError", "ownDisplay", "ownerMarked")
        check("/state 含契约要求的字段（cursor/input/realDesktop/tooltip…）",
              not [k for k in need if k not in data],
              "缺: " + str([k for k in need if k not in data]))
        check("探测接口一个会话都没创建（session() 已换成会抛异常的桩）",
              not created and mod.session_count() == 0,
              f"created={created} sessions={mod.session_count()}")
        check("探测接口没有拉起任何显示服务器进程",
              list(getattr(mod, "_spawned", [])) == [], str(getattr(mod, "_spawned", [])))

        status, _b, _h, _v = client.get("/health")            # 不带令牌
        check("无令牌 → 403", status == 403, f"HTTP {status}")
        status, _b, _h, _v = client.get("/s/" + "a" * 65 + "/state", token)
        check("超长会话 id → 400", status == 400, f"HTTP {status}")
        status, _b, _h, _v = client.get("/s/%2e%2e%2fetc/state", token)
        check("百分号编码的路径穿越 → 400", status == 400, f"HTTP {status}")
        status, _b, _h, _v = client.get("/s/ok-id/nope", token)
        check("未知子路径 → 404", status == 404, f"HTTP {status}")

        # 输入校验必须在**创建会话之前**发生（否则一次错事件就把 Xvfb 拉起来）
        created.clear()
        status, body, _h, _v = client.post("/s/selfcheck-input/input", {"t": "nonsense"}, token)
        check("非法输入类型 → 400 且未创建会话",
              status == 400 and json.loads(body).get("ok") is False and not created,
              f"HTTP {status} created={created}")
        status, _b, _h, _v = client.post("/s/selfcheck-input/input", {"t": "click", "x": 0.5}, token)
        check("click 缺 y → 400", status == 400, f"HTTP {status}")
        status, _b, _h, _v = client.post("/s/selfcheck-input/exec", {"argv": []}, token)
        check("exec 空 argv → 400（不拉起任何东西）", status == 400, f"HTTP {status}")
        status, _b, _h, _v = client.post("/s/selfcheck-input/exec",
                                         {"argv": ["/bin/true"], "cwd": "/no/such/dir"}, token)
        check("exec 目录不存在 → 400", status == 400, f"HTTP {status}")
        status, _b, _h, _v = client.post("/s/selfcheck-input/kill", {"pid": 999999}, token)
        check("kill 不存在的 pid → 404", status == 404, f"HTTP {status}")

        # Cookie 兼容：独立页面第一次带 ?k= 之后不必再把令牌放链接里
        status, _b, headers, _v = client.get("/health", token)
        cookie = headers.get("Set-Cookie") or ""
        check("带 ?k= 的响应回种令牌 Cookie（页面可不再依赖链接里的令牌）",
              f"{mod.COOKIE_NAME}=" in cookie and "HttpOnly" in cookie, cookie[:60])
        req = urllib.request.Request(f"http://127.0.0.1:{port}/health",
                                     headers={"Cookie": f"{mod.COOKIE_NAME}={token}"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                cookie_ok = resp.status == 200
        except Exception:                              # noqa: BLE001
            cookie_ok = False
        check("只带 Cookie（不带 ?k=）也能访问", cookie_ok)
        # 帧管线相关的 HTTP 断言（用桩会话：不碰显示、不起 Xvfb）——
        # ⚠️ 必须在 shutdown() **之前**跑，否则请求会直接连不上（HTTP 0）
        pipeline_interface_checks(mod, client, token)
        keepalive_checks(mod, port, token)
        srv.shutdown()
    finally:
        mod.session = original_session
        srv.server_close()
    check("接口冒烟全程没有创建会话、没有拉起显示服务器",
          mod.session_count() == 0 and list(getattr(mod, "_spawned", [])) == [],
          f"sessions={mod.session_count()} spawned={len(getattr(mod, '_spawned', []))}")


def pipeline_checks(mod, source: str) -> None:
    """帧管线（契约 §5.2/§5.3/§5.5）：用**纯数据**断言，不需要 Xvfb、不产生副作用。

    为什么值得这一组：这些点错了都不会报错，只会"表现不对" ——
        CRC 去重写错     → 静止画面照样满带宽（用户投诉的就是这个）
        part 头少一个    → 客户端丢帧/光标冻住，而服务端日志一片正常
        档位校验不严     → quality=0 被静默夹成边界，调用方以为改成功了
        编码器用多线程   → 首帧慢到 1-3 秒（帧级多线程把头几帧憋在内部缓冲里）
    """
    # ---- 档位默认值与取值范围（契约 §5.2）
    check("默认档 quality=70 / fps=20 / scale=1（契约 §5.2；上限要高于验收线 15）",
          (mod.DEFAULT_QUALITY, mod.DEFAULT_FPS, mod.DEFAULT_SCALE) == (70, 20, 1.0),
          f"{mod.DEFAULT_QUALITY}/{mod.DEFAULT_FPS}/{mod.DEFAULT_SCALE}")
    check("档位范围 quality 1..100、fps 1..30、scale 0.25..1.0",
          (mod.MIN_QUALITY, mod.MAX_QUALITY, mod.MIN_FPS, mod.MAX_FPS,
           mod.MIN_SCALE, mod.MAX_SCALE) == (1, 100, 1, 30, 0.25, 1.0), "")

    # ---- stream-config 参数校验
    base = {"quality": 70, "fps": 15, "scale": 1.0}
    ok, err = mod.parse_stream_config({"quality": "55", "fps": 8, "scale": "0.5"}, base)
    check("stream-config 合法值全部接受（字符串数字也认）",
          err == "" and ok == {"quality": 55, "fps": 8, "scale": 0.5}, f"{ok} {err}")
    ok2, err2 = mod.parse_stream_config({"profile": "saver", "reason": "客户端在省电"},
                                        base)
    check("stream-config 忽略未知键（客户端的 profile/reason、查询串里的 k=）",
          err2 == "" and ok2 == base, f"{ok2} {err2}")
    bad_values = [{"quality": 0}, {"quality": 999}, {"quality": "abc"},
                  {"fps": 0}, {"fps": 99}, {"scale": 0.1}, {"scale": 1.5},
                  {"scale": "x"}, {"quality": True}]
    bad_ok = [p for p in bad_values
              if mod.parse_stream_config(p, base)[1] == ""]
    check("stream-config 非法值全部报错（0/999/abc/布尔/越界）", not bad_ok, str(bad_ok))
    check("stream-config 只改给了的键（没给的 / null 的保持原值）",
          mod.parse_stream_config({"fps": 5}, base)[0] == {"quality": 70, "fps": 5,
                                                          "scale": 1.0}
          and mod.parse_stream_config({"quality": None}, base) == (base, ""), "")

    # ---- CRC32 去重
    dedup = mod.FrameDedup()
    frame_a = bytes(bytearray([7]) * (mod.DEDUP_CRC_ALWAYS + 16))
    changed1, crc1 = dedup.check(frame_a)
    changed2, crc2 = dedup.check(frame_a)
    check("CRC32 去重：同一帧第二次不再算作变化（静止就不编码、不发帧）",
          changed1 is True and changed2 is False and crc1 == crc2 == zlib.crc32(frame_a),
          f"changed={changed1}/{changed2} crc={crc1}")
    frame_b = bytearray(frame_a)
    frame_b[-1] ^= 0xFF
    changed3, crc3 = dedup.check(frame_b)
    check("CRC32 去重：内容变了就放行（末尾一个字节不同也算）",
          changed3 is True and crc3 != crc1, f"changed={changed3} crc={crc3}")
    small = b"\xff\xd8jpeg-frame-1\xff\xd9"
    d2 = mod.FrameDedup()
    s1 = d2.check(small)
    s2 = d2.check(small)
    s3 = d2.check(small + b"x")
    check("小帧（JPEG）走 CRC32 当键：一样→跳过、不一样→放行",
          s1[0] is True and s2[0] is False and s3[0] is True, f"{s1} {s2} {s3}")

    # ---- MJPEG part 头（契约 §5.3：四个头一个都不能少）
    part = mod.mjpeg_part(b"\xff\xd8FAKE\xff\xd9", 42, 1730000000123, "1600x1000",
                          {"x": 0.25, "y": 0.5})
    head, _, body = part.partition(b"\r\n\r\n")
    lines = head.decode("ascii").split("\r\n")
    fields = dict(line.split(": ", 1) for line in lines[1:])
    check("MJPEG part：--frame 边界 + Content-Type 正确",
          lines[0] == "--frame" and fields.get("Content-Type") == "image/jpeg", lines[0])
    check("MJPEG part：Seq/Time/Size/Cursor 四个逐帧头齐全（宿主只透传字节，"
          "少一个客户端就少一样东西）",
          all(k in fields for k in ("X-DSH-Seq", "X-DSH-Time", "X-DSH-Size",
                                    "X-DSH-Cursor")), str(sorted(fields)))
    check("MJPEG part：Content-Length 等于 JPEG 字节数（客户端按它定长取体）",
          fields.get("Content-Length") == "8" and body == b"\xff\xd8FAKE\xff\xd9\r\n",
          fields.get("Content-Length"))
    check("MJPEG part：Seq/Time 是整数、Size 原样透传、Cursor 是归一化 x,y",
          fields.get("X-DSH-Seq") == "42" and fields.get("X-DSH-Time") == "1730000000123"
          and fields.get("X-DSH-Size") == "1600x1000"
          and fields.get("X-DSH-Cursor") == "0.2500,0.5000", str(fields))
    part_none = mod.mjpeg_part(b"\xff\xd8x\xff\xd9", 1, 1, "10x10", None)
    check("MJPEG part：拿不到指针时 Cursor 是 -1,-1（客户端据此不画光标）",
          b"X-DSH-Cursor: -1,-1" in part_none, "")

    # ---- quality → ffmpeg -q:v
    check("quality→-q:v 递减：quality 越高 q 越小（1..100 → 31..2）",
          mod.quality_to_qv(1) == 31 and mod.quality_to_qv(100) == 2
          and mod.quality_to_qv(70) < mod.quality_to_qv(30)
          and 2 <= mod.quality_to_qv(70) <= 31,
          f"70→{mod.quality_to_qv(70)} 30→{mod.quality_to_qv(30)}")

    # ---- JPEG 尺寸解析（X-DSH-Size 靠它，import/win32 路线上只有字节）
    # 手搓一张最小 JPEG：SOI + APP0（长度必须和载荷对得上）+ SOF0 + EOI
    fake = (b"\xff\xd8"
            + b"\xff\xe0" + (16).to_bytes(2, "big") + b"JFIF\0" + b"\x00" * 9
            + b"\xff\xc0" + (17).to_bytes(2, "big") + b"\x08"
            + (500).to_bytes(2, "big") + (800).to_bytes(2, "big") + b"\x03" + b"\x00" * 9
            + b"\xff\xd9")
    check("jpeg_size 能从 SOF0 段读出 (宽,高)", mod.jpeg_size(fake) == (800, 500),
          str(mod.jpeg_size(fake)))
    check("jpeg_size 对非 JPEG 数据返回 None（不瞎猜）",
          mod.jpeg_size(b"not-a-jpeg") is None and mod.jpeg_size(b"") is None, "")

    # ---- 抓帧/编码实现的关键约束（静态断言）
    check("抓帧走 ctypes + libX11 的 XGetImage（不是每帧 spawn import）",
          all(name in source for name in ("XGetImage", "class X11Screen", "XQueryPointer")),
          "")
    check("XImage 按 bytes_per_line 解析（深屏行对齐不能当紧凑排列）",
          "bytes_per_line" in source and "bits_per_pixel" in source, "")
    check("XGetImage 的返回按 data+image 两步释放（XDestroyImage 是宏，libX11 里没这个符号）",
          "XDestroyImage(" not in source and "lib.XFree(data)" in source
          and "lib.XFree(img)" in source, "")
    check("编码器必须 -threads 1（帧级多线程会把头几帧憋到 3 秒才出）",
          '"-threads", "1"' in source and "帧级多线程" in source, "")
    check("damage 驱动 + 没有 XDamage 时退回轮询",
          "XDamageCreate" in source and "xgetimage+XDamage" in source
          and "xgetimage+poll" in source and "IDLE_FPS" in source, "")
    check("回退路径没删：import / grim / win32 / darwin 都还在",
          "_grab_once" in source and "grim" in source and "win_screen" in source
          and "_grab_darwin" in source, "")
    check("自适应降档顺序 fps → scale → quality，且写进 /stats.reason",
          all(k in source for k in ('self.auto["fps"]', 'self.auto["scale"]',
                                    'self.auto["quality"]', "self.reason")), "")
    check("帧管线随会话回收（Session.stop 先停管线、再杀 Xvfb）",
          "self.pipe.stop()" in source and "帧管线收手" in source, "")
    check("keep-alive 下抽干未读 body（否则下一条请求会 501；curl 复现不出来）",
          "_drain_request_body" in source and "BODY_DRAIN_MAX" in source, "")

    # ---- /stats 字段（契约 §5.5）——用一个真 Pipeline 对象（不建会话、不起线程）
    try:
        probe = mod.Session("selfcheck-stats")
        try:
            data = mod.stats_payload("selfcheck-stats", probe)
            need = ("fps", "captured", "encoded", "skipped", "bytesPerSec", "quality",
                    "scale", "mode", "captureMs", "encodeMs", "cursor", "reason")
            check("/stats 含契约 §5.5 的全部字段",
                  not [k for k in need if k not in data],
                  "缺: " + str([k for k in need if k not in data]))
            check("/stats 默认档就是契约的默认值",
                  (data["quality"], data["scale"]) == (mod.DEFAULT_QUALITY,
                                                       mod.DEFAULT_SCALE),
                  f"{data['quality']}/{data['scale']}")
            check("/stats.mode 用约定词表（perf 脚本按它断言）",
                  data["mode"] in ("xgetimage+XDamage", "xgetimage+poll", "import", "grim",
                                   "win32", "darwin", "idle"), data["mode"])
            check("/stats 的额外字段（clients/damageEvents/lastFrameAgeMs/stream）也齐",
                  all(k in data for k in ("clients", "damageEvents", "lastFrameAgeMs",
                                          "stream", "encoder", "cursorOnly", "seq")),
                  str(sorted(data)))
            empty = mod.stats_payload("selfcheck-none", None)
            check("没有会话时 /stats 给默认档零计数（**不创建会话**）",
                  empty["captured"] == 0 and empty["quality"] == mod.DEFAULT_QUALITY
                  and empty["mode"] == "idle", f"{empty['mode']} {empty['quality']}")
        finally:
            probe.pipe.stop(timeout=0.1)
            mod.release_display(probe.sid, probe.number, forget=True)
    except Exception as exc:                             # noqa: BLE001
        check("帧管线自检（/stats 字段）", False, f"{type(exc).__name__}: {exc}")


class _StubPipe:
    """给接口冒烟用的假管线：只回答 HTTP 层问到的几个问题（**不碰显示、不起线程**）。"""

    def __init__(self, frame=b"", defaults=None):
        self.frame = frame
        self.watched = 0
        self.defaults = dict(defaults or {})
        self.config = dict(self.defaults)

    def watch(self):
        self.watched += 1

    def cached_cursor(self, max_age=1.0):
        return {"x": 0.5, "y": 0.5}

    def effective_config(self):
        return dict(self.config)

    def stats(self):
        return {"fps": 0.0, "captured": 0, "encoded": 0, "skipped": 0, "bytesPerSec": 0.0,
                "quality": self.config.get("quality"), "scale": self.config.get("scale"),
                "mode": "idle", "captureMs": 0.0, "encodeMs": 0.0, "cursor": None,
                "reason": "", "clients": 0}

    def set_config(self, cfg):
        self.config = dict(cfg)
        return dict(cfg)

    def encode_shot(self, quality, scale):
        return self.frame


class _StubSession:
    """``/snapshot`` 与 ``/stream-config`` 的桩会话（**不碰显示、不起 Xvfb**）。"""

    def __init__(self, frame=b"", defaults=None):
        self.sid = "stub"
        self.dir = tempfile.gettempdir()
        self.display = ":0"
        self.number = 0
        self.started = True
        self.latest = frame
        self.frame_error = None
        self.pipe = _StubPipe(frame, defaults)
        self.shots = []

    def ensure(self):
        return True

    def touch(self):
        pass

    def snapshot_jpeg(self, quality=None, scale=None):
        self.shots.append((quality, scale))
        return self.latest


def pipeline_interface_checks(mod, client, token) -> None:
    """HTTP 层的几条硬要求：参数非法不 5xx、keep-alive 不被 body 污染、没帧要 503。"""
    original = mod.session
    defaults = {"quality": mod.DEFAULT_QUALITY, "fps": mod.DEFAULT_FPS,
                "scale": mod.DEFAULT_SCALE}
    stub_empty = _StubSession(b"", defaults)
    stub_full = _StubSession(b"\xff\xd8stub-jpeg\xff\xd9", defaults)
    try:
        mod.session = lambda sid: stub_full
        status, body, headers, _v = client.get("/s/stub/snapshot", token)
        check("/snapshot 不带参数 → 200 + image/jpeg",
              status == 200 and headers.get("Content-Type") == "image/jpeg", f"HTTP {status}")
        status, _b, _h, _v = client.get("/s/stub/snapshot?quality=90&scale=0.5", token)
        check("/snapshot?quality=90&scale=0.5 → 单独编一帧（参数真的传下去）",
              status == 200 and stub_full.shots[-1] == (90, 0.5),
              f"HTTP {status} shots={stub_full.shots[-3:]}")
        for bad in ("quality=0", "quality=999", "quality=abc", "scale=0.1", "scale=abc",
                    "quality=-5&scale=99"):
            status, _b, _h, _v = client.get(f"/s/stub/snapshot?{bad}", token)
            if status != 200:
                break
        check("/snapshot 参数非法时忽略并按默认走（要 200，不能 4xx/5xx）", status == 200,
              f"HTTP {status}（{bad}）")
        check("/snapshot 非法参数走后不重新编码（当作没给）",
              stub_full.shots[-1] == (None, None), str(stub_full.shots[-1]))
        mod.session = lambda sid: stub_empty
        status, _b, _h, _v = client.get("/s/stub/snapshot", token)
        check("/snapshot 没有帧 → 立刻 503（不阻塞）", status == 503, f"HTTP {status}")
        status, body, _h, _v = client.get("/s/stub/stats", token)
        check("/stats 在会话存在时返回 200 + §5.5 字段",
              status == 200 and "bytesPerSec" in json.loads(body or b"{}"), f"HTTP {status}")
    finally:
        mod.session = original


def keepalive_checks(mod, port: int, token: str) -> None:
    """keep-alive 连接上"带 body 的错误请求"不能污染下一条请求（真 bug，实测 501）。

    为什么单列：``protocol_version = HTTP/1.1`` 下 Python 的 http.server **不会**替你读
    body，出错分支直接回 404 就会把 body 留成下一条请求的起始行 ——
    curl 不复用连接所以看不出来，Node/undici 与浏览器 fetch 会。
    这条测试**故意用同一条连接发两个请求**。
    """
    import http.client

    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        seen = []
        for method, path, payload in (
                ("POST", f"/s/selfcheck-keepalive/stream-config-typo?k={token}",
                 {"quality": 70, "fps": 15, "scale": 1.0}),
                ("POST", f"/s/selfcheck-keepalive/nope?k={token}", {"x": "y" * 300}),
                ("POST", f"/s/selfcheck-keepalive/stream-config?k={token}", {"quality": 0}),
                ("DELETE", f"/s/selfcheck-keepalive/nope?k={token}", {"x": 1})):
            conn.request(method, path, body=json.dumps(payload),
                         headers={"Content-Type": "application/json"})
            first = conn.getresponse()
            first.read()
            seen.append(first.status)
            conn.request("GET", f"/health?k={token}")
            second = conn.getresponse()
            raw = second.read()
            if second.status not in (200, 403):
                check("keep-alive：带 body 的错误请求之后，同连接的下一条请求正常",
                      False, f"{method} → {first.status}，随后 GET /health → "
                             f"{second.status} {raw[:120]!r}")
                return
        check("keep-alive：带 body 的错误请求之后，同连接的下一条请求正常（4 种错误路径）",
              True, f"错误码 {seen}，随后都是 200")
    except Exception as exc:                             # noqa: BLE001
        check("keep-alive：带 body 的错误请求之后，同连接的下一条请求正常",
              False, f"{type(exc).__name__}: {exc}")
    finally:
        conn.close()


def main() -> int:
    here = Path(__file__).resolve().parent
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else here / "dsh-display-viewer.py"
    _isolate_home()
    print(f"自检对象：{path}")
    print(f"状态目录（自检隔离）：{os.environ.get('DSH_DISPLAY_HOME')}\n")
    try:
        mod = load_viewer(path)
    except Exception as exc:                             # noqa: BLE001
        check(f"模块可加载（{type(exc).__name__}: {exc}）", False)
        return 1

    source = path.read_text(encoding="utf-8")

    # ---- 通用：模块级事实
    check("服务名与版本号存在", mod.SERVICE_NAME == "dsh-display-viewer" and bool(mod.VERSION),
          f"{mod.SERVICE_NAME} {mod.VERSION}")
    check("后端解析为 x11|wayland|win32|darwin",
          mod.BACKEND in ("x11", "wayland", "win32", "darwin"), mod.BACKEND)

    # ---- 通用：会话 id 白名单（路径穿越）
    good = ["a", "abc", "A-9._", "a" * 64, "ci-1234"]
    bad = ["", "a" * 65, "../x", "..", ".", "a/b", "a b", "a\\b", "a;b", "%2e%2e",
           "a\x00b", "会话", None, 123]
    check("合法会话 id 全部通过", all(mod.valid_sid(s) for s in good),
          str([s for s in good if not mod.valid_sid(s)]))
    check("非法/穿越会话 id 全部拒绝", not any(mod.valid_sid(s) for s in bad),
          str([s for s in bad if mod.valid_sid(s)]))
    check("会话 id 白名单正则是契约里的那个",
          mod.SESSION_ID_RE.pattern == r"^[A-Za-z0-9._-]{1,64}$", mod.SESSION_ID_RE.pattern)

    # ---- 通用：令牌
    check("令牌比较用 hmac.compare_digest（不是 ==）", "compare_digest" in source, "")
    saved_token = mod.TOKEN
    try:
        mod.TOKEN = "0123456789abcdef0123456789abcdef"
        tok = mod.TOKEN
        check("正确令牌通过 / 错误令牌拒绝",
              mod.token_ok(f"/health?k={tok}") and not mod.token_ok("/health?k=bad")
              and not mod.token_ok("/health"))
        check("令牌也可以从 Cookie 里来（页面兼容）",
              mod.token_ok("/health", {"Cookie": f"{mod.COOKIE_NAME}={tok}"})
              and not mod.token_ok("/health", {"Cookie": f"{mod.COOKIE_NAME}=bad"}))
        check("查询串里令牌错了就不回退到 Cookie",
              not mod.token_ok("/health?k=bad", {"Cookie": f"{mod.COOKIE_NAME}={tok}"}))
    finally:
        mod.TOKEN = saved_token

    # ---- 通用：滚轮方向（DOM 语义：dy>0 = 向下滚）
    check("wheel_ticks：dy>0 → 正刻度、dy<0 → 负刻度、0 → 0",
          mod.wheel_ticks(120) > 0 and mod.wheel_ticks(-120) < 0 and mod.wheel_ticks(0) == 0,
          f"{mod.wheel_ticks(120)}/{mod.wheel_ticks(-120)}/{mod.wheel_ticks(0)}")
    check("X11：向下滚用按钮 5、向上滚用按钮 4",
          mod.x11_wheel_button(1) == "5" and mod.x11_wheel_button(-1) == "4", "")
    check("win32：DOM 向下滚发**负** mouseData（旧实现方向反了）",
          mod.win_wheel_data(1) == -mod.WIN_WHEEL_DELTA
          and mod.win_wheel_data(-1) == mod.WIN_WHEEL_DELTA, "")
    check("darwin：DOM 向下滚取负滚动量（正值=向上滚）",
          mod.mac_scroll_units(1) < 0 and mod.mac_scroll_units(-1) > 0, "")
    check("wayland：向下滚 0x5、向上滚 0x4",
          mod.wayland_wheel_button(1) == "0x5" and mod.wayland_wheel_button(-1) == "0x4", "")

    # ---- 通用：输入事件类型与校验（新增 down/up）
    check("输入类型覆盖 click/down/up/move/wheel/text/key",
          set(mod.INPUT_TYPES) == {"click", "down", "up", "move", "wheel", "text", "key"},
          str(mod.INPUT_TYPES))
    ok_payloads = [{"t": "click", "x": 0.5, "y": 0.5}, {"t": "down", "x": 0, "y": 1},
                   {"t": "up", "x": 1, "y": 0}, {"t": "move", "x": 0.2, "y": 0.2},
                   {"t": "wheel", "dy": -120}, {"t": "text", "s": "中文显示器"},
                   {"t": "key", "k": "ctrl+Enter"}]
    check("合法事件全部通过校验",
          all(mod.validate_input(p) == "" for p in ok_payloads),
          str([p for p in ok_payloads if mod.validate_input(p)]))
    bad_payloads = [{"t": "nope"}, {"t": "click", "x": 0.5}, {"t": "down", "y": 0.5},
                    {"t": "wheel"}, {"t": "text", "s": 1}, {"t": "key", "k": " "}, {}, None]
    check("非法事件全部拒绝（含缺坐标的 down/up）",
          all(mod.validate_input(p) for p in bad_payloads),
          str([p for p in bad_payloads if not mod.validate_input(p)]))
    check("down/up 真的走 X11 的 mousedown/mouseup",
          "mousedown" in source and "mouseup" in source, "")

    # ---- 通用：空闲回收与生命周期
    check("DSH_VIEW_IDLE_MINUTES 默认 30 分钟",
          mod.IDLE_MINUTES == 30.0 and "DSH_VIEW_IDLE_MINUTES" in source, mod.IDLE_MINUTES)
    check("idle=0 表示关闭回收（不会把 0 当成'立刻回收'）",
          mod.parse_idle_minutes("0") == 0.0 and mod.parse_idle_minutes("-5") == 0.0, "")
    check("idle 非法值退回默认（不让服务崩）",
          mod.parse_idle_minutes("abc") == 30.0 and mod.parse_idle_minutes(None) == 30.0
          and mod.parse_idle_minutes("5") == 5.0, "")
    check("显示号不再是纯 crc32 取模（有锁文件占用检测）",
          "_claim_lock" in source and "displays.json" in source and "locks" in source, "")
    check("同一 sid 的显示号有持久映射（重启后同号）",
          mod.candidate_display("abc") == mod.candidate_display("abc")
          and mod.DISPLAY_BASE == 100, mod.candidate_display("abc"))
    check("抓帧失败可见（frame_error 会进 /state）",
          "frame_error" in source and "frameError" in source, "")

    # ---- 通用：显示归属校验（防止"复用到别人的显示"）
    # 为什么单列一组：X11 的 abstract socket 属于网络命名空间、而 /tmp 是各命名空间私有的，
    # 别的沙箱遗留的 Xvfb 可能占着同一个号并**能应答** —— 只按"有人应答"判断可用，
    # 就会把别人的画面当成自己的（串扰），或者等它退出后留下死号。
    check("归属标记的属性名与两个辅助函数都在",
          mod.OWNER_PROP == "DSH_DISPLAY_SESSION"
          and callable(getattr(mod, "display_owner", None))
          and callable(getattr(mod, "mark_display_owner", None)), mod.OWNER_PROP)
    check("归属标记走 libX11（ctypes），不用 xprop",
          "libX11.so.6" in source and "XChangeProperty" in source,
          "实测 xprop -root -set 返回 0 却什么都没写进去")
    check("Session 带归属校验、探测缓存与换号重试",
          all(hasattr(mod.Session, name) for name in
              ("_owns_display", "_display_answers", "_alive_probe", "_alive",
               "_startup_error", "relocate"))
          and mod.Session._ALIVE_TTL > 0
          and "for _attempt in range(3)" in source, mod.Session._ALIVE_TTL)
    check("启动失败原因可从日志里提取（already active / listening sockets）",
          "listening sockets" in source and "start_error" in source, "")
    saved_x11 = mod._x11
    try:
        mod._x11 = lambda: None                        # 假装加载不到 libX11
        check("拿不到 libX11 时归属校验宁可不认（不可用 → 换号），也不静默复用",
              mod.display_owner(":1") is None
              and mod.mark_display_owner(":1", "s") is False)
    finally:
        mod._x11 = saved_x11
    try:
        # 建一个 Session 只会占用显示号，**不会**启动 Xvfb（也不会起抓帧线程）。
        probe = mod.Session("selfcheck-owner")
        check("没真正拉起 Xvfb 时不认领归属",
              probe._owns_display() is False and probe.started is False, "")
        check("Session 带 start_error/owner_marked/relocated_from 字段（/state 里看得到）",
              all(hasattr(probe, name) for name in
                  ("start_error", "owner_marked", "relocated_from")), "")
        mod.release_display(probe.sid, probe.number, forget=True)
    except Exception as exc:                             # noqa: BLE001
        check("Session 归属相关的自检", False, f"{type(exc).__name__}: {exc}")

    # ---- 通用：页面模板（转义 + 占位符）
    try:
        # ⚠️ 模板参数要与服务端渲染处保持一致 —— 少了任何一个都是 KeyError，
        #    而这一条被 CI 抓到过（服务端加了令牌参数 k，自检这边没跟上）。
        html_old = mod.PAGE.format(base="/s/x", sid="s", disp="测试显示", w=1600, h=1000,
                                   note="只读", k="test-token")
        check("页面模板可渲染且无残留占位符",
              "{disp}" not in html_old and "{base}" not in html_old
              and "测试显示" in html_old)
    except Exception as exc:                             # noqa: BLE001
        check("页面模板可渲染且无残留占位符", False, f"{type(exc).__name__}: {exc}")
    try:
        page = mod.page_html("s1", disp="独立显示 :148", note="可操作", token="tok",
                             missing=[{"tool": "xclip", "why": "中文输入", "package": "xclip"}],
                             base="/s/s1")
        leftover = [t for t in ("__SID__", "__K__", "__DISP__", "__BASE__", "__W__", "__H__",
                                "__NOTE__", "__MISSING__", "__EXTRA__") if t in page]
        check("页面渲染：没有 __X__ 占位符残留", not leftover, str(leftover))
        check("页面渲染：也没有未替换的 {x} 花括号占位符",
              not any(t in page for t in ("{sid}", "{k}", "{disp}", "{base}", "{note}")), "")
        check("页面显示缺失依赖（README 承诺过，旧页面根本没渲染）",
              "xclip" in page and "缺少依赖" in page, "")
        check("页面显示光标位置", "光标" in page, "")
        check("页面的键名与输入法逻辑保持不变（Backspace/Enter/composition）",
              "Backspace" in page and "Enter" in page and "compositionend" in page
              and "composing" in page, "")
        evil_sid = 'x"><script>alert(1)</script>'
        evil_k = "</script><script>alert(2)</script>"
        evil = mod.page_html(evil_sid, disp="</script>", note="n", token=evil_k)
        check("页面 XSS：sid/k 双重转义（注入的 </script> 进不来）",
              evil.count("</script>") == 1 and "alert(1)</script>" not in evil
              and "alert(2)</script>" not in evil and "\\u003c/script\\u003e" in evil,
              f"</script> x{evil.count('</script>')}")
    except Exception as exc:                             # noqa: BLE001
        check("页面渲染", False, f"{type(exc).__name__}: {exc}")

    # ---- 帧管线（契约 §5.2/§5.3/§5.5，纯数据断言，不需要 GUI）
    pipeline_checks(mod, source)

    # ---- 会话回收：/exec 子进程必须真的被杀掉、stop() 一步失败不许拖累后面的步骤
    # （0.3.4 拿 procs_snapshot() 的 dict 列表当三元组解包：只要会话里跑过 /exec，
    #   /close 就抛 AttributeError → 子进程没杀、Xvfb 没停、显示号没释放、响应为空）
    try:
        probe = mod.Session("selfcheck-recycle")
        # ⚠️ start_new_session：_terminate_proc 杀的是**整个进程组**，
        #    不隔离的话这一测会把自检自己（以及 CI 的 shell）一起带走（实测踩过）。
        victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                  start_new_session=True)
        probe.procs = [(victim, ["sleep"], 0.0)]
        snapshot = probe.procs_snapshot()
        check("procs_snapshot 返回 dict 列表、procs_live 返回三元组（回收用后者）",
              isinstance(snapshot, list) and snapshot
              and isinstance(snapshot[0], dict) and snapshot[0].get("pid") == victim.pid
              and all(hasattr(p, "poll") for p, _a, _t in probe.procs_live()),
              str(snapshot)[:80])
        probe.stop()
        check("Session.stop() 真的杀掉了 /exec 子进程（旧实现抛异常直接跳过这一步）",
              victim.poll() is not None, f"poll={victim.poll()}")
        check("Session.stop() 不抛异常（每一步都自己兜住）", True, "")
        if victim.poll() is None:
            victim.kill()
        mod.release_display(probe.sid, probe.number, forget=True)
    except Exception as exc:                             # noqa: BLE001
        check("会话回收（/exec 子进程 + stop() 健壮性）", False,
              f"{type(exc).__name__}: {exc}")

    # ---- 平台相关
    if mod.IS_WIN:
        check("后端解析为 win32", mod.BACKEND == "win32", mod.BACKEND)
        check("输入注入默认关闭（只读观看）", mod.WIN_INPUT is False, f"DSH_VIEW_INPUT={mod.WIN_INPUT}")
        check("抓帧间隔贴着页面节奏", mod.GRAB_INTERVAL == 0.12, mod.GRAB_INTERVAL)

        # INPUT 结构在 64 位下必须是 40 字节，否则 SendInput 会读错内存
        check("INPUT 结构大小 = 40 字节", ctypes.sizeof(mod._INPUT) == 40, ctypes.sizeof(mod._INPUT))
        check("INPUT 联合大小 = 32 字节", ctypes.sizeof(mod._INPUTUNION) == 32,
              ctypes.sizeof(mod._INPUTUNION))

        # 空操作：把光标移到它现在所在的位置（不按键、不点击、不移动）
        pt = mod.wintypes.POINT()
        mod._user32.GetCursorPos(ctypes.byref(pt))
        sent = mod._win_mouse(pt.x, pt.y, mod._MEF_MOVE | mod._MEF_ABSOLUTE)
        check("SendInput 接受鼠标移动事件", sent == 1, f"返回 {sent}（1 = 已插入输入流）")
        pt2 = mod.wintypes.POINT()
        mod._user32.GetCursorPos(ctypes.byref(pt2))
        check("光标没有真的被挪走", (pt2.x, pt2.y) == (pt.x, pt.y),
              f"({pt.x},{pt.y}) -> ({pt2.x},{pt2.y})")
        check("空文本不发任何事件", mod._win_text("") == 0)
        check("未知键名被安全忽略", mod._win_key("Nonsense") == 0)
        keys = mod._VK
        dom = ["Enter", "Backspace", "Delete", "Tab", "Escape", " ",
               "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight",
               "Home", "End", "PageUp", "PageDown"]
        missing = [k for k in dom if k not in keys]
        check("DOM 键名全部有映射", not missing, "缺: " + str(missing))
    elif sys.platform == "darwin":
        check("后端解析为 darwin", mod.BACKEND == "darwin", mod.BACKEND)
        check("抓帧用系统自带 screencapture", hasattr(mod, "_grab_darwin"), "")
        check("输入默认关闭（注入的是真实键鼠）", getattr(mod, "MAC_INPUT", None) is False,
              f"DSH_VIEW_INPUT={getattr(mod, 'MAC_INPUT', None)}")
        check("darwin 是真实桌面后端（页面文案不能写'独立显示'）",
              mod.real_desktop() is True, str(mod.real_desktop()))
    else:
        check("后端解析为 x11", mod.BACKEND == "x11", mod.BACKEND)
        check("依赖自检可调用", callable(mod.missing_tools), "")
        missing = [m["tool"] for m in mod.missing_tools()]
        if missing:
            # 缺 GUI 工具不该让自检变红：CI 机器上完全可能只跑接口那一半。
            skip("x11 运行环境依赖齐全", "缺: " + ", ".join(missing)
                 + "（装上 xvfb/xdotool/xclip/imagemagick 后这里会变成真 PASS）")
        else:
            check("x11 运行环境依赖齐全", True, "无缺失")
            try:
                proc = subprocess.run(["xdotool", "--version"], capture_output=True, timeout=10)
                tool_ok = proc.returncode == 0
            except Exception as exc:                     # noqa: BLE001
                tool_ok = False
                missing = [f"{type(exc).__name__}: {exc}"]
            check("xdotool 真的能跑起来（--version）", tool_ok, str(missing))
        # DOM 键名 → xdotool 名 的换算表：缺一个，那个键在面板里就是没反应
        table = getattr(mod, "_XDOTOOL_KEY", None)
        check("存在 DOM→xdotool 键名换算表", isinstance(table, dict), type(table).__name__)
        if isinstance(table, dict):
            dom = ["Enter", "Backspace", "Delete", "Tab", "Escape", " ",
                   "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight",
                   "Home", "End", "PageUp", "PageDown"]
            missing = [k for k in dom if k not in table]
            check("DOM 键名全部有映射", not missing, "缺: " + str(missing))
            check("回车/退格映射到 X11 名", table.get("Enter") == "Return"
                  and table.get("Backspace") == "BackSpace",
                  f"Enter→{table.get('Enter')} Backspace→{table.get('Backspace')}")
            check("组合键前缀也换算（ctrl+ArrowUp → ctrl+Up）",
                  mod._xdotool_key("ctrl+ArrowUp") == "ctrl+Up", mod._xdotool_key("ctrl+ArrowUp"))
        check("DOM 鼠标键号 → xdotool 键号（1 左 2 中 3 右）",
              [mod._x11_button(b) for b in (1, 2, 3)] == ["1", "3", "2"], "")
        check("X11 抓帧走进程内 XGetImage（GRAB_INTERVAL 只剩余失败退避与 win32/darwin）",
              mod.GRAB_INTERVAL == 0.5 and "XGetImage" in source
              and "libXdamage" in source, mod.GRAB_INTERVAL)
        has_xclip = shutil.which("xclip") is not None
        if has_xclip:
            check("中文走剪贴板（xclip -l 20）",
                  "xclip" in source and '"-l"' in source and '"20"' in source, "")
        else:
            skip("中文走剪贴板（xclip -l 20）", "本机没装 xclip；源码里已实现剪贴板路线")
        if shutil.which("Xvfb") is None or shutil.which("import") is None:
            skip("端到端抓帧/注入（需要 Xvfb + xdotool + imagemagick）",
                 "本机缺 Xvfb/import —— 装了之后由 tools/ 里的自测脚本真跑")
        elif shutil.which("xdotool") is None:
            skip("端到端抓帧/注入", "本机缺 xdotool")
        else:
            skip("端到端抓帧/注入（真正的端到端由 tools/dsh-display-selftest.py 跑）",
                 "自检只做静态与接口层断言，不拉起 Xvfb（避免自检本身有副作用）")

    # ---- 接口冒烟（不需要 GUI）
    interface_checks(mod)

    passed = sum(1 for _n, st, _d in RESULTS if st == "PASS")
    failed = [r for r in RESULTS if r[1] == "FAIL"]
    skipped = sum(1 for _n, st, _d in RESULTS if st == "SKIP")
    print(f"\n== {passed}/{len(RESULTS)} 项通过"
          f"{f'，{skipped} 项跳过' if skipped else ''}"
          f"{f'，{len(failed)} 项失败' if failed else ''} ==")
    for name, status, detail in RESULTS:
        if status != "PASS":
            print(f"  {status}：{name} {detail}")
    if _TEMP_HOME:
        shutil.rmtree(_TEMP_HOME, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
