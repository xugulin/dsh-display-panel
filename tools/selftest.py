#!/usr/bin/env python3
"""dsh-display-panel 一键自检 —— 静态契约检查 + （有 GUI 时）真注入回归。

设计原则（这几条就是它存在的理由）：

* **零副作用**：临时 ``HOME`` + 临时 ``DSH_DISPLAY_HOME`` + 自动挑的空闲端口，
  绝不碰用户真实的 ``~/.cache/dsh-display``（结束前还会对比一次，确认没被碰过）。
* **可重复跑**：每次自己起服务、自己收尸（含服务拉起的 Xvfb 与靶程序）。
* **缺依赖跳过而不是失败**：CI 上没有 Xvfb/xdotool/imagemagick 时只跑静态检查并退出 0，
  否则 CI 永远红着，真正的回归反而没人看。
* **有 FAIL 就退出码非 0**：绿就是绿，不靠"放宽断言"。

用法：
    python3 tools/selftest.py [--viewer 路径] [--port N] [--home DIR] [--timeout 秒]
                              [--no-dynamic] [--keep] [-v]

退出码：0 = 没有 FAIL（SKIP 不算失败）；1 = 至少一项 FAIL；2 = 脚本自身起不来。
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VIEWER = ROOT / "service" / "dsh-display-viewer.py"
SELFCHECK = ROOT / "service" / "selfcheck.py"
CLIENT_JS = ROOT / "lib" / "client.js"
INDEX_JS = ROOT / "lib" / "index.js"
CONTRACT = ROOT / "docs" / "CONTRACT.md"
XTARGET = ROOT / "tools" / "xtarget.py"

PASS, FAIL, SKIP, INFO = "PASS", "FAIL", "SKIP", "INFO"
RESULTS: list[tuple[str, str, str]] = []
TMPDIR = ""
VERBOSE = False


def record(name: str, status, detail: str = "") -> None:
    # `status` 也接受 bool（很多调用点直接传条件表达式）——免得 True/False 混进状态栏
    if isinstance(status, bool):
        status = PASS if status else FAIL
    RESULTS.append((name, status, detail))
    mark = {PASS: "\033[32mPASS\033[0m", FAIL: "\033[31mFAIL\033[0m",
            SKIP: "\033[33mSKIP\033[0m", INFO: "\033[36mINFO\033[0m"}[status]
    if status == INFO and not VERBOSE:
        return
    print(f"{mark}  {name}" + (f"  — {detail}" if detail else ""), flush=True)


def ok(name: str, cond: bool, detail: str = "") -> bool:
    record(name, PASS if cond else FAIL, detail)
    return bool(cond)


def skip(name: str, why: str) -> None:
    record(name, SKIP, why)


class SkipCheck(Exception):
    """该项的前置条件不满足 —— 跳过，不算失败。"""


# ====================================================================== HTTP 小工具
def http_req(port: int, method: str, path: str, body=None, timeout: float = 10.0,
             headers: dict | None = None) -> tuple[int, dict, bytes]:
    """**原样**发送 path（不做归一化）—— 路径穿越那一项必须能发出 ``/s/../..``。"""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    data = None
    head = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode()
        head.setdefault("Content-Type", "application/json")
    try:
        conn.request(method, path, body=data, headers=head)
        resp = conn.getresponse()
        raw = resp.read()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, raw
    finally:
        conn.close()


def as_json(raw: bytes):
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:                                         # noqa: BLE001
        return None


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# ====================================================================== 静态检查
def static_checks() -> None:
    print("\n---- 静态检查 ----")

    # 1) 语法：用 py_compile 但把 .pyc 写到临时目录（否则会污染仓库 / 沙箱只读目录）
    pyc_dir = Path(TMPDIR) / "pyc"
    pyc_dir.mkdir(parents=True, exist_ok=True)
    import py_compile

    for src in (VIEWER, SELFCHECK, XTARGET, Path(__file__).resolve()):
        if not src.exists():
            record(f"py_compile {src.name}", FAIL, "文件不存在")
            continue
        try:
            py_compile.compile(str(src), cfile=str(pyc_dir / (src.name + "c")),
                               doraise=True)
            record(f"py_compile {src.name}", PASS)
        except Exception as exc:                              # noqa: BLE001
            record(f"py_compile {src.name}", FAIL, f"{type(exc).__name__}: {exc}")

    # 2) 服务自带自检（平台相关断言：键名映射、INPUT 结构、页面模板…）
    if not SELFCHECK.exists():
        record("service/selfcheck.py", FAIL, "文件不存在")
    else:
        proc = subprocess.run([sys.executable, str(SELFCHECK), str(VIEWER)],
                              capture_output=True, text=True, timeout=120)
        tail = (proc.stdout or "").strip().splitlines()
        record("service/selfcheck.py 全部通过", proc.returncode == 0,
               (tail[-1] if tail else "") + (f" | stderr: {proc.stderr.strip()[:200]}"
                                             if proc.returncode else ""))

    # 3) 两个 JS 半边语法
    node = shutil.which("node")
    for js in (CLIENT_JS, INDEX_JS):
        if not js.exists():
            record(f"node --check {js.name}", FAIL, "文件不存在")
            continue
        if not node:
            skip(f"node --check {js.name}", "没有 node")
            continue
        proc = subprocess.run([node, "--check", str(js)], capture_output=True, text=True)
        record(f"node --check {js.name}", proc.returncode == 0,
               proc.stderr.strip().splitlines()[0] if proc.returncode else "")

    # 4) 契约关键词：客户端半边**只许同源**（这是本次重构的核心，必须钉死）
    if CLIENT_JS.exists():
        raw = CLIENT_JS.read_text(encoding="utf-8")
        text = js_code_only(raw)
        banned = {
            "127.0.0.1": "客户端不得直连本机服务（HTTPS 下是混合内容 / 跨源）",
            "?k=": "令牌不得出现在浏览器侧",
            "iframe": "不得再用 iframe 嵌服务页面",
            "h('iframe'": "不得再用 iframe 嵌服务页面",
            "http://": "不得出现绝对 http 地址（同源相对路径才安全）",
        }
        for needle, why in banned.items():
            hits = [i + 1 for i, ln in enumerate(text.splitlines()) if needle in ln]
            record(f"client.js 代码里不含 {needle!r}", not hits,
                   f"第 {hits[:5]} 行 —— {why}" if hits else "")
        for needle in ("canvas", "/api/dsh-display-panel", "compositionstart",
                       "wheel", "mousedown"):
            record(f"client.js 代码里含 {needle!r}", needle in text)

    # 5) 契约关键词：宿主半边
    if INDEX_JS.exists():
        text = INDEX_JS.read_text(encoding="utf-8")
        record("index.js 走同源路由 /api/dsh-display-panel",
               "/api/dsh-display-panel" in text, "")
        # 令牌是否被塞进响应体：由下面的功能性探针判定（grep 判不准）

    # 6) 契约关键词：服务端必须有的接口
    if VIEWER.exists():
        text = VIEWER.read_text(encoding="utf-8")
        for needle, why in (("/health", "契约 §1.1：探测专用、无副作用"),
                            ("/exec", "契约 §1.2/§4.1：必须在服务进程里拉起程序"),
                            ("/procs", "契约 §1.2"),
                            ("/kill", "契约 §1.2")):
            record(f"viewer 有 {needle} 接口", needle in text, why)
        has_re = bool(re.search(r"[A-Za-z0-9\.\-_]\{1,64\}|\[\^?A-Za-z0-9\._", text)) \
            or ("A-Za-z0-9._-" in text and "64" in text)
        record("viewer 有 sid 字符集校验（^[A-Za-z0-9._-]{1,64}$）", has_re,
               "契约 §1.2：旧实现直接把 sid 拼进文件路径")

    # 6.5) 来源校验（Host/Origin）与 CORS
    #
    # 这一组是**纯函数**断言，与后端无关，所以在任何平台上都必须跑。
    # 背景：服务监听的端口，浏览器里任意网页都能打；`fetch` 用
    # `Content-Type: text/plain` 发的是"简单请求"**不触发预检**，所以
    # `POST /input` 这类写操作能被跨源页面直接打进来。令牌挡得住"没令牌"，
    # 但挡不住"带着用户凭据的跨源请求"与 DNS rebinding —— 前者靠 Origin /
    # Sec-Fetch-Site 判，后者靠 Host 白名单判（rebinding 时 Host 是攻击者域名）。
    if VIEWER.exists():
        try:
            import importlib.util as _ilu
            _spec = _ilu.spec_from_file_location("ddp_viewer", VIEWER)
            _viewer = _ilu.module_from_spec(_spec)
            _spec.loader.exec_module(_viewer)

            # ⚠️ 服务端必须**不再**发通配 CORS：同源代理不需要它，
            # 而它会把"任意网页读本机回环"从"做不到"变成"只差一个 DNS"。
            # 用 AST 找真正的 `send_header("Access-Control-Allow-Origin", ...)` 调用：
            # 纯文本匹配会把注释/文档字符串里提到这个名字也算成违规。
            import ast as _ast
            _cors_lines = []
            try:
                _tree = _ast.parse(Path(_viewer.__file__).read_text(encoding="utf-8"))
                for _node in _ast.walk(_tree):
                    if not isinstance(_node, _ast.Call):
                        continue
                    _fn = _node.func
                    if not (isinstance(_fn, _ast.Attribute) and _fn.attr == "send_header"):
                        continue
                    _args = _node.args
                    if _args and isinstance(_args[0], _ast.Constant) \
                            and str(_args[0].value).lower() == "access-control-allow-origin":
                        _cors_lines.append(_node.lineno)
            except SyntaxError as _exc:
                _cors_lines = [f"AST 解析失败: {_exc}"]
            record("viewer 不再无条件发 Access-Control-Allow-Origin: *", not _cors_lines,
                   f"第 {_cors_lines[:5]} 行" if _cors_lines else "")

            _ca = _viewer.canonical_authority
            _ca_cases = [
                # (authority, 期望规范化结果, 为什么)
                ("127.0.0.1:8099", ("127.0.0.1", "8099"), "正常回环"),
                ("127.0.0.1", ("127.0.0.1", ""), "无端口"),
                ("LOCALHOST", ("localhost", ""), "大小写不敏感"),
                ("[::1]:8099", ("[::1]", "8099"), "IPv6 回环带端口"),
                ("[0:0:0:0:0:0:0:1]", ("[::1]", ""), "展开写法的 IPv6 回环"),
                ("0x7f.0.0.1", ("127.0.0.1", ""), "十六进制写法的回环（WHATWG 等价形式）"),
                ("2130706433", ("127.0.0.1", ""), "十进制整数写法的回环"),
                ("0x7f000001", ("127.0.0.1", ""), "单个十六进制整数"),
                ("0177.0.0.1", ("127.0.0.1", ""), "八进制段"),
                ("127.1", ("127.0.0.1", ""), "末段独占剩余字节（URL 标准）"),
                ("127.0.0.1.", ("127.0.0.1", ""), "尾随点"),
                ("dsh.internal", ("dsh.internal", ""), "普通域名照常接受"),
                ("a-b.example.com:443", ("a-b.example.com", "443"), "带端口域名"),
                ("127.0.0.1.evil.com", ("127.0.0.1.evil.com", ""), "是合法域名，但**不是**回环"),
                ("127.0.0.1:08099", None, "零填充端口会被浏览器规范化，必须拒绝"),
                ("http://127.0.0.1", None, "不是裸 authority"),
                ("user@127.0.0.1", None, "userinfo 形态（会授权内嵌主机名）"),
                ("127.0.0.1/path", None, "带路径"),
                ("", None, "空"),
                ("evil.com:80a", None, "端口不是数字"),
                ("0x7f.0.0.256", None, "以数字结尾但越界：浏览器解析会失败"),
                ("1.2.3.4.5", None, "五段数字：同上"),
                ("999999999999", None, "整数越界：同上"),
                ("example.123", None, "以数字结尾的域名：URL 标准判它非法"),
            ]
            _ca_bad = [why for value, want, why in _ca_cases if _ca(value) != want]
            record("Host 规范化：与 DSH/WHATWG 的 authority 语义一致",
                   not _ca_bad, f"不符：{_ca_bad}" if _ca_bad else f"{len(_ca_cases)} 例")

            _trusted_cases = [
                ("127.0.0.1:8099", True, "回环任意端口"),
                ("localhost:1", True, "localhost 任意端口"),
                ("127.5.5.5", True, "整段 127/8 都算回环"),
                ("[::1]:8099", True, "IPv6 回环"),
                ("0x7f000001:8099", True, "十六进制写法规范化后仍是回环"),
                ("evil.com", False, "外域"),
                ("evil.com:8099", False, "外域带端口"),
                ("127.0.0.1.evil.com", False, "前缀伪装（DNS rebinding 常用形态）"),
                ("1.2.3.4.5", False, "非法 IPv4 形式不得当域名放行"),
                ("0x7f.0.0.256", False, "越界的十六进制形式"),
                ("", False, "空 Host"),
            ]
            _t_bad = [why for value, want, why in _trusted_cases
                      if _viewer.trusted_authority(value) is not want]
            record("Host 白名单：只信本机回环（含 127/8、::1、localhost、等价写法）",
                   not _t_bad, f"不符：{_t_bad}" if _t_bad else f"{len(_trusted_cases)} 例")

            _reason_cases = [
                ({"Host": "127.0.0.1:8099"}, "", "面板走同源代理的默认形态"),
                ({"Host": "localhost:8099"}, "", "localhost"),
                ({"Host": "127.0.0.1:8099", "Sec-Fetch-Site": "same-origin"}, "", "同源标记"),
                ({"Host": "127.0.0.1:8099", "Origin": "http://127.0.0.1:8099"}, "", "Origin 与 Host 同源"),
                ({"Host": "127.0.0.1:8099", "Origin": "http://127.0.0.1:8099",
                  "Sec-Fetch-Site": "same-site"}, "", "same-site 仍看 Origin"),
                ({}, "缺少 Host", "没有 Host 头"),
                ({"Host": "evil.com"}, "Host", "外域 Host"),
                ({"Host": "127.0.0.1.evil.com"}, "Host", "rebinding 伪装的 Host"),
                ({"Host": "127.0.0.1:8099", "Sec-Fetch-Site": "cross-site"}, "cross-site", "浏览器标记跨站"),
                ({"Host": "127.0.0.1:8099", "Origin": "https://evil.com"}, "Origin", "Origin 与 Host 不同源"),
                ({"Host": "127.0.0.1:8099", "Origin": "http://127.0.0.1:9000"}, "Origin", "同主机不同端口也算跨源"),
            ]
            _r_bad = []
            for headers, want, why in _reason_cases:
                got = _viewer.host_reason(headers)
                if want == "":
                    if got != "":
                        _r_bad.append(f"{why}：期望通过，实得 {got!r}")
                elif want not in got:
                    _r_bad.append(f"{why}：期望原因含 {want!r}，实得 {got!r}")
            record("host_reason：Host / Origin / Sec-Fetch-Site 三条都判对",
                   not _r_bad, "；".join(_r_bad) if _r_bad else f"{len(_reason_cases)} 例")
        except Exception as exc:                                  # noqa: BLE001
            record("来源校验（Host/Origin）纯函数断言", FAIL,
                   f"{type(exc).__name__}: {exc}")

    # 6.6) 设置卡片的 key 域：宿主/客户端用的"行 id"必须与 cordis.patch.yml 一致
    #
    # 这条钉的是一个**致命但静默**的错误：dsh-settings 的 describe()/update() 与
    # 客户端的 configForms.get() 只认 profile entry id（= patch 里的 `- id:`），
    # 而卡片槽位 plugins.bundle.config 的 key 是**包名**。混用的表现不是报错，
    # 而是"卡片在、里面空的、保存还说已保存" —— 所以必须静态钉住。
    if INDEX_JS.exists() and CLIENT_JS.exists() and (ROOT / "cordis.patch.yml").exists():
        patch_text = (ROOT / "cordis.patch.yml").read_text(encoding="utf-8")
        m_id = re.search(r"^\s*-\s*id:\s*([A-Za-z0-9._-]+)\s*$", patch_text, re.M)
        m_name = re.search(r"^\s*name:\s*'([^']+)'\s*$", patch_text, re.M)
        row_id = m_id.group(1) if m_id else None
        pkg_name = m_name.group(1) if m_name else None
        record("cordis.patch.yml 同时声明了 id 与 name（id=行标识、name=包名）",
               bool(row_id and pkg_name), f"id={row_id} name={pkg_name}")
        if row_id:
            index_text = INDEX_JS.read_text(encoding="utf-8")
            client_text = CLIENT_JS.read_text(encoding="utf-8")
            m_host = re.search(r"const SETTINGS_NAMESPACE = '([^']+)'", index_text)
            m_form = re.search(r"const FORM_KEY = '([^']+)'", client_text)
            m_slot = re.search(r"const SLOT_KEY = '([^']+)'", client_text)
            record("宿主用的行 id 与 cordis.patch.yml 的 id 一致",
                   bool(m_host) and m_host.group(1) == row_id,
                   f"index.js={m_host.group(1) if m_host else '?'} patch={row_id}")
            record("客户端取设置面用的行 id 与 patch 一致",
                   bool(m_form) and m_form.group(1) == row_id,
                   f"client.js={m_form.group(1) if m_form else '?'} patch={row_id}")
            record("客户端卡片槽位的 key 是**包名**（不是行 id）",
                   bool(m_slot) and m_slot.group(1) == pkg_name,
                   f"client.js={m_slot.group(1) if m_slot else '?'} 包名={pkg_name}")

    # 7) 冻结契约本身
    record("docs/CONTRACT.md 存在", CONTRACT.exists())
    if CONTRACT.exists() and (ROOT / "package.json").exists():
        try:
            pkg = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
            # 0.4.2 回归钉子：path 自带查询串时，令牌必须用 & 拼（否则 `?quality=90?k=…`
            # 会让服务端把令牌当成前一个参数的值 → 403 → 截图工具写出假 JPEG）
            tools_src = (ROOT / "lib" / "tools.js").read_text(encoding="utf-8")
            record("tools.js：路径自带 ? 时用 & 拼令牌",
                   "path.includes('?') ? '&' : '?'" in tools_src,
                   "callService 的查询串拼接")
            record("tools.js：截图前校验 JPEG magic（不把错误响应写成 .jpg）",
                   "返回的不是 JPEG" in tools_src and "isJpeg" in tools_src,
                   "display_panel_screenshot 的响应校验")

            # ---- 随包工具（0.5.0）：会话墙解析器 + 卡片生成器
            # 会话墙：喂一份**未压缩**的合成记录，断言各类事件都被解析出来。
            #   不需要 X（read_events 是纯解析），所以 CI 上也能跑。
            try:
                import importlib.util as _ilu
                import tempfile as _tf
                spec = _ilu.spec_from_file_location("ddp_wall", ROOT / "tools" / "session-wall.py")
                wall = _ilu.module_from_spec(spec)
                spec.loader.exec_module(wall)
                fixture = Path(_tf.mkdtemp(prefix="ddp-wall-")) / "s.jsonl"
                fixture.write_text("\n".join([
                    '{"type":"session","version":4,"id":"session-x","cwd":"/tmp"}',
                    '{"type":"user/message","seq":1,"time":1700000000000,'
                    '"data":{"content":[{"type":"text","text":"你好"}]}}',
                    '{"type":"assistant/message","seq":2,"time":1700000001000,"data":{"message":{"content":['
                    '{"type":"reasoning","text":"内心戏不该出现"},{"type":"text","text":"收到"}]}}}',
                    '{"type":"tool/call","seq":3,"time":1700000002000,'
                    '"data":{"name":"bash","arguments":"{\\"command\\":\\"ls -la\\"}"}}',
                    '{"type":"team/task","seq":4,"time":1700000003000,'
                    '"data":{"task":{"id":"task-1","subject":"验证容量"}}}',
                    '{"type":"session/title","seq":5,"time":1700000004000,"data":{"title":"合成标题"}}',
                ]), encoding="utf-8")
                evs, wmeta = wall.read_events(str(fixture))
                kinds = [e["kind"] for e in evs]
                record("会话墙：从记录里解析出各类事件（用户/助手/工具/任务）",
                       kinds == ["user", "assistant", "tool", "task"], f"kinds={kinds}")
                body = " ".join(e["text"] for e in evs)
                record("会话墙：取文本时跳过 reasoning（内心戏）",
                       "收到" in body and "内心戏" not in body, f"text={body[:80]!r}")
                record("会话墙：能读到会话标题", wmeta.get("title") == "合成标题", f"title={wmeta.get('title')!r}")
            except Exception as exc:                                  # noqa: BLE001
                record("会话墙：解析器可用", False, f"{type(exc).__name__}: {exc}")

            # 卡片生成器：生成一张小测试图（没有 ImageMagick 就跳过，不算失败）
            if shutil.which("magick") is None:
                skip("卡片工具：能生成彩色测试图", "这台机器没有 ImageMagick")
            else:
                try:
                    import tempfile as _tf2
                    card = Path(_tf2.mkdtemp(prefix="ddp-card-")) / "card.png"
                    done = subprocess.run(
                        [sys.executable, str(ROOT / "tools" / "display-cards.py"),
                         "testcard", "--out", str(card), "--width", "320", "--height", "200"],
                        capture_output=True, timeout=180,
                    )
                    okc = done.returncode == 0 and card.exists() and card.stat().st_size > 1000
                    record("卡片工具：能生成彩色测试图（320x200）", okc,
                           f"退出码 {done.returncode}，{card.stat().st_size if card.exists() else 0} 字节")
                except Exception as exc:                              # noqa: BLE001
                    record("卡片工具：能生成彩色测试图", False, f"{type(exc).__name__}: {exc}")

            # ---- 0.6.0：空闲态（鸡汤 + 插画）与手动关闭显示器
            host_src = (ROOT / "lib" / "index.js").read_text(encoding="utf-8")
            client_src = (ROOT / "lib" / "client.js").read_text(encoding="utf-8")
            record("宿主：注册了 /close 路由（关闭本会话的显示器）",
                   "${BASE_PATH}/close" in host_src and "suffix: 'close'" in host_src,
                   "lib/index.js 的路由表")
            record("工具：新增 display_panel_close（共 8 个工具）",
                   "`${PREFIX}close`" in tools_src
                   and "[status, open, sessions, run, procs, screenshot, input, close]" in tools_src,
                   "lib/tools.js 的工具清单")
            record("客户端：空闲态覆盖层 + 随机鸡汤 + 程序化插画",
                   "className: 'ddp-idle'" in client_src and "const QUOTES = [" in client_src
                   and "function paintScene(" in client_src and client_src.count("scene: '") >= 8,
                   f"{client_src.count(chr(34) + 'scene: ' + chr(39))} 个 ——scene（含场景引用）")
            record("客户端：能手动关闭显示器，且关掉后禁止自动重连",
                   "'data-act': 'display-close'" in client_src
                   and "userClosedRef" in client_src
                   and "if (userClosedRef.current) return" in client_src,
                   "display-close 按钮 + userClosedRef 闸门")
            record("客户端：插画配色跟着主题深浅（跳过透明背景的误判）",
                   "data-art-dark" in client_src and "跳过 alpha" in client_src,
                   "isDarkTheme + data-art-dark 暗纱")

            # ---- 0.7.0：团队看板（成员 / 任务进度 / 每人工作详情）
            # 用一份**合成记录**喂它，断言解析与配对逻辑（纯 Python，不需要 X）。
            try:
                import importlib.util as _ilu3
                import tempfile as _tf3
                spec3 = _ilu3.spec_from_file_location("ddp_board", ROOT / "tools" / "team-board.py")
                board_mod = _ilu3.module_from_spec(spec3)
                spec3.loader.exec_module(board_mod)
                fixture3 = Path(_tf3.mkdtemp(prefix="ddp-board-")) / "t.jsonl"
                fixture3.write_text("\n".join([
                    '{"type":"team/member","time":1,"data":{"member":'
                    '{"id":"m1","name":"t1-playui","description":"W3 播放界面：重构"}}}',
                    '{"type":"team/member","time":2,"data":{"member":'
                    '{"id":"m2","name":"t9-misc","description":"部署 danmu_api 真服务"}}}',
                    '{"type":"team/task","time":3,"data":{"task":'
                    '{"id":"task-3","revision":1,"subject":"W3 播放界面：重构","status":"pending"}}}',
                    '{"type":"team/task","time":4,"data":{"task":'
                    '{"id":"task-3","revision":2,"subject":"W3 播放界面：重构","status":"completed"}}}',
                    '{"type":"team/task","time":5,"data":{"task":'
                    '{"id":"task-10","revision":1,"subject":"W11 真实弹幕数据夹具（danmu_api）","status":"in_progress"}}}',
                    '{"type":"team/message/queued","time":6,"data":{"message":'
                    '{"id":"x1","senderId":"m1","senderName":"t1-playui","targetId":"lead",'
                    '"content":[{"type":"text","text":"我这边做完了"}]}}}',
                    '{"type":"assistant/message","time":7,"data":{}}',
                ]), encoding="utf-8")
                b = board_mod.read_board(str(fixture3))
                names = [m["name"] for m in b["members"]]
                record("团队看板：解析出成员并按 W 编号排序", names == ["t1-playui", "t9-misc"], f"members={names}")
                record("团队看板：任务取**最新修订**（task-3 pending → completed）",
                       b["counts"].get("completed", 0) == 1 and b["total"] == 2, f"counts={b['counts']} total={b['total']}")
                m1 = [m for m in b["members"] if m["name"] == "t1-playui"][0]
                record("团队看板：成员与任务按 W 编号配对",
                       [t["id"] for t in m1["tasks"]] == ["task-3"], f"tasks={[t['id'] for t in m1['tasks']]}")
                m2 = [m for m in b["members"] if m["name"] == "t9-misc"][0]
                record("团队看板：没有 W 编号时用独特 ASCII 词兜底配对",
                       [t["id"] for t in m2["tasks"]] == ["task-10"], f"tasks={[t['id'] for t in m2['tasks']]}")
                record("团队看板：每人最近动态带方向（他发的 → / 发给他的 ←）",
                       m1["latest"].startswith("→ t1-playui") and "做完了" in m1["latest"], m1["latest"][:60])
                record("团队看板：统计活动量与近一分钟活跃度",
                       b["activity"] >= 1 and "lastTime" in b, f"activity={b['activity']} recent={b['recentPerMin']}")
            except Exception as exc:                                  # noqa: BLE001
                record("团队看板：解析器可用", False, f"{type(exc).__name__}: {exc}")

            m = re.search(r"接口契约（v([0-9.]+)", CONTRACT.read_text(encoding="utf-8"))
            cver = m.group(1) if m else "?"
            pver = str(pkg.get("version"))
            # 只比 major.minor：契约描述的是**接口**，补丁版（x.y.Z）改的是实现，
            # 不该逼着每次发补丁都去动契约头部 —— 但跨 minor（0.3 → 0.4）必须同步，
            # 那才是"接口变了、文档没跟上"的时刻。
            def mm(v: str) -> str:
                parts = v.split(".")
                return ".".join(parts[:2]) if len(parts) >= 2 else v
            record("package.json 与契约的版本（major.minor）一致", mm(pver) == mm(cver),
                   f"package.json={pver} 契约={cver}")
        except Exception as exc:                              # noqa: BLE001
            record("package.json 可解析", False, str(exc))


def js_code_only(text: str) -> str:
    """把 JS 的注释替换成空白（**保留换行，行号不变**），只留真正的代码。

    为什么需要：契约检查里有些词只能出现在注释里（例如「不再用 iframe / 127.0.0.1」
    的解释），直接 grep 会把正确的实现判成 FAIL —— 那种"放宽断言"才是真的坏。
    这里按字符串状态机剥离，字符串里的 ``//``（如 ``https://``）不受影响。
    """
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in "\"'`":
            quote = ch
            out.append(ch)
            i += 1
            while i < n:
                if text[i] == "\\":
                    out.append(text[i:i + 2])
                    i += 2
                    continue
                out.append(text[i])
                if text[i] == quote:
                    i += 1
                    break
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i < n:
                if text[i] == "\n":
                    out.append("\n")
                if text[i] == "*" and i + 1 < n and text[i + 1] == "/":
                    i += 2
                    break
                i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


# ====================================================================== 宿主半边功能性探针
HOST_PROBE_JS = r"""
// 宿主半边探针：不需要 DSH，直接把 lib/index.js 的 apply() 挂到 stub ctx 上，
// 然后调用它注册的路由 —— 用来证明「令牌没有被交给浏览器」这类安全断言。
const target = process.argv[2]
const TOKEN = process.env.__PROBE_TOKEN || ''
// 这个探针进程的 argv[1] 是探针自己，跟**真实宿主进程**（argv[1] = dsh 的入口脚本）
// 不一样，所以插件内部的"锚点推导"在这里找不到 DSH 安装位置。为了测到与真机一致
// 的行为，这里显式把锚点指过去（等价于真实宿主里自动推导出来的那个值）。
// 找不到就留空 —— 那正好顺便验证"降级为无表单"这条路径不会崩。
try {
  const fsx = require('fs'), pathx = require('path')
  for (const base of ['/home/xgl/.npm-global/lib/node_modules',
                      pathx.join(process.env.HOME || '', '.npm-global/lib/node_modules')]) {
    const anchor = pathx.join(base, '@deepseek-ai/dsh/package.json')
    if (fsx.existsSync(anchor)) { process.env.DSH_INSTALL_ANCHOR = anchor; break }
  }
} catch (e) { /* 探针自己的便利，失败就当没有 */ }
const routes = []
let rejection
function collect(args) {
  let path = '', handler = null
  for (const a of args) {
    if (typeof a === 'string' && a.startsWith('/')) path = a
    else if (typeof a === 'function') handler = a
    else if (a && typeof a === 'object') {
      if (typeof a.path === 'string') path = a.path
      if (typeof a.handler === 'function') handler = a.handler
      if (typeof a.route === 'string') path = a.route
    }
  }
  routes.push({ path, handler })
}
const webCtx = {
  effect: (fn) => { try { return fn() } catch (e) { console.error('effect: ' + e) } },
  webServer: { register: (...args) => { collect(args); return () => {} } },
  connection: { requestRejection: () => rejection },
  logger: console,
}
const ctx = {
  inject: (...args) => { const cb = args[args.length - 1]; if (typeof cb === 'function') cb(webCtx) },
  effect: (fn) => { try { return fn() } catch (e) { console.error('effect: ' + e) } },
  logger: console,
  slots: { inject: () => {}, register: () => () => {} },
}
const out = { routes: [], errors: [], info: null, status401: null, infoBody: '', settings: null }
;(async () => {
try {
  const mod = require(target)
  // ── 设置卡片：Config 必须是**真 schemastery**，而且要能被 dsh-settings 的
  //    volatileForm() 投影出表单（它就靠 schema.meta.volatile 与 schema.dict）。
  //    漏一个 .volatile() 的后果是"这一项在 GUI 里根本不出现"，很难查，所以钉在这里。
  try {
    const z = mod.Config
    if (z === undefined) {
      out.settings = { present: false }
    } else {
      const volatileForm = (schema) => {
        if (schema.meta && schema.meta.volatile) return schema
        if (schema.type === 'object') {
          const dict = Object.fromEntries(Object.entries(schema.dict || {}).flatMap(([k, child]) => {
            const field = volatileForm(child)
            return field === undefined ? [] : [[k, field]]
          }))
          return Object.keys(dict).length === 0 ? undefined : { type: 'object', dict }
        }
        return undefined
      }
      const form = volatileForm(z)
      let toJsonOk = false
      try { const j = z.toJSON(); toJsonOk = !!j && j.uid !== undefined } catch (e) { toJsonOk = false }
      let validateOk = false
      let validatedFields = []
      let unsetFields = null
      try {
        const v = z['~standard'].validate({})
        validateOk = !v.issues
        validatedFields = Object.keys(v.value || {})
        // ⚠️ 关键断言：**什么都没设**时不能有值。
        //    字段若带 `.default()`，cordis 会把默认值填进 fiber.config，于是宿主侧
        //    "没设置就回落到 DSH_VIEW_* 环境变量"那段逻辑永远走不到（真出过：
        //    DSH_VIEW_IDLE_MINUTES / DSH_VIEW_INPUT 静默失效）。
        // 注意：`required(false)` 的字段**键仍然存在**、只是值为 undefined
        // （schemastery 的行为），所以判据是"有没有值"，不是"有没有键"。
        const empty = z['~standard'].validate(undefined)
        const raw = (empty && empty.value) || {}
        unsetFields = Object.keys(raw).filter((key) => {
          const field = raw[key]
          const plain = field && typeof field.get === 'function' ? field.get() : field
          return plain !== undefined
        })
      } catch (e) { validateOk = false }
      out.settings = {
        present: true,
        formFields: Object.keys((form && form.dict) || {}),
        toJsonOk,
        validateOk,
        validatedFields,
        unsetFields,
      }
    }
  } catch (e) { out.errors.push('settings: ' + e) }
  try { mod.apply(ctx) } catch (e) { out.errors.push('apply: ' + e) }
  out.routes = routes.map((r) => r.path)
  // 路由可能是 exact(/info)、也可能是一个前缀兜底路由 —— 两种都认
  const info = routes.find((r) => r.path && r.path.endsWith('/info') && r.handler)
    || routes.find((r) => r.handler)
  if (!info) { out.errors.push('没有任何可调用的路由') } else {
    const call = (rej) => new Promise((resolve) => {
      rejection = rej
      let status = 0, headers = {}, body = '', done = false
      const finish = () => {
        if (!done) { done = true; resolve({ status: status || res.statusCode, headers, body }) }
      }
      const res = {
        writeHead: (s, h) => { status = s; Object.assign(headers, h || {}) },
        setHeader: (k, v) => { headers[String(k).toLowerCase()] = v },
        end: (b) => { if (b !== undefined) body += Buffer.isBuffer(b) ? b.toString('utf8') : String(b); finish() },
        write: (b) => { if (b !== undefined) body += Buffer.isBuffer(b) ? b.toString('utf8') : String(b) },
        statusCode: 200,
      }
      const req = { method: 'GET', url: '/api/dsh-display-panel/info',
                    headers: { host: '127.0.0.1' }, socket: {} }
      try {
        const r = info.handler(req, res)
        // 宿主半边是 async 的：不等它，res.end() 还没发生就 print → body 永远是空
        if (r && typeof r.then === 'function') r.catch((e) => out.errors.push('handler async: ' + e))
      } catch (e) { out.errors.push('handler: ' + e) }
      setTimeout(finish, 4000)
    })
    const a = await call(undefined)
    out.info = { status: a.status, ctype: a.headers['content-type'] || '' }
    out.infoBody = a.body
    const b = await call(401)
    out.status401 = b.status
  }
} catch (e) {
  out.errors.push('require: ' + e)
}
console.log(JSON.stringify(out))
})().catch((e) => { out.errors.push('fatal: ' + e); console.log(JSON.stringify(out)) })
"""


def host_probe(home: Path) -> None:
    """把 lib/index.js 真跑一遍：路由注册 + 令牌不外泄 + 鉴权拒绝透传。"""
    print("\n---- 宿主半边功能探针 ----")
    node = shutil.which("node")
    if not INDEX_JS.exists():
        record("宿主半边探针", FAIL, "lib/index.js 不存在")
        return
    if not node:
        skip("宿主半边探针", "没有 node")
        return
    token = "SELFTEST-TOKEN-9f3a1c7e"
    probe_home = Path(TMPDIR) / "hosthome"
    probe_home.mkdir(parents=True, exist_ok=True)
    (probe_home / "token").write_text(token)
    (probe_home / "port").write_text("1")
    js = Path(TMPDIR) / "host_probe.js"
    js.write_text(HOST_PROBE_JS, encoding="utf-8")
    env = {**os.environ, "DSH_DISPLAY_HOME": str(probe_home), "__PROBE_TOKEN": token}
    proc = subprocess.run([node, str(js), str(INDEX_JS)], capture_output=True,
                          text=True, env=env, timeout=60)
    line = (proc.stdout or "").strip().splitlines()
    data = None
    for cand in reversed(line):
        try:
            data = json.loads(cand)
            break
        except Exception:                                     # noqa: BLE001
            continue
    if data is None:
        record("宿主半边探针可运行", False,
               f"stdout={proc.stdout[:200]!r} stderr={proc.stderr[:300]!r}")
        return
    record("宿主半边探针可运行", not data["errors"], "; ".join(data["errors"])[:300])
    ok("宿主半边注册了 /api/dsh-display-panel/info 路由",
       any(p and p.endswith("/info") for p in data["routes"]), str(data["routes"]))
    body = data.get("infoBody") or ""
    ok("**/info 响应体里没有令牌**（契约 §2：令牌只在宿主侧）",
       token not in body, f"响应体前 200 字：{body[:200]}")
    ok("/info 响应带 ok:true 且含 service.port",
       '"ok"' in body and "port" in body, body[:200])
    ok("宿主把鉴权拒绝透传给浏览器（401）",
       data.get("status401") in (401, 403), f"requestRejection→401 时实际返回 {data.get('status401')}")

    # 设置卡片（0.8.x）：Config 必须能被 dsh-settings 的 volatileForm 投出三个字段。
    # 拿不到 schemastery 时 Config 是 undefined —— 那是**允许的降级**（插件照常工作、
    # 只是没有表单），所以那种情况单独记一条 SKIP，不要当成失败。
    settings = data.get("settings") or {}
    if not settings.get("present"):
        skip("设置卡片：Config 三个字段都能被 dsh-settings 投影成表单",
             "解析不到 @deepseek-ai/schemastery（Config 降级为 undefined）")
    else:
        want = ["size", "idleMinutes", "inputEnabled"]
        ok("设置卡片：Config 三个字段都能被 dsh-settings 投影成表单",
           sorted(settings.get("formFields") or []) == sorted(want),
           f"实际字段 {settings.get('formFields')}")
        ok("设置卡片：Config.toJSON() 可序列化（describe 要拿它算 revision）",
           settings.get("toJsonOk") is True, "")
        ok("设置卡片：Config 能过 cordis 的 resolveConfig（~standard.validate）",
           settings.get("validateOk") is True,
           f"校验后字段 {settings.get('validatedFields')}")
        ok("设置卡片：未设置时三项都**没有**值（否则 env 兜底是死代码）",
           settings.get("unsetFields") == [],
           f"validate(undefined) 给出 {settings.get('unsetFields')}")


# ====================================================================== 动态检查
class Viewer:
    """在被测服务上做各种事的测试夹具。"""

    def __init__(self, port: int, home: Path, viewer: Path) -> None:
        self.port = port
        self.home = home
        self.viewer = viewer
        self.proc: subprocess.Popen | None = None
        self.xtarget_procs: list[subprocess.Popen] = []
        self.created_sids: set[str] = set()
        self.exec_ok = False

    # ---------------------------------------------------------------- 生命周期
    def start(self, timeout: float = 40.0) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        out_log = open(self.home / "stdout.log", "wb")
        err_log = open(self.home / "stderr.log", "wb")
        env = {**os.environ,
               "HOME": str(self.home / "fakehome"),
               "DSH_DISPLAY_HOME": str(self.home / "dsh-display"),
               "DSH_VIEW_PORT": str(self.port),
               "DSH_VIEW_SIZE": os.environ.get("DSH_VIEW_SIZE", "800x600"),
               "DSH_VIEW_LOG": str(self.home / "viewer.log")}
        (self.home / "fakehome").mkdir(parents=True, exist_ok=True)
        self.proc = subprocess.Popen([sys.executable, str(self.viewer)], env=env,
                                     stdout=out_log, stderr=err_log,
                                     start_new_session=True)
        deadline = time.time() + timeout
        portfile = self.home / "dsh-display" / "port"
        while time.time() < deadline:
            if portfile.exists():
                try:
                    self.port = int(portfile.read_text().strip())
                except ValueError:
                    pass
                try:
                    status, _h, _b = http_req(self.port, "GET", "/", timeout=3)
                    if status:
                        return
                except Exception:                             # noqa: BLE001
                    pass
            if self.proc.poll() is not None:
                raise RuntimeError("服务进程已退出（code=%s）：%s" % (
                    self.proc.returncode, self.tail_logs()[-600:]))
            time.sleep(0.3)
        raise RuntimeError("服务没能在 %.0fs 内就绪" % timeout)

    def tail_logs(self, n: int = 4000) -> str:
        """把服务侧所有日志拼起来 —— 出错时这是唯一能说明"为什么"的东西。"""
        chunks = []
        for name in ("viewer.log", "stderr.log", "stdout.log"):
            try:
                path = self.home / name
                if path.exists() and path.stat().st_size:
                    chunks.append(f"--- {name} ---\n" + path.read_text(
                        encoding="utf-8", errors="replace")[-n:])
            except OSError:
                pass
        return "\n".join(chunks)

    def stop(self) -> None:
        # 先让服务把自己的会话收干净（停 Xvfb、杀 /exec 子进程）—— 不留孤儿显示
        for sid in sorted(self.created_sids):
            try:
                self.close_session(sid)
            except Exception:                                 # noqa: BLE001
                pass
        for proc in self.xtarget_procs:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:                                 # noqa: BLE001
                try:
                    proc.kill()
                except Exception:                             # noqa: BLE001
                    pass
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(os.getpgid(self.proc.pid), 15)
                self.proc.wait(timeout=8)
            except Exception:                                 # noqa: BLE001
                try:
                    os.killpg(os.getpgid(self.proc.pid), 9)
                except Exception:                             # noqa: BLE001
                    pass

    @property
    def token(self) -> str:
        return (self.home / "dsh-display" / "token").read_text().strip()

    def req(self, method: str, path: str, body=None, timeout: float = 10.0,
            with_token: bool = True) -> tuple[int, dict, bytes]:
        sep = "&" if "?" in path else "?"
        full = path + (sep + "k=" + self.token if with_token else "")
        return http_req(self.port, method, full, body=body, timeout=timeout)

    # ---------------------------------------------------------------- 会话
    def display_of(self, sid: str) -> dict:
        # 首次 /display 会真的拉起 Xvfb（契约 §1.2 注释里写明必须 ensure），给足时间
        status, _h, raw = self.req("GET", f"/s/{sid}/display", timeout=30)
        return {"status": status, "json": as_json(raw) or {}, "raw": raw[:200]}

    def snapshot(self, sid: str) -> tuple[int, dict, bytes, float]:
        t0 = time.time()
        status, head, raw = self.req("GET", f"/s/{sid}/snapshot")
        return status, head, raw, time.time() - t0

    # ---------------------------------------------------------------- 会话清理
    def close_session(self, sid: str) -> None:
        """把这个会话收干净（停 Xvfb、杀 /exec 子进程）—— 不留孤儿。"""
        self.created_sids.discard(sid)
        for method in ("POST", "DELETE"):
            try:
                status, _h, _raw = self.req(method, f"/s/{sid}/close")
                if status < 400:
                    return
            except Exception:                                 # noqa: BLE001
                pass
        try:                                                  # 老版本没有 /close：退化成 /kill
            status, _h, raw = self.req("GET", f"/s/{sid}/procs")
            for item in ((as_json(raw) or {}).get("procs") or []):
                if isinstance(item, dict) and item.get("pid"):
                    self.req("POST", f"/s/{sid}/kill", body={"pid": item["pid"]})
        except Exception:                                     # noqa: BLE001
            pass

    def session_diagnostics(self, sid: str) -> str:
        """失败时必须能定位：/state（frameError/started）+ xvfb.log 尾部。"""
        bits = []
        try:
            status, _h, raw = self.req("GET", f"/s/{sid}/state", timeout=10)
            bits.append(f"/state HTTP {status}: {raw[:400].decode('utf-8', 'replace')}")
        except Exception as exc:                              # noqa: BLE001
            bits.append(f"/state 取不到：{type(exc).__name__}: {exc}")
        for path in (self.home / "dsh-display" / "sessions" / sid / "xvfb.log",):
            try:
                if path.exists():
                    bits.append(f"{path.name} 尾部：{path.read_text(errors='replace')[-300:]}")
            except OSError:
                pass
        return " | ".join(bits)

    # ---------------------------------------------------------------- 靶程序
    def start_target(self, sid: str, label: str, timeout: float = 120) -> tuple[str, Path]:
        """起靶程序；**失败重试一次换新会话**（避开"显示号被别的 X 服务器占着"的死局）。

        为什么必须重试：跨沙箱/命名空间遗留的 Xvfb 扫不到、lock 也在对方私有 /tmp 里，
        服务可能挑到别人占着的显示号 → 自己的 Xvfb 立刻 "Server is already active" 退出，
        靶程序随后 cannot open display。这类失败**换一个会话（=换一个显示号）就好了**。
        """
        problems = []
        for candidate in (sid, f"{sid}-r2"):
            try:
                logpath = self._spawn_target(candidate, label, timeout)
            except SkipCheck:
                raise
            text = self.wait_log(candidate, logpath, "READY", timeout=20)
            if "READY" in text and "ERROR" not in text:
                return candidate, logpath
            problems.append(f"[{candidate}] {text.strip().splitlines()[-1] if text.strip() else '（空日志）'}")
            self.close_session(candidate)
            time.sleep(1.0)
        raise RuntimeError("靶程序起不来（试了两次，可能是显示号被别的 X 服务器占着）："
                           + " / ".join(problems) + " || " + self.session_diagnostics(sid))

    def _spawn_target(self, sid: str, label: str, timeout: float) -> Path:
        """优先走契约的 /exec（服务进程命名空间里拉起）；没有 /exec 就本机降级。"""
        self.created_sids.add(sid)
        logpath = self.home / "logs" / f"{sid}.log"
        logpath.parent.mkdir(parents=True, exist_ok=True)
        argv = [sys.executable, str(XTARGET), str(logpath), "--label", label,
                "--timeout", str(int(timeout))]
        status, _h, raw = self.req("POST", f"/s/{sid}/exec",
                                   body={"argv": argv, "cwd": str(self.home), "wait": False})
        payload = as_json(raw) or {}
        if status == 200 and payload.get("ok") is True:
            self.exec_ok = True
            return logpath
        # 降级：selftest 与服务在同一个命名空间里（服务是 selftest 亲手起的），
        # 所以直接起也能落在同一台 Xvfb 上。**但 /exec 本身缺不缺是单独一项断言**。
        info = self.display_of(sid)
        disp = (info["json"] or {}).get("display")
        if not disp or disp.startswith("真实"):
            raise SkipCheck(f"/exec 不可用（HTTP {status}），且 /display 没给出显示号")
        env = {**os.environ, "DISPLAY": disp}
        proc = subprocess.Popen([sys.executable, str(XTARGET), str(logpath),
                                 "--label", label, "--timeout", str(int(timeout))],
                                env=env, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        self.xtarget_procs.append(proc)
        return logpath

    def read_log(self, sid: str, logpath: Path) -> str:
        """有 /exec 就用 /exec cat（证明命名空间穿透）；否则直接读文件。"""
        if self.exec_ok:
            status, _h, raw = self.req("POST", f"/s/{sid}/exec",
                                       body={"argv": ["cat", str(logpath)], "wait": True})
            payload = as_json(raw) or {}
            if status == 200 and payload.get("ok"):
                return str(payload.get("stdout") or "")
        try:
            return logpath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def wait_log(self, sid: str, logpath: Path, needle: str, timeout: float = 8.0) -> str:
        deadline = time.time() + timeout
        text = ""
        while time.time() < deadline:
            text = self.read_log(sid, logpath)
            if needle in text:
                return text
            time.sleep(0.25)
        return text

    def inject(self, sid: str, payload: dict) -> tuple[int, dict]:
        status, _h, raw = self.req("POST", f"/s/{sid}/input", body=payload)
        return status, as_json(raw) or {}


def log_lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln.strip()]


#: 靶程序日志的值可能是带空格的 JSON 字符串（json.dumps 不转义空格），
#: 所以不能简单 split(" ") —— 必须按 "引号串 | 非空白串" 两种形态取值。
FIELD_RE = re.compile(r'([A-Za-z_][\w]*)=("(?:[^"\\]|\\.)*"|\S+)')


def last_line_field(text: str, kind: str, field: str) -> str | None:
    """取最后一条 `<kind> ...` 行里的 `field=值`（值按 JSON 反转义）。"""
    value = None
    for ln in log_lines(text):
        if ln.split(" ", 2)[1:2] != [kind]:
            continue
        for name, raw in FIELD_RE.findall(ln):
            if name == field:
                try:
                    value = json.loads(raw)
                except Exception:                             # noqa: BLE001
                    value = raw
    return value


#: 只有这些行代表"被注入的事件"。READY/FOCUS/EXIT/ERROR 是靶程序**自己异步写的**
#: （FOCUS 尤其晚：XMapWindow 之后几十~几百毫秒才到），拿"总行数"比较必然竞态误报。
INJECTABLE = ("BUTTON", "BUTTONUP", "MOTION", "KEY", "LINE", "PASTE")


def injectable_count(text: str) -> int:
    return sum(1 for ln in log_lines(text) if ln.split(" ", 2)[1:2] and
               ln.split(" ", 2)[1] in INJECTABLE)


def wait_injectable(viewer, sid: str, logpath, timeout: float = 3.0) -> int:
    """等靶程序**真正就绪**（FOCUS 出现或已有可注入事件）再取基线，避免采样竞态。"""
    deadline = time.time() + timeout
    count = 0
    while time.time() < deadline:
        text = viewer.read_log(sid, logpath)
        count = injectable_count(text)
        if "FOCUS" in text or count > 0:
            return count
        time.sleep(0.2)
    return count


def events(text: str, kind: str) -> list[str]:
    return [ln for ln in log_lines(text) if ln.split(" ", 2)[1:2] == [kind]]


def wait_xy(text: str, kind: str, want_x: int, want_y: int, tol: int = 2) -> tuple[bool, str]:
    for ln in events(text, kind):
        m = re.search(r"x=(-?\d+) y=(-?\d+)", ln)
        if m and abs(int(m.group(1)) - want_x) <= tol and abs(int(m.group(2)) - want_y) <= tol:
            return True, ln
    return False, (events(text, kind)[-1] if events(text, kind) else "(没有该事件)")


def dynamic_checks(viewer: Viewer) -> None:
    print("\n---- 动态检查（真起服务 + 真注入）----")
    port = viewer.port

    # D1 服务就绪（start() 已经证明了）+ 索引页不建会话
    ok("服务能起来并写出 port/token", (viewer.home / "dsh-display" / "port").exists()
       and bool(viewer.token), f"端口 {port}")

    # D2 /health 契约
    status, _h, raw = viewer.req("GET", "/health")
    health = as_json(raw) or {}
    keys = ("ok", "service", "version", "backend", "size", "input", "port", "pid",
            "sessions", "missing")
    got = [k for k in keys if k in health]
    ctype = as_json(raw)
    health_ok = status == 200 and isinstance(ctype, dict) and all(k in health for k in keys)
    record("/health 返回契约 JSON（§1.1）", PASS if health_ok else FAIL,
           f"HTTP {status}；缺 {[k for k in keys if k not in health]}；"
           f"实际 {raw[:120]!r}")

    # D3 /health 与索引页都**不得**创建会话
    if health_ok:
        # 版本自报必须跟着 package.json 走：写死常量会在发版后对不上
        # （真发生过：面板状态条写 viewer 0.3.0，而实际装的是 0.3.2，排查时很误导）
        try:
            pkg_ver = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))["version"]
        except Exception:                                     # noqa: BLE001
            pkg_ver = None
        ok("/health 自报版本与 package.json 一致",
           pkg_ver is None or health.get("version") == pkg_ver,
           f"/health={health.get('version')} package.json={pkg_ver}")

        s1 = health.get("sessions")
        viewer.req("GET", "/health")
        viewer.req("GET", "/")
        status2, _h, raw2 = viewer.req("GET", "/health")
        h2 = as_json(raw2) or {}
        ok("/health 与索引页不创建会话（契约 §4.3）",
           s1 == 0 and h2.get("sessions") == 0, f"sessions: {s1} → {h2.get('sessions')}")
    else:
        skip("/health 无副作用（探测专用）", "上一项已 FAIL：/health 不是契约 JSON")

    # D4 会话按需创建
    sid_a = "selftest-a"
    info = viewer.display_of(sid_a)
    disp_a = (info["json"] or {}).get("display")
    size = (info["json"] or {}).get("size") or os.environ.get("DSH_VIEW_SIZE", "800x600")
    ok("/s/<sid>/display 返回显示号与尺寸", info["status"] == 200 and bool(disp_a),
       f"HTTP {info['status']} {info['json']}")

    # D5 无令牌 → 403
    status, _h, raw = viewer.req("GET", f"/s/{sid_a}/display", with_token=False)
    ok("无令牌被拒（403，契约 §1）", status == 403, f"HTTP {status} {raw[:80]!r}")

    # D6 非法 sid → 400（路径穿越）
    bad = ["/s/../../etc/passwd/display", "/s/..%2f..%2fetc/display",
           "/s/" + "a" * 65 + "/display", "/s/a%20b/display"]
    states = []
    for path in bad:
        st, _h, raw = viewer.req("GET", path)
        states.append((path, st, as_json(raw) is not None))
    bad_ok = all(st == 400 for _p, st, _j in states)
    record("非法 sid 一律 400（契约 §1.2）", PASS if bad_ok else FAIL,
           "; ".join(f"{p}→{st}" for p, st, _ in states))

    # D7 /exec 契约
    status, _h, raw = viewer.req("POST", f"/s/{sid_a}/exec",
                                 body={"argv": ["true"], "wait": True})
    payload = as_json(raw) or {}
    exec_ok = status == 200 and payload.get("ok") is True and "code" in payload
    record("/exec 存在且支持 wait:true（契约 §1.2）", PASS if exec_ok else FAIL,
           f"HTTP {status} {raw[:120]!r}")
    if not exec_ok:
        record("/exec 返回 display 字段（§1.2）", FAIL, "接口不存在")
    else:
        st2, _h2, raw2 = viewer.req("POST", f"/s/{sid_a}/exec",
                                    body={"argv": ["echo", "hi"], "wait": False})
        p2 = as_json(raw2) or {}
        record("/exec wait:false 返回 pid 与 display", PASS if p2.get("display") else FAIL,
               str(p2)[:120])

    # D8 起靶程序（失败会自动换一个会话重试一次 —— 显示号可能被别人占着）
    try:
        sid_a, log_a = viewer.start_target(sid_a, "SESSION-A")
    except SkipCheck as exc:
        skip("靶程序可启动（注入断言的前置）", str(exc))
        return
    except Exception as exc:                                  # noqa: BLE001
        record("靶程序可启动（注入断言的前置）", FAIL, f"{exc}"[:500])
        return
    text = viewer.wait_log(sid_a, log_a, "READY", timeout=15)
    if not ok("靶程序可启动（注入断言的前置）", "READY" in text,
              f"sid={sid_a} mode=" + ("exec" if viewer.exec_ok else "direct-fallback")
              + f" | 日志尾部：{text[-160:]!r}"):
        return
    if "ERROR" in text:
        record("靶程序打开了显示", FAIL, text[-200:])
        return
    record("靶程序打开了显示", PASS, log_lines(text)[0])

    # 屏宽高（用于算期望落点）
    mm = re.search(r"screen=(\d+)x(\d+)", text)
    sw, sh = (int(mm.group(1)), int(mm.group(2))) if mm else (800, 600)
    if size and "x" in str(size):
        try:
            sw, sh = (int(v) for v in str(size).split("x"))
        except ValueError:
            pass

    # D9 落点（契约 §1.3：坐标 0..1 归一化）
    for nx, ny in ((0.5, 0.5), (0.25, 0.75), (0.9, 0.1)):
        wx, wy = int(nx * sw), int(ny * sh)
        before = len(events(viewer.read_log(sid_a, log_a), "BUTTON"))
        viewer.inject(sid_a, {"t": "click", "x": nx, "y": ny, "b": 1})
        text = viewer.wait_log(sid_a, log_a, "BUTTON")
        deadline = time.time() + 5
        while time.time() < deadline and len(events(text, "BUTTON")) <= before:
            time.sleep(0.2)
            text = viewer.read_log(sid_a, log_a)
        hit, line = wait_xy(text, "BUTTON", wx, wy, tol=2)
        record(f"click 落点 ({nx},{ny}) → ({wx},{wy})±2px", hit, line)

    # 鼠标键映射：契约 b（1 左 2 右 3 中）→ X11 button（1 左 3 右 2 中）
    for b, want in ((1, 1), (2, 3), (3, 2)):
        viewer.inject(sid_a, {"t": "click", "x": 0.5, "y": 0.5, "b": b})
        deadline = time.time() + 5
        got = None
        while time.time() < deadline:
            text = viewer.read_log(sid_a, log_a)
            btns = [re.search(r"button=(\d+)", ln) for ln in events(text, "BUTTON")]
            tail = [int(m.group(1)) for m in btns if m]
            if tail and tail[-1] == want:
                got = tail[-1]
                break
            got = tail[-1] if tail else None
            time.sleep(0.2)
        record(f"鼠标键 b={b} → X button {want}", got == want, f"实际 {got}")

    # D10 文本（ASCII 逐字符）
    viewer.inject(sid_a, {"t": "text", "s": "hello"})
    text = viewer.wait_log(sid_a, log_a, 'LINE text="hello"', timeout=6)
    record('注入 ASCII 文本 "hello"', 'LINE text="hello"' in text,
           last_line_field(text, "LINE", "text") or "")

    # D11 文本（中文：服务端走剪贴板 + Ctrl+V，靶程序自己实现粘贴）
    viewer.inject(sid_a, {"t": "text", "s": "中文显示器"})
    text = viewer.wait_log(sid_a, log_a, "中文显示器", timeout=8)
    ok_cn = "中文显示器" in text
    detail = repr(last_line_field(text, "LINE", "text"))[:120]
    if not ok_cn:
        # 失败时把"为什么"一起带上：CI 上这条曾偶发失败（xclip 还没拿到选区就粘贴），
        # 当时只看到"目标里还是上一条文本"，无从定位 —— 现在顺手探测剪贴板状态。
        try:
            disp = (viewer.display_of(sid_a)["json"] or {}).get("display")
            env = dict(os.environ)
            if disp:
                env["DISPLAY"] = disp
            probe = subprocess.run(["xclip", "-selection", "clipboard", "-o"],
                                   env=env, capture_output=True, timeout=5)
            clip = probe.stdout.decode("utf-8", "replace")[:40] if probe.returncode == 0 else f"(rc={probe.returncode})"
        except Exception as exc:                              # noqa: BLE001
            clip = f"({type(exc).__name__})"
        detail += f"；xclip 读回={clip!r}；xclip 在 PATH={shutil.which('xclip') is not None}"
    record("注入中文文本（剪贴板路径，契约 §1.3 text）", ok_cn, detail)

    # D12 Backspace
    viewer.inject(sid_a, {"t": "key", "k": "Backspace"})
    text = viewer.wait_log(sid_a, log_a, "keysym=BackSpace", timeout=6)
    record("Backspace 到达（keysym=BackSpace）", "keysym=BackSpace" in text,
           repr(last_line_field(text, "LINE", "text"))[:120])

    # D13 Enter
    viewer.inject(sid_a, {"t": "key", "k": "Enter"})
    text = viewer.wait_log(sid_a, log_a, "keysym=Return", timeout=6)
    record("Enter 到达（keysym=Return）", "keysym=Return" in text, "")

    # D14 down / move / up（拖拽）
    mark = len(log_lines(viewer.read_log(sid_a, log_a)))
    viewer.inject(sid_a, {"t": "down", "x": 0.2, "y": 0.2, "b": 1})
    time.sleep(0.25)
    viewer.inject(sid_a, {"t": "move", "x": 0.6, "y": 0.6})
    time.sleep(0.25)
    viewer.inject(sid_a, {"t": "up", "x": 0.6, "y": 0.6, "b": 1})
    deadline = time.time() + 6
    seq: list[str] = []
    while time.time() < deadline:
        text = viewer.read_log(sid_a, log_a)
        seq = [ln.split(" ", 2)[1] for ln in log_lines(text)[mark:]
               if len(ln.split(" ", 2)) > 1]
        if "BUTTONUP" in seq:
            break
        time.sleep(0.25)
    order_ok = "BUTTON" in seq and "BUTTONUP" in seq and seq.index("BUTTON") < len(seq) - 1
    record("down→move→up 都到达且顺序正确", order_ok, " ".join(seq[:8]))

    # D15 滚轮方向（契约 §1.3：dy>0 = 向下滚 = X11 button 5）
    for dy, want, label in ((120, 5, "dy=+120 → button 5（下滚）"),
                            (-120, 4, "dy=-120 → button 4（上滚）")):
        before = len(events(viewer.read_log(sid_a, log_a), "BUTTON"))
        viewer.inject(sid_a, {"t": "wheel", "dy": dy, "x": 0.5, "y": 0.5})
        deadline = time.time() + 6
        got = None
        while time.time() < deadline:
            text = viewer.read_log(sid_a, log_a)
            btns = [ln for ln in events(text, "BUTTON")]
            if len(btns) > before:
                m = re.search(r"button=(\d+)", btns[-1])
                got = int(m.group(1)) if m else None
                break
            time.sleep(0.2)
        record(f"滚轮 {label}", got == want, f"实际 button={got}")

    # D16 顺序保证：20 个**独立请求**必须按到达顺序串行执行（契约 §1.3）
    viewer.inject(sid_a, {"t": "key", "k": "ctrl+a"})       # 无关，仅占位
    mark = len(log_lines(viewer.read_log(sid_a, log_a)))
    want = "abcdefghijklmnopqrst"
    for ch in want:
        viewer.inject(sid_a, {"t": "text", "s": ch})
    deadline = time.time() + 20
    final_line = ""
    key_order = ""
    while time.time() < deadline:
        text = viewer.read_log(sid_a, log_a)
        tail = log_lines(text)[mark:]
        # LINE 是**累积缓冲区**：只取最后一条，断言它「以期望串结尾」。
        # （20 个独立请求若被并发执行，字符顺序会被打乱 —— 这正是旧实现的 bug。）
        final_line = last_line_field(text, "LINE", "text") or ""
        key_order = "".join(str(last_line_field(t, "KEY", "text") or "") for t in tail
                            if t.split(" ", 2)[1:2] == ["KEY"])
        if final_line.endswith(want):
            break
        time.sleep(0.3)
    record("20 个连续字符顺序不乱（契约 §1.3 串行执行）",
           final_line.endswith(want),
           f"期望以 {want} 结尾 / 缓冲区尾部 {final_line[-26:]!r} / KEY 到达顺序 {key_order[-26:]!r}")

    # D8b 跨进程 DISPLAY：**另一个进程**直接 DISPLAY=:<号> 也要能画窗口、收事件。
    #     （沙箱里 `ls /tmp/.X11-unix` 往往什么都看不到，但 abstract socket 照样连得上
    #      —— 「看不见 ≠ 连不上」，这条断言就是钉这个事实，别用 ls 判断可用性。）
    sid_c = "selftest-c"
    info_c = viewer.display_of(sid_c)
    disp_c = (info_c["json"] or {}).get("display") or ""
    # 这条与 /exec 无关：即使 /exec 可用，也要证明「另一个进程直接 DISPLAY=:N 也能连上」
    if disp_c.startswith(":"):
        log_c = viewer.home / "logs" / f"{sid_c}.log"
        log_c.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.Popen([sys.executable, str(XTARGET), str(log_c),
                                 "--label", "SESSION-C", "--timeout", "30"],
                                env={**os.environ, "DISPLAY": disp_c},
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        viewer.xtarget_procs.append(proc)
        def _read() -> str:
            try:
                return log_c.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return ""

        text_c = ""
        deadline = time.time() + 15
        while time.time() < deadline:
            text_c = _read()
            if "READY" in text_c:
                break
            time.sleep(0.25)
        cross_ok = "READY" in text_c
        if cross_ok:
            viewer.inject(sid_c, {"t": "click", "x": 0.5, "y": 0.5, "b": 1})
            deadline = time.time() + 6
            while time.time() < deadline:
                text_c = _read()
                if "BUTTON" in text_c:
                    break
                time.sleep(0.25)
            cross_ok = "BUTTON" in text_c
        record(f"跨进程 DISPLAY={disp_c} 可用：另一个进程直接连也能画窗口/收事件",
               cross_ok, (log_lines(text_c)[0] if text_c else "（无日志 —— 连不上）"))
    else:
        record("跨进程 DISPLAY 可用", FAIL, f"/display 没给出显示号：{info_c['json']}")

    # D8c /exec 拉起的进程必须真的在跑（契约 §1.2：/procs 与 /state.windows）
    if viewer.exec_ok:
        status, _h, raw = viewer.req("GET", f"/s/{sid_a}/procs")
        payload = as_json(raw) or {}
        procs = payload.get("procs") if isinstance(payload, dict) else None
        listed = bool(procs) and any("xtarget" in " ".join(map(str, p.get("argv", [])))
                                     for p in procs if isinstance(p, dict))
        record("/procs 能看到 /exec 拉起的靶程序", listed, f"HTTP {status} {str(payload)[:160]}")
        status, _h, raw = viewer.req("GET", f"/s/{sid_a}/state")
        st = as_json(raw) or {}
        record("/state 的 windows>0（显示上真有窗口）",
               isinstance(st, dict) and isinstance(st.get("windows"), int)
               and st.get("windows") > 0, f"HTTP {status} windows={st.get('windows')}")
    else:
        skip("/procs 与 /state.windows（/exec 路径）", "D7 已 FAIL：/exec 接口不存在")

    # D17 两会话隔离
    sid_b = "selftest-b"
    info_b = viewer.display_of(sid_b)
    disp_b = (info_b["json"] or {}).get("display")
    record("两个会话拿到不同显示号", bool(disp_a) and bool(disp_b) and disp_a != disp_b,
           f"{disp_a} vs {disp_b}")
    try:
        sid_b, log_b = viewer.start_target(sid_b, "SESSION-B")
        text_b = viewer.wait_log(sid_b, log_b, "READY", timeout=15)
        if "READY" in text_b:
            # 基线要等 B 的靶程序自己那两行（READY/FOCUS）写完再取，且只数**可注入事件**行
            before_b = wait_injectable(viewer, sid_b, log_b, timeout=3.0)
            viewer.inject(sid_a, {"t": "click", "x": 0.33, "y": 0.44, "b": 1})
            viewer.inject(sid_a, {"t": "text", "s": "iso"})
            time.sleep(1.5)
            after_b = viewer.read_log(sid_b, log_b)
            record("A 会话的注入不落到 B 会话（画面互不污染）",
                   injectable_count(after_b) == before_b,
                   f"B 的可注入事件行数 {before_b} → {injectable_count(after_b)}"
                   f"（总行数 {len(log_lines(text_b))} → {len(log_lines(after_b))}，"
                   f"READY/FOCUS 这类自带行不计）")
            st_a, _h, raw_a, dt_a = viewer.snapshot(sid_a)
            st_b, _h, raw_b, dt_b = viewer.snapshot(sid_b)
            record("两会话 snapshot 都是 JPEG 且内容不同",
                   st_a == 200 and st_b == 200
                   and raw_a[:2] == b"\xff\xd8" and raw_b[:2] == b"\xff\xd8"
                   and raw_a != raw_b,
                   f"A {st_a} {len(raw_a)}B {dt_a:.2f}s / B {st_b} {len(raw_b)}B {dt_b:.2f}s")
            record("snapshot 不阻塞（新会话首帧 < 2.5s，契约 §1.2）",
                   dt_a < 2.5, f"A 用时 {dt_a:.2f}s（HTTP {st_a}）")
        else:
            skip("会话隔离的注入断言", "B 会话靶程序没起来：" + text_b[-120:])
    except SkipCheck as exc:
        skip("会话隔离的注入断言", str(exc))

    # D18 非法输入容错
    st, payload = viewer.inject(sid_a, {"t": "nonsense"})
    record("未知输入类型不 5xx（契约 §1.3 响应形状）", st < 500 and isinstance(payload, dict),
           f"HTTP {st} {payload}")
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=8)
    try:
        conn.request("POST", f"/s/{sid_a}/input?k={viewer.token}", body=b"{not json",
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        raw = resp.read()
        record("非法 JSON body 不 5xx", resp.status < 500,
               f"HTTP {resp.status} {raw[:80]!r}")
    except Exception as exc:                                  # noqa: BLE001
        record("非法 JSON body 不 5xx", False, f"{type(exc).__name__}: {exc}")
    finally:
        conn.close()


# ====================================================================== 主流程
def has_all_tools() -> tuple[bool, str]:
    if sys.platform == "win32":
        return False, "win32：本自检的动态部分只覆盖 X11"
    if sys.platform == "darwin":
        return False, "darwin：没有 Xvfb，动态注入部分跳过"
    missing = [t for t in ("Xvfb", "xdotool", "import", "xclip") if shutil.which(t) is None]
    if missing:
        return False, "缺 " + ", ".join(missing)
    if not os.environ.get("DISPLAY") and not Path("/tmp/.X11-unix").exists():
        return False, "没有 X11 环境（/tmp/.X11-unix 不存在）"
    return True, ""


def _listening_pid(port: int) -> tuple[int | None, str]:
    """谁在监听这个端口？返回 (pid, cmdline)。纯 /proc，不依赖 ss/lsof。

    为什么需要：用户自己的显示器服务（systemd 用户单元）会**一直**写
    ``~/.cache/dsh-display``（每几秒一次 xvfb.log）。任何"前后快照必须完全一致"的
    断言都会随机变红，而那是**环境**造成的，不是自检的副作用 —— 必须能分辨归因。
    """
    inode = None
    try:
        for line in Path("/proc/net/tcp").read_text().splitlines()[1:]:
            parts = line.split()
            if len(parts) < 10:
                continue
            local, state, node = parts[1], parts[3], parts[9]
            if state != "0A":                       # 0A = LISTEN
                continue
            try:
                if int(local.split(":")[1], 16) == port:
                    inode = node
                    break
            except (ValueError, IndexError):
                continue
    except OSError:
        return None, ""
    if inode is None:
        return None, ""
    for fd_dir in Path("/proc").iterdir():
        if not fd_dir.name.isdigit():
            continue
        try:
            for fd in (fd_dir / "fd").iterdir():
                if os.readlink(fd) == f"socket:[{inode}]":
                    try:
                        cmd = (fd_dir / "cmdline").read_bytes().replace(b"\0", b" ").decode().strip()
                    except OSError:
                        cmd = ""
                    return int(fd_dir.name), cmd
        except OSError:
            continue
    return None, ""


def real_home_state() -> dict:
    """用户真实状态目录的**关键**指纹（只读，不写不删）。

    只取能稳定归因的三样：我们绝不该把它改成我们的端口、绝不该改它的令牌、
    绝不该在里面留下**我们这次测试的**会话目录。别的服务往这里写日志不算我们的副作用。
    """
    real = Path(os.path.expanduser("~/.cache/dsh-display"))
    state: dict = {"path": str(real), "exists": real.exists(), "port": None,
                   "token": None, "sessions": [], "owner_pid": None, "owner_cmd": ""}
    if not real.exists():
        return state
    for key, name in (("port", "port"), ("token", "token")):
        try:
            state[key] = (real / name).read_text(encoding="utf-8").strip()
        except OSError:
            pass
    try:
        state["sessions"] = sorted(p.name for p in (real / "sessions").iterdir())
    except OSError:
        pass
    if state["port"] and str(state["port"]).isdigit():
        state["owner_pid"], state["owner_cmd"] = _listening_pid(int(state["port"]))
    return state


def zero_side_effect_check(before: dict, after: dict, own_pids: set[int],
                           our_sids: list[str], our_port: int | None,
                           parent_had_override: bool) -> None:
    """三条**能归因**的断言 + 一条"有外部服务在用就跳过"。"""
    name = "零副作用：没有碰用户真实 ~/.cache/dsh-display"
    real = before["path"]
    if parent_had_override:
        skip(name, "调用方自己设了 DSH_DISPLAY_HOME（默认路径本就不该被碰）")
        return
    if not before["exists"]:
        record(name, PASS, f"{real} 本来就不存在（没什么可碰的）")
        return
    owner = before.get("owner_pid")
    foreign = bool(owner) and owner not in own_pids
    if foreign:
        skip(name, f"检测到外部服务正在使用该 home（pid={owner} "
                   f"{str(before.get('owner_cmd'))[:60]}…）—— 归因不明，跳过")
        return

    problems = []
    leaked = [sid for sid in our_sids if sid in (after.get("sessions") or [])]
    if leaked:
        problems.append(f"真实 home 里出现了本次测试的会话目录 sessions/{leaked}")
    if our_port and str(after.get("port")) == str(our_port):
        problems.append(f"真实 home 的 port 被改成了本次测试端口 {our_port}")
    if before.get("token") is not None and after.get("token") != before.get("token"):
        problems.append("真实 home 的 token 被改写了")
    record(name, PASS if not problems else FAIL,
           "; ".join(problems) if problems else
           f"port/token 未变、无本次测试的会话目录（sessions={len(after.get('sessions') or [])} 个，"
           f"属主 pid={owner or '无'}）")


def slow_dns_guard() -> None:
    """回归钉子：``socket.getfqdn()`` 慢的时候，服务必须**照样及时写出端口文件**。

    为什么专门测这个：``http.server.HTTPServer.server_bind()`` 会做一次反向 DNS
    (``socket.getfqdn``)。在没有反向解析的环境（GitHub Actions 的 macOS runner、
    部分容器）这一步会卡十几秒 —— 端口已经绑好、``<home>/port`` 却迟迟不写，
    于是宿主/CI/安装脚本全都判定"服务没起来"。0.3.0 的 macOS CI 就是这么红的
    （实测把 getfqdn 拖慢 20 秒，15 秒内都不出现 port 文件）。
    """
    print("\n---- 回归钉子：慢 DNS 下端口文件仍要及时写出 ----")
    guard_dir = Path(TMPDIR) / "slowdns"
    home = guard_dir / "home"
    guard_dir.mkdir(parents=True, exist_ok=True)
    (guard_dir / "sitecustomize.py").write_text(
        "import socket, time\n"
        "_orig = socket.getfqdn\n"
        "def slow(name=''):\n"
        "    time.sleep(20)\n"
        "    return _orig(name)\n"
        "socket.getfqdn = slow\n",
        encoding="utf-8")
    port = free_port()
    env = {**os.environ, "PYTHONPATH": str(guard_dir), "DSH_DISPLAY_HOME": str(home),
           "DSH_VIEW_PORT": str(port)}
    env.pop("DSH_VIEW_LOG", None)
    proc = subprocess.Popen([sys.executable, str(VIEWER)], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        port_file = home / "port"
        deadline = time.time() + 6.0
        seen = None
        while time.time() < deadline:
            if port_file.exists() and port_file.read_text(encoding="utf-8").strip():
                seen = time.time()
                break
            time.sleep(0.1)
        if seen is None:
            record("慢 DNS 下 <home>/port 仍及时写出", FAIL,
                   "等 6 秒仍没有 port 文件 —— server_bind 里大概又走了 socket.getfqdn()")
        else:
            record("慢 DNS 下 <home>/port 仍及时写出", PASS,
                   f"端口文件 = {port_file.read_text(encoding='utf-8').strip()}")
        if port_file.exists():
            bound = port_file.read_text(encoding="utf-8").strip()
            code, _hdrs, raw = http_req(int(bound), "GET", "/health")
            record("慢 DNS 下拉起的服务仍能应答 /health",
                   code == 403 and b"token" in raw, f"HTTP {code} {raw[:100].decode('utf-8', 'replace')}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:                                     # noqa: BLE001
            proc.kill()


def main(argv: list[str]) -> int:
    global TMPDIR, VERBOSE
    ap = argparse.ArgumentParser(description="dsh-display-panel 自检")
    ap.add_argument("--viewer", default=str(VIEWER))
    ap.add_argument("--port", type=int, default=0, help="0 = 自动挑空闲端口")
    ap.add_argument("--home", default="", help="临时工作目录（默认自动建）")
    ap.add_argument("--timeout", type=float, default=300.0, help="动态部分总超时（秒）")
    ap.add_argument("--no-dynamic", action="store_true", help="只跑静态检查")
    ap.add_argument("--keep", action="store_true", help="保留临时目录")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv[1:])
    VERBOSE = args.verbose

    print(f"自检对象：{ROOT}")
    # 队友在并发改代码 —— 每次跑都打印被测文件指纹，否则"这条结论对应哪一版"说不清
    import hashlib

    for path in (Path(args.viewer), SELFCHECK, CLIENT_JS, INDEX_JS, XTARGET):
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
            print(f"  {path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}"
                  f"  sha256:{digest}  mtime:{int(path.stat().st_mtime)}")
        except OSError as exc:
            print(f"  {path}  读不到：{exc}")
    try:
        rev = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=10).stdout.strip()
        dirty = subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain"],
                               capture_output=True, text=True, timeout=10).stdout.strip()
        print(f"  git HEAD {rev}（工作区{'有未提交改动' if dirty else '干净'}）")
    except Exception:                                         # noqa: BLE001
        pass
    print(f"临时目录：{args.home or '（自动创建）'}")
    parent_had_override = bool(os.environ.get("DSH_DISPLAY_HOME"))
    before = real_home_state()
    TMPDIR = args.home or tempfile.mkdtemp(prefix="dsh-display-selftest-")
    Path(TMPDIR).mkdir(parents=True, exist_ok=True)
    print(f"实际使用：{TMPDIR}")

    viewer = Viewer(args.port or free_port(), Path(TMPDIR) / "viewer-home", Path(args.viewer))
    try:
        static_checks()
        slow_dns_guard()
        host_probe(Path(TMPDIR))
        ready, why = has_all_tools()
        if args.no_dynamic:
            skip("动态检查", "--no-dynamic")
        elif not ready:
            skip("动态检查（起服务 + 真注入）", why + " —— CI 上无 GUI 属正常")
        else:
            t0 = time.time()
            try:
                viewer.start()
                dynamic_checks(viewer)
            except Exception as exc:                          # noqa: BLE001
                import traceback

                record("动态检查整体可运行", FAIL, f"{type(exc).__name__}: {exc}")
                print(traceback.format_exc())
                print("服务日志：\n" + viewer.tail_logs())
            finally:
                viewer.stop()
            if time.time() - t0 > args.timeout:
                record("动态部分未超时", FAIL, f"{time.time() - t0:.0f}s > {args.timeout:.0f}s")
    finally:
        own_pids = {os.getpid()}
        if viewer.proc is not None:
            own_pids.add(viewer.proc.pid)
        zero_side_effect_check(before, real_home_state(), own_pids,
                               ["selftest-a", "selftest-b", "selftest-c"],
                               viewer.port if viewer.proc is not None else None,
                               parent_had_override)
        if args.keep:
            print(f"\n临时目录已保留：{TMPDIR}")
        else:
            shutil.rmtree(TMPDIR, ignore_errors=True)

    # 汇总
    counts = {s: sum(1 for _n, st, _d in RESULTS if st == s) for s in (PASS, FAIL, SKIP, INFO)}
    total = counts[PASS] + counts[FAIL] + counts[SKIP]
    print(f"\n== {counts[PASS]} 通过 / {counts[FAIL]} 失败 / {counts[SKIP]} 跳过"
          f"（共 {total} 项）==")
    for name, status, detail in RESULTS:
        if status == FAIL:
            print(f"  失败：{name}" + (f"  — {detail}" if detail else ""))
    if counts[SKIP]:
        print("  跳过的：" + "、".join(n for n, s, _ in RESULTS if s == SKIP))
    if counts[INFO] and not VERBOSE:
        print(f"  （另有 {counts[INFO]} 条提示，加 -v 查看）")
    print(f"结果：{'PASS' if counts[FAIL] == 0 else 'FAIL'}"
          f"（退出码 {0 if counts[FAIL] == 0 else 1}）")
    return 0 if counts[FAIL] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
