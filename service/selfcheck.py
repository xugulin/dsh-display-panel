#!/usr/bin/env python3
"""显示器服务自检：不产生副作用，只加载模块并核对平台相关的关键事实。

用法： python3 service/selfcheck.py [dsh-display-viewer.py 的路径]

Windows 那几项来自社区用户写的 check_inject.py（把那台机器上验过的断言搬过来），
Linux 这边补上等价的 x11 断言 —— 两边共用同一个脚本，避免各测一半。

**为什么值得单独一个自检**：这些点一旦错了都不会报错，只会"表现不对"——
    INPUT 结构大小写错 → SendInput 读错内存（可能崩、可能乱动鼠标）
    DOM 键名漏映射     → 那个键在面板里就是没反应（退格/回车都踩过）
    页面模板占位符没替换 → 页面直接显示 "{disp}"
"""

from __future__ import annotations

import ctypes
import importlib.util
import sys
from pathlib import Path

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: object = "") -> None:
    RESULTS.append((name, bool(cond), str(detail)))
    print(("PASS  " if cond else "FAIL  ") + name + (("  — " + str(detail)) if detail else ""))


def load_viewer(path: Path):
    spec = importlib.util.spec_from_file_location("viewer", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["viewer"] = mod
    spec.loader.exec_module(mod)          # __name__ != "__main__" → 不会真的起服务
    return mod


def main() -> int:
    here = Path(__file__).resolve().parent
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else here / "dsh-display-viewer.py"
    print(f"自检对象：{path}\n")
    mod = load_viewer(path)

    # ---- 通用：后端解析与关键常量
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
    else:
        check("后端解析为 x11", mod.BACKEND == "x11", mod.BACKEND)
        check("依赖自检可调用", callable(mod.missing_tools), "")
        missing = [m["tool"] for m in mod.missing_tools()]
        check("运行环境依赖齐全", not missing, "缺: " + str(missing) if missing else "无缺失")
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
        check("中文走剪贴板（xclip -l）", "xclip" in open(path, encoding="utf-8").read()
              and '"-l"' in open(path, encoding="utf-8").read(), "")

    # ---- 通用：页面模板能渲染且没有残留占位符
    try:
        # ⚠️ 模板参数要与服务端渲染处保持一致 —— 少了任何一个都是 KeyError，
        #    而这一条被 CI 抓到过（服务端加了令牌参数 k，自检这边没跟上）。
        html = mod.PAGE.format(base="/s/x", sid="s", disp="测试显示", w=1600, h=1000,
                               note="只读", k="test-token")
        check("页面模板可渲染且无残留占位符",
              "{disp}" not in html and "{base}" not in html and "测试显示" in html)
    except Exception as exc:                             # noqa: BLE001
        check("页面模板可渲染且无残留占位符", False, f"{type(exc).__name__}: {exc}")

    passed = sum(1 for _n, ok, _d in RESULTS if ok)
    print(f"\n== {passed}/{len(RESULTS)} 项通过 ==")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  未通过：{name} {detail}")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
