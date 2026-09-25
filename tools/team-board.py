#!/usr/bin/env python3
"""团队看板：把某个会话的 **Agent Teams 总览**（成员 / 任务进度 / 每人工作详情）实时画到
它自己的显示器上，四个队员用不同颜色分区。

和 `session-wall.py`（逐条事件流）互补：那个是"流水账"，这个是"作战地图" ——
一眼看出队伍有几个人、任务完成到哪儿、谁在干什么、最近一次交代/回报是什么。

数据全部来自会话自己的记录文件（`session.v4.jsonl.zstd`，边写边追加）：
    team/member            成员（id / name / 描述里的 W 编号）
    team/task              任务（id / subject / status / revision）—— 取每个 id 的最新修订
    team/message/queued    消息正文（senderName / targetId / content）—— 每人的"最近动态"
    assistant|tool|user    全局活动量（活跃度、最近一次动作的时刻）

**成员 ↔ 任务怎么配对**：记录里没有 ownerId，但作者给两者写了同一套 `W<n>` 编号
（成员 `W2 响应与弹幕…` ↔ 任务 `W2 响应与弹幕：…`），所以按 W 编号配对是最可靠的；
配不上的成员显示"（暂无对应任务）"。任务描述里的 `负责人：X` 也会被读出来当补充。

布局（默认 1600x1000）：
    头部   ● 团队看板 + 工作区/会话 + 成员数 · 任务数 · 完成数 + 时钟
    进度   堆叠条：完成（绿）/ 进行中（蓝）/ 待办（灰）+ 三个计数
    主体   成员网格（默认 2 列）：每人一块**独立颜色**的面板 —— 名字 + W 编号 + 任务状态 + 最近动态
    底部   记录大小 · 最后更新 · 活跃度（近 1 分钟事件数）+ 扫描线动画

用法：
    # 只看解析结果（不需要 X，自检用）
    python3 tools/team-board.py --file <记录> --dry-run
    python3 tools/team-board.py --file <记录> --json

    # 挂到某个会话的显示器上（走宿主接口，wait:false）
    curl -X POST -H 'content-type: application/json' \\
      -d '{"argv":["python3","tools/team-board.py","--file","<记录>","--title","网盘管理_V2"],"wait":false}' \\
      "http://127.0.0.1:<GUI端口>/api/dsh-display-panel/exec?session=<sid>"

依赖与坑同 `session-wall.py`（X11 ctypes 自己 malloc、ImageMagick 用 -annotate 单行、
桌面环境下 DISPLAY 指向真实桌面所以要用会话自己的 DISPLAY）。
"""
from __future__ import annotations

import argparse
import ctypes
import importlib.util
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def _load_wall_kit():
    """复用 session-wall.py 里已经验证过的 X11 封装与文字 strip 渲染（不去改它的 CLI）。"""
    path = os.path.join(HERE, "session-wall.py")
    spec = importlib.util.spec_from_file_location("ddp_session_wall", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


WALL = _load_wall_kit()

#: 成员配色（8 个，够一队用；超出就循环）—— 用户要的"不同颜色区域"
MEMBER_COLORS = [
    ("#4c8dff", (0x4C, 0x8D, 0xFF)),   # 蓝
    ("#22b8cf", (0x22, 0xB8, 0xCF)),   # 青
    ("#22c55e", (0x22, 0xC5, 0x5E)),   # 绿
    ("#f5a524", (0xF5, 0xA5, 0x24)),   # 琥珀
    ("#f76707", (0xF7, 0x67, 0x07)),   # 橙
    ("#e64980", (0xE6, 0x49, 0x80)),   # 粉
    ("#8b5cf6", (0x8B, 0x5C, 0xF6)),   # 紫
    ("#0ea5e9", (0x0E, 0xA5, 0xE9)),   # 天蓝
]
STATUS = {
    "completed": ("完成", "#22c55e"),
    "in_progress": ("进行中", "#4c8dff"),
    "pending": ("待办", "#9aa4b2"),
    "blocked": ("受阻", "#f76707"),
}
STATUS_ORDER = ("completed", "in_progress", "pending", "blocked")


def _text_of(content) -> str:
    out = []
    for item in content or []:
        if isinstance(item, dict) and item.get("type") == "text":
            out.append(str(item.get("text") or ""))
    return "\n".join(x for x in out if x).strip()


def _w_num(text: str):
    """从描述/主题里取 W 编号（W2 / W22a 都算 22；缺 W 就看 task-N 里的 N）。"""
    m = re.search(r"\bW(\d+)", text or "")
    if m:
        return int(m.group(1))
    m = re.search(r"\btask-(\d+)", text or "")
    return int(m.group(1)) if m else None


def _one_line(s: str, n: int = 150) -> str:
    s = " ".join(str(s or "").split())
    s = re.sub(r"[*`#>]+", "", s)
    return s if len(s) <= n else s[: n - 1] + "…"


def read_board(path: str) -> dict:
    """把记录解析成看板数据（纯函数，便于自检）。"""
    records = WALL.read_records(path)
    members: dict[str, dict] = {}
    tasks: dict[str, dict] = {}
    msgs: list[dict] = []
    activity = 0
    last_time = 0
    for rec in records:
        rtype = rec.get("type") or ""
        data = rec.get("data") or {}
        ts = rec.get("time") or 0
        if ts > last_time:
            last_time = ts
        if rtype == "team/member":
            m = data.get("member") or {}
            mid = m.get("id")
            if mid:
                members[mid] = {
                    "id": mid,
                    "name": str(m.get("name") or "?"),
                    "desc": _one_line(m.get("description") or "", 90),
                    "w": _w_num(m.get("description") or m.get("name") or ""),
                }
        elif rtype == "team/task":
            t = data.get("task") or {}
            tid = t.get("id")
            if tid:
                tasks[tid] = {
                    "id": tid,
                    "subject": _one_line(t.get("subject") or "", 110),
                    "status": str(t.get("status") or "pending"),
                    "revision": t.get("revision"),
                    "owner": (_text_of([{"type": "text", "text": t.get("description") or ""}]) or ""),
                    "w": _w_num(t.get("subject") or tid),
                    "updated": ts,
                }
        elif rtype == "team/message/queued":
            msg = data.get("message") or {}
            body = _one_line(_text_of(msg.get("content")), 220)
            if body:
                msgs.append({
                    "id": msg.get("id"),
                    "senderId": msg.get("senderId"),
                    "senderName": str(msg.get("senderName") or "?"),
                    "targetId": msg.get("targetId"),
                    "text": body,
                    "time": ts,
                })
        elif rtype in ("assistant/message", "tool/call", "tool/result", "user/message", "step/start"):
            activity += 1
    # 每人的"最近动态"：他发给别人的、或别人发给他的，取最新一条
    for m in members.values():
        mine = [x for x in msgs if x["targetId"] == m["id"] or x["senderId"] == m["id"]]
        if mine:
            last = mine[-1]
            out = last["senderId"] == m["id"]
            m["latest"] = ("→ " if out else "← ") + last["senderName"] + "：" + _one_line(last["text"], 150)
            m["latestTime"] = last["time"]
            m["msgCount"] = len(mine)
        else:
            m["latest"] = "（还没有往来消息）"
            m["latestTime"] = 0
            m["msgCount"] = 0
    # 成员 ↔ 任务：按 W 编号配对（记录里没有 ownerId，但作者两边用了同一套 W<n>）
    by_w: dict[int, list[dict]] = {}
    for t in tasks.values():
        if t["w"] is not None:
            by_w.setdefault(t["w"], []).append(t)
    taken: set[str] = set()
    for m in members.values():
        hit = [t for t in by_w.get(m["w"] or -1, []) if t["id"] not in taken]
        m["tasks"] = sorted(hit, key=lambda t: STATUS_ORDER.index(t["status"]) if t["status"] in STATUS_ORDER else 9)
        for t in m["tasks"]:
            taken.add(t["id"])
    # 兜底：没写 W 编号的成员，用描述里**独特的 ASCII 词**去配（如 danmu_api）。
    # ⚠️ 不能拿中文词配：像"弹幕"这种词在 9 个任务里都出现，配上就是错的。
    for m in members.values():
        if m["tasks"]:
            continue
        tokens = {tok.lower() for tok in re.findall(r"[A-Za-z][A-Za-z0-9_]{4,}", m["desc"] + " " + m["name"])}
        for t in tasks.values():
            if t["id"] in taken or not tokens:
                continue
            blob = (t["subject"] + " " + t["owner"]).lower()
            if any(tok in blob for tok in tokens):
                m["tasks"] = [t]
                taken.add(t["id"])
                m["matchedBy"] = "keyword"
                break
    # 剩下的任务（比如 lead 自己的 W1）
    unassigned = [t for t in sorted(tasks.values(), key=lambda x: _w_num(x["id"]) or 0) if t["id"] not in taken]
    counts = {k: sum(1 for t in tasks.values() if t["status"] == k) for k in STATUS_ORDER}
    total = len(tasks) or 1
    recent = sum(1 for rec in records if (rec.get("time") or 0) > last_time - 60000
                 and (rec.get("type") or "") in ("assistant/message", "tool/call", "tool/result", "user/message"))
    return {
        "members": sorted(members.values(), key=lambda m: (m["w"] is None, m["w"] or 999, m["name"])),
        "tasks": sorted(tasks.values(), key=lambda t: _w_num(t["id"]) or 0),
        "unassigned": unassigned,
        "counts": counts,
        "total": len(tasks),
        "activity": activity,
        "recentPerMin": recent,
        "lastTime": last_time,
        "bytes": os.path.getsize(path),
    }


# --------------------------------------------------------------------------- 绘制

def _rect(buf: bytearray, W: int, x: int, y: int, w: int, h: int, rgb: tuple) -> None:
    """在整幅 BGRA 缓冲里填一个矩形（纯 Python，行切片，够快）。"""
    b, g, r = rgb[2], rgb[1], rgb[0]
    row = bytes((b, g, r, 255)) * max(0, w)
    for yy in range(max(0, y), min(y + h, len(buf) // (W * 4))):
        off = (yy * W + x) * 4
        buf[off:off + len(row)] = row


def build_canvas(W: int, H: int, board: dict, title: str, sid: str, now: float,
                 bg: bytes, strips) -> bytes:
    """把整块看板合成到一个 BGRA 缓冲里。

    为什么是"整幅合成"而不是每条文字贴一张 XImage：后者每次重排都会 new 出一堆
    XImage（每张几百 KB），挂机几小时就是几百 MB —— 实测崩过一次。整幅只有一张，
    内容变化时替换，动画只重画两个小区域（扫描线 + 进行中那段）。
    """
    buf = bytearray(bg)
    HEAD, PROG, FOOT, GAP = 78, 104, 58, 12

    def text(txt, fg, bar, h, pt, x, y, limit=W - 40, buf=buf):
        png = strips.get(WALL.fit(txt, limit, pt), fg, bar, h, pt)
        WALL.blit(buf, W, png[0], png[1], png[2], x, y)
        return h

    # ---- 头部
    #: 头部要放得下：长 session id 只留前 8 位（UUID 前缀足够认人）
    short_sid = sid.replace("session-", "")
    if len(short_sid) > 12:
        short_sid = short_sid[:8]
    head = (f"● 团队看板   {title}   ·   {short_sid}   ·   成员 {len(board['members'])}"
            f"   ·   任务 {board['total']}   ·   完成 {board['counts'].get('completed', 0)}/{board['total']}"
            f"   ·   {time.strftime('%H:%M:%S')}")
    _rect(buf, W, 0, 0, 8, HEAD, MEMBER_COLORS[0][1])
    text(head, "#eaf3ff", "#4c8dff", HEAD, 27, 0, 0)

    # ---- 进度带：堆叠条（完成/进行中/待办/受阻）
    bar_x, bar_w, bar_h = 24, W - 48, 26
    y0 = HEAD + 22
    _rect(buf, W, bar_x, y0, bar_w, bar_h, (0x1B, 0x24, 0x30))
    total = max(1, board["total"])
    seg_rgb = {"completed": (0x22, 0xC5, 0x5E), "in_progress": (0x4C, 0x8D, 0xFF),
               "pending": (0x5A, 0x64, 0x72), "blocked": (0xF7, 0x67, 0x07)}
    cx = bar_x
    for key in STATUS_ORDER:
        n = board["counts"].get(key, 0)
        if not n:
            continue
        seg = int(bar_w * n / total)
        _rect(buf, W, cx, y0, seg, bar_h, seg_rgb[key])
        cx += seg
    legend = "    ".join(f"{STATUS[k][0]} {board['counts'].get(k, 0)}"
                        for k in STATUS_ORDER if board["counts"].get(k, 0))
    text(f"总进度 {board['counts'].get('completed', 0) * 100 // total}%（按任务数）    {legend}",
         "#dbe7f5", "#22c55e", 40, 20, 24, y0 + bar_h + 12, W - 48)

    # ---- 成员网格（末尾追加"未配对任务"面板）
    panels = list(board["members"])
    if board["unassigned"]:
        un = board["unassigned"]
        lines = []
        for t in un[:3]:
            label = STATUS.get(t["status"], ("?", ""))[0]
            lines.append(f"[{label}] {t['id']} {t['subject']}")
        panels.append({
            "name": f"未配对任务 {len(un)}", "w": None,
            "desc": "（没写 W 编号，或属于 lead 自己）",
            "tasks": un[:3], "latest": "   ".join(lines) or "—",
            "neutral": True,
        })
    cols = 2 if len(panels) > 3 else 1
    rows = max(1, (len(panels) + cols - 1) // cols)
    grid_y = HEAD + PROG
    grid_h = H - grid_y - FOOT
    cell_w = (W - GAP * (cols + 1)) // cols
    cell_h = (grid_h - GAP * (rows + 1)) // rows
    for i, m in enumerate(panels):
        r, c = divmod(i, cols)
        px = GAP + c * (cell_w + GAP)
        py = grid_y + GAP + r * (cell_h + GAP)
        hexc, rgb = ("#9aa4b2", (0x9A, 0xA4, 0xB2)) if m.get("neutral") else MEMBER_COLORS[i % len(MEMBER_COLORS)]
        _rect(buf, W, px, py, cell_w, cell_h, (0x12, 0x1A, 0x24))
        _rect(buf, W, px, py, 8, cell_h, rgb)                       # ← 每人的专属色条
        wtag = f"W{m['w']}" if m.get("w") is not None else ""
        desc = m["desc"]
        if wtag and desc.startswith(wtag):        # 描述自带 "W2 …" 就别重复显示 W 编号
            wtag = ""
        text(f"{m['name']}   {wtag}   {desc}".replace("   " * 2, "   "), hexc, hexc, 34, 21, px + 18, py + 8, cell_w - 40)
        ty = py + 48
        if m.get("neutral"):
            ty += 30                                  # 说明已在标题里，这里留一行空位对齐
        elif m["tasks"]:
            for t in m["tasks"][:2]:
                label, color = STATUS.get(t["status"], ("?", "#9aa4b2"))
                text(f"[{label}]  {t['id']}  {t['subject']}", color, color, 28, 19, px + 18, ty, cell_w - 40)
                ty += 30
        else:
            text("（暂无对应任务）", "#7c8797", "#3b4655", 28, 19, px + 18, ty, cell_w - 40)
            ty += 30
        label = "任务" if m.get("neutral") else "最近"
        text(label + "  " + m["latest"], "#c9d6e6", "#2a3a4d", 26, 18, px + 18,
             min(py + cell_h - 32, ty + 4), cell_w - 40)

    # ---- 底部
    foot = (f"记录 {board['bytes'] // 1024} KB   ·   最后更新 "
            f"{time.strftime('%H:%M:%S', time.localtime(board['lastTime'] / 1000)) if board['lastTime'] else '—'}"
            f"   ·   近 1 分钟活动 {board['recentPerMin']} 条   ·   事件累计 {board['activity']}"
            f"   ·   未配对任务 {len(board['unassigned'])}")
    _rect(buf, W, 0, H - FOOT, W, FOOT, (0x0B, 0x12, 0x1B))
    text(foot, "#a9c0d6", "#22b8cf", FOOT, 20, 0, H - FOOT, W - 40)
    return bytes(buf)


def animate(x, W: int, H: int, board: dict, img, now: float) -> None:
    """内容没变时也要"活着"：只重画两个小区域（进度条 + 扫描线），其余从画布上复位。"""
    bar_x, bar_w, bar_h, y0 = 24, W - 48, 26, 78 + 22
    # 进度条整段复位，再画一次"进行中"的呼吸段
    x.put_rect(img, bar_x, y0, bar_w, bar_h)
    total = max(1, board["total"])
    done = int(bar_w * board["counts"].get("completed", 0) / total)
    seg = int(bar_w * board["counts"].get("in_progress", 0) / total)
    if seg:
        k = 0.72 + 0.28 * abs(((now * 0.6) % 1) - 0.5) * 2
        x.rect(bar_x + done, y0, seg, bar_h,
               (int(0x4C * k) << 16) | (int(0x8D * k) << 8) | int(0xFF * k))
    # 底部扫描线
    x.put_rect(img, 0, H - 8, W, 8)
    k2 = (now * 0.25) % 2
    pos = int((k2 if k2 <= 1 else 2 - k2) * (W - 260))
    x.rect(pos, H - 8, 260, 3, 0x2FD0E0)
    x.flush()


def main() -> int:
    ap = argparse.ArgumentParser(description="团队看板：成员 / 任务进度 / 每人工作详情（实时）")
    ap.add_argument("--file", required=True, help="会话记录（session.v4.jsonl.zstd 或未压缩 .jsonl）")
    ap.add_argument("--title", default="DSH", help="看板标题（一般填工作区名）")
    ap.add_argument("--sid", default="", help="会话 id（显示在头部）")
    ap.add_argument("--width", type=int, default=1600)
    ap.add_argument("--height", type=int, default=1000)
    ap.add_argument("--fps", type=float, default=20.0)
    ap.add_argument("--cache", default="/tmp/dsh-team-board")
    ap.add_argument("--seconds", type=float, default=0, help="跑多久（0=一直跑）")
    ap.add_argument("--dry-run", action="store_true", help="只打印解析结果，不画（不需要 X）")
    ap.add_argument("--json", action="store_true", help="把解析结果打成 JSON（自检用）")
    args = ap.parse_args()

    if args.dry_run or args.json:
        board = read_board(args.file)
        if args.json:
            print(json.dumps(board, ensure_ascii=False))
            return 0
        print(f"成员 {len(board['members'])}  任务 {board['total']}  "
              f"完成 {board['counts']['completed']}  进行中 {board['counts']['in_progress']}  "
              f"待办 {board['counts']['pending']}  未配对 {len(board['unassigned'])}")
        for m in board["members"]:
            t = m["tasks"][0] if m["tasks"] else None
            print(f"  [{m['name']:<12}] W{m['w']}  "
                  f"{('[' + STATUS.get(t['status'], ('?',))[0] + '] ' + t['id'] + ' ' + t['subject']) if t else '（无任务）'}")
            print(f"        {m['latest'][:120]}")
        return 0

    W, H = args.width, args.height
    strips = WALL.Strips(args.cache, W)
    x = WALL.X(W, H, "DSH_TEAM_BOARD")
    bg = bytes(WALL.gradient(W, H, (0x0A, 0x14, 0x22), (0x0D, 0x1B, 0x2A)))
    img = None
    board = None
    sig = None
    last = 0.0
    last_size = -1
    t0 = time.time()
    while True:
        now = time.time()
        if args.seconds and now - t0 > args.seconds:
            break
        try:
            st = os.stat(args.file)
            if st.st_mtime != last or st.st_size != last_size:
                last, last_size = st.st_mtime, st.st_size
                board = read_board(args.file)
        except Exception as exc:                      # noqa: BLE001
            if board is None:
                board = {"members": [], "tasks": [], "unassigned": [], "counts": {},
                         "total": 0, "activity": 0, "recentPerMin": 0, "lastTime": 0, "bytes": 0,
                         "error": f"{type(exc).__name__}: {exc}"}
        if board is None:
            time.sleep(0.2)
            continue
        key = hashlib_key(board)
        if key != sig:
            sig = key
            canvas = build_canvas(W, H, board, args.title, args.sid, now, bg, strips)
            if img is not None:
                x.destroy(img)                 # 旧画布及时释放（挂机不能泄漏）
            img = x.image(canvas, W, H)
            x.put(img, W, H)
            x.flush()
        else:
            animate(x, W, H, board, img, now)
        time.sleep(max(0.0, 1.0 / args.fps))
    return 0


def hashlib_key(board: dict) -> str:
    import hashlib
    core = {
        "m": [(m["id"], m["name"], m["latest"], [t["id"] + t["status"] + str(t["revision"]) for t in m["tasks"]])
              for m in board["members"]],
        "c": board["counts"],
        "t": board["total"],
        "u": [t["id"] for t in board["unassigned"]],
    }
    return hashlib.sha1(json.dumps(core, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


if __name__ == "__main__":
    sys.exit(main())
