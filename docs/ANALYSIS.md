# dsh-display-panel 问题总清单（Lead 分析报告 v1）

> 结论先说：**这个插件不是"差一点"，而是"三个半边各自都有结构性缺陷，且合起来在真实
> DSH 环境里跑不通"**。它"能加载"（宿主半边会挂载、客户端 bundle 会进入启动清单——
> 这两点在真实的 DSH 0.1.7-rc.2 实例上实测通过），但**"能用"的链路是断的**。
>
> 分析方式：通读全部 5 个源文件（1310 行 Python + 288 行 JS），在真实 DSH 实例上装载并
> 渲染，在本机起真实服务用 curl / X11 靶程序做行为验证。下面每条都标注了证据来源。
>
> 标注：[实测] = 本次在真机/真实例上跑出来的；[读码] = 从源码可确定；[推断] = 有依据但未实测。

---

## 0. 环境与验证前提（先说清楚"为什么用户说用不了"）

| 事实 | 证据 |
|---|---|
| 用户机器上**插件根本没装进正在运行的 DSH profile**：`~/.dsh/profiles/web/package.json` 里的依赖 `dsh-display-panel` 指向 `/home/xgl/python/dsh-display-panel`，而项目已经搬到 `/home/xgl/python/DSH插件/dsh-display-panel` → **软链接悬空**；正在运行的桌面版 profile（`.../development/home/profiles/desktop`）里**只有 dsh-browser-panel**。 | [实测] `ls -la ~/.dsh/profiles/web/node_modules/`（`dsh-display-panel -> ../../../../python/dsh-display-panel`，目标不存在）；`cat .../profiles/desktop/package.json` |
| 用户机器上**跑的是另一个 checkout 的显示器服务**：systemd 用户单元 `dsh-display-viewer.service` → `~/…/DeepSeekHarness控制台/tools/dsh-display-viewer.py`，监听 8099，用 `~/.cache/dsh-display`。本仓库这份（dsh-display-panel）**没有任何实例在跑**。 | [实测] `systemctl --user status dsh-display-viewer`（active）+ `/proc/1525018/{cwd,cmdline}` |
| 而本仓库 README 教的 `bash scripts/install-service.sh` 会写**同名单元** `~/.config/systemd/user/dsh-display-viewer.service` —— 也就是**直接覆盖上面那个正在用的单元**（高危，见 §6.2）。 | [读码] scripts/install-service.sh:10-13 |

> 这三条决定了修复的第一优先级：**别再要求用户先手工装好 systemd 服务**。

---

## 1. 致命（P0）：设计与集成错误，导致"装上也不可用"

### P0-1 面板用 iframe 直连另一个端口 —— 三种常见部署下必然失效
`lib/client.js` 渲染 `<iframe src="http://127.0.0.1:<port>/s/<sid>/?k=<token>&v=...">`。[读码] client.js:27,35,87-89,165-170

* **HTTPS 部署**：DSH Web UI 若走 HTTPS，`http://` 子框架被浏览器按**混合内容**拦掉 → 面板永远空白。
* **远程访问**：从另一台机器打开 DSH Web UI 时，`127.0.0.1` 指的是**访问者自己的电脑** → 永远连不上（用户还会以为是插件坏了）。
* **CSP / 反代**：任何把 UI 放在反向代理或收紧 `frame-src` 的环境都会拦掉。
* 与官方插件 `dsh-browser-panel` 的做法（全部走同源 `/api/dsh-browser-panel/*` + WebSocket）完全相反。

### P0-2 访问令牌被送进浏览器
`lib/index.js` 的 `/api/dsh-display-panel/info` 把 `<home>/token` 明文返回给浏览器；客户端再把它拼进 iframe URL 与每个图片请求。[读码] index.js:32-39,59；client.js:89,114,117

* 令牌进了浏览器历史、`Referer`、可能进日志；
* 令牌是"同机其它用户读不到令牌文件"这一**唯一**隔离手段，泄漏即等于把桌面/显示交出去；
* 令牌出现在 URL 里也让 `Cache-Control` 之外的任何中间层都可能留存它。

### P0-3 探测接口有副作用 + 探测结果不可恢复
客户端用 `GET /state?probe=1` 探测端口（`client.js:113,128`），而服务端 `/state` 会 `session(sid).ensure()`——**一次探测就把 Xvfb 拉起来**（`/state` 在 sid 为空时落到索引页，行为还不一致）。[读码] viewer.py:1194-1213,1160

更糟的是探测成功后：
```js
const timer = setInterval(() => { if (!origin) void findService() }, POLL_MS)   // client.js:137
```
`origin` 一旦被赋值，这个轮询就**永远不会再探测**；服务换端口/重启后（服务端本来就会"端口被占就往后找"），面板再也回不来，只能靠用户手动点「重新检测」。[读码] client.js:100-139

### P0-4 会话间"隔离"会被显示号碰撞打破
显示号是 `100 + crc32(sid) % 300`（viewer.py:533）。**300 个号、无占用检测**：两个不同 sessionId 撞到同一个号时，第二个会话的 `_alive()` 会对**别人的** Xvfb 返回 True，于是两个会话共用一台显示——正是这个插件声称已经解决的"串扰"问题。[读码] viewer.py:533,557-576

### P0-5 沙箱与显示通道（**结论已修正，见 §6 勘误**）
初版结论「沙箱里 `DISPLAY=:N` 必然失败、插件的核心链路是断的」**是错的**：
沙箱确实把 `/tmp` 换成了私有 tmpfs（`ls /tmp/.X11-unix` 什么都看不到），
但 X11 客户端优先走 **abstract unix socket**（属于网络命名空间，bwrap 默认不隔离），
所以 `DISPLAY=:N 程序` 实测**能用**。详见 §6「勘误与更正」。

仍然成立的**真实**缺口是：没有 `exec`/进程管理接口时，
(a) 沙箱一旦再收紧（例如加 `--unshare-net`）就立刻失效；
(b) AI 无法列出/结束自己在显示上拉起的程序；
(c) 失败时（`Unable to open display`）没有任何可诊断的通道。

### P0-6 服务必须由用户手工常驻，且缺少任何诊断
服务没起时面板只有一句"显示器还没有打开"，没有：服务是否安装、端口是多少、日志在哪、缺什么依赖、怎么一键拉起。README 说"缺依赖会在页面和 /state 里明说"，但**独立页面根本没渲染 `missing`**（`PAGE` 里没有这个字段），只有 `/state` 的 JSON 里有。[读码] viewer.py:936-1049,1208-1212

---

## 2. 严重（P1）：功能与正确性缺陷

| 编号 | 问题 | 位置 | 后果 |
|---|---|---|---|
| P1-1 | 输入事件**每个都起一个新线程**（`threading.Thread(target=inject...)`） | viewer.py:1106 | 打字顺序会被打乱（`hello`→`hlelo`），拖拽/点击都可能乱序 |
| P1-2 | 没有 `mouseup`/拖拽支持：只有 `mousedown` 发 click，`mousemove` 只在按住时发 `move` | client.js:998-1004 | 选不中文本、拖不动窗口/滑块；右键靠 `contextmenu` 单独一条路，行为不一致 |
| P1-3 | Windows 滚轮方向反了：DOM `dy>0`（向下滚）被映射成 `+WHEEL_DELTA`（Windows 里正值=向上滚） | viewer.py:856-861 | Windows 上滚轮方向相反 |
| P1-4 | `/snapshot` 没有帧时最多**阻塞 5 秒**（20×0.25s），而页面每 130ms 拉一次 | viewer.py:1181-1193 | 服务刚起时面板卡住、宿主侧连接堆积 |
| P1-5 | `/stream` 客户端断开时 `wfile.write` 抛 `BrokenPipeError`，只 catch 了内层 try 的一半；循环也从不检查连接 | viewer.py:1162-1180 | 刷栈、线程泄漏（每个 MJPEG 客户端一条线程永不退出） |
| P1-6 | 会话与 Xvfb **永不回收**：`_sessions` 只增、`_spawned` 只增（失败的 Popen 也塞进去） | viewer.py:634-661,683-689 | 长跑必然泄漏显示与进程；`systemd` 日志里"unit 停了还有进程" |
| P1-7 | 抓帧失败**完全静默**（`except Exception: pass`） | viewer.py:921-932 | 画面全黑时用户与日志都无从判断是"没程序"还是"抓不到" |
| P1-8 | 抓帧每会话每 0.5s 起一个 `import` 进程；页面按 130ms 拉帧，实际帧率与进程开销不匹配 | viewer.py:908-932 | CPU 浪费；`import` 7.x 与 6.x 命令差异也没处理 |
| P1-9 | 独立页面 XSS：`PAGE.format(sid=sid, k=k)` 把 URL 里的原始字符串直接插进 HTML/JS | viewer.py:1231-1236 | 构造 `/s/"><script>…/` 即可在该源（握有令牌的页面）上执行脚本 |
| P1-10 | 会话 id 未校验就拼进文件路径 | viewer.py:525 | 路径穿越（`os.path.join(HOME,"sessions",sid)`） |
| P1-11 | 令牌比较用 `==` | viewer.py:157 | 计时侧信道（低危但不该留） |
| P1-12 | HTTP/1.0（默认）+ 响应头不完整 | viewer.py:1052-1068 | 每个请求一条连接；无 `Connection`/超时控制 |
| P1-13 | win32/darwin 的页面文案与后端不符：`if BACKEND == "win32"` 之外一律写"独立显示"，**darwin 明明是真实桌面** | viewer.py:1224-1230 | 用户以为在操作虚拟显示，其实在动自己的真实桌面 |
| P1-14 | Windows 上"空闲提示"恒为不空闲；`_win_windows` 未做 DPI 处理 | viewer.py:1199-1200,489-506 | 提示失真 |
| P1-15 | `xdotool type` 走 ASCII 分支时用 `--delay 25` 但没做超长文本切分；非 ASCII 才走剪贴板 | viewer.py:764-789 | 长文本/特殊符号仍可能丢字符；剪贴板方案依赖 `xclip` 缺失时静默失败 |
| P1-16 | 客户端文案写死中文，且**没有英文回退**；`order:60`、`BUILD` 常量与实现不同步 | client.js:20,39-51 | 非中文用户体验差；排查困难 |

---

## 3. 一般（P2）：健壮性、体验、可维护性

| 编号 | 问题 | 位置 |
|---|---|---|
| P2-1 | `selfcheck.py` 只覆盖 8 条断言，且**没有覆盖**本轮修复的所有新行为（down/up、wheel 方向、sid 校验、/health 无副作用） | service/selfcheck.py |
| P2-2 | 仓库里**没有** README 承诺的 `tools/dsh-display-selftest.py` / `tools/dsh-display-testcard.py`（它们在另一个仓库），用户无法自测 | README:206-215 |
| P2-3 | CI 只有 macOS 一条（且只跑 selfcheck + curl 冒烟），**没有 Linux CI**；README 却宣称"12 项功能自测 + 真机验证" | .github/workflows/macos.yml |
| P2-4 | README 自相矛盾：第 64 行「macOS 暂不支持 ✗（只有 x11/wayland/win32 三个后端）」vs 第 19/69-88 行"macOS 已支持、CI 已验证"；git 历史里那次"消除矛盾"的提交并没改到 64 行 | README:64,19,69 |
| P2-5 | `scripts/hint.js` 是无效脚手架（`--user xgl` 硬编码、没接到任何 npm 钩子） | scripts/hint.js |
| P2-6 | `package.json` 的 `files` 冗余（`service/selfcheck.py`、`service/windows`、`scripts/publish-npm.sh` 都被父目录覆盖）；没有 `test` 脚本 | package.json:22-31,47-49 |
| P2-7 | `install-service.sh` 依赖 systemd 用户总线（本机不可用），且没有卸载脚本 | scripts/install-service.sh |
| P2-8 | 面板没有：状态条、光标位置（Xvfb 抓帧不含指针 → 用户是"盲点"）、缺依赖提示、后端/真实桌面警告、缩放/适应、重连退避 | client.js 全文 |
| P2-9 | `/state` 的 `windows` 在会话未创建时也会返回，语义含糊；`idle` 对真实桌面后端无意义 | viewer.py:1194-1213 |
| P2-10 | 服务目录权限：`HOME_DIR` 设了 700，但 `sessions/` 子目录用默认 umask（755）创建，别的用户可列举会话 id | viewer.py:548-552 |
| P2-11 | 注入失败只打日志，**HTTP 仍返回 `{"ok":true}`**（客户端以为成功） | viewer.py:1101-1110 |
| P2-12 | `_split()` 对 `/s/<sid>` 无斜杠、百分号编码、多余斜杠的边界行为未定义/未测 | viewer.py:1070-1082 |
| P2-13 | wayland 后端实际上不可用（需要 seatd/seat 组/ydotoold），但仍在文档里当"可用后端"之一 | viewer.py:598-620 |

---

## 4. 本次的验证手段（修复后要用它证明）

| 手段 | 说明 |
|---|---|
| 真实 DSH 实例 | `dsh --profile test --port 19399`（隔离 DSH_HOME，profile 里 link 本插件），实测能加载 → 加载日志、`__DSH_BOOT__` 里出现 `display-panel/client.js` |
| 真实浏览器 | Playwright 1.61 + Brave（`executablePath=/opt/brave.com/brave-origin-beta/brave`），可截图、可看控制台错误、可断言 canvas 画出了帧、可断言输入 POST 的坐标 |
| 可断言的注入靶 | `tools/xtarget.py`（ctypes+libX11）：把收到的 BUTTON x/y、KEY keysym/text 写进日志 → 落点误差、按键名、顺序都能断言，不靠人眼 |
| 服务级自检 | `tools/selftest.py`：零副作用、临时 HOME、随机高位端口，覆盖 /health 无副作用、sid 校验、注入、顺序、隔离 |
| 独立验证 | `verify-dev` 队友做对抗性复核（边界、并发、上游挂掉），结论写 `verify/REPORT.md` |

---

## 5. 修复方向（与 docs/CONTRACT.md 对应）

1. **宿主半边改成同源反向代理 + 生命周期管理**（`lib/index.js`）：所有 `/api/dsh-display-panel/*`
   走宿主，令牌不出宿主；服务未起时自动拉起；提供 `/health` 探测与诊断信息。
2. **客户端半边弃用 iframe**（`lib/client.js`）：canvas 拉帧 + 自己采集输入 + 状态条 + 光标 + i18n + 退避重连。
3. **服务端补齐正确性与生命周期**（`service/dsh-display-viewer.py`）：串行输入队列、down/up、滚轮方向、
   会话回收、显示号分配、sid 校验、XSS、`/health`、`/exec`（解决沙箱 /tmp 问题）、断开处理。
4. **外壳修好**：`install-service.sh` 非 systemd 兜底、README 与代码对齐、Linux CI、`tools/` 自测脚本进仓库。

---

## 6. 勘误与更正（必须读：Lead 自己推翻过一条结论）

### 6.1 P0-5 的原判被推翻：`DISPLAY=:N` 在 DSH 沙箱里其实**能用**

初版报告（以及发给全队的任务书）断言：
> 「DSH 的 bash 工具在 bwrap 沙箱里跑，`/tmp` 是私有 tmpfs，所以服务建的
> `/tmp/.X11-unix/X<n>` 在沙箱里看不到，`DISPLAY=:148 程序` 必然失败。」

**这条是错的。** 复现过程与结论：

```console
$ findmnt -T /tmp -o TARGET,SOURCE,FSTYPE
TARGET SOURCE FSTYPE
/tmp   tmpfs  tmpfs        # 沙箱里 /tmp 确实是独立 tmpfs

$ bwrap --ro-bind / / --dev /dev --unshare-pid --proc /proc --die-with-parent \
        --tmpfs /tmp -- sh -c 'ls /tmp/.X11-unix; DISPLAY=:134 xdotool getdisplaygeometry'
ls: 无法访问 '/tmp/.X11-unix': 没有那个文件或目录
800 600                    # ← 居然连上了

$ bwrap ... 同上，但加 --unshare-net ...
Failed creating new xdo instance    # ← 加网络隔离后才失败（同一台显示仍活着）
```

原因：X11 客户端**优先**连 abstract unix socket（`@/tmp/.X11-unix/X134`），
它属于**网络命名空间**，而 DSH 的沙箱默认**不隔离网络**（`--unshare-pid`、`--tmpfs /tmp`，
没有 `--unshare-net`）。所以文件系统上看不到 socket，连接却照样成功。

**教训**：`ls` 看不到 ≠ 连不上。这次是靠"真的去连一次"才发现的；
如果只按 `ls` 的结果写文档，就会把一个**本来能用**的功能写成"必须改用 exec"。

**修正后的正确说法**（已同步到 docs/CONTRACT.md §4.1）：

| 做法 | 今天是否可用 | 说明 |
|---|---|---|
| `DISPLAY=:<号> 程序` | ✅ 可用 | 靠 abstract socket；**但依赖沙箱不隔离网络** |
| 服务的 `POST /s/<sid>/exec` | ✅ 可用 | 由服务自己拉起，命名空间无关；还能列/杀进程、拿输出，**推荐** |

所以 `exec` 的定位从"修复断掉的链路"降级为"更稳、更可诊断的推荐路径"，
README/文档必须按"两种都行、推荐 exec"来写（已通知 docs-dev 与 service-dev）。

---

## 7. 修复对照表（问题 → 改在哪 → 怎么证明）

> 本节是 §1–§3 那张清单的"结账单"：每条 P0/P1/P2 都对应到具体文件与**可复现的验证**。
> 复现方式统一在两处：`python3 service/selfcheck.py`（静态+接口层，72/73 PASS + 1 SKIP）、
> `python3 tools/selftest.py`（动态，63 PASS / 0 FAIL / 1 SKIP）、
> `node tools/e2e-panel.mjs --url "<带 token 的 DSH URL>"`（真浏览器，17 PASS / 0 FAIL）。

### P0（致命）

| 问题 | 修复 | 验证 |
|---|---|---|
| P0-1 面板 iframe 直连另一个端口（HTTPS 混合内容 / 远程访问 / 反代全废） | 面板改走**同源代理** `lib/client.js` 只用 `/api/dsh-display-panel/*`；宿主 `lib/index.js` 转发 | 源码里 `127.0.0.1`/`localhost`/`?k=`/`<iframe>` 命中数=0（selftest 静态断言）；浏览器 e2e：`canvas=1 iframe=0` |
| P0-2 令牌进浏览器 | 令牌只在宿主侧拼上游 URL；`/info` 不含令牌 | 对全部响应体 grep 令牌 → 0 命中（host-dev 实测）；selftest 有"`/info` 响应体不含令牌"断言 |
| P0-3 探测接口有副作用（打 `/state` 会顺手拉 Xvfb）+ 探测成功后永不再试 | 新增无副作用的 `/health` 作唯一探测入口；宿主侧探测+缓存；客户端指数退避自愈 | `接口冒烟全程没有创建会话、没有拉起显示服务器 — sessions=0 spawned=0`；selftest「/health 前后 sessions 不变」；客户端 503 后 588ms 自动恢复并续拉 57 帧 |
| P0-4 显示号 `crc32%300` 会撞号 → 两会话共用一台显示 | `displays.json` 持久映射 + `locks/X<n>.lock` 原子占用 + 扫 `/proc`；**再加 root window 归属标记**（见 §6.3） | selftest「两个会话拿到不同显示号 :296 vs :342」「同 sid 重启后同号」；selfcheck 4 条归属校验断言 |
| P0-5（已勘误）沙箱里怎么把程序放到那台显示上 | 保留 `DISPLAY=:N`（实测可用，见 §6.1）+ 新增 `POST /s/<sid>/exec`（+`/procs`/`/kill`）作为更稳路径；宿主给 AI 的 `display_panel_run` 走它 | selftest「跨进程 DISPLAY=:300 可用」「/exec wait:true 回显 DISPLAY/QT_QPA_PLATFORM/WAYLAND_DISPLAY」「/procs 能看到 /exec 拉起的靶程序」 |
| P0-6 服务要用户手工常驻、缺任何诊断 | 宿主按需自动拉起（`DSH_VIEW_MANAGED`）；`/info` 给端口/版本/pid/缺依赖/日志路径；客户端状态条显示后端/fps/缺依赖/真实桌面警告 | host-dev 实测：服务未起 `running=false` → `POST /service{start}` → `running=true(pid,version 0.3.0,legacy=false)` → `/frame` 出真 JPEG；面板冷启动路径在两种 e2e 里都验证 |

### P1（严重）

| 问题 | 修复 | 验证 |
|---|---|---|
| P1-1 输入乱序（每事件一线程） | 每会话单 worker + `queue.Queue` 串行 | selftest「20 个连续字符顺序不乱」；selfcheck 断言 |
| P1-2 没有拖拽/抬起 | 新增 `down`/`up`，客户端按住时发 move | selftest「down→move→up 都到达且顺序正确」；客户端 e2e「双击=2 down/2 up」 |
| P1-3 win32 滚轮反方向 | 统一 DOM 语义（dy>0=下滚）再映射各后端 | selftest「dy=+120→按钮5 / dy=-120→按钮4」；win32 侧仅单元断言（无 Windows 机器） |
| P1-4 `/snapshot` 最长阻塞 5 秒 | 没帧立即 503 | selftest「snapshot 不阻塞 — 0.26s / 0.016s」 |
| P1-5 `/stream` 断连刷栈 | 断连检测 + 异常隔离 | 代码审查 + 服务不再打栈（日志无 BrokenPipe） |
| P1-6 会话/Xvfb/Popen 永不回收 | 空闲回收（`DSH_VIEW_IDLE_MINUTES` 默认 30）+ `DELETE /s/<sid>`/`close` + 退出清理 + 死进程不入 `_spawned` | selftest「空闲回收后 sessions=0 且 Xvfb 也没了」；selfcheck「退出清理」 |
| P1-7 抓帧失败静默 | `/state.frameError` + 抓帧失败日志 + 退避 | selftest 在失败注入下能读到 `frameError` |
| P1-8 抓帧进程开销 | 拉帧节奏与抓帧间隔解耦、无会话不抓帧 | `/state.frame` 统计（count/lastAt） |
| P1-9 独立页面 XSS | `html_escape` + `js_str` 双转义 | selfcheck 断言（含 `"><script>` 类 sid） |
| P1-10 会话 id 路径穿越 | 白名单 `^[A-Za-z0-9._-]{1,64}$`（另拒 `.`/`..`）→ 400 | selfcheck + selftest：`../x`/`../../etc/passwd`/65 字符/缺失 全 400 |
| P1-11 令牌 `==` 比较 | `hmac.compare_digest` | 代码审查 + selfcheck 断言 |
| P1-12 HTTP/1.0、响应头不全 | `HTTP/1.1` + 全 `no-store` + handler 异常隔离 | selfcheck 断言（HTTP 版本、no-store） |
| P1-13 darwin/win32 文案把真实桌面写成"独立显示" | 按 `realDesktop` 分文案 | selfcheck 断言；面板状态条也会警告 |
| P1-14 win32 空闲计数/DPI | 保留并改进（仅静态断言） | **未在 Windows 上验证** |
| P1-15 文本注入健壮性 | 剪贴板路径（`xclip -l`）+ 有焦点余量 | selftest「中文经剪贴板真粘进目标程序 `PASTE len=15 text=中文显示器`」 |
| P1-16 写死中文、无构建标记 | zh/en + `BUILD` 常量 + 状态条 | 客户端 e2e（zh/en 文案断言） |

### P2（一般）

`tools/selftest.py`+`tools/xtarget.py`+`tools/e2e-panel.mjs` 进仓库（README 承诺的自测工具终于存在）；
新增 Linux CI（`linux.yml`）并修正 macOS CI；README 全重写（平台矩阵明示未验证项、故障排查、
接口表、DISPLAY vs exec 的正确说法）；`scripts/hint.js` 从死脚手架改成真的状态检查；
`package.json` files 去重 + `test/selfcheck/hint` 脚本；`install-service.sh` 加 systemd 主路径 +
setsid 兜底 + **拒绝覆盖别人的同名单元**（本机真有一个同名单元指向控制台仓库）+ 新增 uninstall；
会话目录/token/displays.json 权限收到 700/600；注入失败不再回 `{"ok":true}`；
面板补状态条/光标准星/缺依赖提示/重连退避。

### 明确**没有**验证的部分（不要当成已验证）

* **Windows**：`win32` 后端、`service/windows/*.bat|vbs`、滚轮方向修复、idle 计数 —— 本轮无 Windows 机器；
* **macOS**：仅 CI 静态断言；输入注入需要真机「辅助功能」权限；
* **wayland 后端**：仍不可用（缺 seatd/vptr），已降级为明确报错；
* **真实系统输入法**（本机只模拟了 composition 事件）、**HTTPS 同源部署**、**dpr>1 屏**。
