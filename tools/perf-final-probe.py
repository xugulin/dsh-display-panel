#!/usr/bin/env python3
"""终版（0.4.0 冻结）复核补充探针 —— 量 `tools/perf-panel.mjs` 覆盖不到的三个口径。

为什么要有这个文件（而不是改 perf-panel.mjs）：
`tools/perf-panel.mjs` 是本项目**既有的、被前几轮用过的**测量工具，它量的是 §5.1 的
主指标（内容 fps / 延迟 / 静止带宽 / 服务自身 CPU）。终版复核另外还需要三样东西，
它们要么是"连接级"的行为（裸 socket），要么是"进程树"的口径（服务 + 编码子进程），
塞进那 1500 行的脚本里只会让主测量更难复现。所以这里独立成一个**只加不改**的探针。

三个模式：

  keepalive  复现 §3.1 那个真 bug 的原始场景：**同一条** HTTP/1.1 连接上
             POST /s/x/stream-config（404，服务端没抽干 body）之后，
             下一个请求还能不能正常（修好了=200；没修=501，正文里会带着上个 POST 的 body）。
             同时对**冻结的 0.3.4 基线服务**跑同一段代码 —— 基线必须仍然复现 501，
             否则说明"这个测试根本测不出那个 bug"，绿了也不算数（阳性对照）。

  idle       契约 §5.2「空闲窗口不参与降档」+「有余量时每 2 秒 +2 滞回爬回」：
             (a) 画面静止 14s，`fpsCap` 一帧都不许掉；
             (b) 静止期间 `/stats.idle` 要变 true、`reason` 要说明是空闲；
             (c) 恢复变化后 `fpsActual` 要爬回 ≥15；
             (d) 重启服务后（控制器从保守档重新爬）采样 `fpsCap` 轨迹，
                 看是不是 "+2 / 2s" 的阶梯 —— 这是"爬回"最直接的证据。

  cpu        契约 §5.1 要求的**两个** CPU 数（外加静止）：
             服务自身 / 服务+编码子进程（走 /proc 子孙树）/ 静止。
             按 fps 档位各量一遍（默认 20 档 + 契约点名的 15 档）。

用法：
    python3 tools/perf-final-probe.py --mode keepalive --port 8513 --home DIR [--baseline-port 8514 --baseline-home DIR]
    python3 tools/perf-final-probe.py --mode idle   --port 8513 --home DIR --session perf1
    python3 tools/perf-final-probe.py --mode cpu    --port 8513 --home DIR --session perf1 --fps-config 20,15
    # 每个模式都把结构化结果打到 stdout（JSON），另可用 --json FILE 落盘。

⚠️ 只连 127.0.0.1 上**自己起的**服务端口；不碰用户的 8099/8100/19387。
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
ANIM = os.path.join(HERE, "perf-anim.py")
CLK = os.sysconf("SC_CLK_TCK")
MY_PORTS = {8503, 8504, 19601, 8513, 8514, 19611}


# --------------------------------------------------------------------- HTTP
def http(port, path, token, method="GET", body=None, timeout=15, raw=False):
    """一次性 HTTP（urllib 默认不复用连接，所以它天生不会踩 keep-alive 污染）。"""
    sep = "&" if "?" in path else "?"
    url = f"http://127.0.0.1:{port}{path}{sep}k={token}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {"content-type": "application/json"} if data else {}
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = r.read()
            return r.status, payload if raw else _maybe_json(payload), dict(r.headers)
    except urllib.error.HTTPError as e:
        payload = e.read()
        return e.code, payload if raw else _maybe_json(payload), dict(e.headers)
    except Exception as e:                                    # noqa: BLE001
        return 0, {"error": str(e)}, {}


def _maybe_json(b: bytes):
    try:
        return json.loads(b.decode("utf-8", "replace"))
    except Exception:                                          # noqa: BLE001
        return {"_raw": b[:400].decode("utf-8", "replace")}


def stats(port, token, sid):
    code, js, _ = http(port, f"/s/{sid}/stats", token, timeout=6)
    return js if code == 200 and isinstance(js, dict) else None


def start_anim(port, token, sid, log, fps, duration, label):
    open(log, "w").close()
    http(port, f"/s/{sid}/display", token, timeout=20)     # 先确保会话/显示存在（会话被回收后会按需重建）
    code, js, _ = http(port, f"/s/{sid}/exec", token, method="POST", timeout=15, body={
        "argv": ["python3", ANIM, "--log", log, "--fps", str(fps),
                 "--duration", str(duration), "--label", label],
        "cwd": REPO, "wait": False})
    time.sleep(0.7)
    return {"status": code, "pid": (js or {}).get("pid"), "display": (js or {}).get("display"), "body": js}


def kill_anim(port, token, sid, pid):
    if not pid:
        return None
    code, js, _ = http(port, f"/s/{sid}/kill", token, method="POST", timeout=8, body={"pid": pid})
    for _ in range(40):
        time.sleep(0.05)
        c2, j2, _ = http(port, f"/s/{sid}/procs", token, timeout=6)
        if c2 == 200 and not any(p.get("pid") == pid for p in (j2 or {}).get("procs", [])):
            return {"status": code, "gone": True}
    return {"status": code, "gone": False}


# --------------------------------------------------------------------- /proc
def proc_stat(pid):
    try:
        with open(f"/proc/{pid}/stat") as fh:
            s = fh.read()
        after = s[s.rindex(")") + 2:].split()
        return {"ppid": int(after[1]), "utime": int(after[11]), "stime": int(after[12]),
                "ticks": int(after[11]) + int(after[12])}
    except Exception:                                          # noqa: BLE001
        return None


def descendants(root):
    """root 的**所有**子孙（BFS）—— 编码器（ffmpeg）是服务直接 spawn 的子进程。"""
    kids = {}
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        st = proc_stat(d)
        if st:
            kids.setdefault(st["ppid"], []).append(int(d))
    out, frontier = [], [root]
    while frontier:
        nxt = []
        for p in frontier:
            for c in kids.get(p, []):
                out.append(c)
                nxt.append(c)
        frontier = nxt
    return out


def snapshot(pids):
    return {p: (proc_stat(p) or {}).get("ticks") for p in pids}


def delta(before, after, seconds):
    out = {}
    for p, v in after.items():
        if v is None or before.get(p) is None:
            continue
        out[p] = (v - before[p]) / CLK / seconds * 100
    return out


def cmdline(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            return fh.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
    except Exception:                                          # noqa: BLE001
        return None


def proc_start_iso(pid):
    try:
        with open(f"/proc/{pid}/stat") as fh:
            s = fh.read()
        ticks = int(s[s.rindex(")") + 2:].split()[19])
        with open("/proc/stat") as fh:
            btime = int([l for l in fh if l.startswith("btime ")][0].split()[1])
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(btime + ticks / CLK))
    except Exception:                                          # noqa: BLE001
        return None


def find_procs(*needles):
    out = []
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        c = cmdline(d)
        if c and all(n in c for n in needles):
            out.append({"pid": int(d), "cmd": c})
    return out


# ----------------------------------------------------------- 裸 socket HTTP/1.1
class Conn:
    """一条**真正的** keep-alive 连接：自己造请求、自己解析响应，绝不替服务端抽 body。"""

    def __init__(self, port, timeout=8):
        self.s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        self.buf = b""

    def close(self):
        try:
            self.s.close()
        except Exception:                                      # noqa: BLE001
            pass

    def send(self, method, path, token, body=None, extra=None):
        sep = "&" if "?" in path else "?"
        head = [f"{method} {path}{sep}k={token} HTTP/1.1",
                f"Host: 127.0.0.1", "Connection: keep-alive", "Accept: */*"]
        payload = b""
        if body is not None:
            payload = json.dumps(body).encode()
            head += ["Content-Type: application/json", f"Content-Length: {len(payload)}"]
        head += list(extra or [])
        self.s.sendall(("\r\n".join(head) + "\r\n\r\n").encode() + payload)

    def _fill(self, want=1):
        while self.buf.count(b"\r\n\r\n") < want:
            try:
                chunk = self.s.recv(65536)
            except socket.timeout:
                return False
            if not chunk:
                return False
            self.buf += chunk
        return True

    def read_head(self, timeout=8):
        """只读到响应头。**故意不主动抽干 body** —— 复现 §3.1 时客户端就是这么干的。"""
        self.s.settimeout(timeout)
        if not self._fill(1):
            return None
        i = self.buf.index(b"\r\n\r\n")
        head, self.buf = self.buf[:i].decode("latin1"), self.buf[i + 4:]
        lines = head.split("\r\n")
        status = int(lines[0].split()[1]) if len(lines[0].split()) > 1 else 0
        hdrs = {}
        for ln in lines[1:]:
            j = ln.find(":")
            if j > 0:
                hdrs[ln[:j].strip().lower()] = ln[j + 1:].strip()
        return {"status": status, "statusLine": lines[0], "headers": hdrs}

    def read_body(self, head, timeout=8, cap=4096):
        """按 Content-Length 读 body（没有 CL 就尽力读一点，够看 501 正文即可）。"""
        self.s.settimeout(timeout)
        n = int(head["headers"].get("content-length") or 0)
        want = min(n, cap)
        while len(self.buf) < want:
            try:
                chunk = self.s.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            self.buf += chunk
        body, self.buf = self.buf[:want], self.buf[want:]
        return body.decode("utf-8", "replace")


def keepalive_probe(port, token, tag):
    """同一个连接：POST /s/x/stream-config（不存在→404）→ 紧接着 GET /health → GET /s/x/stream。"""
    out = {"tag": tag, "port": port, "steps": []}
    c = Conn(port)
    try:
        c.send("POST", "/s/x/stream-config", token, body={"quality": 70, "fps": 15, "scale": 1})
        h1 = c.read_head()
        b1 = c.read_body(h1) if h1 else ""
        out["steps"].append({"req": "POST /s/x/stream-config", "status": h1 and h1["status"],
                             "statusLine": h1 and h1["statusLine"], "body": b1[:200],
                             "contentLength": h1 and h1["headers"].get("content-length")})

        c.send("GET", "/health", token)
        h2 = c.read_head()
        b2 = c.read_body(h2) if h2 else ""
        out["steps"].append({"req": "GET /health（同一条连接）", "status": h2 and h2["status"],
                             "statusLine": h2 and h2["statusLine"], "body": b2[:200]})

        # 真正的验收场景：宿主 POST 完 stream-config 之后，紧接着要在**同一条连接**上开流
        c.send("GET", "/s/x/stream", token)
        h3 = c.read_head()
        out["steps"].append({"req": "GET /s/x/stream（同一条连接）", "status": h3 and h3["status"],
                             "statusLine": h3 and h3["statusLine"],
                             "contentType": h3 and h3["headers"].get("content-type")})
        out["polluted"] = bool(h3 and h3["status"] >= 500)
        out["healthOk"] = bool(h2 and h2["status"] == 200)
        out["streamOk"] = bool(h3 and h3["status"] == 200)
    finally:
        c.close()
    return out


# --------------------------------------------------------------------- 模式
def mode_keepalive(a):
    out = {"mode": "keepalive", "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    for tag, port, home in (("final-0.4.0", a.port, a.home), ("baseline-0.3.4", a.baseline_port, a.baseline_home)):
        if not port or not home:
            continue
        try:
            with open(os.path.join(home, "token")) as fh:
                tok = fh.read().strip()
        except Exception as e:                                 # noqa: BLE001
            out[tag] = {"error": f"读不到 token：{e}"}
            continue
        res = keepalive_probe(port, tok, tag)
        res["servicePid"] = (http(port, "/health", tok, timeout=5)[1] or {}).get("pid")
        res["serviceStart"] = proc_start_iso(res["servicePid"]) if res["servicePid"] else None
        out[tag] = res
    return out


def mode_idle(a):
    tok = open(os.path.join(a.home, "token")).read().strip()
    sid = a.session
    out = {"mode": "idle", "port": a.port, "session": sid, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    log = os.path.join(a.logdir, "anim-idle-probe.jsonl")
    os.makedirs(a.logdir, exist_ok=True)

    # --- 0) 先把档位设成契约默认的 20，并让控制器爬到目标
    http(a.port, f"/s/{sid}/stream-config", tok, method="POST",
         body={"quality": 70, "fps": 20, "scale": 1})
    anim = start_anim(a.port, tok, sid, log, 20, 300, "idle-probe-warm")
    out["warmAnim"] = anim
    warm = []
    t0 = time.time()
    while time.time() - t0 < a.warmup:
        s = stats(a.port, tok, sid)
        if s:
            warm.append({"t": round(time.time() - t0, 2), "fps": s.get("fps"), "fpsCap": s.get("fpsCap"),
                         "fpsActual": s.get("fpsActual"), "reason": s.get("reason"), "costMs": s.get("costMs"),
                         "captureMs": s.get("captureMs"), "encodeMs": s.get("encodeMs")})
        time.sleep(0.5)
    out["warmTrajectory"] = warm
    out["warmFinal"] = stats(a.port, tok, sid)

    # --- 1) 静止 14s：fpsCap 一帧都不许掉
    kill_anim(a.port, tok, sid, anim.get("pid"))
    tk = time.time()
    still = []
    while time.time() - tk < a.still_seconds:
        s = stats(a.port, tok, sid)
        if s:
            still.append({"t": round(time.time() - tk, 2), "fps": s.get("fps"), "fpsCap": s.get("fpsCap"),
                          "userFps": s.get("userFps"), "idle": s.get("idle"), "reason": s.get("reason"),
                          "bytesPerSec": s.get("bytesPerSec"), "damageEvents": s.get("damageEvents"),
                          "lastFrameAgeMs": s.get("lastFrameAgeMs")})
        time.sleep(0.5)
    out["stillTrajectory"] = still
    caps = [r["fpsCap"] for r in still if r.get("fpsCap") is not None]
    out["still"] = {
        "seconds": a.still_seconds, "samples": len(still),
        "fpsCapMin": min(caps) if caps else None, "fpsCapMax": max(caps) if caps else None,
        "fpsCapDropped": bool(caps) and min(caps) < (caps[0] if caps else 0),
        "capAtStart": caps[0] if caps else None, "capAtEnd": caps[-1] if caps else None,
        "idleSeen": any(r.get("idle") is True for r in still),
        "idleAfterSec": next((r["t"] for r in still if r.get("idle") is True), None),
        "reasons": sorted({r.get("reason") for r in still if r.get("reason")}),
        "bytesPerSecMax": max((r.get("bytesPerSec") or 0) for r in still) if still else None,
    }
    out["verdict_idle_no_downgrade"] = (not out["still"]["fpsCapDropped"]) and out["still"]["idleSeen"]

    # --- 2) 恢复变化：fpsActual 能不能爬回 ≥15
    anim2 = start_anim(a.port, tok, sid, log, 20, 120, "idle-probe-recover")
    out["recoverAnim"] = anim2
    tr = []
    t1 = time.time()
    while time.time() - t1 < a.recover_seconds:
        s = stats(a.port, tok, sid)
        if s:
            tr.append({"t": round(time.time() - t1, 2), "fps": s.get("fps"), "fpsCap": s.get("fpsCap"),
                       "fpsActual": s.get("fpsActual"), "idle": s.get("idle"), "reason": s.get("reason"),
                       "costMs": s.get("costMs")})
        time.sleep(0.5)
    out["recoverTrajectory"] = tr
    vals = [r["fpsActual"] for r in tr if r.get("fpsActual") is not None]
    tail = vals[len(vals) // 2:] if vals else []
    out["recover"] = {"maxFps": max(vals) if vals else None,
                      "tailMeanFps": round(sum(tail) / len(tail), 2) if tail else None,
                      "capTail": tr[-1].get("fpsCap") if tr else None}
    out["verdict_climb_back"] = bool(tail) and (sum(tail) / len(tail)) >= 15
    kill_anim(a.port, tok, sid, anim2.get("pid"))

    # --- 3) 冷启动的 "+2/2s" 爬坡阶梯（"有余量时每 2 秒 +2 滞回爬回"的直接证据）
    #     服务没有 /restart 路由（路由清单见 dsh-display-viewer.py 顶部），所以用**会话回收**
    #     拿一个全新的控制器：DELETE /s/<sid> 会停 Xvfb、杀子进程、释放显示号；随后 /exec
    #     会按需重建会话 —— 这就是"管线从零开始"的真实路径。
    if a.restart_staircase:
        code, js, _ = http(a.port, f"/s/{sid}", tok, method="DELETE", timeout=15)
        out["recycleDelete"] = {"status": code, "body": js}
        time.sleep(1.5)
        anim3 = start_anim(a.port, tok, sid, log, 20, 200, "idle-probe-stair")
        stair = []
        t2 = time.time()
        while time.time() - t2 < a.staircase_seconds:
            s = stats(a.port, tok, sid)
            if s:
                stair.append({"t": round(time.time() - t2, 2), "fps": s.get("fps"), "fpsCap": s.get("fpsCap"),
                              "fpsActual": s.get("fpsActual"), "reason": s.get("reason")})
            time.sleep(0.5)
        out["staircaseTrajectory"] = stair
        changes = []
        for r in stair:
            if not changes or changes[-1]["fpsCap"] != r["fpsCap"]:
                changes.append({"t": r["t"], "fpsCap": r["fpsCap"]})
        out["staircaseSteps"] = changes
        out["staircaseDeltas"] = [{"dt": round(changes[i]["t"] - changes[i - 1]["t"], 2),
                                   "dfpsCap": (changes[i]["fpsCap"] or 0) - (changes[i - 1]["fpsCap"] or 0)}
                                  for i in range(1, len(changes))]
        kill_anim(a.port, tok, sid, anim3.get("pid"))
    return out


def mode_cpu(a):
    tok = open(os.path.join(a.home, "token")).read().strip()
    sid = a.session
    code, health, _ = http(a.port, "/health", tok, timeout=6)
    svc = (health or {}).get("pid")
    if not svc:
        return {"mode": "cpu", "error": f"/health 没给 pid（HTTP {code}）"}
    c, disp, _ = http(a.port, f"/s/{sid}/display", tok, timeout=8)
    display = (disp or {}).get("display")
    xvfbs = [p["pid"] for p in find_procs("Xvfb", display or "###")]
    out = {"mode": "cpu", "port": a.port, "session": sid, "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
           "servicePid": svc, "serviceStart": proc_start_iso(svc), "serviceCmdline": cmdline(svc),
           "display": display, "xvfbPids": xvfbs, "loadavgAtStart": open("/proc/loadavg").read().strip(),
           "windows": []}
    log = os.path.join(a.logdir, "anim-cpu-probe.jsonl")
    os.makedirs(a.logdir, exist_ok=True)
    nproc = os.cpu_count()

    for fps in [int(x) for x in str(a.fps_config).split(",") if x.strip()]:
        http(a.port, f"/s/{sid}/stream-config", tok, method="POST",
             body={"quality": 70, "fps": fps, "scale": 1})
        anim = start_anim(a.port, tok, sid, log, max(fps, 20), 300, f"cpu-probe-{fps}")
        time.sleep(a.settle)                       # 让自适应爬到该档位再开始记账
        s_before = stats(a.port, tok, sid)
        kids = descendants(svc)
        pids = [svc] + kids + xvfbs
        b = snapshot(pids)
        t0 = time.time()
        while time.time() - t0 < a.window:
            time.sleep(0.5)
        secs = time.time() - t0
        aft = snapshot(pids)
        s_after = stats(a.port, tok, sid)
        d = delta(b, aft, secs)
        kidsAfter = descendants(svc)
        enc = [p for p in kidsAfter if p != svc]
        row = {
            "fpsConfig": fps, "seconds": round(secs, 2),
            "servicePct": round(d.get(svc, 0.0), 2),
            "encoderChildren": [{"pid": p, "cmd": (cmdline(p) or "")[:90], "pct": round(d.get(p, 0.0), 2)} for p in enc],
            "encoderChildrenPct": round(sum(d.get(p, 0.0) for p in enc), 2),
            "servicePlusEncoderPct": round(d.get(svc, 0.0) + sum(d.get(p, 0.0) for p in enc), 2),
            "xvfbPct": round(sum(d.get(p, 0.0) for p in xvfbs), 2),
            "loadavgDuring": open("/proc/loadavg").read().strip(),
            "statsBefore": s_before, "statsAfter": s_after,
        }
        out["windows"].append(row)
        kill_anim(a.port, tok, sid, anim.get("pid"))
        time.sleep(2.5)

    # --- 静止窗口
    s_before = stats(a.port, tok, sid)
    kids = descendants(svc)
    pids = [svc] + kids + xvfbs
    time.sleep(3)
    b = snapshot(pids)
    t0 = time.time()
    while time.time() - t0 < a.static_window:
        time.sleep(0.5)
    secs = time.time() - t0
    aft = snapshot(pids)
    d = delta(b, aft, secs)
    kidsAfter = descendants(svc)
    out["static"] = {"seconds": round(secs, 2), "servicePct": round(d.get(svc, 0.0), 2),
                     "encoderChildrenPct": round(sum(d.get(p, 0.0) for p in kidsAfter), 2),
                     "servicePlusEncoderPct": round(d.get(svc, 0.0) + sum(d.get(p, 0.0) for p in kidsAfter), 2),
                     "xvfbPct": round(sum(d.get(p, 0.0) for p in xvfbs), 2),
                     "statsBefore": s_before, "statsAfter": stats(a.port, tok, sid),
                     "loadavgDuring": open("/proc/loadavg").read().strip()}
    out["nproc"] = nproc
    out["loadavgAtEnd"] = open("/proc/loadavg").read().strip()
    return out


# --------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", required=True, choices=["keepalive", "idle", "cpu"])
    ap.add_argument("--port", type=int, default=8513)
    ap.add_argument("--home", default="/home/xgl/python/DSH插件/.verify/final-display")
    ap.add_argument("--session", default="perf1")
    ap.add_argument("--baseline-port", type=int, default=8514)
    ap.add_argument("--baseline-home", default=None)
    ap.add_argument("--warmup", type=float, default=14)
    ap.add_argument("--still-seconds", type=float, default=14)
    ap.add_argument("--recover-seconds", type=float, default=16)
    ap.add_argument("--restart-staircase", action="store_true")
    ap.add_argument("--staircase-seconds", type=float, default=20)
    ap.add_argument("--fps-config", default="20,15")
    ap.add_argument("--settle", type=float, default=8)
    ap.add_argument("--window", type=float, default=20)
    ap.add_argument("--static-window", type=float, default=12)
    ap.add_argument("--logdir", default=os.path.join(REPO, "verify", "perf"))
    ap.add_argument("--json")
    a = ap.parse_args()
    if a.port not in MY_PORTS:
        print(f"拒绝：端口 {a.port} 不在白名单 {sorted(MY_PORTS)}（绝不碰别人的服务）", file=sys.stderr)
        return 2
    fn = {"keepalive": mode_keepalive, "idle": mode_idle, "cpu": mode_cpu}[a.mode]
    res = fn(a)
    txt = json.dumps(res, ensure_ascii=False, indent=2)
    print(txt)
    if a.json:
        with open(a.json, "w") as fh:
            fh.write(txt + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
