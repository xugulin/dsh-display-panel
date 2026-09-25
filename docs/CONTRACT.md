# dsh-display-panel 接口契约（v0.4.1，冻结）

> 本文件是**冻结的接口契约**：客户端半边（`lib/client.js`）、宿主半边（`lib/index.js`）、
> 显示器服务（`service/dsh-display-viewer.py`）三方必须严格按此实现。
> 任何一方要改协议，先改本文件，再改代码 —— 否则三方会各说各话。

## 0. 为什么改成同源代理（架构决定的来龙去脉）

旧架构：面板在 `<iframe src="http://127.0.0.1:8099/s/<sid>/?k=<token>">` 里直接嵌显示器服务页面。
它有一串**结构性问题**，不是 bug 补丁能解决的：

| 问题 | 后果 |
|---|---|
| 跨源（面板在 DSH 的端口，页面在 8099） | HTTPS 部署时被浏览器当**混合内容**拦掉（面板永远空白） |
| 写死 `127.0.0.1` | 从别的机器访问 DSH Web UI 时，浏览器连的是**访问者自己的** localhost → 永远连不上 |
| 令牌出现在 iframe URL 里 | 进浏览器历史/Referer；同机其它用户拿到就能看你的桌面 |
| 浏览器直连服务 | 端口探测（8099..8110 逐个试）不可靠，且**探测本身有副作用**（会拉起 Xvfb） |
| 服务必须由用户手工 systemd 起 | 没起服务 = 面板永远「显示器还没有打开」，用户看不出为什么 |
| iframe 里的页面自己做输入 | 面板无法加状态条、光标、缩放、错误提示、i18n |

新架构（与官方 `dsh-browser-panel` 同一套做法）：**宿主半边做同源反向代理 + 服务生命周期管理**。

```
浏览器（DSH Web UI，同源，cookie 鉴权）
   │  /api/dsh-display-panel/{info,frame,state,input,service,exec,display}
   ▼
宿主半边 lib/index.js（Node，持有服务端口与令牌，负责拉起/探测服务）
   │  http://127.0.0.1:<port>/s/<sid>/{snapshot,state,input,exec,display}?k=<token>
   ▼
显示器服务 service/dsh-display-viewer.py（每会话一台 Xvfb；win32/darwin 为真实桌面）
```

**令牌永远不出现在浏览器侧**：只有宿主半边读 `~/.cache/dsh-display/token`。
**端口探测只在宿主侧发生**，且用 `/health`（无副作用）而不是会创建会话的接口。

## 1. 显示器服务（Python）对外契约

所有响应都带 `Cache-Control: no-store`。`k=<token>` 仍是必需（服务监听 127.0.0.1，同机其它用户可连）。

### 1.1 全局（与具体会话无关，**绝不创建会话/不启动 Xvfb**）

| 方法 | 路径 | 返回 |
|---|---|---|
| GET | `/health?k=` | `{"ok":true,"service":"dsh-display-viewer","version":"<ver>","backend":"x11\|wayland\|win32\|darwin","size":"1600x1000","input":true\|false,"port":8099,"pid":123,"sessions":2,"missing":[{"tool","why","package"}]}` |
| GET | `/?k=` | 会话索引页（HTML，人用） |

`/health` 是**探测专用**：必须极快、无副作用（旧的探测打 `/state`，会顺手把 Xvfb 拉起来）。

### 1.2 每会话

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/s/<sid>/` | 独立页面（人用兜底；面板不再用它）。必须：转义 sid、显示缺失依赖、显示光标 |
| GET | `/s/<sid>/snapshot?k=&t=` | 单帧 JPEG。**不阻塞**：没有帧就 503（旧实现在这里最多阻塞 5 秒） |
| GET | `/s/<sid>/stream?k=` | MJPEG 流（备用；实现须能感知客户端断开） |
| GET | `/s/<sid>/state?k=` | `{"session","display","backend","size","windows":n,"idle":bool,"cursor":{"x":0.5,"y":0.5},"missing":[],"input":bool,"realDesktop":bool,"tooltip":"..."}` |
| GET | `/s/<sid>/display?k=` | `{"session","display","backend","size","input"}`（给脚本/AI 用） |
| POST | `/s/<sid>/input?k=` | 见 §1.3 |
| POST | `/s/<sid>/exec?k=` | `{"argv":["xterm","-e","bash"],"cwd":"/tmp","wait":false}` → `{"ok":true,"pid":123,"display":":148"}`。在该会话显示上跑程序；`wait:true` 时返回 `{"ok","code","stdout","stderr"}`。**必须**在同一进程里拉起（见 §4 沙箱说明） |
| GET | `/s/<sid>/procs?k=` | `{"procs":[{"pid","argv","startedAt"}]}` |
| POST | `/s/<sid>/kill?k=` | `{"pid":123}` |

**会话 id 校验（安全，必须做）**：只允许 `^[A-Za-z0-9._-]{1,64}$`，否则 400。
旧实现直接把 sid 拼进文件路径（`os.path.join(HOME, "sessions", sid)`）→ 路径穿越。

### 1.3 输入事件（POST `/s/<sid>/input`，JSON body）

沿用现有事件名，新增 `down`/`up`（拖拽用）。坐标一律 **0..1 归一化**（相对画面宽高）。

| `t` | 字段 | 语义 |
|---|---|---|
| `click` | `x,y,b`（b: 1 左 2 右 3 中，缺省 1） | 在 (x,y) 按下并抬起 |
| `down` / `up` | `x,y,b` | 按下 / 抬起（拖拽、长按） |
| `move` | `x,y` | 移动指针（拖拽中也会发） |
| `wheel` | `dy`（DOM `deltaY`，**dy>0 = 向下滚**，缺省 `x,y` 表示滚轮位置） | 滚轮 |
| `text` | `s` | 一段文本（服务自己决定 `xdotool type` 还是剪贴板 + Ctrl+V） |
| `key` | `k` | DOM 键名（`Enter`/`Backspace`/`ArrowUp`/`a`…），可带 `ctrl+`/`shift+`/`alt+`/`super+` 前缀，可叠加 |

响应：`{"ok":true}` / `{"ok":false,"error":"..."}`（HTTP 200 与 4xx 都用这个 JSON 形状）。

**顺序保证（必须）**：同一会话的输入事件必须**按到达顺序串行执行**。
旧实现每个事件起一个线程 → 打字顺序会被打乱（`hello` 可能变成 `hlelo`）。

### 1.4 环境变量（保持向后兼容）

`DSH_DISPLAY_HOME`（默认 `~/.cache/dsh-display`）、`DSH_VIEW_PORT`（默认 8099）、
`DSH_VIEW_SIZE`（默认 1600x1000）、`DSH_VIEW_BACKEND`、`DSH_VIEW_INPUT`（win32/darwin 注入开关）、
`DSH_VIEW_LOG`、新增 `DSH_VIEW_IDLE_MINUTES`（会话空闲回收，默认 30；0=不回收）。

## 2. 宿主半边 `lib/index.js` 对外契约

全部**同源**、全部经 `ctx.connection.requestRejection(req)` 守卫（不自己造鉴权）。
前缀 `P = /api/dsh-display-panel`。未命中/失败都返回 JSON `{ok:false,error,hint}`。

| 方法 | 路径 | 返回 |
|---|---|---|
| GET | `P/info` | `{"ok":true,"service":{"running":true,"port":8099,"managed":false,"backend":"x11","size":"1600x1000","version":"0.3.0","pid":123},"input":{"enabled":true,"realDesktop":false},"missing":[],"home":"/home/u/.cache/dsh-display","viewer":{"log":"<路径>","hint":"..."}}` |
| POST | `P/info` | 同上但**强制重新探测**（`{"action":"restart"}` 时重启由宿主管理的服务） |
| GET | `P/frame?session=<sid>&t=<ts>` | 透传 JPEG（`image/jpeg`）。服务不可用 → 503 + JSON。**这是面板画面的唯一来源** |
| GET | `P/state?session=<sid>` | 透传 `/state` 的 JSON，并补 `service` 字段 |
| GET | `P/display?session=<sid>` | 透传 `/display` |
| POST | `P/input?session=<sid>` | 透传输入事件（body 原样转发） |
| POST | `P/service` | `{"action":"start"\|"stop"\|"restart"\|"status"}` → 管理显示器服务进程 |
| POST | `P/exec?session=<sid>` | 透传 `/exec`（在会话显示上跑程序） |
| GET | `P/events?session=<sid>` | **可选**：SSE 状态推送（没有就由客户端轮询） |

要点：

* **`session` 一律取自查询串**，宿主不校验会话是否存在（显示按需创建）；
  但必须按 §1.2 的字符集校验后再拼进上游 URL（防注入）。
* **令牌只在宿主侧**：宿主读 `<home>/token`，转发时拼 `?k=`。
* **服务发现顺序**：① `GET <home>/port` 里的端口 + `token` 打 `/health`；② 失败则 8099..8110 逐个打 `/health`；
  ③ 都失败 → `service.running=false`；若 `DSH_VIEW_MANAGED`（默认开）则自动拉起。
* **自动拉起**：`spawn(python3, [<pkg>/service/dsh-display-viewer.py], { detached:true, stdio:['ignore',logfd,logfd] })` + `unref()`，
  然后轮询 `/health` 最多 ~8 秒。找不到 python3 → `missing` 里明说。
* **超时**：所有上游请求 5 秒超时（frame 3 秒）；上游慢不能拖死宿主事件循环（用 `AbortController`）。
* **绝不能影响 harness 启动**：所有逻辑包 try/catch，失败只打日志。

## 2.5 给 AI 用的工具（`lib/tools.js`，由宿主半边注册）

为什么需要：bash 工具在沙箱里看不到服务的 X socket（§4.1），而宿主自己的 HTTP 路由是
**同源 + cookie 鉴权**的，AI 用 curl 打不进去 → **没有工具，AI 就没法用这台显示**。

`lib/tools.js`（CJS，**零依赖**，工具定义手写为普通对象，因为插件目录解析不到
`@deepseek-ai/dsh-tools`）导出：

```js
const { defineTools } = require('./tools.js')
const defs = defineTools({ discover, ensureService, logger })   // -> ToolDefinition[]
```

宿主半边必须提供这两个通道（就是它自己用的探测/拉起逻辑）：

| 通道 | 签名 | 返回 |
|---|---|---|
| `discover` | `(force?: boolean) => Promise<ServiceState>` | 只探测，**绝不拉起服务** |
| `ensureService` | `() => Promise<ServiceState>` | 服务没跑就拉起，再返回状态 |

`ServiceState = {running:boolean, port:number|null, token:string|null, backend:string|null, size:string|null, missing:Array<{tool,why,package}>, home:string, log:string, managed:boolean}`

注册方式（写在 `apply` 里，**失败不能影响其它半边**）：

```js
ctx.inject(['tools'], (toolsCtx) => {
  for (const def of defineTools({ discover, ensureService })) {
    toolsCtx.effect(() => toolsCtx.tools.register(def), `dsh-display-panel: ${def.name}`)
  }
})
```

工具清单（名字都带 `display_panel_` 前缀，避免与官方 `browser_*` / `browser_panel_*` 撞名 —— 撞名会让插件加载失败）：

| 工具 | 作用 |
|---|---|
| `display_panel_status` | 服务是否在跑/端口/后端/显示号/窗口数/是否只读/缺依赖 |
| `display_panel_open` | 确保服务在跑并返回地址 |
| `display_panel_run` | **在本会话显示上跑命令**（走服务的 `/exec`，绕开沙箱 /tmp 问题） |
| `display_panel_procs` | 列出/结束本会话显示上由面板拉起的进程 |
| `display_panel_screenshot` | 抓一帧存成 JPEG 文件并返回路径 |
| `display_panel_input` | 往本会话显示注入鼠标/键盘事件（归一化坐标） |
| `display_panel_sessions` | 服务当前的会话概览 |

会话号一律取自 `exec.agent.id`（没有归属会话时**报错**，不退化成共用显示）。

## 3. 客户端半边 `lib/client.js` 对外契约

* 只访问**同源** `P/*`；**不得**出现 `127.0.0.1:<端口>`、`?k=`、iframe。
* 渲染：`<canvas>` 画帧（保持宽高比，容器内 contain）+ 指针光标覆盖层 + 顶部状态条。
* 输入：mousedown/mousemove/mouseup（拖拽）、wheel（非 passive）、keydown、composition 事件（中文）。
  坐标按 canvas 显示区域归一化到 0..1（**按 contain 后的实际画面矩形算**，不能按元素边框算）。
* 状态机：`checking → need-service（可点「打开显示器」）→ streaming → error`；断线自动退避重连。
* i18n：`zh` / `en` 两套文案（按 `navigator.language` 选），不再写死中文。
* 槽位注册方式保持现状（已被实测证明可用）：
  `ctx.slots.inject('conversation.view', () => ctx.slots.register({name:'conversation.view', id:'display-panel', order:60, label:()=>t('title'), inject:(sessionId)=>({sessionId: typeof sessionId==='string'?sessionId:''})}, Component))`
* 组件必须容错：`sessionId` 为空、宿主接口 401/500、服务未起、帧 404 —— 全部要有明确文案，不许白屏。

## 4. 环境事实（必须写进实现和文档）

1. **DSH 的 bash 工具在沙箱里跑，`/tmp` 是私有 tmpfs —— 但 `DISPLAY=:N` 仍然可用**（实测，见下）。
   * 沙箱命令行：`bwrap --ro-bind / / --dev /dev --unshare-pid --proc /proc --die-with-parent --tmpfs /tmp --bind <工作区> <工作区>`；
     里面 `ls /tmp/.X11-unix` **看不到**任何 socket（`findmnt -T /tmp` 显示是独立 tmpfs）。
   * **但是** `DISPLAY=:134 xdotool getdisplaygeometry` 在同一个沙箱里**成功**（返回 `800 600`）——
     因为 X11 客户端优先走 **abstract unix socket**（`@/tmp/.X11-unix/X134`），
     它属于**网络命名空间**，而 bwrap 默认不隔离网络。
   * 反证：给同一个 bwrap 加上 `--unshare-net` 后，同一台显示立刻变成
     `Failed creating new xdo instance`（而显示本身还活着）。
   * **结论**：`DISPLAY=:<号> 程序` 在今天的 DSH 里能用，但**依赖"沙箱不隔离网络"**，
     任何收紧沙箱的改动都会让它失效。所以：
     - 文档要写"两种都行，但推荐 `exec`"；
     - `exec` 不是"修一个坏掉的东西"，而是**更稳、更可诊断**的那条路（还能列进程、看输出、跨沙箱稳定）。
2. **systemd 用户总线是正常的**（本机实测：`systemctl --user is-active dsh-display-viewer` → `active`，
   单元文件 `/home/xgl/.config/systemd/user/dsh-display-viewer.service`，`DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1001/bus`）。
   ⚠️ 但**沙箱里的 shell 如果没有 `DBUS_SESSION_BUS_ADDRESS`/`XDG_RUNTIME_DIR`，`systemctl --user` 会报
   "Failed to connect to user scope bus"** —— 那是沙箱现象，不是机器没有 systemd（Lead 一开始就被它骗过）。
   所以 `install-service.sh` 以 systemd 为主路径、非 systemd 兜底为辅（兜底仍是必要的：headless 机器、
   容器、WSL 都可能没有用户总线）。
   ⚠️ **单元名冲突（高危）**：这台机器上已经存在同名单元 `dsh-display-viewer.service`
   （指向另一个 checkout：`~/…/DeepSeekHarness控制台/tools/dsh-display-viewer.py`，监听 8099）。
   安装脚本**必须先检测**：若同名单元已存在且 ExecStart 指向别的文件，要么拒绝并提示，
   要么用不同单元名（`dsh-display-panel-viewer.service`）——绝不允许静默覆盖别人的单元。
3. 服务端的 `Xvfb` 是按需创建的；`/health`、索引页、`/state`（无会话时）都**不得**创建会话。
4. win32 / darwin 后端抓的是**真实桌面**，注入默认关闭（`DSH_VIEW_INPUT=1` 才开），
   面板必须把 `realDesktop` 状态显式显示给用户。

---

## 5. 流畅度与观感契约（v0.4.0，冻结）

> 目的：把"更流畅、更好看"变成**可测量的指标**，而不是感觉。
> 0.3.4 的基线（本机实测，1600×1000 X11）：抓帧间隔 0.5s → **内容刷新 2 fps**；
> 每帧 spawn 一次 `import` ≈ 45ms/帧；客户端每 130ms 一次 HTTP 往返，**大量重复帧**
> （同一张 JPEG 被反复传输、解码、重绘）。

### 5.1 指标（验收线）

| 指标 | 基线（0.3.4） | 目标（0.4.0） | 怎么测 |
|---|---|---|---|
| 内容 fps（客户端画面上真正变化的帧） | ~2 | **≥ 15** | `tools/perf-panel.mjs`：在会话显示上跑一个会动的窗口，统计客户端 canvas 的**去重后**帧率 |
| 变化延迟 p50（X 侧改动 → 客户端画出来） | ~600ms | **≤ 150ms** | 同上：在 X 侧切换画面并记录时间戳，客户端检测到像素变化即计时 |
| 静止画面带宽 | ~7 fps × 7KB ≈ 50KB/s | **≤ 5KB/s**（静止时几乎不发） | `/stats` 的 `bytesPerSec` + 客户端统计 |
| 服务单核 CPU（15fps 时） | — | 服务自身 **≤ 15%**；**服务+编码子进程 ≤ 40%**；静止 ≤ 2% | `ps`/`/proc` 采样（两个数都要报） |
| 既有功能 | — | 不回退（selfcheck / selftest / e2e 全绿） | 既有三套测试 |

### 5.2 服务端：抓帧 → 编码 → 流

* **抓帧**：X11 优先走**进程内** `XGetImage`（ctypes + libX11，实测 3ms/帧），
  不再每帧 spawn `import`（45ms/帧）。libX11 不可用时退回 `import`（保持可用，只是慢）。
* **驱动方式**：能用 **XDamage** 就用它（事件驱动：静止时零抓帧零编码、变化时立刻出帧，
  实测 root window 能收到子窗口重绘）。damage 成串爆发要**合并去抖**（按 `fps` 上限出帧，
  但最后那一帧必须发出去）；没有 XDamage 的服务器退回轮询（`IDLE_FPS=4`）。
  `/stats.mode` 要能分辨 `xgetimage+xdamage` / `xgetimage+poll` / `import`。
* **编码**：优先用**常驻**编码进程（`ffmpeg -f rawvideo ... -f mjpeg -` 或 `magick`），
  一次起进程、持续出 JPEG；都没有时退回 `import`。
* **去重**：对抓到的原始帧算 CRC32；**内容没变就不重新编码、不发帧**（静止时带宽趋近 0）。
  每帧时间戳随帧下发，客户端据此算出真实 fps。
* **自适应**：服务按 `quality`（1..100）、`fps`（1..30）、`scale`（0.25..1.0）三个参数工作，
  默认 `quality=70, fps=20, scale=1`（**默认上限要高于验收线**：验收线是内容 fps ≥15，
  若默认上限也设成 15，实测会贴在上限上（~14.7）并因调度抖动判不达标 —— 默认留出余量，
  负载高时由自适应降到 12~15）；客户端/宿主可通过 `POST /s/<sid>/stream-config` 调整。
  抓不动或编码不过来时**自己降档**（先降 fps，再降 scale，最后降 quality），并在 `/stats` 里说明原因。

### 5.3 传输：MJPEG 长连接（不是每帧一次 HTTP）

`GET /api/dsh-display-panel/stream?session=<sid>`（宿主同源代理 → 上游 `/s/<sid>/stream`）
返回 `multipart/x-mixed-replace; boundary=frame`，每个 part：

```
--frame\r\n
Content-Type: image/jpeg\r\n
Content-Length: <n>\r\n
X-DSH-Seq: <单调递增帧号>\r\n
X-DSH-Time: <抓帧时刻的 epoch 毫秒>\r\n
X-DSH-Size: <WxH>\r\n
X-DSH-Cursor: <x,y 归一化 0..1>\r\n
\r\n
<JPEG 字节>\r\n
```

* 客户端用 `fetch()` + `ReadableStream` 解析（**不要**用 `<img>`：断了不会重连、也无法丢帧）。
* 游标位置**随帧下发**，客户端不必再轮询 `/state`（0.3.4 是 2 秒一次，肉眼可见的滞后）。
* 兼容：不支持流的上游（旧版服务）→ 客户端自动退回轮询 `/frame`（既有路径不得删）。

### 5.4 客户端：渲染与观感

* **绘制**：`createImageBitmap` + `requestAnimationFrame`；**最新帧优先** ——
  解码排队时丢掉中间帧（只画最新），绝不为了"每帧都画"而堆积延迟。
* **清晰度**：canvas 按 `devicePixelRatio` 分配后备缓冲；`imageSmoothingQuality='high'`；
  提供 **1:1（点对点）/ 适应窗口** 两种缩放模式，默认适应窗口。
* **视觉**：跟随 DSH 主题变量（浅色/深色都要能看）；状态条含连接指示、fps、后端、
  分辨率、以及"真实桌面/只读"警示；工具条含 适应 / 1:1 / 缩放 / 平滑 / 全屏；
  载入有骨架屏，首帧淡入，断线有明确文案；letterbox 底色用主题面色，不再写死纯黑。
* **交互反馈**：鼠标指针画成光标精灵（不是十字线）；点击有涟漪反馈；拖拽有状态提示。
* **省电**：标签页不可见（`document.visibilityState==='hidden'`）时暂停拉流并通知服务降档；
  重新可见时恢复。
* **自适应必须是双向的**：实测跟不上可以请求降档，但**降档之后必须能自己爬回来** ——
  交付帧率贴着当前档上限（≥90%）持续 `≥20s` 就试回上一档；再掉下去按 2 的幂退避
  （20s→40s→80s…），连续健康 5 分钟清零。**用户手动选的档位任何情况下都不得被自动逻辑改掉。**
  （为什么写进契约：单向降档实测把面板永久压在 8fps —— 只要开面板那一刻机器忙，之后空闲也不恢复。）

### 5.5 观测接口

* `GET /s/<sid>/stats?k=`（字段名冻结，perf 脚本按它断言）：
  ```json
  {"fps":19.7,            // 实测出帧率
   "fpsActual":19.7,      // 同上（兼容两种叫法）
   "fpsCap":20,           // 自适应控制器当前允许的上限
   "userFps":20,          // 客户端通过 stream-config 设的档
   "idle":false,          // 距上一次画面变化 >2s 视为空闲（空闲窗口不参与降档评估）
   "reason":"…",         // 人类可读：为什么降档/回升/空闲
   "quality":70,"scale":1.0,
   "mode":"xgetimage+XDamage","encoder":"ffmpeg",
   "captureMs":4.1,"captureMaxMs":…,"encodeMs":18.0,"encodeMaxMs":…,"costMs":…,
   "damageEvents":…,"damageAgoMs":…,"lastFrameAgeMs":…,
   "captured":…,"encoded":…,"skipped":…,"cursorOnly":…,
   "bytesPerSec":…,"clients":…,"seq":…,"crc":…,"cursor":{"x":…,"y":…},
   "stream":{"fps":…,"fpsCap":…,"quality":…,"scale":…,"mode":…}}
  ```
* 宿主 `GET /api/dsh-display-panel/stats?session=` 透传（供面板显示与 perf 脚本断言）。
