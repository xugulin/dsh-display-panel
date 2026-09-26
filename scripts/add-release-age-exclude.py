#!/usr/bin/env python3
"""把一条 `pkg@version` 加进 pnpm-workspace.yaml 的 minimumReleaseAgeExclude。

用法：``python3 add-release-age-exclude.py <workspace.yaml> <pkg@version>``

设计要点（都是被真实事故逼出来的）：

* **为什么要解析 YAML 而不是文本匹配**：`minimumReleaseAgeExclude:` 有多种完全
  合法的写法 —— 行内注释、flow 序列（`[a@1]`）、零缩进列表项。按行文本匹配一定会
  漏，而漏的后果是写出**重复键**或**混缩进**，让整个 profile 的 pnpm 从此读不了这个
  文件（每次 `dsh plugin add` 都报 duplicated mapping key）。
* **为什么不用 `yaml.safe_dump` 整体重写**：那会**丢掉用户全部的注释**、丢掉原有
  缩进风格（实测把两空格缩进变成零缩进）。改别人的配置文件要"只动该动的那一行"，
  所以这里只在**原文里插入一行**。
* **解析失败就拒绝，不猜**：文件本身有问题、或用了锚点/合并键这类我们不认的写法，
  就打人工指令退出 —— 绝不半懂不懂地写坏它。

退出码：0 成功（stdout 为 added/already）；3 缺 PyYAML；4 文件形态不认识；5 写失败；6 读回校验失败。
"""
from __future__ import annotations

import re
import sys

KEY = "minimumReleaseAgeExclude"
#: 顶层键（允许有前导空格，但键名必须完整）；用于确认"列表块到哪里结束"。
TOP_KEY = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_.-]*)\s*:")
#: 块式列表项：`- xxx`（允许任意缩进 + 行尾注释）。
LIST_ITEM = re.compile(r"^(\s*)-\s")
#: 该键本身（允许行尾注释）。
KEY_LINE = re.compile(r"^(\s*)" + KEY + r"\s*:(\s*)(.*)$")


def fail(code: int, message: str) -> None:
    print(message)
    sys.exit(code)


def main() -> int:
    try:
        import yaml
    except ImportError:
        fail(3, "no-yaml")

    if len(sys.argv) != 3:
        fail(4, "usage: add-release-age-exclude.py <file> <pkg@version>")
    path, entry = sys.argv[1], sys.argv[2]

    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        fail(4, f"read-error: {exc}")

    # ---- 1) 先让解析器确认"整份文件是合法 YAML，且顶层是映射" ----
    try:
        document = yaml.safe_load(text)
    except Exception as exc:                                   # noqa: BLE001
        fail(4, f"parse-error: {type(exc).__name__}: {exc}")
    if document is None:
        document = {}
    if not isinstance(document, dict):
        fail(4, f"not-a-mapping: {type(document).__name__}")

    current = document.get(KEY)
    if isinstance(current, list) and entry in current:
        fail(0, "already")
    if current is not None and not isinstance(current, list):
        fail(4, f"unexpected-shape: {type(current).__name__} {current!r}")

    lines = text.splitlines(keepends=True)
    # ⚠️ 先保证"末行以换行结尾"。否则最后一行没有 `\n` 时，往它后面 insert 的
    # 新条目会**粘在它屁股上**（`  - a@1.0.0  - pkg@9.9.9` 变成同一条），
    # 实测过：读回断言会拦下来，但那时文件已经被改坏一次了。
    if lines and not lines[-1].endswith("\n"):
        lines[-1] = lines[-1] + "\n"
        text = "".join(lines)

    # ---- 2) 定位该键所在行（顶层，允许行尾注释） ----
    key_index = None
    key_indent = ""
    trailing = ""
    for index, line in enumerate(lines):
        match = KEY_LINE.match(line.rstrip("\n"))
        if match and not match.group(1).strip():
            key_index = index
            key_indent = match.group(1)
            trailing = match.group(3).strip()
            break

    inserted = False
    # ⚠️ 块式序列项在 YAML 里**可以与键同缩进**（`key:` 后 `- item` 顶格），
    # 而且那正是 pnpm 自己写出来的形态（`packages:\n- .`）。所以新条目的缩进
    # 一律**沿用该键的缩进**，不要自作聪明加两个空格 —— 加了就会写出
    # `- a@1.0.0\n  - pkg@2` 这种"缩进更深的续行"，被解析成上一条的一部分
    # （实测：`['a@1.0.0 - pkg@9.9.9']`，读回断言会拦下来，白改一次文件）。
    item_indent_default = key_indent
    if key_index is None:
        # 没有这个键：追加到文件末尾（保持 YAML 合法 + 一行空行分隔）
        prefix = "" if (not lines or lines[-1].endswith("\n")) else "\n"
        piece = f"{prefix}\n{KEY}:\n{item_indent_default}- {entry}\n"
        new_text = text + piece
        inserted = True
    elif trailing.startswith("["):
        # flow 序列：整行替换成块式（块式才是 pnpm 自己写的形态，也不会再踩这条规则）
        items = [str(item) for item in (current or [])]
        items.append(entry)
        body = "".join(f"{item_indent_default}- {item}\n" for item in items)
        lines[key_index] = f"{key_indent}{KEY}:\n{body}"
        new_text = "".join(lines)
        inserted = True
    else:
        # 块式（或空值 + 块式列表）：插在该块的**最后一项之后**，缩进沿用该键
        item_indent = None
        last_item = None
        for index in range(key_index + 1, len(lines)):
            stripped = lines[index].rstrip("\n")
            if not stripped.strip():                            # 空行：跳过
                continue
            if stripped.lstrip().startswith("#"):               # 纯注释行：跳过
                continue
            item = LIST_ITEM.match(stripped)
            if item:
                item_indent = item.group(1)
                last_item = index
                continue
            if TOP_KEY.match(stripped):                         # 撞到下一个顶层键：块结束
                break
            # 既不是列表项也不是顶层键（例如块标量）：认不出来就不动
            fail(4, f"unexpected-line: {stripped[:80]!r}")
        if last_item is not None:
            lines.insert(last_item + 1, f"{item_indent if item_indent is not None else item_indent_default}- {entry}\n")
            new_text = "".join(lines)
            inserted = True
        elif trailing in ("", "~", "null"):
            # 空值（`key:` 后面什么都没有、也没块）：补一个块
            lines.insert(key_index + 1, f"{item_indent_default}- {entry}\n")
            new_text = "".join(lines)
            inserted = True
        else:
            fail(4, f"unexpected-value: {trailing[:80]!r}")

    if not inserted:
        fail(4, "no-insertion-point")

    # ---- 3) 写回（保留原文件类型：软链用覆盖写，别用 mv 换掉它） ----
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(new_text)
    except OSError as exc:
        fail(5, f"write-error: {exc}")

    # ---- 4) 读回断言：新条目在列表里、别的顶层键一个不少、整份文件仍可解析 ----
    try:
        with open(path, encoding="utf-8") as handle:
            check = yaml.safe_load(handle)
    except Exception as exc:                                   # noqa: BLE001
        fail(6, f"verify-parse-error: {type(exc).__name__}: {exc}")
    if not isinstance(check, dict):
        fail(6, "verify-not-a-mapping")
    if entry not in (check.get(KEY) or []):
        fail(6, "verify-entry-missing")
    before = sorted(k for k in document if k != KEY)
    after = sorted(k for k in check if k != KEY)
    if before != after:
        fail(6, f"verify-keys-changed: {before} -> {after}")
    if check.get("packages") != document.get("packages"):
        fail(6, "verify-packages-changed")

    print("added")
    return 0


if __name__ == "__main__":
    sys.exit(main())
