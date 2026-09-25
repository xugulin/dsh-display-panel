#!/usr/bin/env python3
"""卡片工具：生成"彩色测试图 / 文字鸡汤卡"，并把任意图片铺到某台显示上。

为什么放这儿：这两件事在调试显示管线时特别常用 ——
一张彩色测试图能一眼看出色偏、缩放、丢帧；一张大字卡片能把"这台显示现在该显示什么"
讲清楚。它们的实现都踩过坑，所以固化成脚本，别每次重写。

用法：
   # ① 彩色测试图（彩条 + 等离子彩带 + 灰阶 + 纯色圆点）
   python3 tools/display-cards.py testcard --out /tmp/card.png [--width 1600 --height 1000]

   # ② 文字卡片（深色渐变 + 居中的中日韩大字）
   python3 tools/display-cards.py quote --text "世上没有白走的路" --text "每一步都算数" \\
       --sub "修不好的盘可以再试一次，走错的路也算风景" --foot "U盘修复" --out /tmp/soup.png

   # ③ 铺到显示上（无边框全屏、循环显示）
   python3 tools/display-cards.py show /tmp/card.png [--display :125] [--seconds 0]

为什么要用 ffplay 铺图：本机 Xvfb 上 ImageMagick 的 `display -window root` 是**静默失败**
（退出码 1、stderr 空），而 ffplay 稳定可用；窗口正好落在 0,0 且铺满整屏。
（想设"根窗口壁纸"要自己写 ctypes + XSetWindowBackgroundPixmap，本工具不做。）

依赖：ImageMagick（`magick`）、ffplay（只有 show 用）、中日韩字体（默认 Noto-Sans-CJK-HK）。
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

FONT_DEFAULT = "Noto-Sans-CJK-HK"


def need_magick() -> None:
    if shutil.which("magick") is None:
        raise SystemExit("缺 ImageMagick（magick）—— 装：apt install imagemagick / brew install imagemagick")


def run(cmd: list[str]) -> None:
    done = subprocess.run(cmd, capture_output=True)
    if done.returncode != 0:
        tail = done.stderr.decode("utf-8", "replace").strip().splitlines()
        raise SystemExit(f"命令失败（{cmd[0]}，退出码 {done.returncode}）：{tail[-1] if tail else ''}")


def testcard(out: str, width: int, height: int) -> None:
    """彩色测试图：8 条彩条 / 等离子彩带 / 灰阶渐变 / 七色圆点。

    分区高度按 3:2.6:2:2.4 分配，最后整体缩放到请求尺寸（所以任何尺寸都能生成）。
    """
    need_magick()
    import tempfile

    tmp = tempfile.mkdtemp(prefix="ddp-testcard-")
    bar_w = width // 8
    bar_h = int(height * 0.30)
    bars = os.path.join(tmp, "bars.png")
    args = ["magick"]
    for color in ("#ffffff", "#ffe600", "#00e5ff", "#00d43c", "#ff00c8", "#ff2d2d", "#1b4dff", "#0b0b0b"):
        args += ["(", "-size", f"{bar_w}x{bar_h}", f"xc:{color}", ")"]
    args += ["+append", bars]
    run(args)

    plasma = os.path.join(tmp, "plasma.png")
    run(["magick", "-size", f"{width}x{int(height * 0.26)}", "plasma:fractal", "-modulate", "115,140", plasma])

    grays = os.path.join(tmp, "grays.png")
    run(["magick", "-size", f"{width}x{int(height * 0.20)}", "gradient:#000000-#ffffff", grays])

    dots = os.path.join(tmp, "dots.png")
    dot_h = int(height * 0.24)
    r = max(20, int(dot_h * 0.25))
    cy = dot_h // 2
    colors = ("#ff3b30", "#ff9500", "#ffcc00", "#34c759", "#00c7be", "#007aff", "#af52de")
    args = ["magick", "-size", f"{width}x{dot_h}", "xc:#12161c"]
    step = width // (len(colors) + 1)
    for i, color in enumerate(colors):
        cx = step * (i + 1)
        args += ["-fill", color, "-draw", f"circle {cx},{cy} {cx},{cy - r}"]
    args += [
        "-fill", "#ffffff", "-font", FONT_DEFAULT, "-pointsize", str(max(14, width // 53)),
        "-gravity", "southwest", "-annotate", f"+{width // 40}+{dot_h // 12}",
        f"DSH display panel · color test card · {width}x{height}",
        dots,
    ]
    run(args)

    run(["magick", bars, plasma, grays, dots, "-append", "-resize", f"{width}x{height}!", "-strip", out])
    print(f"彩色测试图 → {out}（{width}x{height}）")


def quote(out: str, texts: list[str], sub: str, foot: str, width: int, height: int, top: str, bottom: str) -> None:
    """文字卡片：深色渐变底 + 居中大字（可多行）+ 一行小字注解。"""
    need_magick()
    lines = [t for t in texts if t]
    if not lines:
        raise SystemExit("至少要有一行 --text")
    size = max(28, width // 23)
    args = ["magick", "-size", f"{width}x{height}", f"gradient:{top}-{bottom}", "-font", FONT_DEFAULT, "-gravity", "center"]
    # 主句（多行时按行距排布），整体往上挪一点给副标题留位置
    total = len(lines)
    for i, text in enumerate(lines):
        offset = int(-height * 0.08) + int((i - (total - 1) / 2) * size * 1.45)
        args += ["-fill", "#eaf2ff", "-pointsize", str(size), "-annotate", f"+0{offset:+d}", text]
    if sub:
        args += ["-fill", "#8fb8ff", "-pointsize", str(max(18, int(size * 0.56))),
                 "-annotate", f"+0+{int(height * 0.09):+d}", sub]
    if foot:
        args += ["-fill", "#6f8299", "-pointsize", str(max(14, int(size * 0.38))),
                 "-annotate", f"+0+{int(height * 0.21):+d}", foot]
    args += ["-quality", "94", out]
    run(args)
    print(f"文字卡片 → {out}（{width}x{height}，{len(lines)} 行）")


def show(path: str, display: str | None, seconds: float, geometry: str | None) -> None:
    """在显示上无边框全屏循环显示一张图（窗口落在 0,0 并铺满）。

    ⚠️ 用 ffplay 而不是 ImageMagick 的 `display -window root`：后者在本机 Xvfb 上是
    静默失败（退出码 1、无 stderr），ffplay 稳定。
    """
    if not os.path.exists(path):
        raise SystemExit(f"找不到图片：{path}")
    if shutil.which("ffplay") is None:
        raise SystemExit("缺 ffplay（装：apt install ffmpeg）")
    w = h = None
    if geometry:
        w, h = geometry.lower().split("x")[:2]
    env = dict(os.environ)
    if display:
        env["DISPLAY"] = display
    if env.get("DISPLAY") is None:
        raise SystemExit("没给 --display，环境里也没有 DISPLAY")
    cmd = ["ffplay", "-hide_banner", "-loglevel", "error", "-loop", "1", "-i", path, "-noborder",
           "-window_title", "DSH_CARD"]
    if w and h:
        cmd += ["-x", w, "-y", h]
    if seconds and seconds > 0:
        cmd = ["timeout", str(int(seconds))] + cmd
    print(f"在 {env['DISPLAY']} 上显示 {path}" + (f"（{w}x{h}）" if w else ""))
    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if seconds and seconds > 0:
        proc.wait()
    else:
        print(f"已后台显示（pid {proc.pid}）；要撤掉就 kill 它")


def main() -> int:
    ap = argparse.ArgumentParser(description="生成/显示卡片（彩色测试图、文字卡片）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("testcard", help="生成彩色测试图")
    t.add_argument("--out", default="testcard.png")
    t.add_argument("--width", type=int, default=1600)
    t.add_argument("--height", type=int, default=1000)

    q = sub.add_parser("quote", help="生成文字卡片（可多行）")
    q.add_argument("--text", action="append", default=[], help="主句，可重复多次（多行）")
    q.add_argument("--sub", default="", help="副标题")
    q.add_argument("--foot", default="", help="页脚小字")
    q.add_argument("--out", default="quote.png")
    q.add_argument("--width", type=int, default=1600)
    q.add_argument("--height", type=int, default=1000)
    q.add_argument("--top", default="#0a1526", help="渐变起色（默认深蓝）")
    q.add_argument("--bottom", default="#14405f", help="渐变止色")

    s = sub.add_parser("show", help="把图片铺到某台显示上")
    s.add_argument("path")
    s.add_argument("--display", default=None, help="目标显示，例如 :125（默认用环境里的 DISPLAY）")
    s.add_argument("--seconds", type=float, default=0, help="显示多久（0=一直显示）")
    s.add_argument("--geometry", default=None, help="窗口尺寸 WxH（默认由播放器决定）")

    args = ap.parse_args()
    if args.cmd == "testcard":
        testcard(args.out, args.width, args.height)
    elif args.cmd == "quote":
        quote(args.out, args.text, args.sub, args.foot, args.width, args.height, args.top, args.bottom)
    elif args.cmd == "show":
        show(args.path, args.display, args.seconds, args.geometry)
    return 0


if __name__ == "__main__":
    sys.exit(main())
