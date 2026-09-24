# dsh-display-panel

[![npm](https://img.shields.io/npm/v/dsh-display-panel.svg)](https://www.npmjs.com/package/dsh-display-panel)
[![Linux self-check](https://github.com/xugulin/dsh-display-panel/actions/workflows/linux.yml/badge.svg)](https://github.com/xugulin/dsh-display-panel/actions/workflows/linux.yml)
[![macOS self-check](https://github.com/xugulin/dsh-display-panel/actions/workflows/macos.yml/badge.svg)](https://github.com/xugulin/dsh-display-panel/actions/workflows/macos.yml)

给 **DeepSeek Harness Web UI** 加一个「**显示器**」标签：在「对话 / 轨迹 / 浏览器」旁边，
实时看到 AI 在**它自己的显示**上做了什么 —— 而且可以**直接在上面点击、打字**
（含中文、退格、回车、Ctrl+V），登录、扫码、验证码都能在这个标签里完成。

> A display tab for the DeepSeek Harness Web UI. Watch — and drive — the AI's own
> per-session display: isolated per session, mouse and keyboard injected back.
> The panel talks **only** to the same-origin `/api/dsh-display-panel/*` proxy,
> so the access token never reaches the browser, and the host half starts the
> viewer service **on demand** — no manual service setup required.

## 30 秒了解它

* **看**：面板里是**本会话那台显示**的实时画面（画布主动拉帧，断了自动退避重连）；
* **操作**：点击、拖拽、滚轮、键盘、中文输入法、退格/回车/Ctrl 组合键，全部注入回那台显示；
* **隔离**：**每个会话一台独立显示**（Linux），互不可见、互不污染；
* **AI 也能用**：内置 `display_panel_*` 工具，AI 可以自己把程序跑在那台显示上、
  截图、列进程、注入输入 —— 不用你在两边来回倒手；
* **不碰你的桌面**：Linux 上跑在独立的 headless X 服务器里，不占用你的 VT、不动你的输入设备
  （Windows / macOS 例外，见下）。

## 平台支持与验证状态

| 平台 | 状态 | 显示是什么 | 怎么验证的 / 还没验证什么 |
|---|---|---|---|
| **Linux**（X11 会话；Wayland 会话下同样走 Xvfb） | ✅ 支持 | 每会话一台独立 Xvfb | 作者实机 + 本仓库自测（`python3 tools/selftest.py`）。缺依赖会在面板、`/state`、启动日志里明说该装哪个包 |
| **Windows 10/11** | ✅ 支持 | **本机真实桌面**（所有会话共用），注入默认关闭 | 社区用户真机验证（GDI `BitBlt` + GDI+，纯 `ctypes`，零外部依赖）。⚠️ **本机未复测**：这一轮改动没有 Windows 机器可用，`service/windows/*.bat` 也没有在本轮跑过 |
| **macOS** | ✅ 支持 | **本机真实桌面**，注入默认关闭 | GitHub Actions 的 macOS runner（[`macos.yml`](.github/workflows/macos.yml)）：ctypes 绑定、后端解析、服务启动、令牌校验、HTTP 接口、**抓帧**（真 JPEG）。⚠️ **未验证**：输入注入需要真机授予「辅助功能」权限，CI 上给不了 |
| **同机多用户** | ✅ 隔离 | —— | 令牌文件 `~/.cache/dsh-display/token`（600，目录 700）。实测：不带令牌 `GET /` → **403**，带令牌 → **200**（本机跑过） |
| **多实例 / 多会话** | ✅ | 每会话一台显示 | 服务端口被占会自动顺延（8099→8110），宿主按 `<home>/port` 优先、再 8099..8110 顺序探测。实测：同一台机器上 8099（systemd 常驻）与 8331（后台兜底）**同时在跑**，互不干扰 |

> 说明：这张表里**没写的**就是没验证过。Windows 的那部分来自社区用户，改了代码之后
> 必须真机再跑一次才算数；macOS 的注入同理。

## 架构：三个半边，各管一段

```
浏览器（DSH Web UI，**同源**，cookie 鉴权）
  │  /api/dsh-display-panel/{info,frame,state,display,input,service,exec,procs,kill,events}
  ▼
宿主半边 lib/index.js（Node：反向代理 + 服务生命周期 + 给 AI 的工具）
  │  http://127.0.0.1:<port>/s/<sid>/{snapshot,state,input,exec,…}?k=<token>
  ▼
显示器服务 service/dsh-display-viewer.py（每会话一台 Xvfb；win32/darwin 为真实桌面）
```

三条设计红线（0.3.0 起）：

1. **令牌不进浏览器**：只有宿主半边读 `<home>/token`，转发时替客户端拼上 `?k=`。
   面板代码里不会出现令牌，也不会出现 `127.0.0.1:<端口>`（否则 HTTPS 部署会被当混合内容拦掉，
   从别的机器访问会连到访问者自己的 localhost）。
2. **端口不用你记**：宿主先读 `<home>/port`，再 8099..8110 逐个探测，都以 `/health` 为准
   （`/health` 极快、无副作用，不会顺手把 Xvfb 拉起来）。
3. **服务不用你先装**：面板要用显示时，宿主会自己拉起服务；`scripts/install-service.sh`
   只是让它**常驻**（省掉冷启动，也让显示不被宿主重启带走）。

## 安装

### 0) 依赖（只有 Linux 需要）

| 发行版 | 命令 |
|---|---|
| Arch / Manjaro | `sudo pacman -S xorg-server-xvfb xdotool xclip imagemagick` |
| Debian / Ubuntu | `sudo apt install xvfb xdotool xclip imagemagick` |
| Fedora / RHEL | `sudo dnf install xorg-x11-server-Xvfb xdotool xclip ImageMagick` |
| openSUSE | `sudo zypper install xvfb-run xdotool xclip ImageMagick` |

| 命令 | 用途 | 缺了会怎样 |
|---|---|---|
| `Xvfb` | 每会话一台虚拟显示 | 面板一直停在「需要先打开显示器」 |
| `xdotool` | 鼠标/键盘注入（XTest，**不需要任何权限**） | 能看不能点 |
| `import` | 抓画面（ImageMagick） | 画面全黑 |
| `xclip` | **中文输入**（剪贴板 + Ctrl+V） | 英文能打，中文打不进去 |

* **Windows 什么都不用装**：`win32` 后端是纯 `ctypes`（GDI + GDI+）；
* **macOS 也什么都不用装**：抓帧用系统自带的 `screencapture -x -t jpeg`；
* 缺依赖**不会**让安装失败 —— 面板会直接告诉你缺哪个、装哪个包
  （例如「缺 `xclip`」只表现为"中文打不进去"，很隐蔽，所以必须显式提示）。

### 1) 装插件本体（官方路径，实测可用）

```sh
# <profile> 是你跑 DSH 用的 profile 名（例如 web / desktop / test）
dsh plugin --profile <profile> add link:/绝对路径/dsh-display-panel
# 或已发布到 npm 的版本 / 本地打包：
dsh plugin --profile <profile> add dsh-display-panel
dsh plugin --profile <profile> add file:/绝对路径/dsh-display-panel-0.3.0.tgz
```

这条命令会做两件事（实测，隔离 DSH_HOME 里跑过）：

* 在 `$DSH_HOME/profiles/<profile>/` 里装依赖（`link:` 就是软链，改代码不用重装）；
* **自动把包名写进 `dsh.profile.bundles`**，不需要再手工编辑 `package.json`。

> **`DSH_HOME` / profile 语义**：DSH 把状态放在 `$DSH_HOME`（桌面版是它自己的目录，
> CLI 默认是 `~/.dsh`）。profile 目录 = `$DSH_HOME/profiles/<profile>/`，
> **插件装在 profile 里，不是装在 DSH 本体里** —— 所以换 profile 要重装一次，
> 多个 profile 可以各装不同版本。不确定当前用的是哪个：看 `dsh web` 启动日志里的 profile 路径。

> ⚠️ **装到哪个 profile 有讲究**：这个插件的宿主半边要 `webServer` 与 `connection`
> 两个服务（由 `@deepseek-ai/dsh-web-app` 提供）。**只装进带 Web UI 的 profile**
> （`web` / `desktop` 这类，花名册里有 `@deepseek-ai/dsh-web-app`）。
> 装进纯 headless / acp 这类没有 webServer 的 profile，宿主半边会一直"等待服务"，
> 而 DSH loader 会把"没激活"当加载失败 —— **整个 profile 起不来**（实测报
> `plugin tree failed to load: 1 entry did not activate … pending (waiting for services: webServer, connection)`）。
> 真装错了就 `dsh plugin --profile <p> remove dsh-display-panel` 退掉。

### 2) 服务：**默认什么都不用做**

打开「显示器」标签即可。服务没起时宿主会**自动拉起**它（默认开，
宿主的 `DSH_VIEW_MANAGED=0` 可关），第一次应答慢约一两秒。

想让服务**常驻**（避免每次冷启动、也避免宿主重启时把显示带走）：

```sh
bash scripts/install-service.sh
```

两条路径自动选，功能一样：

| 情况 | 会做什么 | 怎么验证的 |
|---|---|---|
| **主路径**：`systemctl --user` 可用 | 写 `~/.config/systemd/user/dsh-display-panel-viewer.service` 并 `enable --now`（随登录常驻、开机自启） | 单元渲染 + `daemon-reload` + `enable --now` 的命令序列用桩 `systemctl` 实测；真机上用户总线可用（`systemctl --user is-active` 有应答）也实测过 |
| **兜底**：没有用户总线 | `setsid` + `nohup` 后台化，PID 写 `<home>/viewer.pid`（功能一样，只是不随登录自启） | 实测（`DSH_VIEW_SYSTEMD=0` 跑通：起服务 → `--status` → `--stop` → `uninstall`） |

> **为什么要有兜底**：在容器 / headless / WSL / 某些沙箱 shell 里，`systemctl --user` 会直接报
> `Failed to connect to user scope bus via local transport: $DBUS_SESSION_BUS_ADDRESS and $XDG_RUNTIME_DIR not defined`
> （通常是那两个变量没设，不是机器坏了）。这类环境旧脚本一步都走不了，用户只能看到面板里
> 一句"显示器还没有打开"。现在会自动换路，也可以显式指定：`DSH_VIEW_SYSTEMD=0` 兜底、`=1` 强制 systemd。

> **⚠️ 单元名，以及"别人的单元"**：默认单元名是 **`dsh-display-panel-viewer`**，不是 `dsh-display-viewer`
> —— 后者很可能已经被**同一个用户、另一份 checkout 的服务**占着（本机就是这样：它 active 且占着 8099）。
> 本脚本的策略是**绝不碰别人的单元**：
> * 安装时若同名单元不是本包装的（没有 `X-DSH-Display-Panel=1` 标记，`ExecStart` 也不指向本包）
>   → **拒绝覆盖**并打印对策，确实要覆盖才加 `--force`；
> * `--stop` / `uninstall-service.sh` 只停/删**本包装的、且属于本运行目录的**那个单元。
> 两份服务并存时各占一个端口（以 `<home>/port` 与 `/health` 为准），互不影响；
> `--status` 会把发现的老单元列出来（只报告，不动手）。

管理它：

```sh
bash scripts/install-service.sh --status     # 端口 / PID / 日志位置 / 缺什么依赖 / 接口是否应答
bash scripts/install-service.sh --stop
bash scripts/install-service.sh --restart    # 注意：显示会重建，上面跑的程序需要重新拉起
bash scripts/install-service.sh --foreground # 前台跑，排查用
bash scripts/install-service.sh --force      # 确实要覆盖"不是本包装的"同名单元时才用
bash scripts/uninstall-service.sh            # 停服务 + 移除单元，**保留** <home> 里的会话数据
bash scripts/uninstall-service.sh --purge    # 连 <home> 一起删（令牌 / 会话映射 / 日志）
```

> **从 DSH 的 bash 工具里跑本脚本**：能跑，但别指望后台进程活过这一次工具调用
> （每次调用都是新沙箱）。要么用 systemd 那条路，要么在**你自己的终端**里跑。

### 3) 刷新 Web UI

重启 harness（或按你的部署方式重启 `dsh web`），刷新页面，标签栏里就会出现「**显示器**」。

### 移除

```sh
dsh plugin --profile <profile> remove dsh-display-panel   # 插件本体（实测：同时从 dsh.profile.bundles 去掉）
bash scripts/uninstall-service.sh                         # 停服务 + 移除 systemd 单元，保留 <home> 里的会话数据
bash scripts/uninstall-service.sh --purge                 # 连 <home> 一起删（令牌 / 会话映射 / 日志）
# 还要装依赖也一起删的话：按上面的发行版命令自行卸载 xvfb/xdotool/xclip/imagemagick
```

> 只删**本包装的**单元。早期版本 / 别的 checkout 留下的 `dsh-display-viewer.service`
> 不在本脚本的处理范围内（它可能还在给别的服务供画面）——要清它请用它的安装方提供的方式。

## 用法

### 看和点

打开「显示器」标签就是**本会话**那台显示。画面按容器等比缩放（contain），
鼠标坐标按缩放后的实际画面矩形归一化，所以高分屏 / 缩放都不影响落点。
状态条会显示：后端（x11/wayland/win32/darwin）、分辨率、是否可注入、
Windows/macOS 上还会明确写「**真实桌面**」（那上面动的是你的真键鼠）。

### 让程序跑在那台显示上

**推荐**：让 AI 自己跑 —— 它有这么几个工具（名字都带 `display_panel_` 前缀）：

| 工具 | 作用 |
|---|---|
| `display_panel_status` | 服务在不在跑、端口、后端、显示号、窗口数、是否只读、缺依赖 |
| `display_panel_open` | 确保服务在跑并给出面板地址 |
| `display_panel_run` | **在本会话显示上跑命令**（走服务的 `/exec`，见下） |
| `display_panel_procs` | 列出 / 结束面板在本会话显示上拉起的进程 |
| `display_panel_screenshot` | 抓一帧存成 JPEG 并返回路径 |
| `display_panel_input` | 注入鼠标/键盘事件（0..1 归一化坐标） |
| `display_panel_sessions` | 服务当前的会话概览 |

也可以自己打 HTTP：

```sh
# 拿显示号（经宿主同源接口；面板走的就是这条路）
curl -s 'http://127.0.0.1:<DSH端口>/api/dsh-display-panel/display?session=<sessionId>'

# 在该会话显示上跑程序（服务自己拉起，能拿退出码和输出）
curl -s -X POST 'http://127.0.0.1:<DSH端口>/api/dsh-display-panel/exec?session=<sessionId>' \
     -H 'content-type: application/json' \
     -d '{"argv":["xterm","-e","bash"],"wait":false}'
```

> 上面两条要在浏览器里带 cookie 才过得了宿主的鉴权；**AI 用工具走的是同一条路**。
> 服务侧对应的那两个接口本机实测通过（新服务已落地）：
> `POST /s/docsdev/exec?k=<token>` + `{"argv":["xdotool","getdisplaygeometry"],"wait":true}`
> → `{"ok":true,"code":0,"stdout":"1600 1000\n","pid":1638754,"display":":303"}`
> （`/display`、`/state`、`/procs`、`/kill`、非法 sid 400、无帧 503 也都是实测结果）。
> 宿主代理那两条 curl 属于面板内部路径（同源 + cookie 鉴权），由 `tools/e2e-panel.mjs` 做端到端验证。

#### `DISPLAY=:<号> 程序` 还能用吗？能用，但要知道它的前提

这是本轮专门澄清的一件事，**不要**被"沙箱看不到 X socket"误导：

* `ls /tmp/.X11-unix` 在 DSH 的 bash 沙箱里**是空的** —— 每次调用都是新沙箱，
  `/tmp` 是私有 tmpfs（`bwrap … --unshare-pid --tmpfs /tmp`）；
* **但 `DISPLAY=:<号> 程序` 仍然连得上**：X11 客户端优先走 **abstract unix socket**
  （`@/tmp/.X11-unix/X<n>`），它属于**网络命名空间**，而沙箱默认不隔离网络。
  实测：同一个沙箱里 `DISPLAY=:303 xdotool getdisplaygeometry` → `1600 1000`（那台显示的真实尺寸）；
* **反证**：给同一个沙箱加上 `--unshare-net` 之后，同一条命令立刻变成
  `Failed creating new xdo instance`（显示本身还活着）。

所以：

| 方式 | 今天可用？ | 什么时候会失效 | 适合 |
|---|---|---|---|
| `DISPLAY=:<号> 程序` | ✅ 可用（实测） | 沙箱一旦收紧到隔离网络（`--unshare-net`）就失效；也依赖你知道显示号 | 手工排查、一次性跑个东西 |
| `POST /s/<sid>/exec`（服务自己拉起） | ✅ **推荐**（实测） | —— | AI 与脚本：**跨沙箱稳定**，还能列进程（`/procs`）、结束进程（`/kill`）、`wait:true` 拿退出码与 stdout/stderr |

两者不是"新旧替代"关系，而是"能用"与"更稳、更可诊断"的差别。文档以前只写了前一种，
现在两种都写清楚。

### 环境变量

| 变量 | 默认 | 谁读 | 作用 |
|---|---|---|---|
| `DSH_DISPLAY_HOME` | `~/.cache/dsh-display` | 服务 / 宿主 / 脚本 | 令牌、端口、PID、日志、会话映射都在这 |
| `DSH_VIEW_PORT` | `8099` | 服务 / 宿主 / 脚本 | 起始端口（被占自动顺延，最多 12 个） |
| `DSH_VIEW_SIZE` | `1600x1000` | 服务 | 每会话显示的分辨率 |
| `DSH_VIEW_BACKEND` | 按平台自动 | 服务 | 强制 `x11` / `wayland` / `win32` / `darwin` |
| `DSH_VIEW_INPUT` | 关 | 服务 | win32/darwin 上开启注入（动的是**真实键鼠**） |
| `DSH_VIEW_IDLE_MINUTES` | `30` | 服务 | 空闲多久回收会话与 Xvfb；`0` = 不回收 |
| `DSH_VIEW_LOG` | `<home>/viewer.log` | 服务 / 宿主 | 日志文件 |
| `DSH_VIEW_MANAGED` | 开 | **宿主** | 设 `0` 关掉"自动拉起服务" |
| `DSH_VIEW_PYTHON` | 自动找 `python3`→`python` | **宿主** | 宿主拉起服务用的解释器（找不到会在 `/info` 的 `missing[]` 里明说） |
| `DSH_VIEW_SYSTEMD` | `auto` | **脚本** | `1` 强制用 systemd 单元、`0` 强制用后台兜底 |
| `DSH_VIEW_UNIT_NAME` | `dsh-display-panel-viewer` | 脚本 | 单元名（同机多实例时改它） |
| `DSH_VIEW_UNIT_DIR` | `~/.config/systemd/user` | 脚本 | 单元目录（测试时可指到临时目录） |
| `DSH_VIEW_FORCE` | 关 | 脚本 | `1` = 允许覆盖"不是本包装的"同名单元（等价 `--force`） |
| `PYTHON` | 自动 | 脚本 | 脚本与服务用的解释器 |

## HTTP 接口

### 宿主半边（**同源**，面板只走这些）

前缀 `/api/dsh-display-panel`；全部经宿主自己的请求守卫（cookie 鉴权），失败返回 JSON `{ok:false,error,hint}`。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/info` | 服务在不在跑、端口、后端、分辨率、版本、`missing[]`、日志路径 |
| POST | `/info` | 同上但**强制重新探测**；`{"action":"restart"}` 可重启宿主管理的服务 |
| GET | `/frame?session=<sid>&t=<ts>` | 一帧 JPEG（`image/jpeg`）—— **面板画面的唯一来源** |
| GET | `/state?session=<sid>` | 透传服务 `/state`，并补 `service` 字段 |
| GET | `/display?session=<sid>` | 显示号 / 后端 / 分辨率（脚本、AI 用） |
| POST | `/input?session=<sid>` | 注入输入事件（body 原样转发） |
| POST | `/exec?session=<sid>` | 在会话显示上跑程序 |
| GET | `/procs?session=<sid>` · POST `/kill?session=<sid>` | 列出 / 结束由面板拉起的进程 |
| POST | `/service` | `{"action":"start"\|"stop"\|"restart"\|"status"}` |
| GET | `/events?session=<sid>` | 可选：SSE 状态推送（没有就由客户端轮询） |

### 显示器服务（`http://127.0.0.1:<port>`，仅本机）

这些口**面板不走**（面板走上面的同源代理）。给脚本/AI 直接用时必须带 `?k=<token>`
（令牌是给"同机其它用户"设的闸：服务监听 127.0.0.1，别的用户也能连上）。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health?k=` | 探测专用：版本、后端、端口、PID、会话数、缺依赖。**无副作用** |
| GET | `/?k=` | 会话索引页（HTML，人用兜底） |
| GET | `/s/<sid>/` | 该会话的独立页面（面板不用它） |
| GET | `/s/<sid>/snapshot?k=&t=` | 单帧 JPEG；没有帧就 503（不阻塞） |
| GET | `/s/<sid>/stream?k=` | MJPEG 流（备用） |
| GET | `/s/<sid>/state?k=` | 窗口数、是否空闲、光标位置、是否真实桌面、缺依赖 |
| GET | `/s/<sid>/display?k=` | 显示号 / 后端 / 分辨率 |
| POST | `/s/<sid>/input?k=` | `{t:'click'\|'down'\|'up'\|'move'\|'wheel'\|'text'\|'key',…}`，坐标 0..1 |
| POST | `/s/<sid>/exec?k=` | `{"argv":[…],"cwd":"/tmp","wait":false}` → `{"ok":true,"pid":…,"display":":148"}` |
| GET | `/s/<sid>/procs?k=` · POST `/s/<sid>/kill?k=` | 列进程 / 结束进程 |

会话号（`sid`）只允许 `^[A-Za-z0-9._-]{1,64}$`，别的返回 400。

## 故障排查

| 现象 | 先看什么 | 怎么办 |
|---|---|---|
| 面板停在「需要先打开显示器」/ 一直转圈 | 面板状态条里的服务状态与日志路径；`bash scripts/install-service.sh --status` | 宿主会自动拉起，稍等一两秒；仍不行就看日志尾部（`--status` 会打印路径），常见是 `python3` 找不到或端口全被占 |
| `--status` 说"没有服务在应答" | `<home>/port`、`<home>/viewer.log` | `bash scripts/install-service.sh`；注意 `DSH_DISPLAY_HOME` 要跟宿主一致，否则两边看的是两个目录 |
| 面板空白 / 报 503 | `--status` 里的 `/health` 与 `missing[]` | 服务在但抓不到帧：多数是缺 `import`（ImageMagick）；Windows/macOS 上还可能是**屏幕录制权限**没给 |
| 画面全黑，但状态是 streaming | `/state` 里的 `windows` 数 | 该显示上还没有窗口 —— 让 AI `display_panel_run` 跑一个（例如 `xterm`） |
| 能看不能点 | `/state` 里的 `input` 字段 | Linux：装 `xdotool`；Windows/macOS：注入默认关闭，要显式设 `DSH_VIEW_INPUT=1`（动的是真实键鼠，想清楚再开） |
| 中文打不进去，英文可以 | `/health` 的 `missing[]` | 缺 `xclip`（中文走剪贴板 + Ctrl+V） |
| AI 说它没法在那台显示上跑程序 | 它的工具列表里有没有 `display_panel_*` | 面板要能跑起来（服务在）；工具在老宿主上会自动退化成"只有面板"；手工可用 `DISPLAY=:<号> 程序`（见上节）或 `/s/<sid>/exec` |
| 面板上写着「真实桌面」 | —— | Windows/macOS 无法为每会话开一块独立屏，抓的是**你的真实桌面**：画面里是你在干的事，开注入就等于把键鼠交出去 |
| 端口冲突 / 起了两份服务 | `<home>/port` 与服务日志 | 服务会自动顺延端口；同机多实例请各用各的 `DSH_DISPLAY_HOME`（脚本的 `--status` 会显示它认的是哪一个） |
| `systemctl --user` 报 `Failed to connect to user scope bus` | `echo $DBUS_SESSION_BUS_ADDRESS $XDG_RUNTIME_DIR` | 这是环境没有用户总线（容器 / headless / WSL / 沙箱 shell），不是机器坏了：脚本会自动改用兜底；也可以显式 `DSH_VIEW_SYSTEMD=0 bash scripts/install-service.sh`（功能一样，不随登录自启） |
| 装完发现面板连到了**别的服务** | `bash scripts/install-service.sh --status` 的"旧单元"提示；`<home>/port` | 同机可能有两份 viewer（例如旧的 `dsh-display-viewer` 占着 8099）。本脚本不会覆盖/停止别人的单元；建议各用各的 `DSH_DISPLAY_HOME`，或统一用本包的单元 |
| 改了代码没生效 | `dsh plugin --profile <p> add link:` 装的是软链 | 宿主半边改动重启 DSH 生效；面板（client）改动刷新页面；服务改动 `bash scripts/install-service.sh --restart` |

一条命令看全部现场：

```sh
bash scripts/install-service.sh --status   # 端口 / PID / 日志 / 缺依赖 / 是否有人在应答
node scripts/hint.js                       # 更轻的一份：只看运行目录 + 令牌 + /health（不依赖 bash）
```

## 自测

```sh
python3 service/selfcheck.py     # 服务内部不变式（后端解析、键名映射、页面模板、依赖自检）
                                 # 缺 Xvfb/xdotool/xclip/imagemagick 时相关项 SKIP 而不是 FAIL —— CI 无 GUI 也能跑
python3 tools/selftest.py        # 端到端自测：临时 HOME + 随机高端口，自己起服务、自己收尸
                                 # 覆盖 /health 无副作用、sid 校验、注入、顺序、隔离…
```

`tools/selftest.py` 的输出长这样（数字随版本/环境不同；**跳过不算失败**）：

```
== 63 通过 / 0 失败 / 1 跳过（共 64 项）==
  跳过的：<为什么跳过，例如这台机器没有 xdotool>
结果：PASS（退出码 0）
```

失败时会把**现场数据**一起打出来（哪一条、期望什么、实际什么），例如
`失败：A 会话的注入不落到 B 会话（画面互不污染） — B 日志行数 1 → 2`，
而不是只说一句 FAIL。

退出码 **0 = 没有 FAIL（跳过不算失败）**，非 0 = 有 FAIL。常用参数：
`--no-dynamic`（只跑静态检查）、`--port N`、`--home DIR`、`--keep`（保留临时目录）、`-v`。

浏览器里的真·端到端（需要先有一个在跑的 DSH Web UI + Playwright + Brave）：

```sh
# --url 用 `dsh web` 启动日志里那行带 ?token= 的地址
node tools/e2e-panel.mjs --url 'http://127.0.0.1:<DSH端口>/?token=<…>' --shot-dir /tmp/shots
```

它自己**不**起 DSH 实例；缺 Playwright/Brave 时会明文报 `SKIP(缺依赖)` 并**退出码 3**
（不是 0 —— 免得 CI 里静默变绿），加 `--allow-skip` 才退 0。

> 本轮状态（如实）：`tools/selftest.py` 与 `tools/e2e-panel.mjs` 都已随包落地，本机的服务级
> 自测跑出过 `62 通过 / 1 失败 / 1 跳过`，唯一失败是
> 「A 会话的注入不落到 B 会话（画面互不污染） — B 日志行数 1 → 2」，已交给 service-dev/verify-dev 定位；
> **最终结论以 `verify/REPORT.md` 为准**。CI 只跑不需要 GUI 的部分，见
> [`.github/workflows/linux.yml`](.github/workflows/linux.yml) 与 [`macos.yml`](.github/workflows/macos.yml)。

## 实现要点（踩过的坑都在这）

* **底座选 Xvfb 而不是 Wayland**：Wayland 下每会话一台合成器**不可行** —— seatd 的 seat 是
  VT-bound、同一时刻只允许一个客户端，logind 的会话又被用户桌面占着；而 X11 没有 seat
  概念，能同时跑任意多个 display，注入走 XTest **不需要任何权限**。
* **画面用画布主动拉帧**，不是把 MJPEG 塞进 `<img>`：`<img>` 上的 MJPEG 一旦断开
  （服务重启）**不会重连**，画面会永远黑着。
* **中文必须走剪贴板**：`xdotool type` 靠临时映射 keysym 打字符，CJK 打不进去；
  改成 `xclip` 写剪贴板 + `Ctrl+V`，并且 `xclip` 要带 **`-l 20`**（它默认只服务一次
  选区请求就退出，而 Qt 读剪贴板要分几次请求 → 否则"Ctrl+V 什么都没粘上"）。
* **键名两套不一样**：页面事件是 DOM 名（`Backspace`/`Enter`/`ArrowUp`），
  X11 是 keysym 名（`BackSpace`/`Return`/`Up`），宿主侧做了换算。
* **输入必须串行**：早先每个事件起一个线程 → 打字顺序会被打乱（`hello` 变成 `hlelo`）；
  现在同一会话的事件按到达顺序排队执行。
* **服务退出要自己收拾 Xvfb**：否则 systemd 日志里会出现
  `Unit process … remains running after unit stopped`。
* **令牌只在宿主侧**：面板拿不到令牌，也就没法把它写进 URL / 历史 / Referer。

## 打包与发布

`npm pack --dry-run` 会打这些（本机实测，共 25 个文件 / 解包约 460 kB；
**没有** `.verify/`、`__pycache__`、`*.pyc`、`node_modules/` 混进去）：

```
LICENSE  README.md  cordis.patch.yml  package.json
docs/{ANALYSIS.md,CONTRACT.md,PACKAGING.md}
lib/{index.js,client.js,tools.js}
scripts/{install-service.sh,uninstall-service.sh,service-common.sh,hint.js,publish-npm.sh}
service/{dsh-display-viewer.py,selfcheck.py,dsh-display-viewer.service,windows/*}
tools/{selftest.py,e2e-panel.mjs,xtarget.py}
```

取舍理由见 [`docs/PACKAGING.md`](docs/PACKAGING.md)。发版：

```sh
bash scripts/publish-npm.sh      # 会先 dry-run 列内容、要你确认，再 npm publish --access public
```

## 许可

MIT
