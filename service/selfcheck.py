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
                "missing", "input", "realDesktop", "tooltip")
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
        srv.shutdown()
    finally:
        mod.session = original_session
        srv.server_close()
    check("接口冒烟全程没有创建会话、没有拉起显示服务器",
          mod.session_count() == 0 and list(getattr(mod, "_spawned", [])) == [],
          f"sessions={mod.session_count()} spawned={len(getattr(mod, '_spawned', []))}")


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
        check("抓帧间隔 0.5s（X11 每次都要起外部进程）", mod.GRAB_INTERVAL == 0.5,
              mod.GRAB_INTERVAL)
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
