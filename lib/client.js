/**
 * dsh-display-panel — 浏览器半边：在「对话 / 轨迹 / 浏览器」那一排里加一个「显示器」标签。
 *
 * 架构（契约 docs/CONTRACT.md §3）：面板**不**直连显示器服务，只跟宿主半边的**同源**接口
 * `/api/dsh-display-panel/*` 说话。之前那版是 iframe 直连服务的，踩过一串结构性坑：
 * 跨源 + 混合内容（HTTPS 部署下面板永远空白）、回环地址（从别的机器访问 DSH 时连的是
 * **访问者自己**的机器）、访问令牌出现在浏览器地址里（进历史/Referer）。现在令牌只留在
 * 宿主侧，浏览器只发同源请求。
 *
 * 所以这里要自己干三件事：拉帧画到 canvas、把鼠标/键盘/输入法采集下来发回去、把状态
 * （后端、真实桌面、缺依赖、光标）显示清楚。
 *
 * 这个文件是"裸 JS 模块"：由 DSH 的客户端模块系统当 bundle 直接执行，注册方式是
 * `window.__ModuleLoader__.load({ id, factory })`，factory 里 `require('react')` 拿宿主提供的
 * React。**不要** import 任何 npm 包，也不要去改注册方式（已被实测证明可用）。
 *
 * 构建标记 BUILD：状态条上会显示，用来判断浏览器里加载的到底是哪一版（插件 JS 有缓存）。
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
    const BUILD = '2026-09-25a'
    /** 宿主半边的同源接口前缀（契约 §2）。浏览器只跟它说话。 */
    const API = '/api/dsh-display-panel'

    /** 拉帧节奏：服务端约 7~8 帧/秒；单帧慢时自动放慢，最低 120ms 一次、最高 1s 一次。 */
    const FRAME_MIN_MS = 120
    const FRAME_MAX_MS = 1000
    /** /state 轮询：光标、后端、尺寸、缺依赖。光标是画在画面上的准星，别太慢。 */
    const STATE_MS = 700
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
     * /frame 会一直 503（Xvfb 还没抓到第一帧）。所以给它一段宽限期 + 更短的重试间隔。
     */
    const FRAME_WARM_STRIKES = 16
    const FRAME_WARM_MS = 300

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
        kbHint: '点击画面后键盘生效',
        kbOn: '键盘已连接',
        cursor: '光标',
        backend: '后端',
        display: '显示',
        session: '会话',
        build: '构建',
        fps: 'fps',
        live: '实时画面',
        waitingFrame: '等待第一帧…',
        waitingFrameHint: '服务在跑，但上游还没抓到画面（刚拉起时常见）。',
        stateLost: '状态读取失败',
        inputLost: '输入发送失败',
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
        kbHint: 'Click the picture to enable the keyboard',
        kbOn: 'Keyboard attached',
        cursor: 'cursor',
        backend: 'backend',
        display: 'display',
        session: 'session',
        build: 'build',
        fps: 'fps',
        live: 'live',
        waitingFrame: 'Waiting for the first frame…',
        waitingFrameHint: 'The service is up, but no frame has been captured yet (common right after a start).',
        stateLost: 'state polling failed',
        inputLost: 'input delivery failed',
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

    /** 样式只注入一次。背景保持 transparent —— 底色由 DSH 面板自己给，深色/浅色主题都不打架。 */
    const CSS = `
      .ddp-root {
        width: 100%; height: 100%; min-height: 0; display: flex; flex-direction: column;
        background: transparent; color: inherit; position: relative; overflow: hidden;
      }
      .ddp-bar {
        flex: 0 0 auto; display: flex; flex-wrap: wrap; align-items: center; gap: 6px;
        padding: 6px 10px; font-size: 11.5px; line-height: 1.5;
        border-bottom: 1px solid var(--dsh-border, rgba(128,128,128,.22));
      }
      .ddp-dot { width: 8px; height: 8px; border-radius: 50%; background: #8a8a8a; flex: 0 0 auto; }
      .ddp-dot-ok { background: #3ecf6d; }
      .ddp-dot-warn { background: #e8a33d; }
      .ddp-dot-bad { background: #e8574a; }
      .ddp-stat { opacity: .85; }
      .ddp-badge {
        padding: 1px 7px; border-radius: 999px; white-space: nowrap;
        border: 1px solid var(--dsh-border, rgba(128,128,128,.3));
        background: var(--dsh-surface-alt, rgba(128,128,128,.10));
      }
      .ddp-badge.ddp-warn { border-color: rgba(232,163,61,.65); color: #e8a33d; }
      .ddp-badge.ddp-bad { border-color: rgba(232,87,74,.7); color: #e8574a; }
      .ddp-grow { flex: 1 1 auto; }
      .ddp-mini { font-size: 10.5px; opacity: .45; white-space: nowrap; }
      .ddp-sid {
        opacity: .6; white-space: normal; word-break: break-all; max-width: 100%;
        user-select: text; -webkit-user-select: text; cursor: text;
      }
      .ddp-body { position: relative; flex: 1 1 auto; min-height: 0; display: flex; }
      .ddp-stage { position: relative; flex: 1 1 auto; min-width: 0; min-height: 0; background: #000; display: flex; }
      .ddp-canvas {
        width: 100%; height: 100%; display: block; background: #000;
        cursor: crosshair; touch-action: none; outline: none; user-select: none;
      }
      .ddp-overlay {
        position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
        padding: 20px; overflow: auto; background: rgba(8,8,10,.78); color: #f2f2f2;
      }
      .ddp-card { max-width: 620px; text-align: center; display: flex; flex-direction: column; gap: 10px; align-items: center; }
      .ddp-title { font-size: 15px; font-weight: 600; }
      .ddp-note { font-size: 12.5px; line-height: 1.75; opacity: .78; }
      .ddp-hint { font-size: 11.5px; line-height: 1.7; opacity: .55; word-break: break-word; }
      .ddp-list { margin: 0; padding: 0; list-style: none; font-size: 11.5px; opacity: .8; text-align: left; }
      .ddp-list li { padding: 1px 0; word-break: break-word; }
      .ddp-btn {
        margin-top: 2px; padding: 7px 16px; font: inherit; font-size: 12.5px; border-radius: 8px;
        cursor: pointer; color: inherit; border: 1px solid var(--dsh-border, rgba(255,255,255,.28));
        background: var(--dsh-surface-alt, rgba(255,255,255,.08));
      }
      .ddp-btn:hover:not(:disabled) { border-color: var(--dsh-accent, #4c8dff); }
      .ddp-btn:disabled { opacity: .55; cursor: default; }
      .ddp-btn-primary { border-color: var(--dsh-accent, #4c8dff); }
      .ddp-err { font-size: 11.5px; color: #e8574a; word-break: break-word; }
      .ddp-pill {
        position: absolute; left: 50%; bottom: 10px; transform: translateX(-50%);
        padding: 3px 10px; border-radius: 999px; font-size: 11px; pointer-events: none;
        background: rgba(0,0,0,.6); color: #f2f2f2; border: 1px solid rgba(255,255,255,.18);
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

    function clamp01(value) {
      if (!Number.isFinite(value)) return 0
      if (value < 0) return 0
      if (value > 1) return 1
      return Math.round(value * 10000) / 10000
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

    /**
     * 把 /info 的响应归一化。
     *
     * 新形状（契约 §2）：`{ok, service:{running,port,backend,size,version}, input:{enabled,realDesktop}, missing:[]}`。
     * 旧形状（0.2.x 宿主）：`{home, port, token}` —— 只有端口，端口有值就认为服务在跑；
     * 宿主半边还没升级完（T3）时面板也得能显示点东西，而不是白屏。
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

    /* ---------------------------------------------------------------- 组件 */

    function Display(props) {
      const sid = (props && typeof props.sessionId === 'string') ? props.sessionId : ''
      const [lang] = useState(pickLang)
      const t = TEXT[lang] || TEXT.zh

      /** 状态机：checking | need-service | streaming | error（sid 为空时渲染"没有会话"卡片）。 */
      const [phase, setPhase] = useState('checking')
      const [problem, setProblem] = useState(null)   // {kind, status, hint}
      const [info, setInfo] = useState(null)         // normalizeInfo 的结果
      const [snapshot, setSnapshot] = useState(null) // normalizeState 的结果
      const [fps, setFps] = useState(0)
      const [attempt, setAttempt] = useState(0)
      const [starting, setStarting] = useState(false)
      const [startError, setStartError] = useState('')
      const [keyReady, setKeyReady] = useState(false)
      const [inputLost, setInputLost] = useState(false)
      const [stateLost, setStateLost] = useState(false)
      /** 是否已经画过至少一帧：没画过时状态条要说"等待第一帧"，而不是假装在放画面。 */
      const [hasFrame, setHasFrame] = useState(false)

      const canvasRef = useRef(null)
      const sinkRef = useRef(null)
      /** contain 之后的**实际画面矩形**（canvas 内的 CSS 像素），输入坐标换算必须用它。 */
      const drawRectRef = useRef(null)
      /** /state 报的光标（归一化）；画帧时顺便把准星画上，所以走 ref 不走 state。 */
      const cursorRef = useRef(null)
      /** 出站输入队列：串行发，保证服务端收到的事件顺序与用户操作一致。 */
      const queueRef = useRef([])
      const inflightRef = useRef(false)
      const aliveRef = useRef(true)
      const ctrlRef = useRef(null)
      const composingRef = useRef(false)
      const sidRef = useRef(sid)
      const probeRef = useRef(null)
      const frameFailRef = useRef(null)

      useEffect(() => { sidRef.current = sid }, [sid])

      // 生命周期：换会话/卸载时断掉在途请求、清空队列，别留悬挂任务。
      useEffect(() => {
        aliveRef.current = true
        queueRef.current = []
        inflightRef.current = false
        composingRef.current = false
        drawRectRef.current = null
        cursorRef.current = null
        ctrlRef.current = (typeof AbortController === 'function') ? new AbortController() : null
        return () => {
          aliveRef.current = false
          queueRef.current = []
          if (ctrlRef.current) { try { ctrlRef.current.abort() } catch (error) { /* 已结束 */ } }
          ctrlRef.current = null
        }
      }, [sid])

      // 样式 + 版本横幅。换会话时重新打一遍，日志里好对上。
      useEffect(() => {
        ensureStyle()
        try { console.log('[dsh-display-panel] build', BUILD, 'lang', lang, 'sid', sid || '(none)') } catch (error) { /* 无所谓 */ }
      }, [sid, lang])

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
       * 用 contain 之后的实际画面矩形（drawRectRef，CSS 像素）换算：
       * 元素比画面宽/高时两侧是黑边，点在黑边上会被夹到 0 或 1。
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
          setFps(0)
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

      /* ------------------------------------------------------- 画帧：/frame 循环 */

      useEffect(() => {
        if (phase !== 'streaming' || !sid) return undefined
        let alive = true
        let strikes = 0
        const timers = new Set()
        const aborts = new Set()
        const stamps = []
        let lastPublish = 0
        /** 单飞：同一时刻只允许一条拉帧链（否则可见性抖动会让多条链叠加，白烧 CPU/带宽）。 */
        let running = false
        let scheduled = null
        /** 拉帧只走这一个入口：已经在跑或已经排好下一次就不再排。 */
        const schedule = (ms) => {
          if (!alive || running || scheduled !== null) return
          const id = setTimeout(() => {
            timers.delete(id)
            scheduled = null
            if (alive) void run()
          }, ms)
          scheduled = id
          timers.add(id)
        }

        /** 画一帧：contain（黑底 letterbox），并把实际画面矩形留给输入换算。 */
        const paint = (source, iw, ih) => {
          const canvas = canvasRef.current
          if (!canvas || !source || !(iw > 0) || !(ih > 0)) return false
          let c2d = null
          try { c2d = canvas.getContext('2d') } catch (error) { c2d = null }
          if (!c2d) return false           // 没有 2d 上下文（例如 jsdom 无 canvas 实现）：不画，但不算错
          const dpr = Math.min(2, Math.max(1, Number(window.devicePixelRatio) || 1))
          const cssW = canvas.clientWidth || canvas.width || iw
          const cssH = canvas.clientHeight || canvas.height || ih
          const bw = Math.max(1, Math.round(cssW * dpr))
          const bh = Math.max(1, Math.round(cssH * dpr))
          if (canvas.width !== bw || canvas.height !== bh) { canvas.width = bw; canvas.height = bh }
          try {
            c2d.setTransform(dpr, 0, 0, dpr, 0, 0)
            c2d.fillStyle = '#000'
            c2d.fillRect(0, 0, cssW, cssH)
            const scale = Math.min(cssW / iw, cssH / ih)
            const dw = Math.max(1, Math.round(iw * scale))
            const dh = Math.max(1, Math.round(ih * scale))
            const dx = Math.round((cssW - dw) / 2)
            const dy = Math.round((cssH - dh) / 2)
            c2d.drawImage(source, dx, dy, dw, dh)
            drawRectRef.current = { dx, dy, dw, dh }
            // 光标覆盖层：Xvfb 抓的帧里**没有**指针，不画准星用户就是盲点。
            const cursor = cursorRef.current
            if (cursor) {
              const cx = dx + cursor.x * dw
              const cy = dy + cursor.y * dh
              c2d.save()
              c2d.lineWidth = 1.5
              c2d.strokeStyle = 'rgba(255,64,64,.95)'
              c2d.beginPath()
              c2d.moveTo(cx - 11, cy); c2d.lineTo(cx - 3, cy)
              c2d.moveTo(cx + 3, cy); c2d.lineTo(cx + 11, cy)
              c2d.moveTo(cx, cy - 11); c2d.lineTo(cx, cy - 3)
              c2d.moveTo(cx, cy + 3); c2d.lineTo(cx, cy + 11)
              c2d.stroke()
              c2d.beginPath()
              c2d.arc(cx, cy, 5.5, 0, Math.PI * 2)
              c2d.stroke()
              c2d.restore()
            }
          } catch (error) {
            return false
          }
          return true
        }

        /** 解码 JPEG：优先 createImageBitmap（Chromium），退回 objectURL + <img>。 */
        const drawBlob = async (blob, fallbackUrl) => {
          if (typeof window.createImageBitmap === 'function') {
            let bitmap = null
            try { bitmap = await window.createImageBitmap(blob) } catch (error) { bitmap = null }
            if (bitmap) {
              const painted = paint(bitmap, bitmap.width, bitmap.height)
              try { bitmap.close() } catch (error) { /* 部分实现没有 close */ }
              return painted
            }
          }
          if (!alive) return false
          let objectUrl = null
          if (typeof URL !== 'undefined' && typeof URL.createObjectURL === 'function') {
            try { objectUrl = URL.createObjectURL(blob) } catch (error) { objectUrl = null }
          }
          const src = objectUrl || fallbackUrl
          if (!src) return false
          const img = new Image()
          const loaded = await new Promise((resolve) => {
            img.onload = () => resolve(true)
            img.onerror = () => resolve(false)
            try { img.src = src } catch (error) { resolve(false) }
          })
          if (!alive) {
            if (objectUrl) { try { URL.revokeObjectURL(objectUrl) } catch (error) { /* 已释放 */ } }
            return false
          }
          const painted = loaded ? paint(img, img.naturalWidth || img.width, img.naturalHeight || img.height) : false
          if (objectUrl) { try { URL.revokeObjectURL(objectUrl) } catch (error) { /* 已释放 */ } }
          return painted
        }

        const run = async () => {
          if (!alive || running) return
          // 标签页不可见就彻底停拉帧（不让定时器空转）；visible 时由 onVisible 唤醒。
          if (document.visibilityState === 'hidden') return
          running = true
          // 下一次拉帧的间隔。注意：schedule() 必须在 running=false **之后**调用，
          // 否则会被自己的单飞判断挡掉（画面就永远停在第一帧）。
          let next = FRAME_MIN_MS
          try {
            const started = Date.now()
            const url = API + '/frame?session=' + encodeURIComponent(sid) + '&t=' + started
            const ctrl = new AbortController()
            aborts.add(ctrl)
            let status = 0
            let hint = ''
            let painted = false
            try {
              const res = await fetch(url, { cache: 'no-store', signal: ctrl.signal })
              if (res.ok) {
                const blob = await res.blob()
                painted = await drawBlob(blob, url)
              } else {
                status = res.status
                // 宿主失败时是 {ok:false,error,hint}；把它的原话带给用户（例如"还没有帧"）。
                try {
                  const body = await res.json()
                  hint = (body && (body.hint || body.error)) || ''
                } catch (error) { hint = '' }
              }
            } catch (error) {
              status = 0
            } finally {
              aborts.delete(ctrl)
            }
            if (!alive) return
            if (painted) {
              strikes = 0
              setHasFrame(true)
              const now = Date.now()
              stamps.push(now)
              while (stamps.length > 2 && now - stamps[0] > 2000) stamps.shift()
              if (now - lastPublish > 900) {
                lastPublish = now
                const span = stamps.length > 1 ? (stamps[stamps.length - 1] - stamps[0]) : 0
                // 帧率只在这个两秒窗口里算；span 为 0（同一毫秒内画完）时数字没意义，保留上一次。
                if (span > 0) setFps(Math.round(((stamps.length - 1) * 1000 / span) * 10) / 10)
              }
              next = Math.min(FRAME_MAX_MS, Math.max(FRAME_MIN_MS, Date.now() - started))
            } else {
              strikes += 1
              // 401/403 立刻切错误（再拉也是白拉）。
              if (status === 401 || status === 403) {
                if (frameFailRef.current) frameFailRef.current(status, hint)
                return
              }
              // 503 / 网络错误多半是"服务刚被拉起、上游还没抓到第一帧"（宿主实测头 1~2 秒如此）：
              // 给它一段宽限期，用更短的间隔重试，别急着报错。
              const warming = (status === 503 || status === 0)
              if (strikes >= (warming ? FRAME_WARM_STRIKES : FRAME_STRIKES)) {
                if (frameFailRef.current) frameFailRef.current(status, hint)
                return
              }
              next = warming ? FRAME_WARM_MS : FRAME_MIN_MS
            }
          } finally {
            running = false
          }
          schedule(next)
        }

        const onVisible = () => {
          if (!alive || document.visibilityState !== 'visible') return
          schedule(0)
        }
        document.addEventListener('visibilitychange', onVisible)
        void run()
        return () => {
          alive = false
          document.removeEventListener('visibilitychange', onVisible)
          for (const id of timers) clearTimeout(id)
          timers.clear()
          for (const ctrl of aborts) { try { ctrl.abort() } catch (error) { /* 已结束 */ } }
          aborts.clear()
        }
      }, [phase, sid])

      /* ------------------------------------------------------- 状态：/state 轮询（光标/后端/缺依赖） */

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
            cursorRef.current = next.cursor
            setSnapshot(next)
            // 宿主在 /state 里也带了 service 块：服务掉了就别等下一次 /info（最长 10 秒），立刻重探。
            if (json.service && json.service.running === false && probeRef.current) probeRef.current()
          } else {
            // /state 挂了不影响画面（画面走 /frame）；只是光标/状态条脏了，标一下就好。
            misses += 1
            if (misses >= 5) setStateLost(true)
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
          // 右键整条交给 contextmenu 发一个 click(b=2)：否则 press/release + click 会在远端变成两次右键。
          if (b === 2) return
          push({ t: 'down', x: point.x, y: point.y, b })
        }
        const onMove = (event) => {
          const point = normPoint(canvas, event)
          if (!point) return
          push({ t: 'move', x: point.x, y: point.y })
        }
        const onUp = (event) => {
          const b = buttonOf(event.button)
          if (!b || b === 2) return
          const point = normPoint(canvas, event)
          if (!point) return
          gesturePairs += 1
          push({ t: 'up', x: point.x, y: point.y, b })
        }
        const onWheel = (event) => {
          event.preventDefault()                       // 非 passive：不许页面跟着滚
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

      const badges = []
      if (backend) badges.push(h('span', { key: 'be', className: 'ddp-badge' }, t.backend + ' ' + backend))
      if (displayNo) badges.push(h('span', { key: 'dp', className: 'ddp-badge' }, t.display + ' ' + displayNo))
      if (size) badges.push(h('span', { key: 'sz', className: 'ddp-badge' }, size))
      if (phase === 'streaming') badges.push(h('span', { key: 'fps', className: 'ddp-badge' }, fps + ' ' + t.fps))
      if (realDesktop) badges.push(h('span', { key: 'rd', className: 'ddp-badge ddp-bad', title: t.realDesktopHint }, t.realDesktop))
      if (!inputEnabled) badges.push(h('span', { key: 'io', className: 'ddp-badge ddp-warn', title: t.inputOffHint }, t.inputOff))
      if (missing.length) badges.push(h('span', { key: 'ms', className: 'ddp-badge ddp-warn' }, t.missing(missing.length)))
      if (stateLost) badges.push(h('span', { key: 'sl', className: 'ddp-badge ddp-warn' }, t.stateLost))
      if (inputLost) badges.push(h('span', { key: 'il', className: 'ddp-badge ddp-warn' }, t.inputLost))

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

      // 有画面区域（streaming / error）就一直挂着 canvas：出错时保留最后一帧，不闪白。
      const showStage = !!sid && (phase === 'streaming' || phase === 'error')
      const stage = showStage
        ? h('div', { className: 'ddp-stage' },
          h('canvas', {
            ref: canvasRef,
            className: 'ddp-canvas',
            'data-build': BUILD,
            'aria-label': t.title + (sid ? ' · ' + sid : ''),
          }),
          phase === 'streaming'
            ? h('div', { className: 'ddp-pill' }, keyReady ? t.kbOn : t.kbHint)
            : null,
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

      return h('div', { className: 'ddp-root', 'data-build': BUILD, 'data-phase': sid ? phase : 'no-session' },
        bar,
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
