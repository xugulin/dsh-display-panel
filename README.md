# dsh-display-panel

[![npm](https://img.shields.io/npm/v/dsh-display-panel.svg)](https://www.npmjs.com/package/dsh-display-panel)

给 **DeepSeek Harness Web UI** 加一个「**显示器**」标签：在「对话 / 轨迹 / 浏览器」旁边，
实时看到 AI 在**它自己的虚拟显示**上做了什么 —— 而且可以**直接在上面点击、打字**
（含中文、退格、回车、Ctrl+V），登录、扫码、验证码都能在这个标签里完成。

> A display tab for the DeepSeek Harness Web UI. Watch — and drive — the AI's own
> headless test display: per-session, fully isolated, with mouse and keyboard injected back.

## 它解决什么问题

AI 做 GUI 相关的活（跑桌面程序、验证界面、登录某个网站）时，人通常看不到它在干什么。
这个插件把 AI 用的那台虚拟显示**搬进 Web UI**：

* **看**：实时画面（画布主动拉帧，约 7~8 fps，断了自动重连）；
* **操作**：点击、滚轮、键盘、中文输入法、退格/回车、Ctrl 组合键，全部注入回那台显示；
* **隔离**：**每个会话一台独立显示**，互不可见、互不污染（早先共用一台时，别的项目的
  登录窗口会混进你的画面）；
* **不碰你的桌面**：显示跑在独立的 headless X 服务器上，不占用你的 VT、不动你的输入设备。

## Windows 使用

Windows 上**没有可多开的 headless 显示** ✗ —— 所以 `win32` 后端抓的是**本机真实桌面**
（所有会话共用那一块屏 ✓），注入默认**关闭** ✓（只读观看 ✓）。

| 项 | 说明 |
|---|---|
| 抓帧 | GDI `BitBlt` + GDI+ 编码 JPEG（**纯 ctypes** ✓ 不依赖 Pillow / ffmpeg ✓ 实测约 34ms/帧 @1920×1080 ✓） |
| 注入 | `SendInput` ✓ 但**默认关闭** ✗ —— 因为它注入的是**真实鼠标键盘** ✓。要开启：设 `DSH_VIEW_INPUT=1` ✓ |
| 启动服务 | 双击 `service/windows/启动显示器服务.bat` ✓（无窗口版：`启动显示器服务-Silent.vbs` ✓；停止：`停止显示器服务.bat` ✓ 按 **8099 端口**精确停止 ✓） |
| 开机自启 | 把 `service/windows/开机自启用-DSH显示器服务.vbs` 放进「启动」文件夹 ✓ |

> Windows 部分的修复由社区用户提供（[修复报告](https://github.com/xugulin/dsh-display-panel) 随包附上：
> 服务起不来 = `signal.SIGHUP` 在 Windows 不存在；没有帧源 = 抓帧原本只有 X11/Wayland 两条路）。
> Linux 的两条路一行未动 ✓ —— 已用 12 项自测回归确认 ✓。

## 依赖

Linux（X11 底座）+ 以下命令，缺一个都跑不起来：

| 命令 | 用途 | Arch 安装 |
|---|---|---|
| `Xvfb` | 每会话一台虚拟显示 | `xorg-server-xvfb` |
| `xdotool` | 鼠标/键盘注入（XTest，**不需要权限**） | `xdotool` |
| `import` | 抓画面（ImageMagick） | `imagemagick` |
| `xclip` | **中文输入**（走剪贴板 + Ctrl+V） | `xclip` |

## 安装

### 0) 从 npm 装（最省事）

```sh
npm i dsh-display-panel
```

装完仍需按下面第 1 步启动**显示器服务**，再把它登记进 DSH profile 的 `dsh.profile.bundles`。


### 1) 显示器服务（必需）

```sh
bash scripts/install-service.sh
```

它会装好并启动一个 **systemd 用户服务** `dsh-display-viewer.service`，监听
`http://127.0.0.1:8099/`。

> 为什么必须是独立服务：harness 是 systemd 服务，**重启它会按 cgroup 连带杀掉同一
> cgroup 里的进程**。若把显示器服务挂在 harness 的 shell 下，每次重启 harness 面板
> 就会变成"显示器还没有打开"（作者实测踩过）。

管理：

```sh
systemctl --user status  dsh-display-viewer
systemctl --user restart dsh-display-viewer     # 注意：显示会重建，上面的程序需重新拉起
journalctl --user -u dsh-display-viewer -f      # 注入失败也打在这里
```

### 2) 插件本体

把包放进 DSH profile，并登记进 `dsh.profile.bundles`：

```sh
cd ~/.dsh/profiles/web
npm i /path/to/dsh-display-panel        # 或用 pnpm / 直接放 node_modules
```

然后编辑 `~/.dsh/profiles/web/package.json`，把 `"dsh-display-panel"` 加进
`dsh.profile.bundles` 数组（和 `dsh-browser-panel` 并列），重启 harness：

```sh
systemctl --user restart dsh-web.service
```

刷新 Web UI，标签栏里就会出现「**显示器**」。

## 用法

* 打开「显示器」标签即可看到**本会话**那台显示的实时画面，直接在上面操作；
* 想让 AI 把程序跑在那台显示上，告诉它即可；也可以自己来：

```sh
# 查这个会话的显示号
curl http://127.0.0.1:8099/s/<sessionId>/display
# 在该会话的显示上跑程序
DISPLAY=:<号> QT_QPA_PLATFORM=xcb 你的程序
```

> `QT_QPA_PLATFORM=xcb` 要显式指定：环境里若残留 `WAYLAND_DISPLAY`，Qt 会去加载
> wayland 插件并失败（作者实测踩过）。

### HTTP 接口（都在 `http://127.0.0.1:8099`）

| 路径 | 说明 |
|---|---|
| `/` | 会话索引页 |
| `/s/<sessionId>/` | 该会话的显示器页面（画面 + 输入捕获） |
| `/s/<sessionId>/snapshot` | 单帧 JPEG（页面用它拉帧） |
| `/s/<sessionId>/stream` | MJPEG 流（备用） |
| `/s/<sessionId>/state` | `{display, windows, idle}` —— 判断"空闲还是坏了" |
| `/s/<sessionId>/display` | `{display, backend, size}` |
| `POST /s/<sessionId>/input` | `{t:'click'|'move'|'wheel'|'text'|'key', ...}` |

坐标一律用 **0..1 归一化值**（页面缩放、高分屏都不用管）。

## 实现要点（踩过的坑都在这）

* **底座选 Xvfb 而不是 Wayland**：Wayland 下每会话一台合成器**不可行** —— seatd 的 seat 是
  VT-bound、同一时刻只允许一个客户端，logind 的会话又被用户桌面占着；而 X11 没有 seat
  概念，可以同时跑任意多个 display，注入走 XTest **不需要任何权限**。
* **画面用画布主动拉帧**，不是把 MJPEG 塞进 `<img>`：`<img>` 上的 MJPEG 一旦断开
  （服务重启）**不会重连**，画面会永远黑着。
* **中文必须走剪贴板**：`xdotool type` 靠临时映射 keysym 打字符，CJK 打不进去；
  改成 `xclip` 写剪贴板 + `Ctrl+V`，并且 `xclip` 要带 **`-l 20`**（它默认只服务一次
  选区请求就退出，而 Qt 读剪贴板要分几次请求 → 否则"Ctrl+V 什么都没粘上"）。
* **键名两套不一样**：页面事件是 DOM 名（`Backspace`/`Enter`/`ArrowUp`），
  X11 是 keysym 名（`BackSpace`/`Return`/`Up`），宿主侧做了换算。
* **路由要先剥查询串**：页面为了防缓存会带 `?t=…`，否则所有带参数的请求都 404。
* **服务退出要自己收拾 Xvfb**：否则 systemd 日志里会出现
  `Unit process … remains running after unit stopped`。

## 自测

两个测试工具都在**控制台仓库**（`dsh-console`）的 `tools/` 下：

```sh
python3 service/dsh-display-viewer.py &     # 或走 systemd
python3 tools/dsh-display-selftest.py       # 自动化：12 项 —— 服务/隔离/画面/鼠标/键盘/中文/退格/回车…
python3 tools/dsh-display-testcard.py       # 人眼自检卡：8 条色条 / 32 级灰阶 / 时钟 + 秒针
                                            #   --list 看有哪些会话显示、--browser 换 Chromium 渲染
```

自测卡验的是"画面本身对不对"（色通道有没有串、台阶有没有被抹平、是不是实时画面），
自动化自测验的是"功能通不通" —— 一个给人看，一个给脚本看。

## 许可

MIT
