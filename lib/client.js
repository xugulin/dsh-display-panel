/**
 * dsh-display-panel — 浏览器半边：在「对话 / 轨迹 / 浏览器」那一排里加一个「显示器」标签。
 *
 * 架构（契约 docs/CONTRACT.md §3）：面板**不**直连显示器服务，只跟宿主半边的**同源**接口
 * `/api/dsh-display-panel/*` 说话。之前那版是 iframe 直连服务的，踩过一串结构性坑：
 * 跨源 + 混合内容（HTTPS 部署下面板永远空白）、回环地址（从别的机器访问 DSH 时连的是
 * **访问者自己**的机器）、访问令牌出现在浏览器地址里（进历史/Referer）。现在令牌只留在
 * 宿主侧，浏览器只发同源请求。
 *
 * 0.4.0 起画面走**流**（契约 §5.3）：`fetch('/stream')` + `ReadableStream` 解 MJPEG，
 * 每个 part 带 `X-DSH-Seq/Time/Size/Cursor` 头。为什么要自己解而不是 `<img src=流>`：
 * `<img>` 断了不会重连，也没法丢帧。所以这里有一条**最新帧优先**的流水线：
 *
 *   reader → pushFrame（覆盖式，只留最新一帧待解码）
 *          → createImageBitmap（异步解码，同一时刻只解一个）
 *          → frameRef（解好的最新帧）
 *          → requestAnimationFrame 统一绘制（缩放/光标/涟漪都在这一帧里画完）
 *
 * 宿主/上游不支持流（404/501/没有 multipart）或连接一直失败 → 自动退回既有的
 * `/frame` 轮询路径（那条路一直保留，是兜底）。标签页隐藏时**断开流**（AbortController）
 * 并尽力知会服务降档；重新可见时恢复。组件卸载时流、rAF、定时器、监听器全部收干净。
 *
 * 这个文件是"裸 JS 模块"：由 DSH 的客户端模块系统当 bundle 直接执行，注册方式是
 * `window.__ModuleLoader__.load({ id, factory })`，factory 里 `require('react')` 拿宿主提供的
 * React。**不要** import 任何 npm 包，也不要去改注册方式（已被实测证明可用）。
 *
 * 构建标记 BUILD：状态条上会显示，用来判断浏览器里加载的到底是哪一版（插件 JS 有缓存）。
 * 观测（给 perf 脚本用）：`window.__ddpStats` 与 `.ddp-root` 上的 `data-*` 属性，
 * 字段含义见 publishStats 的注释。
 */
window.__ModuleLoader__.load({
  id: 'dsh-display-panel',
  factory: (require) => {
    const React = require('react')
    const { useEffect, useRef, useState } = React
    const h = React.createElement

    /** 槽位 id：别改（改了用户的标签位置/记忆会错位）。 */
    const PANEL_ID = 'display-panel'
    /** 构建标记：面板状态条与 data-build 上都会出现。 */
    const BUILD = '2026-09-25b'
    /** 宿主半边的同源接口前缀（契约 §2）。浏览器只跟它说话。 */
    const API = '/api/dsh-display-panel'
    /** 流式长连接（契约 §5.3）。 */
    const STREAM_PATH = '/stream'
    /** 档位调整（契约 §5.2 的 quality/fps/scale；profile 是给宿主的额外提示）。 */
    const STREAM_CONFIG_PATH = '/stream-config'

    /* ------------------------------------------------------------ 节奏与阈值 */

    /** 轮询兜底：单帧慢时自动放慢，最低 120ms 一次、最高 1s 一次。 */
    const FRAME_MIN_MS = 120
    const FRAME_MAX_MS = 1000
    /**
     * `/state` 轮询：0.4.0 起光标/尺寸都随帧头下发，这里只补后端、缺依赖、注入开关，
     * 所以从 700ms 放到 3s（少一条无谓的请求）。
     */
    const STATE_MS = 3000
    /** /info 轮询：一切正常时慢一点（只是看服务还活着），等待服务时快一点。 */
    const INFO_OK_MS = 10000
    const INFO_WAIT_MS = 2500
    /** 失败退避：0.5s → 1s → 2s → 4s → 5s（封顶），成功后立刻清零。 */
    const RETRY_MIN_MS = 500
    const RETRY_MAX_MS = 5000
    /** 连续多少帧拿不到之后才算"帧出问题了"（避免一次网络抖动就把画面切走）。 */
    const FRAME_STRIKES = 3
    /**
     * 503 / 网络错误算"还没就绪"而不是"坏了"：宿主实测服务刚被拉起后的头 1~2 秒，
     * 上游一直 503（Xvfb 还没抓到第一帧）。所以给它一段宽限期 + 更短的重试间隔。
     */
    const FRAME_WARM_STRIKES = 16
    const FRAME_WARM_MS = 300
    /** 流断了之后的重连退避（0.5s→5s），成功连上立刻清零。 */
    const STREAM_RETRY_MIN_MS = 500
    const STREAM_RETRY_MAX_MS = 5000
    /** 退回轮询之后，多久再试一次流（上游可能是旧版服务，升级后要能自己回来）。 */
    const STREAM_REPROBE_MS = 30000
    /** 连续几次流失败后就不再死磕流，先切轮询保证有画面。 */
    const STREAM_FAILS_TO_POLL = 3
    /** 解码超时：createImageBitmap 卡住时不能让"最新帧优先"的流水线永远堵着。 */
    const DECODE_TIMEOUT_MS = 1500
    /** 后备缓冲最多按 3 倍 DPR 分配（再高只是白烧内存/带宽，肉眼没差别）。 */
    const DPR_MAX = 3
    /** 缩放范围（以"适应窗口"为基准的 100%）。 */
    const ZOOM_MIN = 0.25
    const ZOOM_MAX = 4
    /** 实测帧率/丢帧的统计窗口（契约要求"最近 3 秒"）。 */
    const FPS_WINDOW_MS = 3000
    /** 状态条刷新节奏（同时也是 window.__ddpStats 的刷新节奏）。 */
    const STATS_TICK_MS = 500
    /** 点击涟漪时长。 */
    const RIPPLE_MS = 450
    /** 自适应降档：两次请求之间至少隔这么久，避免抖动。 */
    const ADAPT_COOLDOWN_MS = 15000
    /** 自动降档后等多久才试探着爬回去（每次再失败就翻倍，见 maybeRecover）。 */
    const RECOVER_WAIT_MS = 20000
    /** 「自动」档连续健康这么久 → 退避计数清零。 */
    const RECOVER_RESET_MS = 300000
    /** MJPEG part 分隔符（契约 §5.3）。 */
    const BOUNDARY = '--frame'
    /** 解析缓冲区上限：超过就说明流是坏的（没有边界），别把内存吃光。 */
    const PARSE_BUFFER_MAX = 4 * 1024 * 1024

    /**
     * 档位 → 服务参数（契约 §5.2：quality 1..100、fps 1..30、scale 0.25..1.0）。
     * `auto` 是默认：不改服务参数，只在实测跟不上时请求降档。
     */
    const PROFILES = {
      // 界面概念（自动/流畅/省流）在客户端本地映射成契约的三个数值，发出去的只有这三个。
      auto: { label: 'auto', quality: 70, fps: 20, scale: 1 },     // 均衡（= 契约 §5.2 的默认档）
      smooth: { label: 'smooth', quality: 85, fps: 30, scale: 1 }, // 流畅：更高帧率 + 更高画质
      saver: { label: 'saver', quality: 50, fps: 8, scale: 0.75 }, // 省流
      paused: { label: 'paused', quality: 50, fps: 2, scale: 0.5 }, // 标签页隐藏时（省电）
    }

    const TEXT = {
      zh: {
        title: '显示器',
        noSession: '这里拿不到会话 id，无法确定这个面板对应哪台显示。打开一个会话后再切到「显示器」。',
        checking: '正在检查显示器…',
        checkingHint: '正在问宿主半边：显示器服务起了没有、缺不缺依赖。',
        needService: '显示器服务没有运行',
        needServiceHint: '画面由宿主半边（lib/index.js）同源代理提供，服务没起来时面板没有画面可画。'
          + '点下面的按钮让宿主把它拉起来；Linux 上每个会话一台独立显示，别的会话看不到这里。',
        start: '打开显示器',
        starting: '正在启动…',
        startingHint: '宿主正在拉起显示器服务（最多约 8 秒），起来后这里会自动出现画面。',
        retryNow: '立即重试',
        retryIn: (n, sec) => `第 ${n} 次失败，${sec} 秒后自动重试…`,
        errAuth: '没有权限访问宿主接口（HTTP 401/403）',
        errAuthHint: '面板复用 DSH 自己的请求守卫。请在 DSH 页面重新登录，然后刷新本页。',
        errApi: '宿主接口不可用',
        errApiHint: '宿主半边（lib/index.js）可能没就绪、没启用，或还是 0.2.x 的旧版（旧版只有 /info）。'
          + '确认插件目录里 lib/index.js 是 0.3.x，并重启 dsh web。',
        errNet: '连不上宿主接口（网络错误）',
        errNetHint: '请求本身就没发出去：页面可能离线，或 DSH 的 web 服务刚重启。',
        errService: '显示器服务不可用（HTTP 503）',
        errServiceHint: '宿主说服务没在跑，或正在拉起。稍后会自动重试；一直这样就用上面的按钮手动拉一次。',
        errFrame: '拿不到这个会话的画面（HTTP 404）',
        errFrameHint: '服务里还没有这个会话的显示（显示是按需创建的）。稍后会自动重试。',
        errHttp: (status) => `请求失败（HTTP ${status}）`,
        missing: (n) => `缺 ${n} 项依赖`,
        missingHint: '在跑显示器服务的那台机器上安装：',
        realDesktop: '真实桌面：注入的是真实鼠标键盘',
        realDesktopHint: 'win32 / darwin 后端抓的是本机真实桌面（所有会话共用这一块屏），'
          + '面板里的点击会真的点在你自己的桌面上。',
        inputOff: '输入注入未开启',
        inputOffHint: '服务端没开输入注入（win32/darwin 需要 DSH_VIEW_INPUT=1），现在只能看、不能操作。',
        readonly: '只读',
        kbHint: '点击画面后键盘生效',
        kbOn: '键盘已连接',
        backend: '后端',
        display: '显示',
        session: '会话',
        build: '构建',
        fps: 'fps',
        still: '静止',
        stillHint: '画面没有变化时不重传帧（省带宽与 CPU）—— 动一下就会立刻恢复出帧。',
        live: '实时画面',
        waitingFrame: '等待第一帧…',
        waitingFrameHint: '服务在跑，但上游还没抓到画面（刚拉起时常见）。',
        stateLost: '状态读取失败',
        inputLost: '输入发送失败',
        /* ——— 0.4.0 新增 ——— */
        skeleton: '正在建立实时画面…',
        transport: '传输',
        streamMode: '流式',
        pollMode: '轮询',
        streamOff: '流不可用，已切回轮询',
        reconnecting: (sec) => `画面流中断，${sec} 秒后重连…`,
        dropped: '丢帧',
        latency: '延迟',
        decodeFail: '解码失败',
        quality: '质量',
        zoom: '缩放',
        profile: '档位',
        fit: '适应',
        fitHint: '适应窗口：整屏可见（等比缩放，不裁切）',
        one: '1:1',
        oneHint: '1:1 点对点：一个画面像素对一个屏幕像素（Shift+拖动 平移）',
        zoomIn: '放大',
        zoomOut: '缩小',
        smooth: '平滑',
        smoothHint: '图像平滑：开=柔和（适合缩放看整体），关=锐利（适合 1:1 看像素）',
        fullscreen: '全屏',
        fullscreenHint: '全屏显示画面（Esc 退出）',
        exitFullscreen: '退出全屏',
        profileHint: '档位：自动=按实测自适应；流畅=更高帧率；省流=降帧率/降码率',
        profileAuto: '自动',
        profileSmooth: '流畅',
        profileSaver: '省流',
        profilePaused: '暂停',
        autoDowngrade: '实测帧率偏低，已自动降档',
        autoRecover: '已恢复到「自动」档（实测够快）',
        panHint: 'Shift+拖动 平移 · Ctrl+滚轮 缩放',
        dragging: '拖动查看中…',
      },
      en: {
        title: 'Display',
        noSession: 'No session id here, so this panel cannot tell which display it belongs to. Open a session, then switch to Display.',
        checking: 'Checking the display…',
        checkingHint: 'Asking the host half whether the viewer service is up and whether any dependency is missing.',
        needService: 'The display service is not running',
        needServiceHint: 'Frames come from the host half (lib/index.js) over a same-origin proxy; with no service there is nothing to draw. '
          + 'Use the button below to let the host start it. On Linux every session gets its own isolated display.',
        start: 'Start display',
        starting: 'Starting…',
        startingHint: 'The host is starting the viewer service (up to ~8s); the picture shows up here automatically.',
        retryNow: 'Retry now',
        retryIn: (n, sec) => `Attempt ${n} failed, retrying in ${sec}s…`,
        errAuth: 'Not allowed to call the host API (HTTP 401/403)',
        errAuthHint: 'The panel reuses the DSH request guard. Sign in to DSH again, then reload this page.',
        errApi: 'Host API unavailable',
        errApiHint: 'The host half (lib/index.js) may be missing, disabled, or still the old 0.2.x one (which only serves /info). '
          + 'Make sure lib/index.js is 0.3.x and restart dsh web.',
        errNet: 'Cannot reach the host API (network error)',
        errNetHint: 'The request never left the browser: the page may be offline, or the DSH web server just restarted.',
        errService: 'Display service unavailable (HTTP 503)',
        errServiceHint: 'The host reports the service is down or still starting. It retries automatically; use the button to start it manually.',
        errFrame: 'No picture for this session (HTTP 404)',
        errFrameHint: 'The service has no display for this session yet (displays are created on demand). Retrying automatically.',
        errHttp: (status) => `Request failed (HTTP ${status})`,
        missing: (n) => `${n} missing dependency(ies)`,
        missingHint: 'Install on the machine running the viewer service:',
        realDesktop: 'Real desktop: injects your real mouse and keyboard',
        realDesktopHint: 'The win32 / darwin backend captures the real desktop (shared by every session), '
          + 'so clicks in this panel land on your own screen.',
        inputOff: 'Input injection is off',
        inputOffHint: 'The service has input injection disabled (win32/darwin need DSH_VIEW_INPUT=1); watch-only for now.',
        readonly: 'read-only',
        kbHint: 'Click the picture to enable the keyboard',
        kbOn: 'Keyboard attached',
        backend: 'backend',
        display: 'display',
        session: 'session',
        build: 'build',
        fps: 'fps',
        still: 'idle',
        stillHint: 'Identical frames are not re-sent (saves bandwidth and CPU) — it resumes the moment something changes.',
        live: 'live',
        waitingFrame: 'Waiting for the first frame…',
        waitingFrameHint: 'The service is up, but no frame has been captured yet (common right after a start).',
        stateLost: 'state polling failed',
        inputLost: 'input delivery failed',
        /* ——— 0.4.0 ——— */
        skeleton: 'Opening the live stream…',
        transport: 'transport',
        streamMode: 'stream',
        pollMode: 'polling',
        streamOff: 'stream unavailable, fell back to polling',
        reconnecting: (sec) => `Stream dropped, reconnecting in ${sec}s…`,
        dropped: 'dropped',
        latency: 'latency',
        decodeFail: 'decode failed',
        quality: 'quality',
        zoom: 'zoom',
        profile: 'profile',
        fit: 'Fit',
        fitHint: 'Fit to window: the whole screen stays visible (uniform scale, no cropping)',
        one: '1:1',
        oneHint: '1:1 pixel-perfect: one remote pixel per screen pixel (Shift+drag to pan)',
        zoomIn: 'Zoom in',
        zoomOut: 'Zoom out',
        smooth: 'Smooth',
        smoothHint: 'Image smoothing: on = softer when scaled, off = crisp pixels at 1:1',
        fullscreen: 'Fullscreen',
        fullscreenHint: 'Show the picture fullscreen (Esc to exit)',
        exitFullscreen: 'Exit fullscreen',
        profileHint: 'Profile: auto = adapts to measurements; smooth = higher fps; saver = lower fps/bitrate',
        profileAuto: 'Auto',
        profileSmooth: 'Smooth',
        profileSaver: 'Saver',
        profilePaused: 'Paused',
        autoDowngrade: 'Measured frame rate is low — downgraded automatically',
        autoRecover: 'Back to auto (measured fast enough)',
        panHint: 'Shift+drag to pan · Ctrl+wheel to zoom',
        dragging: 'Panning…',
      },
    }

    /**
     * 按浏览器语言选文案（契约 §3）。拿不到 navigator 就退回中文（插件的主要用户群）。
     */
    function pickLang() {
      try {
        const list = [navigator.language].concat(navigator.languages || [])
        for (const item of list) {
          const tag = String(item || '').toLowerCase()
          if (!tag) continue
          if (tag.indexOf('zh') === 0) return 'zh'
          return 'en'
        }
      } catch (error) { /* 没有 navigator：用默认 */ }
      return 'zh'
    }

    /**
     * 样式只注入一次。所有取色都走 DSH 主题变量（`--dsw-alias-*` / `--dsw-static-*`），
     * 深度兜底写在 var() 的第二个参数里 —— 浅色/深色主题都要能看，别写死颜色。
     */
    const CSS = `
      .ddp-root {
        --ddp-border: var(--dsw-alias-border-l2, rgba(128,128,128,.28));
        --ddp-border-strong: var(--dsw-alias-border-l3, rgba(128,128,128,.45));
        --ddp-surface: var(--dsw-alias-bg-skeleton, rgba(128,128,128,.10));
        --ddp-surface-hover: var(--dsw-alias-interactive-bg-hover, rgba(128,128,128,.18));
        --ddp-text: var(--dsw-alias-label-primary, inherit);
        --ddp-muted: var(--dsw-alias-label-tertiary, rgba(128,128,128,.85));
        --ddp-accent: var(--dsw-static-deepseek-450, #4d93f8);
        --ddp-ok: var(--dsw-alias-state-success-primary, #22c55e);
        --ddp-warn: var(--dsw-alias-state-warn-primary, #f59e0b);
        --ddp-bad: var(--dsw-alias-state-error-primary, #dc2626);
        /* letterbox（画面之外的留白）：用主题的遮罩色 —— 浅色主题是浅灰，不是一块死黑。 */
        --ddp-letterbox: var(--dsw-alias-bg-mask-2, rgba(0,0,0,.12));
        --ddp-card: var(--dsw-alias-bg-overlay, rgba(24,26,32,.9));
        --ddp-toast-bg: var(--dsw-alias-toast-bg, rgba(20,22,28,.78));
        --ddp-toast-fg: var(--dsw-alias-toast-label, #f5f6f8);

        width: 100%; height: 100%; min-height: 0; display: flex; flex-direction: column;
        background: transparent; color: var(--ddp-text); position: relative; overflow: hidden;
      }
      .ddp-bar {
        flex: 0 0 auto; display: flex; flex-wrap: wrap; align-items: center; gap: 6px;
        padding: 6px 10px; font-size: 11.5px; line-height: 1.5;
        border-bottom: 1px solid var(--ddp-border);
      }
      .ddp-dot {
        width: 8px; height: 8px; border-radius: 50%; flex: 0 0 auto;
        background: var(--dsw-alias-state-idle-primary, #9aa0a6);
      }
      .ddp-dot-ok { background: var(--ddp-ok); box-shadow: 0 0 0 3px color-mix(in srgb, var(--ddp-ok) 18%, transparent); }
      .ddp-dot-warn { background: var(--ddp-warn); }
      .ddp-dot-bad { background: var(--ddp-bad); }
      .ddp-stat { opacity: .9; }
      .ddp-badge {
        padding: 1px 7px; border-radius: 999px; white-space: nowrap;
        border: 1px solid var(--ddp-border);
        background: var(--ddp-surface);
        font-variant-numeric: tabular-nums;
      }
      .ddp-badge.ddp-warn {
        border-color: var(--ddp-warn); color: var(--ddp-warn);
        background: color-mix(in srgb, var(--ddp-warn) 14%, transparent);
      }
      .ddp-badge.ddp-bad {
        border-color: var(--ddp-bad); color: var(--ddp-bad);
        background: color-mix(in srgb, var(--ddp-bad) 14%, transparent);
      }
      .ddp-badge.ddp-ok { border-color: var(--ddp-border-strong); }
      .ddp-grow { flex: 1 1 auto; }
      .ddp-mini {
        font-size: 10.5px; opacity: .5; white-space: nowrap;
        font-variant-numeric: tabular-nums;
      }
      .ddp-sid {
        opacity: .6; white-space: normal; word-break: break-all; max-width: 100%;
        user-select: text; -webkit-user-select: text; cursor: text;
      }
      .ddp-tools {
        flex: 0 0 auto; display: flex; flex-wrap: wrap; align-items: center; gap: 4px;
        padding: 4px 8px; font-size: 11px;
        border-bottom: 1px solid var(--ddp-border);
      }
      .ddp-tbtn {
        font: inherit; font-size: 11px; line-height: 1; padding: 4px 8px; border-radius: 6px;
        cursor: pointer; color: var(--ddp-text); background: var(--ddp-surface);
        border: 1px solid transparent; display: inline-flex; align-items: center; gap: 4px;
      }
      .ddp-tbtn:hover:not(:disabled) { background: var(--ddp-surface-hover); }
      .ddp-tbtn:focus-visible { outline: 2px solid var(--ddp-accent); outline-offset: 1px; }
      .ddp-tbtn[aria-pressed="true"] { border-color: var(--ddp-accent); color: var(--ddp-text); }
      .ddp-tbtn:disabled { opacity: .5; cursor: default; }
      .ddp-zoomval {
        min-width: 46px; text-align: center; opacity: .8;
        font-variant-numeric: tabular-nums;
      }
      .ddp-sep { width: 1px; height: 14px; background: var(--ddp-border); margin: 0 3px; }
      .ddp-body { position: relative; flex: 1 1 auto; min-height: 0; display: flex; }
      .ddp-stage {
        position: relative; flex: 1 1 auto; min-width: 0; min-height: 0; display: flex;
        background: var(--ddp-letterbox);
        transition: box-shadow .15s ease;
      }
      .ddp-stage[data-focus="1"] { box-shadow: inset 0 0 0 2px var(--ddp-accent); }
      .ddp-stage[data-panning="1"] { cursor: grabbing; }
      .ddp-stage:fullscreen { background: var(--ddp-letterbox); }
      .ddp-canvas {
        width: 100%; height: 100%; display: block; background: transparent;
        cursor: default; touch-action: none; outline: none; user-select: none;
        opacity: 0; transition: opacity .35s ease;
      }
      .ddp-canvas[data-has-frame="1"] { opacity: 1; }
      .ddp-overlay {
        position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
        padding: 20px; overflow: auto; z-index: 3;
        background: var(--ddp-card); color: var(--dsw-alias-label-primary, var(--ddp-text));
      }
      .ddp-card { max-width: 620px; text-align: center; display: flex; flex-direction: column; gap: 10px; align-items: center; }
      .ddp-title { font-size: 15px; font-weight: 600; }
      .ddp-note { font-size: 12.5px; line-height: 1.75; opacity: .82; }
      .ddp-hint { font-size: 11.5px; line-height: 1.7; opacity: .6; word-break: break-word; }
      .ddp-list { margin: 0; padding: 0; list-style: none; font-size: 11.5px; opacity: .82; text-align: left; }
      .ddp-list li { padding: 1px 0; word-break: break-word; }
      .ddp-btn {
        margin-top: 2px; padding: 7px 16px; font: inherit; font-size: 12.5px; border-radius: 8px;
        cursor: pointer; color: var(--ddp-text);
        border: 1px solid var(--ddp-border-strong);
        background: var(--ddp-surface);
      }
      .ddp-btn:hover:not(:disabled) { border-color: var(--ddp-accent); background: var(--ddp-surface-hover); }
      .ddp-btn:disabled { opacity: .55; cursor: default; }
      .ddp-btn-primary { border-color: var(--ddp-accent); }
      .ddp-err { font-size: 11.5px; color: var(--ddp-bad); word-break: break-word; }
      .ddp-pill {
        position: absolute; left: 50%; bottom: 10px; transform: translateX(-50%);
        max-width: calc(100% - 20px); padding: 4px 11px; border-radius: 999px; font-size: 11px;
        pointer-events: none; z-index: 2; text-align: center;
        background: var(--ddp-toast-bg); color: var(--ddp-toast-fg);
        border: 1px solid var(--dsw-alias-border-inverted, rgba(255,255,255,.18));
      }
      .ddp-skel {
        position: absolute; inset: 0; z-index: 1; overflow: hidden;
        display: flex; align-items: center; justify-content: center; gap: 8px;
        background: var(--dsw-alias-bg-base, transparent); color: var(--ddp-muted);
        font-size: 12px;
      }
      .ddp-skel-tex {
        position: absolute; inset: 0; pointer-events: none;
        background: linear-gradient(100deg,
          transparent 20%,
          var(--dsw-alias-bg-skeleton, rgba(128,128,128,.14)) 42%,
          transparent 64%);
        background-size: 220% 100%;
        animation: ddp-shimmer 1.5s linear infinite;
      }
      .ddp-skel-spin {
        width: 14px; height: 14px; border-radius: 50%; flex: 0 0 auto;
        border: 2px solid var(--ddp-border-strong); border-top-color: var(--ddp-accent);
        animation: ddp-spin .9s linear infinite;
      }
      @keyframes ddp-shimmer { from { background-position: 150% 0; } to { background-position: -50% 0; } }
      @keyframes ddp-spin { to { transform: rotate(360deg); } }
      @media (prefers-reduced-motion: reduce) {
        .ddp-skel-tex, .ddp-skel-spin { animation: none; }
        .ddp-canvas { transition: none; }
      }
      .ddp-sink {
        position: absolute; left: 0; top: 0; width: 1px; height: 1px; opacity: 0;
        border: 0; padding: 0; margin: 0; resize: none; pointer-events: none;
      }
    `
    function ensureStyle() {
      try {
        if (document.getElementById('ddp-style')) return
        const el = document.createElement('style')
        el.id = 'ddp-style'
        el.textContent = CSS
        document.head.appendChild(el)
      } catch (error) { /* 没有 head 就算了，功能不受影响 */ }
    }

    /* ---------------------------------------------------------------- 纯函数 */

    function clamp(value, lo, hi) {
      if (!Number.isFinite(value)) return lo
      if (value < lo) return lo
      if (value > hi) return hi
      return value
    }

    function clamp01(value) {
      if (!Number.isFinite(value)) return 0
      if (value < 0) return 0
      if (value > 1) return 1
      return Math.round(value * 10000) / 10000
    }

    /** 单调时钟（performance.now 优先；jsdom/老浏览器退回 Date.now）。 */
    function nowMs() {
      try {
        if (typeof performance !== 'undefined' && performance && typeof performance.now === 'function') return performance.now()
      } catch (error) { /* 用 Date.now */ }
      return Date.now()
    }

    /** 失败退避：0.5s→1s→2s→4s→5s，带 ±20% 抖动（多开几个面板时别一起撞上来）。 */
    function backoffDelay(attempt) {
      const base = Math.min(RETRY_MAX_MS, RETRY_MIN_MS * Math.pow(2, Math.max(0, attempt - 1)))
      const jitter = base * 0.2 * (Math.random() * 2 - 1)
      return Math.max(200, Math.round(base + jitter))
    }

    async function readJsonSafe(res) {
      try { return await res.json() } catch (error) { return null }
    }

    /** 后备缓冲的像素比：1..DPR_MAX（0.5 这种怪值也兜住）。 */
    function dprNow() {
      let dpr = 1
      try { dpr = Number(window.devicePixelRatio) || 1 } catch (error) { dpr = 1 }
      return clamp(dpr, 1, DPR_MAX)
    }

    /** 关掉 ImageBitmap（部分实现没有 close，别让它把整条流水线带崩）。 */
    function closeBitmap(bitmap) {
      if (!bitmap) return
      try { if (typeof bitmap.close === 'function') bitmap.close() } catch (error) { /* 已经关了 */ }
    }

    /**
     * 带超时的 Promise。解码卡住（jsdom、坏 JPEG、GPU 抽风）时不能让
     * "最新帧优先"的流水线永远堵在那里 —— 超时返回 null，并回收迟到的结果。
     */
    function withTimeout(promise, ms, onLate) {
      return new Promise((resolve) => {
        let settled = false
        const timer = setTimeout(() => {
          if (settled) return
          settled = true
          resolve(null)
        }, ms)
        const finish = (value) => {
          if (settled) { if (onLate) { try { onLate(value) } catch (error) { /* 无所谓 */ } } return }
          settled = true
          clearTimeout(timer)
          resolve(value)
        }
        promise.then(finish, () => finish(null))
      })
    }

    /**
     * 把 /info 的响应归一化。
     *
     * 新形状（契约 §2）：`{ok, service:{running,port,backend,size,version}, input:{enabled,realDesktop}, missing:[]}`。
     * 旧形状（0.2.x 宿主）：`{home, port, token}` —— 只有端口，端口有值就认为服务在跑；
     * 宿主半边还没升级完时面板也得能显示点东西，而不是白屏。
     */
    function normalizeInfo(json) {
      const source = (json && typeof json === 'object') ? json : {}
      const service = (source.service && typeof source.service === 'object') ? source.service : {}
      const input = (source.input && typeof source.input === 'object') ? source.input : {}
      const legacyPort = Number(source.port)
      const running = (typeof service.running === 'boolean')
        ? service.running
        : (Number.isFinite(legacyPort) && legacyPort > 0)
      return {
        running,
        service: {
          port: Number.isFinite(Number(service.port)) ? Number(service.port)
            : (Number.isFinite(legacyPort) ? legacyPort : null),
          backend: typeof service.backend === 'string' ? service.backend : '',
          size: typeof service.size === 'string' ? service.size : '',
          version: typeof service.version === 'string' ? service.version : '',
        },
        input: {
          enabled: (typeof input.enabled === 'boolean') ? input.enabled : true,
          realDesktop: input.realDesktop === true,
        },
        missing: Array.isArray(source.missing) ? source.missing : [],
      }
    }

    /** 把 /state 的响应归一化（契约 §1.2 的字段 + 宿主补的 service）。 */
    function normalizeState(json) {
      const source = (json && typeof json === 'object') ? json : {}
      const cursor = (source.cursor && typeof source.cursor === 'object') ? source.cursor : null
      const cx = cursor ? Number(cursor.x) : NaN
      const cy = cursor ? Number(cursor.y) : NaN
      return {
        display: typeof source.display === 'string' ? source.display : '',
        backend: typeof source.backend === 'string' ? source.backend : '',
        size: typeof source.size === 'string' ? source.size : '',
        input: (typeof source.input === 'boolean') ? source.input : null,
        realDesktop: source.realDesktop === true,
        missing: Array.isArray(source.missing) ? source.missing : [],
        cursor: (Number.isFinite(cx) && Number.isFinite(cy)) ? { x: clamp01(cx), y: clamp01(cy) } : null,
      }
    }

    /** 缺依赖的一条：契约里是 `{tool,why,package}`，也容忍纯字符串。 */
    function missingLine(item) {
      if (item === null || item === undefined) return ''
      if (typeof item === 'string') return item
      if (typeof item !== 'object') return String(item)
      const tool = item.tool || item.name || item.command || '?'
      const why = item.why ? ' — ' + String(item.why) : ''
      const pkg = item.package || item.pkg || ''
      return String(tool) + why + (pkg ? '（' + String(pkg) + '）' : '')
    }

    /**
     * 帧头 `X-DSH-Cursor: x,y`（归一化 0..1）→ 光标：
     *   · 有坐标      → `{x,y}`（画）
     *   · `-1,-1`     → `{hide:true}`（服务端明说"这次不画光标"，别把它夹到左上角）
     *   · 头缺失/坏值 → `null`（保持上一次的位置，不要闪烁）
     */
    function parseCursorHeader(text) {
      if (typeof text !== 'string' || !text) return null
      const parts = text.split(',')
      if (parts.length < 2) return null
      const x = Number(parts[0])
      const y = Number(parts[1])
      if (!Number.isFinite(x) || !Number.isFinite(y)) return null
      if (x < 0 || y < 0) return { hide: true }
      return { x: clamp01(x), y: clamp01(y) }
    }

    /** 帧头 `X-DSH-Size: WxH` → {w,h}；解析不了返回 null。 */
    function parseSizeHeader(text) {
      if (typeof text !== 'string' || !text) return null
      const match = /^\s*(\d+)\s*[x×]\s*(\d+)\s*$/.exec(text)
      if (!match) return null
      const w = Number(match[1])
      const h = Number(match[2])
      if (!(w > 0) || !(h > 0)) return null
      return { w, h }
    }

    /** JPEG 魔数（0xFFD8）：part 体是不是一张图，直接决定"这帧能不能解"。 */
    function looksLikeJpeg(bytes) {
      return !!(bytes && bytes.length > 3 && bytes[0] === 0xFF && bytes[1] === 0xD8)
    }

    /**
     * MJPEG 解析器（契约 §5.3）。按 `--frame` 边界切 part，读每个 part 的头 + JPEG 字节。
     *
     * 优先用 `Content-Length` 定长取体（JPEG 压缩数据里理论上可能出现 `--frame` 这串字节，
     * 定长才是可靠的）；没有 Content-Length 时退回"找下一个边界"。
     * `push(chunk, onPart)` 可以喂任意切分的字节块（TCP 不保证 part 边界对齐）。
     */
    function createMjpegParser() {
      const boundaryBytes = []
      for (let i = 0; i < BOUNDARY.length; i += 1) boundaryBytes.push(BOUNDARY.charCodeAt(i))
      let buffer = new Uint8Array(0)

      const findBoundary = (from) => {
        const limit = buffer.length - boundaryBytes.length
        for (let i = Math.max(0, from); i <= limit; i += 1) {
          let hit = true
          for (let j = 0; j < boundaryBytes.length; j += 1) {
            if (buffer[i + j] !== boundaryBytes[j]) { hit = false; break }
          }
          if (hit) return i
        }
        return -1
      }
      const findHeaderEnd = (from) => {
        for (let i = Math.max(0, from); i + 3 < buffer.length; i += 1) {
          if (buffer[i] === 13 && buffer[i + 1] === 10 && buffer[i + 2] === 13 && buffer[i + 3] === 10) return i
        }
        return -1
      }
      const readHeaders = (from, to) => {
        const out = {}
        let start = from
        for (let i = from; i <= to; i += 1) {
          if (i === to || buffer[i] === 10) {
            let end = i
            if (end > start && buffer[end - 1] === 13) end -= 1
            if (end > start) {
              let text = ''
              for (let k = start; k < end; k += 1) text += String.fromCharCode(buffer[k])
              const colon = text.indexOf(':')
              if (colon > 0) out[text.slice(0, colon).trim().toLowerCase()] = text.slice(colon + 1).trim()
            }
            start = i + 1
          }
        }
        return out
      }

      return {
        /** 喂一块字节；每解析出一个完整 part 就回调一次（headers + body 副本）。 */
        push(chunk, onPart) {
          if (chunk && chunk.length) {
            const next = new Uint8Array(buffer.length + chunk.length)
            next.set(buffer, 0)
            next.set(chunk, buffer.length)
            buffer = next
          }
          for (;;) {
            const bStart = findBoundary(0)
            if (bStart < 0) {
              // 还没看到边界：只留足够长的尾巴，别让坏流把内存吃光。
              if (buffer.length > PARSE_BUFFER_MAX) buffer = buffer.slice(buffer.length - boundaryBytes.length)
              return
            }
            let cursor = bStart + boundaryBytes.length
            if (buffer[cursor] === 13 && buffer[cursor + 1] === 10) cursor += 2
            else if (buffer[cursor] === 10) cursor += 1
            const hEnd = findHeaderEnd(cursor)
            if (hEnd < 0) {
              buffer = buffer.slice(bStart)
              if (buffer.length > PARSE_BUFFER_MAX) buffer = new Uint8Array(0)
              return
            }
            const headers = readHeaders(cursor, hEnd)
            const bodyStart = hEnd + 4
            const declared = Number(headers['content-length'])
            let bodyEnd = -1
            let next = -1
            if (Number.isFinite(declared) && declared > 0) {
              if (buffer.length < bodyStart + declared) {
                buffer = buffer.slice(bStart)          // 体还没收全，等下一块
                return
              }
              bodyEnd = bodyStart + declared
              next = bodyEnd
            } else {
              const nb = findBoundary(bodyStart)
              if (nb < 0) {
                buffer = buffer.slice(bStart)
                return
              }
              bodyEnd = nb
              if (bodyEnd >= 2 && buffer[bodyEnd - 2] === 13 && buffer[bodyEnd - 1] === 10) bodyEnd -= 2
              else if (bodyEnd >= 1 && buffer[bodyEnd - 1] === 10) bodyEnd -= 1
              next = nb
            }
            // 复制一份：buffer 马上会被 slice 掉，别让 view 指到旧内存上。
            const body = buffer.slice(bodyStart, bodyEnd)
            buffer = buffer.slice(next)
            onPart(headers, body, bodyStart, bodyEnd)
            if (!buffer.length) return
          }
        },
        reset() { buffer = new Uint8Array(0) },
        /** 仅供测试/诊断：还没解析完的字节数。 */
        pending() { return buffer.length },
      }
    }

    /** 空统计对象：所有观测字段在这里定义（perf 脚本读的就是这些）。 */
    function emptyStats() {
      return {
        transport: '',        // 'stream' | 'poll'（当前传输方式；轮询是兜底）
        fps: 0,               // 最近 3 秒**真正画出来**的内容帧率（去重后）
        sourceFps: 0,         // 最近 3 秒服务端帧号（X-DSH-Seq）推进的速度
        received: 0,          // 收到多少个 part/帧
        drawn: 0,             // 真的画了多少帧
        dropped: 0,           // 被丢掉的中间帧（最新帧优先的代价，越小越好）
        decodeFails: 0,       // 解码失败次数
        bytes: 0,             // 收到的 JPEG 总字节
        bytesPerSec: 0,       // 最近 3 秒的带宽
        latencyMs: -1,        // 最后一帧：抓帧时刻 → 画到屏幕（同一台机器，时钟可比）
        latencyP50: -1,       // 最近 60 帧的 p50 延迟
        seq: -1,              // 最后一帧的 X-DSH-Seq
        reconnects: 0,        // 流重连次数
        hidden: false,        // 是否因为标签页隐藏而暂停
        profile: 'auto',      // 当前档位（用户选的或自动降的）
        serviceFps: 0,        // 服务端自报的 fps（/stream-config 或 /stats 回包里有就填）
        quality: 0,
        scale: 0,
        dpr: 1,
      }
    }

    /** 帧率窗口：只留最近 FPS_WINDOW_MS 的样本，算"内容帧率"。 */
    function makeWindow() {
      return { drawn: [], seqs: [], bytes: [], lat: [] }
    }
    function pruneWindow(list, now, keepMs) {
      const limit = now - keepMs
      let cut = 0
      while (cut < list.length && list[cut].t < limit) cut += 1
      if (cut > 0) list.splice(0, cut)
    }
    function rateOf(list) {
      if (list.length < 2) return 0
      const span = list[list.length - 1].t - list[0].t
      if (!(span > 0)) return 0
      return Math.round(((list.length - 1) * 1000 / span) * 10) / 10
    }
    function median(list) {
      if (!list.length) return -1
      const sorted = list.slice().sort((a, b) => a - b)
      const mid = Math.floor(sorted.length / 2)
      return sorted.length % 2 ? sorted[mid] : Math.round((sorted[mid - 1] + sorted[mid]) / 2)
    }

    /* ---------------------------------------------------------------- 组件 */

    function Display(props) {
      const sid = (props && typeof props.sessionId === 'string') ? props.sessionId : ''
      const [lang] = useState(pickLang)
      const t = TEXT[lang] || TEXT.zh

      /** 状态机：checking | need-service | streaming | error（sid 为空时渲染"没有会话"卡片）。 */
      const [phase, setPhase] = useState('checking')
      const [problem, setProblem] = useState(null)   // {kind, status, hint, wait}
      const [info, setInfo] = useState(null)         // normalizeInfo 的结果
      const [snapshot, setSnapshot] = useState(null) // normalizeState 的结果
      const [attempt, setAttempt] = useState(0)
      const [starting, setStarting] = useState(false)
      const [startError, setStartError] = useState('')
      const [keyReady, setKeyReady] = useState(false)
      const [inputLost, setInputLost] = useState(false)
      const [stateLost, setStateLost] = useState(false)
      /** 是否已经画过至少一帧：没画过时给骨架屏 + "等待第一帧"，不假装在放画面。 */
      const [hasFrame, setHasFrame] = useState(false)
      /** 观测快照（500ms 一次）：状态条上的 fps/丢帧/带宽/档位都来自它。 */
      const [stats, setStats] = useState(emptyStats)
      /** 视图：适应窗口 / 1:1、缩放、平滑。 */
      const [view, setView] = useState({ mode: 'fit', zoom: 1, smooth: true, fullscreen: false })
      /** 传输方式（流式 / 轮询）—— 显示出来，用户与 perf 脚本都能对得上。 */
      const [transport, setTransport] = useState('')
      /** 断线重连倒计时：{at, wait, left}。 */
      const [reconnect, setReconnect] = useState(null)
      /** 档位：auto | smooth | saver。 */
      const [profile, setProfile] = useState('auto')
      /** 短暂提示（自动降档/切档），走 ref 记时间，由 500ms tick 清掉，不留额外定时器。 */
      const [notice, setNotice] = useState('')
      /** 正在本地平移查看（Shift+拖动 / 中键拖动）。 */
      const [panning, setPanning] = useState(false)

      const canvasRef = useRef(null)
      const stageRef = useRef(null)
      const sinkRef = useRef(null)
      /** contain/缩放之后的**实际画面矩形**（canvas 内的 CSS 像素），输入坐标换算必须用它。 */
      const drawRectRef = useRef(null)
      /** 最近一次绘制的几何（缩放/平移的镜头运算要用）。 */
      const geomRef = useRef(null)
      /** 光标（归一化）：**帧头 X-DSH-Cursor 优先**，/state 只是兜底。 */
      const cursorRef = useRef(null)
      const cursorFrameAtRef = useRef(0)
      /** 画面自然尺寸：帧头 X-DSH-Size 优先，/state 兜底（首帧之前也能算几何）。 */
      const sizeRef = useRef(null)
      /** 出站输入队列：串行发，保证服务端收到的事件顺序与用户操作一致。 */
      const queueRef = useRef([])
      const inflightRef = useRef(false)
      const aliveRef = useRef(true)
      const ctrlRef = useRef(null)
      const composingRef = useRef(false)
      const sidRef = useRef(sid)
      const probeRef = useRef(null)
      const frameFailRef = useRef(null)
      /** 已解码的最新帧（重画/缩放都复用它，不用等下一帧）。 */
      const frameRef = useRef(null)
      /** 最新待解码帧 = 1：解码排队时直接覆盖（"最新帧优先"就靠这里丢中间帧）。 */
      const pendingRef = useRef(null)
      const rafRef = useRef(0)
      const ripplesRef = useRef([])
      const statsRef = useRef(emptyStats())
      const viewRef = useRef({ mode: 'fit', zoom: 1, panX: 0, panY: 0, smooth: true, fullscreen: false })
      const profileRef = useRef('auto')
      const noticeRef = useRef(null)
      /** 引擎注入的重画钩子（工具条/输入改视图后立刻重画，不等下一帧）。 */
      const redrawRef = useRef(null)
      /** 引擎控制面：工具条与输入处理用它。 */
      const engineRef = useRef(null)
      const hasFrameRef = useRef(false)

      useEffect(() => { sidRef.current = sid }, [sid])

      // 生命周期：换会话/卸载时断掉在途请求、清空队列、关掉位图与 rAF，别留悬挂任务。
      useEffect(() => {
        aliveRef.current = true
        queueRef.current = []
        inflightRef.current = false
        composingRef.current = false
        drawRectRef.current = null
        geomRef.current = null
        cursorRef.current = null
        cursorFrameAtRef.current = 0
        sizeRef.current = null
        hasFrameRef.current = false
        ripplesRef.current = []
        statsRef.current = emptyStats()
        ctrlRef.current = (typeof AbortController === 'function') ? new AbortController() : null
        return () => {
          aliveRef.current = false
          queueRef.current = []
          pendingRef.current = null
          if (frameRef.current) {
            if (frameRef.current.bitmap) closeBitmap(frameRef.current.src)
            frameRef.current = null
          }
          if (rafRef.current) {
            try { if (typeof cancelAnimationFrame === 'function') cancelAnimationFrame(rafRef.current) } catch (error) { /* 已取消 */ }
            rafRef.current = 0
          }
          ripplesRef.current = []
          if (ctrlRef.current) { try { ctrlRef.current.abort() } catch (error) { /* 已结束 */ } }
          ctrlRef.current = null
        }
      }, [sid])

      // 样式 + 版本横幅。换会话时重新打一遍，日志里好对上。
      useEffect(() => {
        ensureStyle()
        try { console.log('[dsh-display-panel] build', BUILD, 'lang', lang, 'sid', sid || '(none)') } catch (error) { /* 无所谓 */ }
      }, [sid, lang])

      /* ------------------------------------------------------- 视图（缩放/平移/平滑） */

      /** 把视图补丁写进 ref + state，并立刻重画（不等下一帧）。 */
      function applyView(patch) {
        const next = Object.assign({}, viewRef.current, patch || {})
        next.zoom = clamp(next.zoom, ZOOM_MIN, ZOOM_MAX)
        next.mode = next.mode === 'one' ? 'one' : 'fit'
        next.smooth = next.smooth !== false
        viewRef.current = next
        setView({
          mode: next.mode, zoom: next.zoom, smooth: next.smooth,
          fullscreen: next.fullscreen === true,
        })
        if (redrawRef.current) redrawRef.current()
      }

      /** 本地平移（只有画面比容器大时才有意义；越界由绘制时的几何夹住）。 */
      function panBy(dx, dy) {
        const v = viewRef.current
        v.panX = (v.panX || 0) + dx
        v.panY = (v.panY || 0) + dy
        if (redrawRef.current) redrawRef.current()
      }

      /** 以指针为锚点缩放：那个像素缩放前后停在同一个屏幕位置。 */
      function zoomAtPoint(clientX, clientY, factor) {
        const canvas = canvasRef.current
        const v = viewRef.current
        const geom = geomRef.current
        const nextZoom = clamp((v.zoom || 1) * factor, ZOOM_MIN, ZOOM_MAX)
        if (canvas && geom && geom.dw > 0 && geom.dh > 0) {
          let rect = null
          try { rect = canvas.getBoundingClientRect() } catch (error) { rect = null }
          const px = clientX - ((rect && rect.left) || 0)
          const py = clientY - ((rect && rect.top) || 0)
          const ix = (px - geom.dx) / geom.dw
          const iy = (py - geom.dy) / geom.dh
          const scale = v.mode === 'one' ? nextZoom : geom.fitScale * nextZoom
          const dw2 = geom.iw * scale
          const dh2 = geom.ih * scale
          v.panX = (px - ix * dw2) - (geom.cssW - dw2) / 2
          v.panY = (py - iy * dh2) - (geom.cssH - dh2) / 2
        }
        applyView({ zoom: nextZoom })
      }

      /** 点击涟漪：纯本地视觉反馈（不影响注入的事件）。 */
      function pushRipple(clientX, clientY) {
        const canvas = canvasRef.current
        if (!canvas) return
        let rect = null
        try { rect = canvas.getBoundingClientRect() } catch (error) { rect = null }
        if (!rect) return
        const list = ripplesRef.current
        list.push({ x: clientX - rect.left, y: clientY - rect.top, t0: nowMs() })
        while (list.length > 12) list.shift()
        if (redrawRef.current) redrawRef.current()
      }

      function setNoticeNow(text) {
        noticeRef.current = { text: text, at: Date.now() }
        setNotice(text)
      }

      function toggleFullscreen() {
        const stage = stageRef.current
        const doc = document
        try {
          if (doc.fullscreenElement) {
            if (doc.exitFullscreen) { const p = doc.exitFullscreen(); if (p && p.catch) p.catch(() => {}) }
          } else if (stage && stage.requestFullscreen) {
            const p = stage.requestFullscreen()
            if (p && p.catch) p.catch(() => { setNoticeNow(t.fullscreenHint) })
          }
        } catch (error) { /* 宿主环境不支持全屏：按钮点不动也不报错 */ }
      }

      // 全屏状态跟随浏览器（用户按 Esc 时按钮要跟着回到未选中）。
      useEffect(() => {
        if (typeof document === 'undefined' || !document.addEventListener) return undefined
        const sync = () => {
          const on = !!document.fullscreenElement
          if (viewRef.current.fullscreen !== on) applyView({ fullscreen: on })
        }
        document.addEventListener('fullscreenchange', sync)
        return () => { try { document.removeEventListener('fullscreenchange', sync) } catch (error) { /* 无所谓 */ } }
      }, [])

      /**
       * 出站队列：**串行**发送（契约 §1.3 要求同一会话按到达顺序执行）。
       * 连续的 move 会就地合并 —— 鼠标快速划动不必每个像素都占一个 HTTP 往返，
       * 但 down/up/wheel/key/text 一个都不许被合并掉。
       */
      async function pump() {
        if (inflightRef.current) return
        inflightRef.current = true
        let fails = 0
        try {
          while (aliveRef.current && queueRef.current.length) {
            const event = queueRef.current.shift()
            const ctrl = ctrlRef.current
            let ok = false
            try {
              const res = await fetch(API + '/input?session=' + encodeURIComponent(sidRef.current), {
                method: 'POST',
                headers: { 'content-type': 'application/json' },
                body: JSON.stringify(event),
                cache: 'no-store',
                signal: ctrl ? ctrl.signal : undefined,
              })
              ok = !!res && res.ok
            } catch (error) { ok = false }
            if (!aliveRef.current) return
            fails = ok ? 0 : (fails + 1)
            if (fails >= 5) setInputLost(true)
            else if (ok && fails === 0) setInputLost(false)
          }
        } finally {
          inflightRef.current = false
        }
      }

      function push(payload) {
        const queue = queueRef.current
        const last = queue.length ? queue[queue.length - 1] : null
        if (payload.t === 'move' && last && last.t === 'move') queue[queue.length - 1] = payload
        else if (queue.length > 256) queue.shift()   // 兜底：极端情况下丢最旧的，别让队列无限长
        else queue.push(payload)
        void pump()
      }

      /**
       * 事件坐标 → 0..1 归一化（相对画面本身，不是相对元素边框）。
       * 用缩放/平移之后的实际画面矩形（drawRectRef，CSS 像素）换算：
       * 元素比画面宽/高时两侧是留白，点在留白上会被夹到 0 或 1。
       */
      function normPoint(canvas, event) {
        if (!canvas || !event || typeof event.clientX !== 'number') return null
        let rect = null
        try { rect = canvas.getBoundingClientRect() } catch (error) { rect = null }
        const box = (rect && rect.width > 0) ? rect : null
        const draw = drawRectRef.current
        const hasDraw = !!(draw && draw.dw > 0 && draw.dh > 0)
        const dw = hasDraw ? draw.dw : (box ? box.width : 1)
        const dh = hasDraw ? draw.dh : (box ? box.height : 1)
        const dx = hasDraw ? draw.dx : 0
        const dy = hasDraw ? draw.dy : 0
        return {
          x: clamp01((event.clientX - (box ? box.left : 0) - dx) / dw),
          y: clamp01((event.clientY - (box ? box.top : 0) - dy) / dh),
        }
      }

      /** DOM 事件 → 契约里的键名：`ctrl+shift+A`、`Enter`、`a`…（meta 记作 super）。 */
      function keySpec(event) {
        const key = event && event.key
        if (!key) return ''
        // 单独按下修饰键不发送：修饰键通过前缀表达，单独发一个 "Shift" 只会污染远端。
        const bare = ['Control', 'Shift', 'Alt', 'Meta', 'AltGraph', 'CapsLock', 'NumLock', 'ScrollLock', 'Dead', 'Unidentified']
        if (bare.indexOf(key) >= 0) return ''
        const parts = []
        if (event.ctrlKey) parts.push('ctrl')
        if (event.shiftKey) parts.push('shift')
        if (event.altKey) parts.push('alt')
        if (event.metaKey) parts.push('super')
        parts.push(key)
        return parts.join('+')
      }

      /** DOM button → 契约的 b（1 左 2 右 3 中）。 */
      function buttonOf(button) {
        if (button === 0) return 1
        if (button === 1) return 3
        if (button === 2) return 2
        return 0
      }

      /* ------------------------------------------------------- 引擎：/info 轮询 + 状态机 */

      useEffect(() => {
        if (!sid) {
          setPhase('checking')
          setInfo(null)
          setSnapshot(null)
          setHasFrame(false)
          hasFrameRef.current = false
          setStats(emptyStats())
          return undefined
        }
        let alive = true
        let tries = 0
        const timers = new Set()
        const aborts = new Set()
        const later = (ms, fn) => {
          const id = setTimeout(() => { timers.delete(id); if (alive) fn() }, ms)
          timers.add(id)
        }
        const request = async (path, init) => {
          const ctrl = new AbortController()
          aborts.add(ctrl)
          try {
            return await fetch(API + path, Object.assign({ cache: 'no-store' }, init || {}, { signal: ctrl.signal }))
          } finally { aborts.delete(ctrl) }
        }
        /** 失败 → 分类 + 退避重试。分类决定文案，不能只会说"显示器还没有打开"。 */
        const fail = (kind, status, hint) => {
          if (!alive) return
          tries += 1
          const wait = backoffDelay(tries)
          setAttempt(tries)
          setProblem({ kind, status: status || 0, hint: hint || '', wait })
          setPhase('error')
          later(wait, probe)
        }
        const probe = async () => {
          if (!alive) return
          let res
          try {
            res = await request('/info')
          } catch (error) {
            fail('net', 0, String((error && error.message) || error || ''))
            return
          }
          if (!alive) return
          const json = await readJsonSafe(res)
          if (!alive) return
          if (!res.ok) {
            const hint = json && typeof json === 'object' ? (json.hint || json.error || '') : ''
            if (res.status === 401 || res.status === 403) fail('auth', res.status, '')
            else if (res.status === 404) fail('api', res.status, hint)
            else fail('api', res.status, hint)
            return
          }
          const next = normalizeInfo(json)
          tries = 0
          setAttempt(0)
          setProblem(null)
          setInfo(next)
          if (next.running) {
            setPhase('streaming')
            later(INFO_OK_MS, probe)
          } else {
            // 服务没起：这不是"错误"，是一个有按钮可点的正常状态。
            setPhase('need-service')
            later(INFO_WAIT_MS, probe)
          }
        }
        // 给按钮/外部用的两把钩子：立刻重探、上报拉帧失败。
        probeRef.current = () => { tries = 0; setAttempt(0); void probe() }
        frameFailRef.current = (status, hint) => {
          if (!status) fail('net', 0, hint || '')
          else if (status === 401 || status === 403) fail('auth', status, '')
          else if (status === 503) fail('service', status, hint || '')
          else if (status === 404) fail('frame', status, hint || '')
          else fail('frame', status, hint || '')
        }
        void probe()
        return () => {
          alive = false
          probeRef.current = null
          frameFailRef.current = null
          for (const id of timers) clearTimeout(id)
          timers.clear()
          for (const ctrl of aborts) { try { ctrl.abort() } catch (error) { /* 已结束 */ } }
          aborts.clear()
        }
      }, [sid])

      /* ------------------------------------------------------- 引擎：流/轮询 → 解码 → rAF 绘制 */

      useEffect(() => {
        if (phase !== 'streaming' || !sid) return undefined

        let alive = true
        const timers = new Set()
        /** 流/轮询的在途请求：hidden 时全断（契约 §5.4 省电）。 */
        const aborts = new Set()
        /** stream-config 之类的小请求：hidden 时也要让它发完。 */
        const softAborts = new Set()
        const win = makeWindow()
        const parser = createMjpegParser()
        let mode = 'stream'
        let reader = null
        let streamWait = STREAM_RETRY_MIN_MS
        let streamFails = 0
        let reconnectAt = 0
        let reconnectWait = 0
        let reprobePending = false
        let pollStrikes = 0
        let pollBusy = false
        let decodeBusy = false
        let adaptAt = 0
        //: 「自动」档因为实测慢而降过一次档 —— 之后要能自己爬回来（见 maybeRecover）。
        let autoPicked = false
        //: 降档次数：爬回来又掉下去的话，下次等更久再试（退避），避免来回抖。
        let downgrades = 0
        //: 上一次处于「自动」档且健康的时刻（连续健康够久就把退避计数清零）。
        let autoHealthyAt = 0
        let statsStartAt = nowMs()
        let lastTransport = ''
        let lastPaintTime = -1

        const later = (ms, fn) => {
          const id = setTimeout(() => { timers.delete(id); if (alive) fn() }, ms)
          timers.add(id)
          return id
        }

        const publishStats = (extra) => {
          try {
            window.__ddpStats = Object.assign({}, statsRef.current, {
              build: BUILD, sid: sid, phase: phase, ts: Date.now(),
            }, extra || {})
          } catch (error) { /* 没有 window（不该发生）：观测不上就算了 */ }
        }

        const setTransportNow = (value) => {
          statsRef.current.transport = value
          if (lastTransport !== value) { lastTransport = value; setTransport(value) }
        }

        /* ---------------------------------------------------- 绘制 */

        /** 镜头几何：适应窗口 / 1:1 × 缩放 × 平移，并把夹住后的平移写回视图。 */
        const computeGeom = (cssW, cssH, iw, ih) => {
          const v = viewRef.current
          const fitScale = Math.min(cssW / iw, cssH / ih)
          const zoom = clamp(v.zoom || 1, ZOOM_MIN, ZOOM_MAX)
          const scale = v.mode === 'one' ? zoom : fitScale * zoom
          const dw = iw * scale
          const dh = ih * scale
          const centerX = (cssW - dw) / 2
          const centerY = (cssH - dh) / 2
          // 画面比容器大：可以平移，但不许把画面拖出留白（边界刚好贴住）。
          const dx = dw > cssW ? clamp(centerX + (v.panX || 0), cssW - dw, 0) : centerX
          const dy = dh > cssH ? clamp(centerY + (v.panY || 0), cssH - dh, 0) : centerY
          v.panX = dx - centerX
          v.panY = dy - centerY
          return { dx: dx, dy: dy, dw: dw, dh: dh, scale: scale, fitScale: fitScale, cssW: cssW, cssH: cssH, iw: iw, ih: ih }
        }

        /** 光标**精灵**（不是十字线）：白色箭头 + 深色描边，深浅底都看得清。 */
        const drawCursor = (c2d, geom) => {
          const cursor = cursorRef.current
          if (!cursor || cursor.hide) return
          const x = geom.dx + cursor.x * geom.dw
          const y = geom.dy + cursor.y * geom.dh
          if (!Number.isFinite(x) || !Number.isFinite(y)) return
          c2d.save()
          c2d.translate(x, y)
          c2d.beginPath()
          c2d.moveTo(0, 0)
          c2d.lineTo(0, 17)
          c2d.lineTo(4.4, 13.1)
          c2d.lineTo(7.4, 19.6)
          c2d.lineTo(10.4, 18.2)
          c2d.lineTo(7.4, 11.9)
          c2d.lineTo(12.2, 11.9)
          c2d.closePath()
          c2d.shadowColor = 'rgba(0,0,0,.45)'
          c2d.shadowBlur = 3
          c2d.fillStyle = 'rgba(250,251,253,.98)'
          c2d.fill()
          c2d.shadowBlur = 0
          c2d.lineWidth = 1.2
          c2d.strokeStyle = 'rgba(15,17,21,.92)'
          c2d.stroke()
          c2d.restore()
        }

        /** 点击涟漪：扩散的圆环 + 芯（纯本地视觉，不占输入队列）。 */
        const drawRipples = (c2d) => {
          const list = ripplesRef.current
          if (!list.length) return
          const now = nowMs()
          for (let i = list.length - 1; i >= 0; i -= 1) {
            const item = list[i]
            const age = now - item.t0
            if (!(age >= 0) || age > RIPPLE_MS) { list.splice(i, 1); continue }
            const p = age / RIPPLE_MS
            const radius = 4 + p * 26
            const alpha = (1 - p) * 0.6
            c2d.save()
            c2d.beginPath()
            c2d.arc(item.x, item.y, radius, 0, Math.PI * 2)
            c2d.lineWidth = 2
            c2d.strokeStyle = 'rgba(255,255,255,' + alpha.toFixed(3) + ')'
            c2d.stroke()
            c2d.beginPath()
            c2d.arc(item.x, item.y, Math.max(1.5, radius * 0.22), 0, Math.PI * 2)
            c2d.fillStyle = 'rgba(77,147,248,' + (alpha * 0.95).toFixed(3) + ')'
            c2d.fill()
            c2d.restore()
          }
        }

        /**
         * 画一帧（rAF 统一入口）：DPR 后备缓冲 + 主题留白 + 缩放 + 光标 + 涟漪。
         * 不在画面里的东西一律不画；没有 2d 上下文（jsdom）就安静地什么都不做。
         */
        const drawNow = () => {
          if (!alive) return
          const canvas = canvasRef.current
          if (!canvas) return
          let c2d = null
          try { c2d = canvas.getContext('2d') } catch (error) { c2d = null }
          if (!c2d) return
          const frame = frameRef.current
          const size = sizeRef.current
          const iw = frame ? frame.w : (size ? size.w : 0)
          const ih = frame ? frame.h : (size ? size.h : 0)
          if (!(iw > 0) || !(ih > 0)) return
          const dpr = dprNow()
          const parent = canvas.parentNode
          const cssW = Math.max(1, Math.round(canvas.clientWidth || (parent && parent.clientWidth) || iw))
          const cssH = Math.max(1, Math.round(canvas.clientHeight || (parent && parent.clientHeight) || ih))
          const bw = Math.max(1, Math.round(cssW * dpr))
          const bh = Math.max(1, Math.round(cssH * dpr))
          if (canvas.width !== bw || canvas.height !== bh) { canvas.width = bw; canvas.height = bh }
          const geom = computeGeom(cssW, cssH, iw, ih)
          try {
            c2d.setTransform(dpr, 0, 0, dpr, 0, 0)
            const smooth = viewRef.current.smooth !== false
            c2d.imageSmoothingEnabled = smooth
            try { c2d.imageSmoothingQuality = smooth ? 'high' : 'low' } catch (error) { /* 旧实现没有这个属性 */ }
            // 留白**不填色**：让 .ddp-stage 的主题底色透出来（浅色主题下不再是一块死黑）。
            c2d.clearRect(0, 0, cssW, cssH)
            if (frame) c2d.drawImage(frame.src, geom.dx, geom.dy, geom.dw, geom.dh)
            drawCursor(c2d, geom)
            drawRipples(c2d)
          } catch (error) {
            return
          }
          drawRectRef.current = { dx: geom.dx, dy: geom.dy, dw: geom.dw, dh: geom.dh }
          geomRef.current = geom
          const s = statsRef.current
          s.dpr = dpr
          if (frame) {
            if (Number.isFinite(frame.seq) && frame.seq >= 0) s.seq = frame.seq
            if (!frame.painted) {
              frame.painted = true
              s.drawn += 1
              win.drawn.push({ t: nowMs(), v: 1 })
              // 延迟只按**新的抓帧时刻**记一次：服务端的"仅光标变化"帧复用同一张 JPEG
              // （X-DSH-Time 是老时刻），拿它们算延迟会把 p50 灌水。
              if (frame.time > 0 && frame.time !== lastPaintTime) {
                lastPaintTime = frame.time
                const lat = Date.now() - frame.time
                if (lat >= 0 && lat < 60000) {
                  s.latencyMs = Math.round(lat)
                  win.lat.push({ t: nowMs(), v: s.latencyMs })
                  while (win.lat.length > 60) win.lat.shift()
                }
              }
              if (!hasFrameRef.current) { hasFrameRef.current = true; setHasFrame(true) }
            } else {
              s.repaints += 1
            }
          }
        }

        const onRaf = () => {
          rafRef.current = 0
          try { drawNow() } catch (error) { /* 画不出来不影响拉流 */ }
          if (alive && ripplesRef.current.length) redraw()
        }

        /** 需要重画时排一次 rAF（同一时刻只排一个；空闲时一个都不排）。 */
        const redraw = () => {
          if (!alive || rafRef.current) return
          try {
            if (typeof requestAnimationFrame === 'function') rafRef.current = requestAnimationFrame(onRaf)
          } catch (error) { rafRef.current = 0 }
        }
        redrawRef.current = redraw

        /* ---------------------------------------------------- 解码（最新帧优先） */

        /** 解码一个 blob：createImageBitmap 优先，退回 objectURL + <img>；都带超时。 */
        const decodeBlob = async (blob) => {
          if (typeof window.createImageBitmap === 'function') {
            const bitmap = await withTimeout(window.createImageBitmap(blob), DECODE_TIMEOUT_MS, closeBitmap)
            if (bitmap) return { src: bitmap, w: bitmap.width, h: bitmap.height, bitmap: true }
            if (!alive) return null
          }
          let objectUrl = null
          try {
            if (typeof URL !== 'undefined' && typeof URL.createObjectURL === 'function') objectUrl = URL.createObjectURL(blob)
          } catch (error) { objectUrl = null }
          if (!objectUrl) return null
          const img = new Image()
          const loaded = await withTimeout(new Promise((resolve) => {
            img.onload = () => resolve(img)
            img.onerror = () => resolve(null)
            try { img.src = objectUrl } catch (error) { resolve(null) }
          }), DECODE_TIMEOUT_MS)
          try { URL.revokeObjectURL(objectUrl) } catch (error) { /* 已释放 */ }
          if (!alive || !loaded) return null
          const w = loaded.naturalWidth || loaded.width || 0
          const h = loaded.naturalHeight || loaded.height || 0
          if (!(w > 0) || !(h > 0)) return null
          return { src: loaded, w: w, h: h, bitmap: false }
        }

        const closeFrame = (frame) => {
          if (frame && frame.bitmap) closeBitmap(frame.src)
        }

        /** 同一时刻只解一个；解完立刻看还有没有更新的（有就继续，中间那些已经被覆盖掉了）。 */
        const pumpDecode = async () => {
          if (!alive || decodeBusy) return
          const job = pendingRef.current
          if (!job) return
          pendingRef.current = null
          decodeBusy = true
          let decoded = null
          try {
            decoded = await decodeBlob(job.blob)
          } catch (error) { decoded = null }
          decodeBusy = false
          if (!alive) {
            if (decoded) closeFrame(decoded)
            return
          }
          if (!decoded) {
            statsRef.current.decodeFails += 1
          } else {
            const prev = frameRef.current
            if (prev) {
              // 解出来还没画就被更新的帧顶掉 → 这一帧白解了，记进丢帧（这正是"最新帧优先"的代价）
              if (!prev.painted) statsRef.current.dropped += 1
              closeFrame(prev)
            }
            frameRef.current = {
              src: decoded.src, bitmap: decoded.bitmap, w: decoded.w, h: decoded.h,
              seq: Number.isFinite(job.seq) ? job.seq : -1,
              time: Number.isFinite(job.time) ? job.time : 0,
              painted: false,
            }
            redraw()
          }
          if (pendingRef.current) void pumpDecode()
        }

        /** 收下一帧：待解码槽只有一个 —— 覆盖就是丢中间帧（绝不为了"每帧都画"堆延迟）。 */
        const pushFrame = (job) => {
          if (!alive) return
          const s = statsRef.current
          const stamp = nowMs()
          s.received += 1
          s.bytes += job.bytes || 0
          win.bytes.push({ t: stamp, v: job.bytes || 0 })
          if (Number.isFinite(job.seq) && job.seq >= 0) {
            // 服务端帧号推进速度：和"画出来的帧率"分开看，才分得清是源慢还是客户端丢帧。
            win.seqs.push({ t: stamp, v: job.seq })
            s.seq = job.seq
          }
          if (job.cursor) {
            // 光标跟着帧走：即使这一帧的解码被后来的帧顶掉，位置也已经更新了。
            cursorRef.current = job.cursor
            cursorFrameAtRef.current = Date.now()
          }
          if (job.size) sizeRef.current = job.size
          if (pendingRef.current) s.dropped += 1
          pendingRef.current = job
          void pumpDecode()
        }

        /** 一个 MJPEG part → 一帧（头里的 X-DSH-* 是契约 §5.3 冻结字段）。 */
        const onPart = (headers, body) => {
          if (!alive || !body || !body.length) return
          const type = String(headers['content-type'] || '').toLowerCase()
          if (type && type.indexOf('image') < 0) return       // 心跳/文本 part：跳过，别当帧
          if (!looksLikeJpeg(body)) return
          const seq = Number(headers['x-dsh-seq'])
          const time = Number(headers['x-dsh-time'])
          pushFrame({
            blob: new Blob([body], { type: 'image/jpeg' }),
            bytes: body.length,
            seq: Number.isFinite(seq) ? seq : -1,
            time: Number.isFinite(time) ? time : NaN,
            cursor: parseCursorHeader(headers['x-dsh-cursor']),
            size: parseSizeHeader(headers['x-dsh-size']),
          })
        }

        /* ---------------------------------------------------- 观测 / 自适应档位 */

        const sendStreamConfig = async (name, reason) => {
          // name = 本地档位（auto|smooth|saver|paused），reason 只留给日志/调试，都不进 body。
          const cfg = PROFILES[name] || PROFILES.auto
          const ctrl = new AbortController()
          softAborts.add(ctrl)
          try {
            const res = await fetch(API + STREAM_CONFIG_PATH + '?session=' + encodeURIComponent(sid), {
              method: 'POST',
              headers: { 'content-type': 'application/json' },
              cache: 'no-store',
              signal: ctrl.signal,
              // ⚠️ 只发契约 §5.2 的三个字段：宿主/上游只放行 quality/fps/scale，
              // 多发 profile/reason 会被丢掉（等于白发，还让服务端多解析一次）。
              body: JSON.stringify({ quality: cfg.quality, fps: cfg.fps, scale: cfg.scale }),
            })
            if (res && res.ok) {
              const json = await readJsonSafe(res)
              const applied = (json && (json.stream || json.config || json.applied)) || null
              const s = statsRef.current
              if (applied && typeof applied === 'object') {
                if (Number.isFinite(Number(applied.fps))) s.serviceFps = Number(applied.fps)
                if (Number.isFinite(Number(applied.quality))) s.quality = Number(applied.quality)
                if (Number.isFinite(Number(applied.scale))) s.scale = Number(applied.scale)
              }
            }
          } catch (error) { /* 宿主没有这个接口就算了：档位只是优化，不是功能 */ }
          finally { softAborts.delete(ctrl) }
          statsRef.current.profile = name
        }

        /** 实测跟不上就请求降档（只在"自动"档、有足够样本、且过了冷却期时）。 */
        const maybeAdapt = () => {
          const s = statsRef.current
          if (profileRef.current !== 'auto' || s.transport !== 'stream' || s.hidden) return
          if (s.received < 30) return
          const now = Date.now()
          if (now - adaptAt < ADAPT_COOLDOWN_MS) return
          const slow = (s.fps > 0 && s.fps < 6) || (s.dropped > s.received * 0.5 && s.fps < 12)
          if (!slow) return
          adaptAt = now
          autoPicked = true
          downgrades += 1
          setNoticeNow(t.autoDowngrade)
          profileRef.current = 'saver'
          setProfile('saver')
          void sendStreamConfig('saver', 'auto-slow')
        }

        /**
         * 自动降档之后**自己爬回来**。
         *
         * 为什么必须有：降档是一次性的单向开关时，只要用户开面板的那一刻机器忙
         * （同伴 agent、编译、跑测试……），面板就会被永久压在「省流」（8fps / 半分辨率），
         * 之后再空闲也不会恢复 —— 恰恰把"更流畅"这件事毁掉。实测踩到：并行跑测量时
         * 面板被压到 userFps=8，负载结束 20 分钟仍是 8。
         *
         * 判据：交付帧率贴着**当前档的上限**（≥90%）说明瓶颈不在这一档 —— 值得试一次更高的档。
         * 试失败（又降档）就按 2 的幂退避更久再试；连续健康 5 分钟则清零，回到正常节奏。
         * 用户**手动**选的档永远不会被这段逻辑改（autoPicked 只在自己降档时置位）。
         */
        const maybeRecover = () => {
          const s = statsRef.current
          if (!autoPicked) return
          if (s.transport !== 'stream' || s.hidden || s.received < 30) return
          const now = Date.now()
          const wait = RECOVER_WAIT_MS * Math.min(2 ** Math.max(0, downgrades - 1), 8)
          if (now - adaptAt < wait) return
          const cap = (PROFILES[profileRef.current] || PROFILES.auto).fps
          if (!(s.fps >= Math.max(6, cap * 0.9))) return   // 没贴着上限就别急着爬
          adaptAt = now
          autoPicked = false
          setNoticeNow(t.autoRecover)
          profileRef.current = 'auto'
          setProfile('auto')
          void sendStreamConfig('auto', 'auto-recover')
        }

        const tick = () => {
          if (!alive) return
          const s = statsRef.current
          const now = nowMs()
          pruneWindow(win.drawn, now, FPS_WINDOW_MS)
          pruneWindow(win.seqs, now, FPS_WINDOW_MS)
          pruneWindow(win.bytes, now, FPS_WINDOW_MS)
          s.fps = rateOf(win.drawn)
          s.sourceFps = rateOf(win.seqs)
          let bytes = 0
          for (const item of win.bytes) bytes += item.v
          const elapsed = Math.max(500, Math.min(FPS_WINDOW_MS, now - statsStartAt))
          s.bytesPerSec = Math.round(bytes * 1000 / elapsed)
          s.latencyP50 = median(win.lat.map((item) => item.v))
          s.hidden = false
          try { s.hidden = document.visibilityState === 'hidden' } catch (error) { s.hidden = false }
          s.profile = profileRef.current
          s.serviceFps = s.serviceFps || 0
          const cursor = cursorRef.current
          // 光标也进观测（perf 脚本要拿它算"光标跟手延迟"）。
          s.cursor = cursor ? { x: cursor.x, y: cursor.y, hide: cursor.hide === true } : null
          publishStats()
          setStats(Object.assign({}, s))
          if (reconnectAt) {
            const left = Math.max(0, Math.ceil((reconnectAt + reconnectWait - Date.now()) / 1000))
            setReconnect({ at: reconnectAt, wait: reconnectWait, left: left })
          }
          if (noticeRef.current && Date.now() - noticeRef.current.at > 4000) {
            noticeRef.current = null
            setNotice('')
          }
          maybeAdapt()
          maybeRecover()
          // 「自动」档连续健康 5 分钟：把退避计数清零，下次真出问题还是 20 秒就能试回来。
          if (profileRef.current === 'auto' && s.transport === 'stream' && !s.hidden && s.fps >= 12) {
            if (autoHealthyAt === 0) autoHealthyAt = Date.now()
            else if (Date.now() - autoHealthyAt > RECOVER_RESET_MS) { downgrades = 0; autoHealthyAt = Date.now() }
          } else {
            autoHealthyAt = 0
          }
        }

        /* ---------------------------------------------------- 传输：流优先，轮询兜底 */

        const stopTransport = () => {
          for (const id of timers) clearTimeout(id)
          timers.clear()
          reconnectAt = 0
          reconnectWait = 0
          reprobePending = false
          for (const ctrl of aborts) { try { ctrl.abort() } catch (error) { /* 已结束 */ } }
          aborts.clear()
          if (reader) {
            // cancel() 返回的 promise 会以 "BodyStreamBuffer was aborted" 拒绝：
            // 必须自己吞掉，否则控制台里留一条未捕获的 rejection。
            try {
              const p = reader.cancel()
              if (p && typeof p.catch === 'function') p.catch(() => {})
            } catch (error) { /* 已经断了 */ }
            reader = null
          }
          if (rafRef.current) {
            try { if (typeof cancelAnimationFrame === 'function') cancelAnimationFrame(rafRef.current) } catch (error) { /* 已取消 */ }
            rafRef.current = 0
          }
          pollBusy = false
          pendingRef.current = null
        }

        /** 流不可用/连不上：退回既有的 /frame 轮询（契约 §5.3 的兼容路径）。 */
        const toPoll = (reason) => {
          if (!alive) return
          mode = 'poll'
          parser.reset()
          setTransportNow('poll')
          if (reason === 'unsupported') {
            setNoticeNow(t.streamOff)
            // 上游可能是旧版服务：过一会儿再试一次流，升级后能自己回来。
            if (!reprobePending) {
              reprobePending = true
              later(STREAM_REPROBE_MS, () => { reprobePending = false; if (alive && mode === 'poll') void openStream() })
            }
          }
          pollStrikes = 0
          pollRun()
        }

        const reconnectLater = () => {
          const wait = streamWait
          streamWait = Math.min(STREAM_RETRY_MAX_MS, Math.round(streamWait * 2))
          reconnectAt = Date.now()
          reconnectWait = wait
          setReconnect({ at: reconnectAt, wait: wait, left: Math.ceil(wait / 1000) })
          later(wait, () => {
            reconnectAt = 0
            reconnectWait = 0
            setReconnect(null)
            void openStream()
          })
        }

        /** 打开 MJPEG 长连接（契约 §5.3）。 */
        const openStream = async () => {
          if (!alive) return
          let hidden = false
          try { hidden = document.visibilityState === 'hidden' } catch (error) { hidden = false }
          if (hidden) return
          mode = 'stream'
          parser.reset()
          const ctrl = new AbortController()
          aborts.add(ctrl)
          let res = null
          try {
            res = await fetch(API + STREAM_PATH + '?session=' + encodeURIComponent(sid) + '&t=' + Date.now(), {
              cache: 'no-store',
              signal: ctrl.signal,
              headers: { accept: 'multipart/x-mixed-replace' },
            })
          } catch (error) {
            aborts.delete(ctrl)
            if (!alive || ctrl.signal.aborted) return
            streamFails += 1
            if (streamFails >= FRAME_WARM_STRIKES) { streamFails = 0; toPoll('net'); return }
            later(FRAME_WARM_MS, () => { void openStream() })
            return
          }
          if (!alive || ctrl.signal.aborted) { aborts.delete(ctrl); return }
          if (!res || !res.ok) {
            const status = res ? res.status : 0
            let hint = ''
            try {
              const json = await res.json()
              hint = (json && (json.hint || json.error)) || ''
            } catch (error) { hint = '' }
            aborts.delete(ctrl)
            if (!alive || ctrl.signal.aborted) return
            if (status === 401 || status === 403) { if (frameFailRef.current) frameFailRef.current(status, hint); return }
            if (status === 404 || status === 405 || status === 406 || status === 400 || status === 415 || status === 501 || status === 505) {
              // 宿主/上游不支持流（旧版）：立刻退回轮询，别让用户干等。
              toPoll('unsupported')
              return
            }
            if (status === 503) {
              streamFails += 1
              if (streamFails >= FRAME_WARM_STRIKES) { streamFails = 0; toPoll('warm') ; return }
              later(FRAME_WARM_MS, () => { void openStream() })
              return
            }
            streamFails += 1
            if (streamFails >= STREAM_FAILS_TO_POLL) { streamFails = 0; toPoll('http'); return }
            reconnectLater()
            return
          }
          const ctype = String(res.headers.get('content-type') || '').toLowerCase()
          const body = res.body
          if (ctype.indexOf('multipart') < 0 || !body || typeof body.getReader !== 'function') {
            try { ctrl.abort() } catch (error) { /* 已结束 */ }
            aborts.delete(ctrl)
            pendingRef.current = null
            toPoll('no-body')
            return
          }
          // 连上了：清零退避，开始收帧。
          streamFails = 0
          streamWait = STREAM_RETRY_MIN_MS
          reconnectAt = 0
          reconnectWait = 0
          setReconnect(null)
          setTransportNow('stream')
          statsStartAt = nowMs()
          publishStats()
          try {
            reader = body.getReader()
            for (;;) {
              const step = await reader.read()
              if (!alive || ctrl.signal.aborted) { aborts.delete(ctrl); return }
              if (step.done) break
              if (step.value && step.value.length) {
                const value = step.value
                statsRef.current.bytes += value.length
                win.bytes.push({ t: nowMs(), v: value.length })
                parser.push(value, onPart)
              }
            }
          } catch (error) {
            if (!alive || ctrl.signal.aborted) { aborts.delete(ctrl); return }
          } finally {
            aborts.delete(ctrl)
            reader = null
          }
          if (!alive || ctrl.signal.aborted) return
          // 服务端把连接关了 / 读到一半断了：自己回来（用户不用刷新页面）。
          statsRef.current.reconnects += 1
          reconnectLater()
        }

        /** 轮询兜底（0.3.4 的老路径，语义保持不变：宽限期 + 退避 + 分类错误）。 */
        const pollRun = async () => {
          if (!alive || mode !== 'poll' || pollBusy) return
          let hidden = false
          try { hidden = document.visibilityState === 'hidden' } catch (error) { hidden = false }
          if (hidden) return
          pollBusy = true
          const started = Date.now()
          const ctrl = new AbortController()
          aborts.add(ctrl)
          let status = 0
          let hint = ''
          let ok = false
          let next = FRAME_MIN_MS
          try {
            const url = API + '/frame?session=' + encodeURIComponent(sid) + '&t=' + started
            const res = await fetch(url, { cache: 'no-store', signal: ctrl.signal })
            if (res.ok) {
              ok = true
              const blob = await res.blob()
              if (alive) pushFrame({ blob: blob, bytes: blob.size || 0, seq: -1, time: NaN, cursor: null, size: null })
            } else {
              status = res.status
              // 宿主失败时是 {ok:false,error,hint}；把它的原话带给用户（例如"还没有帧"）。
              try {
                const json = await res.json()
                hint = (json && (json.hint || json.error)) || ''
              } catch (error) { hint = '' }
            }
          } catch (error) {
            status = 0
          } finally {
            aborts.delete(ctrl)
            pollBusy = false
          }
          if (!alive || ctrl.signal.aborted) return
          if (ok) {
            pollStrikes = 0
            next = Math.min(FRAME_MAX_MS, Math.max(FRAME_MIN_MS, Date.now() - started))
          } else {
            // 401/403 立刻切错误（再拉也是白拉）。
            if (status === 401 || status === 403) {
              if (frameFailRef.current) frameFailRef.current(status, hint)
              return
            }
            pollStrikes += 1
            // 503 / 网络错误多半是"服务刚被拉起、上游还没抓到第一帧"（宿主实测头 1~2 秒如此）：
            // 给它一段宽限期，用更短的间隔重试，别急着报错。
            const warming = (status === 503 || status === 0)
            if (pollStrikes >= (warming ? FRAME_WARM_STRIKES : FRAME_STRIKES)) {
              if (frameFailRef.current) frameFailRef.current(status, hint)
              return
            }
            next = warming ? FRAME_WARM_MS : FRAME_MIN_MS
          }
          later(next, () => { void pollRun() })
        }

        /* ---------------------------------------------------- 可见性（省电） */

        const onVisibility = () => {
          if (!alive) return
          let hidden = false
          try { hidden = document.visibilityState === 'hidden' } catch (error) { hidden = false }
          if (hidden) {
            // 隐藏：断流（AbortController）+ 停 rAF/定时器，并尽力知会服务降档。
            statsRef.current.hidden = true
            publishStats()
            void sendStreamConfig('paused', 'hidden')
            stopTransport()
            statsRef.current.transport = 'paused'
            if (lastTransport !== 'paused') { lastTransport = 'paused'; setTransport('paused') }
            return
          }
          // 恢复：窗口统计清零（隐藏那段时间不算帧率/带宽），立刻重连。
          statsStartAt = nowMs()
          win.drawn.length = 0
          win.seqs.length = 0
          win.bytes.length = 0
          win.lat.length = 0
          streamWait = STREAM_RETRY_MIN_MS
          streamFails = 0
          pollStrikes = 0
          if (profileRef.current !== 'auto') void sendStreamConfig(profileRef.current, 'visible')
          if (mode === 'poll') void pollRun()
          else void openStream()
        }

        /** 尺寸/DPR 变了要重画（否则画面会被拉伸成旧尺寸的样子）。 */
        const onResize = () => { redraw() }

        document.addEventListener('visibilitychange', onVisibility)
        try { window.addEventListener('resize', onResize) } catch (error) { /* 无所谓 */ }
        let observer = null
        try {
          if (typeof ResizeObserver === 'function' && stageRef.current) {
            observer = new ResizeObserver(() => { redraw() })
            observer.observe(stageRef.current)
          }
        } catch (error) { observer = null }

        engineRef.current = {
          redraw: redraw,
          geom: () => geomRef.current,
          sendConfig: (name, reason) => { void sendStreamConfig(name, reason) },
        }

        // 用户在上一次流里选过档位：连上就把它带过去（选择要能"记住"）。
        if (profileRef.current !== 'auto') void sendStreamConfig(profileRef.current, 'resume')
        // 统计/自适应心跳：**独立于传输定时器**（隐藏时传输要停，但统计心跳要留着，
        // 否则恢复可见后状态条就再也不刷新了）。
        const tickTimer = setInterval(() => { if (alive) tick() }, STATS_TICK_MS)
        tick()
        void openStream()

        return () => {
          alive = false
          clearInterval(tickTimer)
          redrawRef.current = null
          engineRef.current = null
          document.removeEventListener('visibilitychange', onVisibility)
          try { window.removeEventListener('resize', onResize) } catch (error) { /* 无所谓 */ }
          if (observer) { try { observer.disconnect() } catch (error) { /* 无所谓 */ } }
          stopTransport()
          for (const ctrl of softAborts) { try { ctrl.abort() } catch (error) { /* 已结束 */ } }
          softAborts.clear()
          parser.reset()
        }
      }, [phase, sid])

      /* ------------------------------------------------------- 状态：/state 轮询（后端/缺依赖/光标兜底） */

      useEffect(() => {
        if (phase !== 'streaming' || !sid) return undefined
        let alive = true
        let misses = 0
        const timers = new Set()
        const aborts = new Set()
        const later = (ms, fn) => {
          const id = setTimeout(() => { timers.delete(id); if (alive) fn() }, ms)
          timers.add(id)
        }
        const tick = async () => {
          if (!alive) return
          const ctrl = new AbortController()
          aborts.add(ctrl)
          let json = null
          let ok = false
          try {
            const res = await fetch(API + '/state?session=' + encodeURIComponent(sid), { cache: 'no-store', signal: ctrl.signal })
            ok = res.ok
            if (res.ok) json = await readJsonSafe(res)
          } catch (error) { ok = false } finally { aborts.delete(ctrl) }
          if (!alive) return
          if (ok && json) {
            misses = 0
            setStateLost(false)
            const next = normalizeState(json)
            // 光标以帧头为准；只有 3 秒没拿到帧头光标（旧版服务）才用 /state 的兜底值。
            if (next.cursor && Date.now() - cursorFrameAtRef.current > 3000) {
              cursorRef.current = next.cursor
              if (redrawRef.current) redrawRef.current()
            }
            if (next.size) {
              const parsed = parseSizeHeader(next.size)
              if (parsed) sizeRef.current = parsed
            }
            setSnapshot(next)
            // 宿主在 /state 里也带了 service 块：服务掉了就别等下一次 /info（最长 10 秒），立刻重探。
            if (json.service && json.service.running === false && probeRef.current) probeRef.current()
          } else {
            // /state 挂了不影响画面（画面走流/帧）；只是状态条脏了，标一下就好。
            misses += 1
            if (misses >= 3) setStateLost(true)
          }
          later(STATE_MS, tick)
        }
        void tick()
        return () => {
          alive = false
          for (const id of timers) clearTimeout(id)
          timers.clear()
          for (const ctrl of aborts) { try { ctrl.abort() } catch (error) { /* 已结束 */ } }
          aborts.clear()
        }
      }, [phase, sid])

      /* ------------------------------------------------------- 输入：原生监听（wheel 必须非 passive） */

      useEffect(() => {
        if (phase !== 'streaming' || !sid) return undefined
        const canvas = canvasRef.current
        const sink = sinkRef.current
        if (!canvas) return undefined
        const offs = []
        const on = (target, type, fn, opts) => {
          target.addEventListener(type, fn, opts)
          offs.push(() => target.removeEventListener(type, fn, opts))
        }
        let gesturePairs = 0
        let panning = false
        let panLast = null
        /**
         * 画面上有没有"按下未松开"的键。mouseup 挂在 window 上（拖到画面外松手也要收到），
         * 但工具条/状态条上的点击也会冒泡到 window —— 那些**不能**变成远端的 mouseup。
         */
        let pressing = false

        const focusSink = () => {
          if (!sink || typeof sink.focus !== 'function') return
          try { sink.focus({ preventScroll: true }) } catch (error) { try { sink.focus() } catch (error2) { /* 没法聚焦 */ } }
        }

        const onDown = (event) => {
          const b = buttonOf(event.button)
          if (!b) return
          event.preventDefault()
          focusSink()
          const point = normPoint(canvas, event)
          if (!point) return
          if (event.detail <= 1) gesturePairs = 0
          pushRipple(event.clientX, event.clientY)
          // Shift+拖动 / 中键拖动 = **本地平移查看**（发到远端就变成一次真实拖拽了，不能混淆）。
          if (event.shiftKey || b === 3) {
            panning = true
            panLast = { x: event.clientX, y: event.clientY }
            setPanning(true)
            return
          }
          // 右键整条交给 contextmenu 发一个 click(b=2)：否则 press/release + click 会在远端变成两次右键。
          if (b === 2) return
          pressing = true
          push({ t: 'down', x: point.x, y: point.y, b })
        }
        const onMove = (event) => {
          if (panning && panLast) {
            panBy(event.clientX - panLast.x, event.clientY - panLast.y)
            panLast = { x: event.clientX, y: event.clientY }
            return
          }
          const point = normPoint(canvas, event)
          if (!point) return
          push({ t: 'move', x: point.x, y: point.y })
        }
        const onUp = (event) => {
          if (panning) {
            panning = false
            panLast = null
            setPanning(false)
            return
          }
          if (!pressing) return                     // 不是画面上按下的（工具条点击等）：不发
          pressing = false
          const b = buttonOf(event.button)
          if (!b || b === 2) return
          const point = normPoint(canvas, event)
          if (!point) return
          gesturePairs += 1
          push({ t: 'up', x: point.x, y: point.y, b })
        }
        const onWheel = (event) => {
          event.preventDefault()                       // 非 passive：不许页面跟着滚
          // Ctrl/Cmd + 滚轮 = 本地缩放（不进远端，免得"想放大看看"变成远端滚轮）。
          if (event.ctrlKey || event.metaKey) {
            zoomAtPoint(event.clientX, event.clientY, event.deltaY < 0 ? 1.15 : 1 / 1.15)
            return
          }
          const point = normPoint(canvas, event)
          if (!point) return
          push({ t: 'wheel', x: point.x, y: point.y, dy: event.deltaY })
        }
        const onContext = (event) => {
          event.preventDefault()
          const point = normPoint(canvas, event)
          if (!point) return
          push({ t: 'click', x: point.x, y: point.y, b: 2 })
        }
        const onDblClick = (event) => {
          // 双击的两次 down/up 已经由 mousedown/mouseup 逐对转发了；只有当这一轮里
          // 少于两对（事件被吞了）时才补一对，否则每次双击会变成四下点击。
          if (gesturePairs >= 2) return
          const point = normPoint(canvas, event)
          if (!point) return
          gesturePairs += 1
          push({ t: 'down', x: point.x, y: point.y, b: 1 })
          push({ t: 'up', x: point.x, y: point.y, b: 1 })
        }
        const onKeyDown = (event) => {
          // 输入法合成期间绝不发送（中文输入法会把每个候选键都变成 keydown）。
          if (composingRef.current || event.isComposing || event.keyCode === 229) return
          const spec = keySpec(event)
          if (!spec) return
          event.preventDefault()
          push({ t: 'key', k: spec })
        }
        const onCompositionStart = () => { composingRef.current = true }
        const onCompositionEnd = (event) => {
          composingRef.current = false
          const text = (event && typeof event.data === 'string') ? event.data : ''
          if (text) push({ t: 'text', s: text })       // 合成结束：整段文本一次性发过去
          if (sink) { try { sink.value = '' } catch (error) { /* 无所谓 */ } }
        }
        const onFocus = () => setKeyReady(true)
        const onBlur = () => setKeyReady(false)

        on(canvas, 'mousedown', onDown)
        on(canvas, 'mousemove', onMove)
        on(window, 'mouseup', onUp)                   // 拖到画面外松手也要收到 up
        on(canvas, 'wheel', onWheel, { passive: false })
        on(canvas, 'contextmenu', onContext)
        on(canvas, 'dblclick', onDblClick)
        on(canvas, 'dragstart', (event) => event.preventDefault())
        if (sink) {
          on(sink, 'keydown', onKeyDown)
          on(sink, 'compositionstart', onCompositionStart)
          on(sink, 'compositionend', onCompositionEnd)
          on(sink, 'focus', onFocus)
          on(sink, 'blur', onBlur)
          on(sink, 'input', () => { try { sink.value = '' } catch (error) { /* 无所谓 */ } })
        }
        return () => { for (const off of offs) off() }
      }, [phase, sid])

      /* ------------------------------------------------------- 动作 */

      const startService = async () => {
        if (starting) return
        setStarting(true)
        setStartError('')
        try {
          const res = await fetch(API + '/service', {
            method: 'POST',
            headers: { 'content-type': 'application/json' },
            body: JSON.stringify({ action: 'start' }),
            cache: 'no-store',
          })
          const json = await readJsonSafe(res)
          if (!res.ok || (json && json.ok === false)) {
            const detail = (json && (json.hint || json.error)) || ('HTTP ' + res.status)
            setStartError(String(detail))
          }
          if (probeRef.current) probeRef.current()
        } catch (error) {
          setStartError(String((error && error.message) || error))
        } finally {
          setStarting(false)
        }
      }

      const retryNow = () => {
        setProblem(null)
        setStartError('')
        if (probeRef.current) probeRef.current()
        else setPhase('checking')
      }

      const chooseProfile = (name) => {
        profileRef.current = name
        setProfile(name)
        setNoticeNow(name === 'auto' ? t.profileAuto : (name === 'smooth' ? t.profileSmooth : t.profileSaver))
        const engine = engineRef.current
        if (engine && engine.sendConfig) engine.sendConfig(name, 'user')
      }

      const doFit = () => { applyView({ mode: 'fit', zoom: 1, panX: 0, panY: 0 }) }
      const doOne = () => { applyView({ mode: 'one', zoom: 1, panX: 0, panY: 0 }) }
      const doZoom = (factor) => { applyView({ zoom: clamp((viewRef.current.zoom || 1) * factor, ZOOM_MIN, ZOOM_MAX) }) }
      const doSmooth = () => { applyView({ smooth: !(viewRef.current.smooth !== false) }) }

      /* ------------------------------------------------------- 渲染 */

      const missing = (info && info.missing && info.missing.length ? info.missing : null)
        || ((snapshot && snapshot.missing && snapshot.missing.length) ? snapshot.missing : [])
      const service = info ? info.service : {}
      const backend = (snapshot && snapshot.backend) || service.backend || ''
      const displayNo = (snapshot && snapshot.display) || ''
      const size = (snapshot && snapshot.size) || service.size || ''
      const realDesktop = !!(snapshot && snapshot.realDesktop) || !!(info && info.input && info.input.realDesktop)
      const inputEnabled = (snapshot && snapshot.input === false) ? false
        : !!(info && info.input && info.input.enabled !== false)

      let tone = 'warn'
      let stateText = t.checking
      if (phase === 'streaming') {
        // 服务在跑但还没画到帧（常见于刚拉起）：说"等待第一帧"，别假装在放画面。
        tone = hasFrame ? 'ok' : 'warn'
        stateText = hasFrame ? t.live : t.waitingFrame
      } else if (phase === 'need-service') { tone = 'warn'; stateText = t.needService }
      else if (phase === 'error') {
        tone = 'bad'
        const kind = problem ? problem.kind : ''
        if (kind === 'auth') stateText = t.errAuth
        else if (kind === 'net') stateText = t.errNet
        else if (kind === 'service') stateText = t.errService
        else if (kind === 'frame') stateText = t.errFrame
        else if (kind === 'api') stateText = t.errApi
        else stateText = t.errHttp(problem && problem.status ? problem.status : 0)
      }

      const showStage = !!sid && (phase === 'streaming' || phase === 'error')
      const zoomText = Math.round(clamp(view.zoom || 1, ZOOM_MIN, ZOOM_MAX) * 100) + '%'
      const profileLabel = profile === 'smooth' ? t.profileSmooth
        : (profile === 'saver' ? t.profileSaver : (profile === 'paused' ? t.profilePaused : t.profileAuto))

      /* ---------------------------------------------------- 状态条 */

      const badges = []
      const transportLabel = transport === 'stream' ? t.streamMode
        : (transport === 'poll' ? t.pollMode : (transport === 'paused' ? t.profilePaused : ''))
      if (transportLabel && (phase === 'streaming' || phase === 'error')) {
        badges.push(h('span', { key: 'tr', className: 'ddp-badge' }, t.transport + ' ' + transportLabel))
      }
      if (backend) badges.push(h('span', { key: 'be', className: 'ddp-badge' }, t.backend + ' ' + backend))
      if (displayNo) badges.push(h('span', { key: 'dp', className: 'ddp-badge' }, t.display + ' ' + displayNo))
      if (size) badges.push(h('span', { key: 'sz', className: 'ddp-badge' }, size))
      if (phase === 'streaming') {
        // 静止时**不要**显示 "0.0 fps"：那看起来像卡死，其实是"没有变化就不重传帧"的省流设计。
        const fpsVal = stats.fps || 0
        const stillNow = fpsVal < 0.05
        badges.push(h('span', {
          key: 'fps',
          className: 'ddp-badge' + (stillNow ? '' : ' ddp-ok'),
          title: stillNow ? t.stillHint : undefined,
        }, stillNow ? t.still : (fpsVal.toFixed(1) + ' ' + t.fps)))
        if (stats.dropped > 0) {
          badges.push(h('span', { key: 'dr', className: 'ddp-badge' }, t.dropped + ' ' + stats.dropped))
        }
        if (stats.latencyP50 >= 0) {
          badges.push(h('span', { key: 'lat', className: 'ddp-badge' }, t.latency + ' ' + stats.latencyP50 + 'ms'))
        }
        badges.push(h('span', { key: 'pf', className: 'ddp-badge', title: t.profileHint }, t.profile + ' ' + profileLabel))
        if (stats.quality > 0) {
          badges.push(h('span', { key: 'q', className: 'ddp-badge' },
            t.quality + ' ' + stats.quality + (stats.scale > 0 && stats.scale < 1 ? ' · ' + Math.round(stats.scale * 100) + '%' : '')))
        }
      }
      if (reconnect) {
        badges.push(h('span', { key: 'rc', className: 'ddp-badge ddp-warn' },
          t.reconnecting(Math.max(1, reconnect.left || Math.round((reconnect.wait || 0) / 1000)))))
      }
      if (realDesktop) badges.push(h('span', { key: 'rd', className: 'ddp-badge ddp-bad', title: t.realDesktopHint }, t.realDesktop))
      if (!inputEnabled) badges.push(h('span', { key: 'io', className: 'ddp-badge ddp-warn', title: t.inputOffHint }, t.inputOff))
      if (missing.length) badges.push(h('span', { key: 'ms', className: 'ddp-badge ddp-warn' }, t.missing(missing.length)))
      if (stateLost) badges.push(h('span', { key: 'sl', className: 'ddp-badge ddp-warn' }, t.stateLost))
      if (inputLost) badges.push(h('span', { key: 'il', className: 'ddp-badge ddp-warn' }, t.inputLost))
      if (stats.decodeFails > 0 && phase === 'streaming') {
        badges.push(h('span', { key: 'df', className: 'ddp-badge ddp-warn' }, t.decodeFail + ' ' + stats.decodeFails))
      }

      const bar = h('div', { className: 'ddp-bar' },
        h('span', { className: 'ddp-dot ddp-dot-' + tone }),
        h('span', { className: 'ddp-stat' }, stateText),
        badges,
        h('span', { className: 'ddp-grow' }),
        h('span', { className: 'ddp-mini' },
          t.build + ' ' + BUILD
          + (service.version ? ' · viewer ' + service.version : '')),
        // 会话 id **完整**显示（不截断）：截断值拿去调 /exec 会打到别的会话上，
        // 排查问题时比"太长"危险得多。title 也放完整值，样式允许选中复制。
        sid ? h('span', { className: 'ddp-mini ddp-sid', title: t.session + ' ' + sid }, t.session + ' ' + sid) : null,
      )

      /* ---------------------------------------------------- 工具条（视图/档位） */

      const zoom = clamp(view.zoom || 1, ZOOM_MIN, ZOOM_MAX)
      const toolbar = showStage ? h('div', { className: 'ddp-tools', role: 'toolbar' },
        h('button', {
          type: 'button', className: 'ddp-tbtn', 'data-act': 'fit',
          'aria-pressed': view.mode === 'fit', title: t.fitHint + ' · ' + t.panHint,
          onClick: doFit,
        }, t.fit),
        h('button', {
          type: 'button', className: 'ddp-tbtn', 'data-act': 'one',
          'aria-pressed': view.mode === 'one', title: t.oneHint,
          onClick: doOne,
        }, t.one),
        h('span', { className: 'ddp-sep' }),
        h('button', {
          type: 'button', className: 'ddp-tbtn', 'data-act': 'zoom-out', title: t.zoomOut,
          disabled: zoom <= ZOOM_MIN + 1e-6, onClick: () => doZoom(1 / 1.25),
        }, '−'),
        h('span', { className: 'ddp-zoomval', title: t.zoom + ' · ' + t.panHint }, zoomText),
        h('button', {
          type: 'button', className: 'ddp-tbtn', 'data-act': 'zoom-in', title: t.zoomIn,
          disabled: zoom >= ZOOM_MAX - 1e-6, onClick: () => doZoom(1.25),
        }, '+'),
        h('span', { className: 'ddp-sep' }),
        h('button', {
          type: 'button', className: 'ddp-tbtn', 'data-act': 'smooth',
          'aria-pressed': view.smooth !== false, title: t.smoothHint,
          onClick: doSmooth,
        }, t.smooth),
        h('button', {
          type: 'button', className: 'ddp-tbtn', 'data-act': 'fullscreen',
          'aria-pressed': view.fullscreen === true,
          title: view.fullscreen ? t.exitFullscreen : t.fullscreenHint,
          onClick: toggleFullscreen,
        }, view.fullscreen ? t.exitFullscreen : t.fullscreen),
        h('span', { className: 'ddp-grow' }),
        h('span', { className: 'ddp-mini' }, t.profile),
        h('button', {
          type: 'button', className: 'ddp-tbtn', 'data-act': 'profile-auto',
          'aria-pressed': profile === 'auto', title: t.profileHint,
          onClick: () => chooseProfile('auto'),
        }, t.profileAuto),
        h('button', {
          type: 'button', className: 'ddp-tbtn', 'data-act': 'profile-smooth',
          'aria-pressed': profile === 'smooth', title: t.profileHint,
          onClick: () => chooseProfile('smooth'),
        }, t.profileSmooth),
        h('button', {
          type: 'button', className: 'ddp-tbtn', 'data-act': 'profile-saver',
          'aria-pressed': profile === 'saver', title: t.profileHint,
          onClick: () => chooseProfile('saver'),
        }, t.profileSaver),
      ) : null

      /* ---------------------------------------------------- 画面（canvas + 骨架屏 + 提示） */

      const pillText = notice ? notice
        : (reconnect ? t.reconnecting(Math.max(1, reconnect.left || Math.round((reconnect.wait || 0) / 1000)))
          : (panning ? t.dragging
            : (keyReady ? t.kbOn : (inputEnabled ? t.kbHint : t.readonly + ' · ' + t.panHint))))

      const skeleton = (phase === 'streaming' && !hasFrame)
        ? h('div', { className: 'ddp-skel', 'data-ddp-skeleton': '1' },
          h('div', { className: 'ddp-skel-tex' }),
          h('div', { className: 'ddp-skel-spin' }),
          h('div', { style: { position: 'relative', textAlign: 'center' } },
            h('div', null, t.skeleton),
            h('div', { className: 'ddp-mini' }, t.waitingFrameHint)))
        : null

      const stage = showStage
        ? h('div', {
          className: 'ddp-stage',
          ref: stageRef,
          'data-focus': keyReady ? '1' : '0',
          'data-panning': panning ? '1' : '0',
        },
        h('canvas', {
          ref: canvasRef,
          className: 'ddp-canvas',
          'data-build': BUILD,
          'data-has-frame': hasFrame ? '1' : '0',
          'aria-label': t.title + (sid ? ' · ' + sid : ''),
        }),
        skeleton,
        phase === 'streaming' ? h('div', { className: 'ddp-pill' }, pillText) : null,
        )
        : null

      let card = null
      if (!sid) {
        card = h('div', { className: 'ddp-card' },
          h('div', { className: 'ddp-title' }, t.title),
          h('div', { className: 'ddp-note' }, t.noSession),
          h('div', { className: 'ddp-hint' }, t.build + ' ' + BUILD),
        )
      } else if (phase === 'checking') {
        card = h('div', { className: 'ddp-card' },
          h('div', { className: 'ddp-title' }, t.checking),
          h('div', { className: 'ddp-hint' }, t.checkingHint),
          h('div', { className: 'ddp-hint' }, t.build + ' ' + BUILD),
        )
      } else if (phase === 'need-service') {
        card = h('div', { className: 'ddp-card' },
          h('div', { className: 'ddp-title' }, t.needService),
          h('div', { className: 'ddp-note' }, t.needServiceHint),
          missing.length ? h('div', null,
            h('div', { className: 'ddp-hint' }, t.missingHint),
            h('ul', { className: 'ddp-list' }, missing.map((item, index) => h('li', { key: 'm' + index }, missingLine(item)))),
          ) : null,
          h('button', {
            type: 'button',
            className: 'ddp-btn ddp-btn-primary',
            disabled: starting,
            onClick: () => { void startService() },
          }, starting ? t.starting : t.start),
          starting ? h('div', { className: 'ddp-hint' }, t.startingHint) : null,
          startError ? h('div', { className: 'ddp-err' }, startError) : null,
          h('div', { className: 'ddp-hint' }, t.build + ' ' + BUILD + (service.port ? ' · port ' + service.port : '')),
        )
      } else if (phase === 'error') {
        const kind = problem ? problem.kind : ''
        const hint = (kind === 'auth') ? t.errAuthHint
          : (kind === 'net') ? t.errNetHint
            : (kind === 'service') ? t.errServiceHint
              : (kind === 'frame') ? t.errFrameHint
                : t.errApiHint
        // 宿主自己的原话（{ok:false,error,hint}）永远比我们猜的准确，直接显示。
        const hostHint = (problem && problem.hint) ? String(problem.hint) : ''
        card = h('div', { className: 'ddp-card' },
          h('div', { className: 'ddp-title' }, stateText),
          h('div', { className: 'ddp-note' }, hint),
          hostHint ? h('div', { className: 'ddp-hint' }, hostHint) : null,
          missing.length ? h('div', null,
            h('div', { className: 'ddp-hint' }, t.missingHint),
            h('ul', { className: 'ddp-list' }, missing.map((item, index) => h('li', { key: 'm' + index }, missingLine(item)))),
          ) : null,
          h('div', { className: 'ddp-hint' }, attempt > 0
            ? t.retryIn(attempt, Math.max(1, Math.round(((problem && problem.wait) || RETRY_MIN_MS) / 1000)))
            : ''),
          h('div', { style: { display: 'flex', gap: '8px', flexWrap: 'wrap', justifyContent: 'center' } },
            h('button', {
              type: 'button', className: 'ddp-btn',
              onClick: retryNow,
            }, t.retryNow),
            h('button', {
              type: 'button', className: 'ddp-btn ddp-btn-primary',
              disabled: starting,
              onClick: () => { void startService() },
            }, starting ? t.starting : t.start),
          ),
          startError ? h('div', { className: 'ddp-err' }, startError) : null,
          h('div', { className: 'ddp-hint' }, t.build + ' ' + BUILD),
        )
      }

      return h('div', {
        className: 'ddp-root',
        'data-build': BUILD,
        'data-phase': sid ? phase : 'no-session',
        'data-transport': transport || '',
        // ↓ perf 脚本（tools/perf-panel.mjs）稳定依赖的观测属性，与 window.__ddpStats 同源
        'data-fps': (stats.fps || 0).toFixed(1),
        'data-drawn': String(stats.drawn || 0),
        'data-received': String(stats.received || 0),
        'data-dropped': String(stats.dropped || 0),
        'data-has-frame': hasFrame ? '1' : '0',
        'data-view': view.mode,
        'data-zoom': String(view.zoom || 1),
        'data-profile': profile,
      },
      bar,
      toolbar,
      h('div', { className: 'ddp-body' },
        stage,
        card ? h('div', { className: 'ddp-overlay' }, card) : null,
        // 键盘 sink：隐藏但可聚焦，keydown/composition 都在这里收。点击画面时聚焦它，
        // 免得抢走对话输入框的焦点（面板切过来时不自动抢）。
        h('textarea', {
          ref: sinkRef,
          className: 'ddp-sink',
          tabIndex: -1,
          spellCheck: false,
          autoComplete: 'off',
          autoCorrect: 'off',
          autoCapitalize: 'off',
          'aria-hidden': 'true',
        }),
      ),
      )
    }

    const apply = (ctx) => {
      try {
        // ⚠️ 必须先 slots.inject 等声明再 register；inject 里带上 sessionId —— 面板才知道
        // 该显示**哪个会话**的那台显示（写法同 dsh-browser-panel，已被实测证明可用）。
        ctx.slots.inject('conversation.view', () =>
          ctx.slots.register(
            {
              name: 'conversation.view', id: PANEL_ID, order: 60,
              label: () => TEXT[pickLang()].title,
              inject: (sessionId) => ({ sessionId: typeof sessionId === 'string' ? sessionId : '' }),
            },
            (props) => h(Display, props),
          ),
        )
      } catch (error) {
        console.warn('[dsh-display-panel] slot registration failed:', error)
      }
    }

    return { apply, inject: ['slots'] }
  },
})
