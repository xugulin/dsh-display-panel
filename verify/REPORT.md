# dsh-display-panel 独立验证报告

**验证者**：verify-dev（独立于 T1/T2/T3 的改动方）
**验证对象**（最终回归时的文件指纹，跑 selftest 会自动打印）：

```
git HEAD a680c04（0.3.0）—— 除 tools/ 与 verify/ 外均与提交一致
service/dsh-display-viewer.py  sha256:b0bfc969ee701f7e
service/selfcheck.py           sha256:a422b99a8a07b421
lib/client.js                  sha256:66644e53cd724fb0   ← 含 §6.2 的修复
lib/index.js                   sha256:1458b1b86f7fb4ff
tools/xtarget.py               sha256:92652c8331e63084
tools/selftest.py              sha256:3b1191b22745ebc8
tools/e2e-panel.mjs            sha256:2b925438752b7eaa
```

**一句话结论**：契约 §1/§2/§3 的主线在**真服务 + 真注入 + 真浏览器**上都成立（selftest 63 项通过 / 0 失败 / 1 条解释性跳过；e2e **18 项通过 / 0 失败**，其中含"强制 503 窗口后必须自动恢复"的确定性回归）；win32/darwin/wayland 三个后端与 HTTPS 部署未验证（本机没有对应环境）。第一轮观察到的"面板 503 后永久空白"已由 client-dev 定位为客户端缺陷并修复，§6.2 给出**独立复验**。

---

## 0. 复现方式（每条结论都能照着跑）

```bash
cd <repo>

# ① 一键自检：静态契约 + 真服务 + 真注入（零副作用，临时 HOME + 随机高位端口）
python3 tools/selftest.py                 # 退出码 0=无 FAIL；--keep 保留临时目录；--no-dynamic 只跑静态

# ② 真浏览器端到端（要一个**隔离的** DSH 实例 + 隔离的 DSH_DISPLAY_HOME）
node tools/e2e-panel.mjs --url "http://127.0.0.1:<port>/?token=<token>" --shot-dir verify/shots --with-target
#   --frame-outage MS 会在打开面板时**强制 /frame 回 503**（默认 2500ms），用来回归
#   "帧循环静默死掉"（§6.2 那个缺陷：窗口期内必须仍在重试，窗口结束后必须自动恢复）

# ③ 靶程序单独试（不需要 gcc，纯标准库 + ctypes）
Xvfb :190 -screen 0 800x600x24 & DISPLAY=:190 python3 tools/xtarget.py /tmp/xt.log --label T
DISPLAY=:190 xdotool mousemove 400 300 click 1 ; DISPLAY=:190 xdotool type "hello"
cat /tmp/xt.log      # → BUTTON x=400 y=300 button=1 / KEY keysym=h text="h" / LINE text="hello"
```

---

## 1. 三个工具的实际状态（都真跑过，不是"写完了"）

| 工具 | 状态 | 实测证据 |
|---|---|---|
| `tools/xtarget.py` | ✅ 可用 | Xvfb + xdotool 实跑：`BUTTON x=400 y=300 button=1`、`KEY keysym=h text="h"`、`KEY keysym=BackSpace text="\b"`、`KEY keysym=Return text="\n"`、`PASTE text="中文测试 ok"`（Ctrl+V 走 XConvertSelection 读 CLIPBOARD/UTF8_STRING）、`BUTTON ... button=5`（滚轮） |
| `tools/selftest.py` | ✅ 63 通过 / 0 失败 / 1 跳过（退出码 0） | 见 §2 输出（最终版 a680c04 上复跑一致） |
| `tools/e2e-panel.mjs` | ✅ 18 通过 / 0 失败 | 见 §5 输出（含强制 503 窗口回归） |

`xtarget.py` 的两个实现要点（踩过）：
1. **必须 `XInternAtom(..., only_if_exists=False)`**：靶程序启动时剪贴板属主（xclip）通常还没起，只查不建会拿到 0，Ctrl+V 永远不请求 → `PASTE` 一直不出现。
2. **不要 `XLoadQueryFont` + `XSetFont`**：`XSetFont` 要的是 `XFontStruct.fid` 而不是结构体指针，传错就是 `BadFont`，而 Xlib 默认**直接终止进程**；现在装了错误处理器（写一行 `XERROR` 继续跑）。

---

## 2. 自检汇总（原始输出）

```
$ python3 tools/selftest.py
  service/dsh-display-viewer.py  sha256:b0bfc969ee701f7e  mtime:1790269609
  service/selfcheck.py           sha256:a422b99a8a07b421  mtime:1790269617
  lib/client.js                  sha256:66644e53cd724fb0  mtime:1790269040
  lib/index.js                   sha256:1458b1b86f7fb4ff  mtime:1790268955
  tools/xtarget.py               sha256:92652c8331e63084  mtime:1790266595
  git HEAD a680c04（工作区有未提交改动）
---- 静态检查 ----
PASS  py_compile dsh-display-viewer.py / selfcheck.py / xtarget.py / selftest.py
PASS  service/selfcheck.py 全部通过  — == 8/8 项通过 ==
PASS  node --check client.js / index.js
PASS  client.js 代码里不含 '127.0.0.1' / '?k=' / 'iframe' / "h('iframe'" / 'http://'
PASS  client.js 代码里含 'canvas' / '/api/dsh-display-panel' / 'compositionstart' / 'wheel' / 'mousedown'
PASS  viewer 有 /health、/exec、/procs、/kill 接口
PASS  viewer 有 sid 字符集校验（^[A-Za-z0-9._-]{1,64}$）
PASS  package.json 版本与契约版本一致  — package.json=0.3.0 契约=0.3.0
---- 宿主半边功能探针 ----
PASS  宿主半边注册了 /api/dsh-display-panel/info 路由  — ['/info','/frame','/state','/display','/input','/exec','/procs','/kill','/service','/events']
PASS  **/info 响应体里没有令牌**（契约 §2：令牌只在宿主侧）
PASS  /info 响应带 ok:true 且含 service.port
PASS  宿主把鉴权拒绝透传给浏览器（401）
---- 动态检查（真起服务 + 真注入）----
PASS  /health 返回契约 JSON（§1.1）  — 缺 []
PASS  /health 与索引页不创建会话（契约 §4.3）  — sessions: 0 → 0
PASS  无令牌被拒（403，契约 §1）      PASS  非法 sid 一律 400（契约 §1.2）
PASS  /exec 存在且支持 wait:true（契约 §1.2）  — {"ok":true,"code":0,"stdout":"",...}
PASS  click 落点 (0.5,0.5) → (400,300)±2px  — BUTTON x=400 y=300 button=1
PASS  click 落点 (0.25,0.75) → (200,450)±2px / (0.9,0.1) → (720,60)±2px
PASS  鼠标键 b=1 → X button 1 / b=2 → X button 3 / b=3 → X button 2
PASS  注入 ASCII 文本 "hello"          PASS  注入中文文本（剪贴板路径）  — 'hello中文显示器'
PASS  Backspace 到达（keysym=BackSpace）  PASS  Enter 到达（keysym=Return）
PASS  down→move→up 都到达且顺序正确  — MOTION BUTTON MOTION MOTION BUTTONUP
PASS  滚轮 dy=+120 → button 5（下滚）  PASS  滚轮 dy=-120 → button 4（上滚）
PASS  20 个连续字符顺序不乱（契约 §1.3 串行执行）  — 缓冲区尾部 'o中文显示\nabcdefghijklmnopqrst'
PASS  跨进程 DISPLAY=:300 可用：另一个进程直接连也能画窗口/收事件  — READY window=... display=":300"
PASS  /procs 能看到 /exec 拉起的靶程序        PASS  /state 的 windows>0
PASS  两个会话拿到不同显示号  — :296 vs :342
PASS  A 会话的注入不落到 B 会话  — B 的可注入事件行数 0 → 0
PASS  两会话 snapshot 都是 JPEG 且内容不同  — A 200 5504B / B 200 2786B
PASS  snapshot 不阻塞（新会话首帧 < 2.5s）  — A 用时 0.02s
PASS  未知输入类型不 5xx / 非法 JSON body 不 5xx  — 400 {"ok":false,...}
SKIP  零副作用：没有碰用户真实 ~/.cache/dsh-display
      — 检测到外部服务正在使用该 home（pid=1525018 …/DeepSeekHarness控制台/tools/dsh-display-viewer.py）—— 归因不明，跳过
== 63 通过 / 0 失败 / 1 跳过（共 64 项）==
结果：PASS（退出码 0）
```

**为什么那条是 SKIP 而不是 PASS**：用户自己的显示器服务（systemd 用户单元，8099，`HOME=~/.cache/dsh-display`）每几秒就往那个目录写一次 `xvfb.log`，任何"前后快照完全一致"的断言都会随机变红。
自检改成**能归因**的判定：① 读 `~/.cache/dsh-display/port`，用 `/proc/net/tcp` + `/proc/*/fd` 找到监听进程，若不是本次测试自己起的 → 打印属主并 SKIP；② 否则只断言三件"我们绝不该造成的变化"（没出现本次测试 sid 的 `sessions/<sid>/`、`port` 没被改成测试端口、`token` 没被改写）。**没有放宽**：真出这三种情况仍是 FAIL。

---

## 3. 已验证成立（逐条对应契约）

| 契约 | 验证方式 | 结果 |
|---|---|---|
| §1.1 `/health` 契约 JSON 且**无副作用** | 连打两次 + 中间打索引页，比对 `sessions` | 0 → 0；字段齐全 `ok/service/version/backend/size/input/port/pid/sessions/missing` |
| §1.2 sid 字符集校验 → 400 | `/s/../../etc/passwd/display`、`/s/..%2f..%2fetc/display`、65 字符 sid、`/s/a%20b/display` | 全部 **400**（旧实现是路径穿越） |
| §1.2 `/exec`（同命名空间拉起） | `wait:true` 拿 `code/stdout`；`wait:false` 拿 `pid/display` | ok；且 `/procs` 看得到、`/state.windows>0` |
| §1.2 snapshot 不阻塞 | 新会话首次 `/snapshot` 计时 | 0.02s（200 JPEG 2721B），不是旧实现的阻塞 5s |
| §1.3 归一化坐标落点 | 3 个坐标 × 断言 `x/y` 误差 ≤2px | 全中 |
| §1.3 鼠标键映射 | b=1/2/3 → X button 1/3/2 | 全中（`{1:"1",2:"3",3:"2"}`） |
| §1.3 中文 text | 服务走剪贴板 + Ctrl+V，靶程序自己实现粘贴 | `LINE text="hello中文显示器"` |
| §1.3 顺序保证 | 20 个**独立** HTTP 请求连续发 | 缓冲区以 `abcdefghijklmnopqrst` 结尾（旧实现每事件一线程会乱序） |
| §1.3 滚轮方向 | dy=±120 → X button 5/4 | 方向正确 |
| §1.2 会话隔离 | 两会话两台 Xvfb；只往 A 注入 | 显示号不同；B 的可注入事件行数 0 → 0；两帧 JPEG 内容不同 |
| §1 令牌 | 不带 `k=` | 403 `{"ok":false,...}` |
| §2 令牌只在宿主侧 | Node 里 stub `ctx` 真跑 `lib/index.js`，调用它注册的 `/info` handler | 响应体**不含**令牌串；`requestRejection→401` 时返回 401 |
| §3 客户端只同源 | 代码（剥离注释后）关键词检查 | 无 `127.0.0.1` / `?k=` / `iframe` / `http://` |
| §3 canvas 渲染 | 真浏览器 | `canvas=1 iframe=0`；`/frame` 200 + `image/jpeg` + FFD8 |

### 3.1 案例：「`ls` 看不到 ≠ 连不上」（值得写进文档的一条环境事实）

沙箱里 `ls /tmp/.X11-unix` 什么都看不到，但 **X11 的 abstract socket 走网络命名空间、bwrap 默认不隔离网络** —— 所以另一个进程/另一个沙箱 `DISPLAY=:N` 照样连得上。

- 证据 A（自检里现在是**正向断言**）：`PASS 跨进程 DISPLAY=:300 可用：另一个进程直接连也能画窗口/收事件 — READY window=2097153 display=":300"`。靶程序是 selftest **自己 fork 的另一个进程**（不走 `/exec`），照样开窗、收点击。
- 证据 B：从**另一个 bash 调用**里跑 `DISPLAY=:205 xdotool getdisplaygeometry` → `1024 640`（exit 0），`DISPLAY=:205 import -window root JPEG:-` → 2721 字节 JPEG（exit 0），而同一个壳里 `ls /tmp/.X11-unix/` 里根本没有 `X205`。
- **推论（写进文档/实现）**：判断"显示可不可用"**绝不能**用 `ls /tmp/.X11-unix` 或 socket 文件存在性，只能用"真连一次"（服务里的 `_alive()` 用 `xdotool getdisplaygeometry` 是对的）。
- 反向风险：也正因为 abstract socket 跨沙箱可见，**显示号可能与别的命名空间遗留的 Xvfb 撞号**（见 §6.3）。

---

## 4. 未验证 / 无法验证（如实列出，不要当成"已通过"）

1. **win32 后端**：没有 Windows 机器；`service/selfcheck.py` 里那几项（INPUT 结构 40 字节、SendInput 空操作）本机跑不到。
2. **darwin 后端**：没有 Mac；契约自己也标注"未真机验证"。
3. **wayland 后端**：本机没有 `sway`/`grim`/`wtype`/seatd，动态部分只覆盖 x11。
4. **HTTPS / 反向代理下的同源代理**：本机是 `http://127.0.0.1`，没有验证"HTTPS 部署时不再有混合内容"这一条（架构上已消除 iframe 与 127.0.0.1，静态检查覆盖了代码层面）。
5. **真实输入法（IME）中文合成**：面板的 `compositionstart/compositionend` 只做了静态关键词检查；端到端注入用的是 `/input {t:text}` 直投，没有模拟真实的拼音输入法上屏。
6. **`scripts/install-service.sh` 的非 systemd 兜底**：没有执行安装脚本（会动用户 systemd/自启），未验证。
7. **用户真实环境（8099 + 真实 HOME）**：出于隔离纪律**刻意未触碰**，所以"用户那台真实服务在新版宿主下是否正常"未验证。

---

## 5. 端到端（真浏览器）结果

```
$ node tools/e2e-panel.mjs --url "http://127.0.0.1:8601/?token=…" --shot-dir verify/shots --with-target
PASS  打开 DSH Web UI / 首次配置弹窗已跳过 / 进入新建会话
PASS  会话视图出现「显示器」标签  — 标签=["对话","轨迹","显示器"]
PASS  点击「显示器」标签
PASS  强制 503 窗口（2500ms）后帧循环自动恢复（契约 §3 退避重连）  — **窗口期内仍然重试了 8 次；窗口结束后放行 55 次**
PASS  面板渲染出 canvas（契约 §3：不再用 iframe）  — canvas=1 iframe=0
PASS  同源 /frame 返回真 JPEG（200 + image/jpeg + FFD8）  — 200-JPEG=103/112 首帧=2721B 魔数ok=true
PASS  会话 id 从 /frame?session= 取到  — session-bb37b32e-…（**不是**状态条上截断的 session-bb37）
PASS  canvas 上画出了画面（非全黑）  — nonBlack=140/861520
INFO  画面几何  — canvas=1210x712 显示=1024x640 contain=1139x712 offset=(35,0) 点击=(657,431)
PASS  点画面 → POST /input 且坐标 0..1 归一化  — [{"t":"move","x":0.2994,"y":0.4491},{"t":"down",…},{"t":"up",…}] 状态=[200,200,200]
PASS  点击几何换算正确（点画面 30%,45% → 客户端算出 ≈0.30,0.45）  — 实际=(0.2994,0.4491)
PASS  在面板上打字 → 有 input 事件带上输入的字符  — [{"t":"key","k":"h"},…,{"t":"key","k":"3"}]
PASS  宿主 /exec 在会话显示上拉起靶程序  — {"ok":true,"pid":1755861,"display":":113"}
PASS  该显示上出现窗口（/state.windows>0）  — windows=1
PASS  点击真的落到显示上（像素误差 ≤3）  — 客户端发出=(0.2994,0.4491) 期望≈(307,287) 靶程序收到=(306,287)
PASS  键盘输入真的落到显示上  — LINE text="hello123"
PASS  插件接口没有 4xx/5xx（/frame 首帧 503 属契约允许）  — /frame 503=9（随后 200 JPEG=103）
PASS  页面没有 JS 异常（pageerror）  — 无
/frame 请求 185 次：["17970ms:503","18274ms:503","18577ms:503","18883ms:503","19186ms:503","19490ms:503","19793ms:503","20096ms:503"] … 末次 44618ms:200
== 18 通过 / 0 失败 ==  结果：PASS（退出码 0）
```

**为什么要加"强制 503 窗口"这一条**：新会话的**第一帧本来就可能合法地 503**（服务契约：没有帧就 503）。而"帧循环被写死"这类缺陷的表现是**静默 0 帧、canvas 永远停在默认 300x150** —— 页面不报错、服务侧一切正常，只有这种确定性回归抓得住。窗口期内/外的次数是可断言的硬指标。

三条值得记下的"坑"（已写进脚本，README 可引用）：
1. **空会话不显示视图标签**：必须先发一条消息（没有 API Key 也会失败，但用户消息已落库），`对话/轨迹/显示器` 才出现；
2. **sid 要从 `/frame?session=…` 的请求 URL 里取**：面板状态条显示的是**截断 id**（`session-bb37`），拿它去调 `/exec` 会作用到别的会话；
3. **点击坐标要按 contain 后的画面矩形算**（客户端会抠掉黑边），且靶程序报的是**窗口内坐标**（窗口有 `+40+40` 偏移时要减掉）。

---

## 6. 发现的新问题 / 观察（含归因与现状）

### 6.1 【已解释，非缺陷】`setHasFrame is not defined`（pageerror）
一次 e2e 运行里浏览器报 `pageerror: setHasFrame is not defined`，同时 `/plugins/??dsh-display-panel/client.js&rev=…` 出现 **404 + ERR_ABORTED**。
归因：**client-dev 当时正在改 `lib/client.js`，浏览器加载到了"用了 `setHasFrame` 但还没声明"的中间态**。当前文件第 338 行已声明、`node --check` 通过、随后两次 e2e 该项均 PASS。
给团队的提醒：并发改客户端 JS 时 e2e 可能红；看到这类 pageerror 先看 client.js 的 mtime。

### 6.2 【已定位为客户端缺陷 → 已修复 → 本报告独立复验通过】"面板只有 1 次 /frame（503）后不再拉帧"

**我第一轮的观察**：某次 e2e 里面板打开后 30 秒内 `/frame` 只被请求 **1 次**且返回 503，canvas 停在默认 300x150（永久空白）；同一时刻服务侧 `frame.count=57`、`/snapshot` 返回 200/2721B —— 服务 1 秒后就有帧了，却没人再去拿。

**client-dev 的定位（我的观察成立，且是我的报告里"归因不明"的那一条）**：帧循环新加的"单飞"保护写成了从 `run()` 内部（`running === true` 的窗口内）调用 `schedule()`，而 `schedule()` 自己第一句就是 `if (!alive || running || scheduled !== null) return` → **第一次拉帧之后就再也排不上下一次，任何状态码都一样**：循环静默死掉、不报错、canvas 从未画过所以停在 300x150。与现象逐条吻合。

**修复后的静态证据**（`lib/client.js`，git a680c04）：

```js
const run = async () => {
  if (!alive || running) return
  running = true
  // 下一次拉帧的间隔。注意：schedule() 必须在 running=false **之后**调用，
  // 否则会被自己的单飞判断挡掉（画面就永远停在第一帧）。
  ...
  } finally { running = false }
  schedule(next)          // ← 现在在 try/finally 之外
}
```
并且 503/网络错误有了"还没就绪"宽限期（`FRAME_WARM_STRIKES` 次 × `FRAME_WARM_MS`，注释写明宿主头 1~2 秒会一直 503），宽限期过了才切 error。

**我的独立动态复验**（不是引用他的用例）：给 `tools/e2e-panel.mjs` 加了 `--frame-outage`（默认 2500ms）—— 在**点击「显示器」标签之前**用 Playwright 的 `page.route` 拦下所有 `/frame`，窗口期内一律回 503，窗口结束后放行：

```
PASS  强制 503 窗口（2500ms）后帧循环自动恢复（契约 §3 退避重连）
      — 窗口期内仍然重试了 8 次；窗口结束后放行 55 次
/frame 请求 185 次：["17970ms:503","18274ms:503","18577ms:503","18883ms:503",…] 末次 44618ms:200
200-JPEG=103/112      canvas 上画出了画面（非全黑）：nonBlack=140/861520
```
窗口期内 8 次重试 ≈ **312ms 一次**，与 `FRAME_WARM_MS`(300ms) 吻合；窗口一结束立刻恢复成 200 并持续拉帧。**这条断言在旧写法下必然红**（窗口期内只会出现 1 次请求 → `blocked503 >= 2` 不成立），所以它是对该缺陷的**有效**回归。
（反事实没有实机执行：那个有缺陷的中间版本从未提交，而 `lib/client.js` 是 client-dev 的写入范围，我不改它。）

### 6.3 【环境】显示号与"别的命名空间遗留的 Xvfb"撞号
- 现象（lead 也独立遇到）：服务挑了显示号 N，但 N 已被**另一个命名空间**里遗留的 Xvfb 占着（对方 `/tmp/.X11-unix/XN` 与 lock 文件在它自己的私有 `/tmp` 里，本进程扫不到），于是自己那个 Xvfb 以 `Server is already active for display N` 退出 → 靶程序 `cannot open display ':N'`。
- 已做的缓解（本脚本内）：靶程序启动**失败自动换一个会话重试一次**（换会话=换显示号），仍失败才 FAIL，并把 `/state`（含 `frameError/started`）与 `xvfb.log` 尾部一起打进失败信息；会话在结束时统一 `/close`（不留孤儿 Xvfb）。
- 建议（已派给 service-dev）：显示归属做校验（例如给 root window 打 `DSH_DISPLAY_SESSION` 标记），别只看"能连上"。

### 6.4 【一次性，已消失】`service/selfcheck.py` 自身失败
第 2 次回归时抓到 `AssertionError: 探测接口竟然创建了会话：ci-never-created`（该次运行整体 FAIL）。随后 3 次重跑该项均 `PASS`（`== 8/8 项通过 ==`），判定为 T1 改代码过程中的中间态。
**注意**：这条恰好说明"探测接口不得创建会话"是 service 自检里的硬断言，值得保留。

### 6.5 【环境，非产品缺陷】我的测试实例被别人的清理动作杀掉（3 次）

- 第 1 次：selftest 动态检查中途 `ConnectionRefusedError`，`viewer.log` **无 traceback**（只有正常启动两行）—— 符合"收到 SIGTERM 后走正常退出处理器"的特征；第 3 次复跑全绿。
- 第 2/3 次：我在 8303 起的服务（后台作业）被外部再次终止（作业状态 `completed, exit code 0`，日志无异常），一次发生在 e2e 运行中 → 那次 e2e 立刻报 `canvas=0`、`/frame 请求 0 次` 并整轮 FAIL（**失败得很响，没有静默变绿**，这是正确的行为）；重启服务后同一脚本 18/0 全绿。
- 归因：队友清理测试实例时用了宽泛匹配（脚本名/端口相同就会误伤）。**建议：清理自己的实例用精确 PID**（本报告作者的所有临时实例都是自己起、自己收；最后一次收尾用 `kill <精确 PID>`，8303/8601 已释放、我拉起的 Xvfb 与靶程序全部退出，用户自己的 8099/:99/:100 未动）。

### 6.6 【提示】测试脚本对"环境抖动"的两种反应（都已处理）

- **能分辨归因**：真实 HOME 被判为"有外部服务在用"时 → SKIP 并打印属主 pid（§2 那条）；
- **不能分辨时宁可响**：服务被杀掉时 e2e 报 `canvas=0` / `/frame 请求 0 次` 并整轮 FAIL，绝不静默变绿（§6.5）；靶程序因显示号撞号起不来时，selftest 会**换会话重试一次**，仍失败才 FAIL 并把 `/state` + `xvfb.log` 尾部打进失败信息。

---

## 7. 结论

- **主线可用**：在真服务 + 真注入 + 真浏览器下，"面板显示本会话独立显示、点击/打字真的注入到那台显示"端到端成立；
- **安全与契约要点成立**：sid 校验、令牌只在宿主侧、`/info` 不含令牌、无令牌 403、非法输入 4xx、输入串行、会话隔离；
- **三个工具可直接用**：`tools/selftest.py`（零副作用、缺依赖跳过、FAIL 退非 0）、`tools/e2e-panel.mjs`（真浏览器，退出码 0/1/3，含强制 503 窗口回归）、`tools/xtarget.py`（无需 gcc 的 X11 靶程序）；
- **第一轮那条"归因不明"的观察已结案**：client-dev 定位为 `schedule()` 单飞判断挡掉自己（帧循环静默死亡），已修复；我用确定性 503 窗口独立复验通过（§6.2）；
- **未验证项见 §4**，不要把它们当成"已验证"。
