/**
 * dsh-display-panel — 宿主半边（host half）。
 *
 * 它做三件事，全部发生在**宿主进程里**（也就是以这个用户的身份、能读令牌文件的那个进程）：
 *
 * 1. **同源反向代理**：浏览器只访问 DSH 自己的源 `/api/dsh-display-panel/*`，
 *    由这里转发到 `http://127.0.0.1:<port>/s/<sid>/*`。跨源 iframe 没了 → HTTPS
 *    部署不再被混合内容拦掉；写死的 `127.0.0.1` 没了 → 从别的机器访问 DSH 也能用；
 *    令牌不再进浏览器 → 不进历史/Referer，同机其它用户偷不到。
 * 2. **服务发现**：读 `<home>/port` 打 `/health`（无副作用），失败再扫 8099..8110，
 *    再失败就认为服务没起 —— 探测全在宿主侧，浏览器不必（也不该）去扫端口。
 * 3. **服务生命周期**：没起就自动拉起 `service/dsh-display-viewer.py`
 *    （`detached` + `unref`，不随宿主退出），并支持 start/stop/restart/status。
 *
 * 设计约束（与 docs/CONTRACT.md §2/§4 对应）：
 *
 * * 每个路由都走 `ctx.connection.requestRejection(req)` —— 复用宿主自己的鉴权，
 *   不自己造一套（造错了就是"任意网页都能读令牌"）。
 * * **令牌永不出现在响应里**：只在本文件内部拼上游 URL 用。
 * * 所有上游请求带 `AbortController` 超时（默认 5s，frame 3s）：上游卡住不能把
 *   宿主的事件循环拖死。
 * * 整段包在 try/catch 里：出任何问题都只是"面板用不了"，**绝不能影响 harness 启动**。
 * * 纯 CommonJS，**零依赖**（插件目录解析不到 `@deepseek-ai/*`，一律用环境变量做配置）。
 *
 * @module dsh-display-panel
 */
'use strict'

const fs = require('fs')
const http = require('http')
const os = require('os')
const path = require('path')
const { spawn, spawnSync } = require('child_process')

/** 插件名（日志前缀、effect 标签用）。 */
const NAME = 'dsh-display-panel'

/** 路由前缀：客户端半边只需要知道这一个常量。 */
const BASE_PATH = '/api/dsh-display-panel'

/** 本文件所在目录 = `<pkg>/lib`，服务脚本在 `<pkg>/service/`。 */
const PKG_DIR = path.resolve(__dirname, '..')
const SERVICE_SCRIPT = path.join(PKG_DIR, 'service', 'dsh-display-viewer.py')

/** 本半边自己声明的版本（面板状态条显示用）。 */
const VERSION = '0.3.0'

/** 服务默认端口与探测范围（与 `service/dsh-display-viewer.py` 的 `_bind` 一致）。 */
const DEFAULT_PORT = 8099
const PORT_SCAN_FIRST = 8099
const PORT_SCAN_LAST = 8110

/** 上游超时（毫秒）。探测比常规请求更短，info 必须秒回。 */
const TIMEOUT_PROBE_MS = 2000
const TIMEOUT_DEFAULT_MS = 5000
const TIMEOUT_FRAME_MS = 3000
const TIMEOUT_EXEC_WAIT_MS = 30000

/** 自动拉起后等待 `/health` 通过的上限。 */
const SPAWN_WAIT_MS = 8000

/** 请求体上限（小型控制接口，输入事件不会超过这个量级）。 */
const MAX_BODY_BYTES = 256 * 1024

/** 会话 id 白名单：**拼进上游 URL 前必须校验**（防路径穿越/查询串注入）。 */
const SESSION_RE = /^[A-Za-z0-9._-]{1,64}$/

/** 环境变量读取（空串按未设置处理）。 */
function env(name) {
  const value = process.env[name]
  return typeof value === 'string' && value.trim() !== '' ? value.trim() : undefined
}

/** `DSH_DISPLAY_HOME`（与服务端同一个默认值）。 */
const DISPLAY_HOME = env('DSH_DISPLAY_HOME') || path.join(os.homedir(), '.cache', 'dsh-display')

/** 面板日志文件（宿主拉起服务时的 stdout/stderr 去处）。 */
const VIEWER_LOG = env('DSH_VIEW_LOG') || path.join(DISPLAY_HOME, 'viewer.log')

/** 是否允许宿主自动拉起服务（默认允许；`DSH_VIEW_MANAGED=0` 关掉）。 */
const MANAGED = !['0', 'false', 'no', 'off'].includes(String(env('DSH_VIEW_MANAGED') ?? '1').toLowerCase())

/** 期望端口：`DSH_VIEW_PORT`（宿主拉起服务时同时传给服务端）。 */
function expectedPort() {
  const raw = Number.parseInt(String(env('DSH_VIEW_PORT') ?? ''), 10)
  return Number.isInteger(raw) && raw > 0 && raw < 65536 ? raw : null
}

/** 日志：优先用宿主 logger，没有就退到 console。任何情况下不抛。 */
function log(level, message) {
  const line = `[${NAME}] ${message}`
  try {
    const logger = globalThis.__dshDisplayPanelLogger
    if (logger && typeof logger[level] === 'function') {
      logger[level](line)
      return
    }
  } catch {
    /* logger 不可用就落到 console */
  }
  const sink = level === 'error' ? console.error : level === 'warn' ? console.warn : console.log
  try {
    sink(line)
  } catch {
    /* stdout 关掉了也不能抛 */
  }
}

/**
 * 模块级共享状态。
 *
 * 挂在 `globalThis` 上而不是模块作用域：profile 的 `patchReload: live` 会重新加载
 * 插件模块，模块作用域的变量会被重置 —— 那样已经拉起的服务就会变成"孤儿"
 * （既不知道是自己起的，也没法 stop/restart）。
 */
const SHARED_KEY = Symbol.for('dsh-display-panel.state')
const SHARED = (globalThis[SHARED_KEY] = globalThis[SHARED_KEY] || {
  /** 最近一次探测结果（见 probe()）。 */
  status: null,
  /** 上一次探测时间戳（毫秒）。 */
  at: 0,
  /** 进行中的探测 promise（并发去重，别把服务探成 DDoS）。 */
  inflight: null,
  /** 宿主拉起的子进程。 */
  child: null,
  /** 我们是否尝试过自动拉起（避免反复 spawn）。 */
  spawnAttempted: false,
  /** 是否尝试过"从旧版服务升级到本插件的服务"（只试一次）。 */
  upgradeAttempted: false,
  /** 活跃的 SSE 连接（卸载时统一关闭）。 */
  sse: new Set(),
  /** webServer 实例 → 已注册路径集合（effect 重复执行时避免重复注册）。 */
  registry: new WeakMap(),
  /** python 解释器缓存：undefined = 还没查过，null = 查过、没有。 */
  python: undefined,
})

/** 该 webServer 上尚未注册过的路径。 */
function claim(webServer, keys) {
  let set = SHARED.registry.get(webServer)
  if (!set) {
    set = new Set()
    SHARED.registry.set(webServer, set)
  }
  return keys.filter((key) => !set.has(key)).map((key) => {
    set.add(key)
    return key
  })
}

// ---------------------------------------------------------------------------
// 小工具
// ---------------------------------------------------------------------------

/** 写 JSON 响应（统一 `no-store`；客户端断开就安静收场）。 */
function writeJson(res, status, value) {
  let body
  try {
    body = Buffer.from(JSON.stringify(value), 'utf8')
  } catch (error) {
    body = Buffer.from(JSON.stringify({ ok: false, error: `respond failed: ${String(error && error.message)}` }), 'utf8')
    status = 500
  }
  try {
    if (!res.headersSent) {
      res.writeHead(status, {
        'content-type': 'application/json; charset=utf-8',
        'content-length': String(body.length),
        'cache-control': 'no-store',
      })
    }
    res.end(body)
  } catch {
    /* 客户端提前断开：没有任何补救动作是有意义的 */
  }
}

/** 失败响应：契约里所有失败都是 `{ok:false,error,hint}`。 */
function fail(res, status, error, hint) {
  const payload = { ok: false, error: String(error) }
  if (hint) payload.hint = String(hint)
  writeJson(res, status, payload)
}

/** 从 URL 里取一个查询参数（`req.url` 是路径+查询串，不需要 base）。 */
function queryParam(req, name) {
  const raw = typeof req.url === 'string' ? req.url : ''
  const index = raw.indexOf('?')
  if (index < 0) return undefined
  try {
    const value = new URLSearchParams(raw.slice(index + 1)).get(name)
    return value === null || value === '' ? undefined : value
  } catch {
    return undefined
  }
}

/** 读请求体（带上限；超限抛错让调用方回 413）。 */
async function readBody(req) {
  const chunks = []
  let size = 0
  for await (const chunk of req) {
    size += chunk.length
    if (size > MAX_BODY_BYTES) {
      const error = new Error(`request body too large (max ${MAX_BODY_BYTES} bytes)`)
      error.status = 413
      throw error
    }
    chunks.push(chunk)
  }
  return Buffer.concat(chunks)
}

/** 宽松解析 JSON：解析不了就返回 undefined（上游可能回 HTML/纯文本）。 */
function parseJson(buffer) {
  if (!buffer || buffer.length === 0) return undefined
  try {
    return JSON.parse(buffer.toString('utf8'))
  } catch {
    return undefined
  }
}

/** 会话 id 校验；不合法返回 null。 */
function validSession(raw) {
  return typeof raw === 'string' && SESSION_RE.test(raw) ? raw : null
}

/** 校验失败时的统一提示。 */
const SESSION_HINT = 'session 参数只允许 ^[A-Za-z0-9._-]{1,64}$（由客户端从当前会话 id 传入）'

// ---------------------------------------------------------------------------
// 令牌与端口文件
// ---------------------------------------------------------------------------

/** 读 `<home>/token`（只在本进程内用来拼上游 URL，绝不写进响应）。 */
function readToken() {
  try {
    return fs.readFileSync(path.join(DISPLAY_HOME, 'token'), 'utf8').trim()
  } catch {
    return ''
  }
}

/**
 * 确保令牌存在（目录 700 / 文件 600，与服务端 `ensure_token()` 完全一致）。
 *
 * 为什么宿主也要写：拉起服务时把令牌作为 `DSH_VIEW_TOKEN` 传下去，服务端会沿用，
 * 于是**从服务启动的第一毫秒起**两边令牌就是同一个 —— 否则会出现"服务刚起、
 * 令牌文件还没写"的窗口期，那期间所有转发都 403。
 */
function ensureToken() {
  const existing = readToken()
  if (existing) return existing
  let token = ''
  try {
    token = require('crypto').randomBytes(16).toString('hex')
    fs.mkdirSync(DISPLAY_HOME, { recursive: true, mode: 0o700 })
    try {
      fs.chmodSync(DISPLAY_HOME, 0o700)
    } catch {
      /* 目录不是自己的就算了 */
    }
    fs.writeFileSync(path.join(DISPLAY_HOME, 'token'), token, { mode: 0o600 })
    try {
      fs.chmodSync(path.join(DISPLAY_HOME, 'token'), 0o600)
    } catch {
      /* 同上 */
    }
  } catch (error) {
    log('warn', `无法写令牌文件（${DISPLAY_HOME}/token）：${String(error && error.message)}`)
  }
  return token
}

/** 读 `<home>/port`。 */
function readPortFile() {
  try {
    const raw = fs.readFileSync(path.join(DISPLAY_HOME, 'port'), 'utf8').trim()
    const port = Number.parseInt(raw, 10)
    return Number.isInteger(port) && port > 0 && port < 65536 ? port : null
  } catch {
    return null
  }
}

// ---------------------------------------------------------------------------
// 上游 HTTP
// ---------------------------------------------------------------------------

/**
 * 一次上游请求。
 *
 * 用 `http.request`（而不是 fetch）是为了**零依赖 + 完全控制**：超时用
 * `AbortController`，响应体自己收成 Buffer（帧最大也就几百 KB）。
 * 任何失败都以 `{ok:false}` 返回，**不抛** —— 调用方只需要看 `ok`。
 *
 * @param {object} options
 * @param {number} options.port - 上游端口。
 * @param {string} options.pathname - 上游路径（已含 `/s/<sid>/...`）。
 * @param {string} [options.query] - 额外查询串（如 `t=123`）；令牌由本函数拼。
 * @param {string} [options.method] - HTTP 方法，默认 GET。
 * @param {Buffer} [options.body] - 请求体。
 * @param {string} [options.contentType] - 请求体类型。
 * @param {number} [options.timeout] - 超时毫秒。
 * @param {string} [options.token] - 令牌；省略则现读。
 */
function upstream(options) {
  const {
    port,
    pathname,
    query,
    method = 'GET',
    body,
    contentType,
    timeout = TIMEOUT_DEFAULT_MS,
  } = options
  const token = options.token !== undefined ? options.token : readToken()

  const params = []
  if (query) params.push(query)
  // 令牌只在宿主侧拼上去（§1：服务监听 127.0.0.1，同机其它用户也能连，必须带 k）。
  if (token) params.push(`k=${encodeURIComponent(token)}`)
  const search = params.length > 0 ? `?${params.join('&')}` : ''

  return new Promise((resolve) => {
    let settled = false
    const done = (value) => {
      if (settled) return
      settled = true
      resolve(value)
    }

    let req
    try {
      req = http.request(
        {
          host: '127.0.0.1',
          port,
          path: `${pathname}${search}`,
          method,
          headers: {
            accept: 'application/json, image/jpeg, */*',
            ...(body && body.length > 0
              ? {
                  'content-type': contentType || 'application/json; charset=utf-8',
                  'content-length': String(body.length),
                }
              : {}),
          },
        },
        (res) => {
          const chunks = []
          res.on('data', (chunk) => chunks.push(chunk))
          res.on('end', () => {
            done({
              ok: true,
              status: res.statusCode || 0,
              headers: res.headers,
              body: Buffer.concat(chunks),
            })
          })
          res.on('error', (error) => done({ ok: false, error: `upstream read failed: ${error.message}` }))
        },
      )
    } catch (error) {
      done({ ok: false, error: `upstream request failed: ${String(error && error.message)}` })
      return
    }

    const timer = setTimeout(() => {
      try {
        req.destroy(new Error(`upstream timeout after ${timeout}ms`))
      } catch {
        /* 已经结束了 */
      }
    }, timeout)
    if (typeof timer.unref === 'function') timer.unref()

    req.on('error', (error) => {
      done({ ok: false, error: String((error && error.message) || error) })
    })
    req.on('close', () => clearTimeout(timer))
    if (body && body.length > 0) req.write(body)
    req.end()
  })
}

// ---------------------------------------------------------------------------
// 服务探测
// ---------------------------------------------------------------------------

/**
 * 确认某个端口上是不是"我们的"显示器服务。
 *
 * 顺序（新老版本都能识别）：
 *  1. `GET /health?k=` —— 新版本有；`{"ok":true,"service":"dsh-display-viewer",...}`；
 *  2. 退到 `GET /?k=` —— 老版本没有 `/health`（会拿到索引页），**200 就说明服务活着**。
 *
 * ⚠️ **403 不算"我们的服务在跑"**：那说明端口上有个服务但令牌不匹配（别的用户的服务，
 * 或 `DSH_DISPLAY_HOME` 指错了地方）。把它当 running=true 会让面板进入 streaming 然后
 * 每一帧都 403 —— 实测踩过（本机 8099 上正好有另一个实例）。这种情况如实报 running=false，
 * 但把原因写进 note，面板才能告诉用户"端口被谁占了"。
 *
 * 为什么不用 `/state` 探测：那会顺手把 Xvfb 拉起来（有副作用）。`/health` 与索引页
 * 都不创建会话。
 */
async function checkPort(port, token) {
  const health = await upstream({
    port,
    pathname: '/health',
    timeout: TIMEOUT_PROBE_MS,
    token,
  })
  if (health.ok && health.status >= 200 && health.status < 300) {
    const info = parseJson(health.body)
    if (info && typeof info === 'object' && (info.service === 'dsh-display-viewer' || info.ok === true)) {
      return {
        running: true,
        port,
        legacy: false,
        backend: typeof info.backend === 'string' ? info.backend : null,
        size: typeof info.size === 'string' ? info.size : null,
        input: typeof info.input === 'boolean' ? info.input : null,
        realDesktop: typeof info.realDesktop === 'boolean' ? info.realDesktop : null,
        version: typeof info.version === 'string' ? info.version : null,
        pid: Number.isInteger(info.pid) ? info.pid : null,
        sessions: Number.isInteger(info.sessions) ? info.sessions : null,
        missing: Array.isArray(info.missing) ? info.missing : [],
      }
    }
    // /health 存在但 403：端口上有服务，只是不是"我们的"。
    if (health.status === 403) {
      return notOurs(port, '令牌不匹配', 'health-403')
    }
  }

  const root = await upstream({ port, pathname: '/', timeout: TIMEOUT_PROBE_MS, token })
  if (root.ok && root.status === 200) {
    return {
      running: true,
      port,
      legacy: true,
      backend: null,
      size: null,
      input: null,
      realDesktop: null,
      version: null,
      pid: null,
      sessions: null,
      missing: [],
      note: '服务在运行，但版本较旧（没有 /health），后端/尺寸/缺依赖等诊断信息不可用',
    }
  }
  if (root.ok && root.status === 403) {
    return notOurs(port, '令牌不匹配', '403')
  }

  return {
    running: false,
    port,
    legacy: false,
    backend: null,
    size: null,
    input: null,
    realDesktop: null,
    version: null,
    pid: null,
    sessions: null,
    missing: [],
    note: health.ok ? `端口 ${port} 上没有显示器服务` : `端口 ${port} 连接失败：${health.error}`,
  }
}

/** "端口被占但不是我们的服务" —— 如实报 running=false，把原因带上。 */
function notOurs(port, why, code) {
  return {
    running: false,
    port,
    legacy: false,
    backend: null,
    size: null,
    input: null,
    realDesktop: null,
    version: null,
    pid: null,
    sessions: null,
    missing: [],
    note: `端口 ${port} 上有服务在应答，但它不接受本用户的令牌（${why}，HTTP ${code}）：`
      + '可能是别的用户/另一个 DSH_DISPLAY_HOME 的实例。宿主不会去动它。',
    conflict: true,
  }
}

/** 探测：① `<home>/port` → ② 8099..8110。 */
async function probe({ force = false } = {}) {
  const now = Date.now()
  if (!force && SHARED.status && now - SHARED.at < 1500) return SHARED.status
  if (SHARED.inflight) return SHARED.inflight

  SHARED.inflight = (async () => {
    let token = readToken()
    if (!token) token = ensureToken()

    const tried = []
    const filePort = readPortFile() || expectedPort()
    const candidates = []
    if (filePort) candidates.push(filePort)
    for (let port = PORT_SCAN_FIRST; port <= PORT_SCAN_LAST; port += 1) {
      if (!candidates.includes(port)) candidates.push(port)
    }

    let result = null
    /** 旧版服务（没有 /health）先记下来当兜底：找到本插件的服务就换掉它。 */
    let legacyFallback = null
    const conflicts = []
    for (const port of candidates) {
      const check = await checkPort(port, token)
      if (check.running) {
        // ⚠️ 同一个 DSH_DISPLAY_HOME 上可能同时跑着**别的项目**的旧版 viewer
        //    （例如控制台仓库那份，它也在 8099、也用同一份 token）。
        //    旧版没有 /health、/exec、cursor —— 能用但缺能力，所以：
        //    本插件的服务（有 /health）优先，旧版只做兜底。
        if (check.legacy !== true) {
          result = { ...check, home: DISPLAY_HOME, log: VIEWER_LOG, token }
          break
        }
        if (legacyFallback === null) {
          legacyFallback = { ...check, home: DISPLAY_HOME, log: VIEWER_LOG, token }
        }
        tried.push(`${port}: 旧版服务（无 /health），继续找本插件的服务`)
        continue
      }
      if (check.conflict) conflicts.push(check.note)
      tried.push(`${port}: ${check.note}`)
    }
    if (result === null) result = legacyFallback
    if (!result) {
      const conflictNote = conflicts.length > 0 ? `；${conflicts[0]}` : ''
      result = {
        running: false,
        port: filePort || DEFAULT_PORT,
        legacy: false,
        backend: null,
        size: null,
        input: null,
        realDesktop: null,
        version: null,
        pid: null,
        sessions: null,
        missing: [],
        home: DISPLAY_HOME,
        log: VIEWER_LOG,
        token,
        note: `未找到显示器服务（已探测 ${candidates.length} 个端口）${conflictNote}`,
        tried,
        conflict: conflicts.length > 0,
      }
    }
    const previousPort = SHARED.status && SHARED.status.port
    SHARED.status = result
    SHARED.at = Date.now()
    if (result.running && previousPort !== result.port) {
      log('info', `显示器服务在 127.0.0.1:${result.port}（${result.legacy ? '旧版本，无 /health' : `${result.backend || '?'} ${result.size || ''}`}）`)
    }
    return result
  })()

  try {
    return await SHARED.inflight
  } finally {
    SHARED.inflight = null
  }
}

// ---------------------------------------------------------------------------
// 服务生命周期
// ---------------------------------------------------------------------------

/**
 * 找 python 解释器。`DSH_VIEW_PYTHON` 优先；找不到返回 null（调用方写进 missing）。
 *
 * 用 `spawnSync` 逐个 `--version` 试：比 `which` 可靠（Windows/便携包没有 which），
 * 而且失败只花几毫秒。
 */
function findPython() {
  const explicit = env('DSH_VIEW_PYTHON')
  const candidates = explicit
    ? [explicit]
    : process.platform === 'win32'
      ? ['python.exe', 'python3.exe', 'py.exe']
      : ['python3', 'python']
  for (const candidate of candidates) {
    try {
      const probe = spawnSync(candidate, ['--version'], { stdio: 'ignore', timeout: 5000 })
      if (!probe.error && probe.status === 0) return candidate
    } catch {
      /* 试下一个 */
    }
  }
  return null
}

/** 缺失项（给面板显示"为什么用不了、怎么装"）。 */
function pythonMissing() {
  return [
    {
      tool: 'python3',
      why: `宿主无法拉起显示器服务（${SERVICE_SCRIPT}）`,
      package: process.platform === 'win32' ? 'python.org 装 Python，或设 DSH_VIEW_PYTHON' : 'apt install python3（或用 DSH_VIEW_PYTHON 指定解释器）',
    },
  ]
}

/** 子进程是否还活着。 */
function childAlive() {
  const child = SHARED.child
  if (!child) return false
  return child.exitCode === null && child.signalCode === null
}

/**
 * python 解释器是否可用（**结果缓存**：`spawnSync --version` 会阻塞事件循环几毫秒，
 * 而它只在"服务没在跑、要给用户解释为什么"时才需要）。
 *
 * @returns {string|null} 解释器名字；找不到返回 null。
 */
function pythonAvailable() {
  if (SHARED.python === undefined) SHARED.python = findPython()
  return SHARED.python
}

/**
 * 拉起服务：`detached` + `unref`，所以**不随宿主退出**（用户关掉 DSH，显示器里的
 * 程序不该跟着死）；日志走文件，找不到 python 就明说。
 */
function spawnService() {
  const python = findPython()
  if (!python) {
    log('warn', `找不到 python 解释器，无法自动拉起 ${SERVICE_SCRIPT}（可用 DSH_VIEW_PYTHON 指定）`)
    return { ok: false, error: 'python interpreter not found', missing: pythonMissing() }
  }

  const token = ensureToken()
  const port = expectedPort()
  // 查到解释器就顺手更新缓存：否则 pythonAvailable() 可能一直记着"没有 python"，
  // 让 /info 的 missing 里永远挂着一个已经不缺的依赖。
  SHARED.python = python
  let logFd = null
  try {
    fs.mkdirSync(path.dirname(VIEWER_LOG), { recursive: true, mode: 0o700 })
    logFd = fs.openSync(VIEWER_LOG, 'a')
  } catch (error) {
    log('warn', `无法打开日志文件 ${VIEWER_LOG}：${String(error && error.message)}`)
  }

  try {
    const child = spawn(python, [SERVICE_SCRIPT], {
      detached: true,
      stdio: ['ignore', logFd === null ? 'ignore' : logFd, logFd === null ? 'ignore' : logFd],
      env: {
        ...process.env,
        // 令牌从第一毫秒就与服务端一致（服务端会沿用这个值）。
        DSH_VIEW_TOKEN: token,
        DSH_DISPLAY_HOME: DISPLAY_HOME,
        ...(port === null ? {} : { DSH_VIEW_PORT: String(port) }),
        ...(env('DSH_VIEW_LOG') ? {} : { DSH_VIEW_LOG: VIEWER_LOG }),
      },
    })
    SHARED.child = child
    child.on('error', (error) => {
      log('warn', `显示器服务启动失败：${String(error && error.message)}`)
    })
    child.on('exit', (code, signal) => {
      if (SHARED.child === child) SHARED.child = null
      log('info', `宿主拉起的显示器服务已退出（code=${code} signal=${signal}）`)
    })
    child.unref()
    log('info', `已拉起显示器服务：${python} ${SERVICE_SCRIPT}（pid ${child.pid}，日志 ${VIEWER_LOG}）`)
    return { ok: true, pid: child.pid, python }
  } catch (error) {
    return { ok: false, error: String((error && error.message) || error) }
  } finally {
    if (logFd !== null) {
      try {
        fs.closeSync(logFd)
      } catch {
        /* 子进程已经 dup 走了，关不掉也无所谓 */
      }
    }
  }
}

/** 等 `/health` 通过（最多 SPAWN_WAIT_MS）。 */
async function waitForService() {
  const deadline = Date.now() + SPAWN_WAIT_MS
  let last = null
  while (Date.now() < deadline) {
    const result = await probe({ force: true })
    if (result.running) return result
    last = result
    await new Promise((resolve) => {
      const timer = setTimeout(resolve, 400)
      if (typeof timer.unref === 'function') timer.unref()
    })
  }
  return last || (await probe({ force: true }))
}

/**
 * 确保有服务可用：没起就（在允许的前提下）自动拉起。**内部实现**，对外见 ensureService()。
 *
 * @param {object} [options]
 * @param {boolean} [options.spawn] - 是否允许拉起，默认按 DSH_VIEW_MANAGED。
 * @returns {Promise<object>} 最新探测结果。
 */
async function ensureRunning({ spawn: allowSpawn } = {}) {
  let status = await probe()

  const canSpawn = allowSpawn === undefined ? MANAGED : allowSpawn === true && MANAGED

  // 只有旧版服务在跑（没有 /health、/exec、cursor）：能用但缺能力。
  // 允许自动拉起时，把**本插件的服务**也起起来（它会顺延到下一个空闲端口），
  // 然后重新探测 —— probe() 优先本插件的服务，于是 exec/光标/诊断都能用上。
  if (status.running && status.legacy === true && canSpawn && !SHARED.upgradeAttempted) {
    SHARED.upgradeAttempted = true
    log('info', `检测到旧版显示器服务（127.0.0.1:${status.port}，无 /health）—— 尝试另外拉起本插件的服务以获得完整能力`)
    const spawned = spawnService()
    if (spawned.ok) {
      // ⚠️ 这里不能直接用 waitForService()：旧版服务仍在应答，probe() 会立刻返回"running"，
      //    于是升级判定会在本插件的服务起来之前就放弃。要明确等到**有 /health 的**那个。
      const deadline = Date.now() + SPAWN_WAIT_MS
      while (Date.now() < deadline) {
        const next = await probe({ force: true })
        if (next.running && next.legacy !== true) return next
        await new Promise((resolve) => {
          const timer = setTimeout(resolve, 400)
          if (typeof timer.unref === 'function') timer.unref()
        })
      }
      log('warn', `本插件的服务未在 ${SPAWN_WAIT_MS}ms 内就绪，暂时继续用旧版服务（日志 ${VIEWER_LOG}）`)
    }
    return status
  }

  if (status.running) return status

  if (!canSpawn) {
    if (allowSpawn === true && !MANAGED) {
      return { ...status, note: `${status.note}；自动拉起已被 DSH_VIEW_MANAGED=0 关闭`, missing: [...status.missing, ...pythonMissing()] }
    }
    return status
  }

  if (SHARED.spawnAttempted && !childAlive()) {
    // 起过一次又死了：再试一次（用户可能刚装上 python / 修好依赖）。
    SHARED.spawnAttempted = false
  }
  SHARED.spawnAttempted = true
  const spawned = spawnService()
  if (!spawned.ok) {
    const result = await probe({ force: true })
    return { ...result, missing: [...result.missing, ...(spawned.missing || pythonMissing())], note: `自动拉起失败：${spawned.error}` }
  }
  status = await waitForService()
  if (!status.running) {
    return { ...status, missing: [...status.missing, ...pythonMissing()], note: `${status.note}；已尝试拉起但 /health 未在 ${SPAWN_WAIT_MS}ms 内就绪（日志 ${VIEWER_LOG}）` }
  }
  return status
}

/** 平台默认后端（拿不到服务自报时用）。 */
function defaultBackend() {
  if (process.platform === 'win32') return 'win32'
  if (process.platform === 'darwin') return 'darwin'
  return 'x11'
}

/**
 * 归一化成对外承诺的 `ServiceState`（docs/CONTRACT.md §2.5）。
 *
 * 与内部探测结果的区别只有一处：**服务没在跑时 `port` 一律是 null** ——
 * `tools.js` 靠 `running !== true || !port` 判断"不可用"，给一个"期望端口"会把它骗过去。
 * 内部用到的字段（`pid`/`sessions`/`note`/…）一并带上，`tools.js` 忽略它们。
 *
 * @param {object} status - probe()/ensureRunning() 的结果。
 * @returns {object} ServiceState。
 */
function serviceState(status) {
  const running = Boolean(status.running)
  const missing = Array.isArray(status.missing) ? [...status.missing] : []
  // 服务没跑、连 python 都没有 —— 这就是"为什么用不了"的完整答案，必须带上。
  if (!running && pythonAvailable() === null) missing.push(...pythonMissing())
  return {
    running,
    port: running ? status.port : null,
    token: status.token || null,
    backend: running ? status.backend || defaultBackend() : null,
    size: running ? status.size || null : null,
    missing,
    home: DISPLAY_HOME,
    log: VIEWER_LOG,
    managed: childAlive(),
    // 以下为宿主半边内部/面板用的附加字段（tools.js 不看）。
    version: status.version || null,
    pid: status.pid === undefined ? null : status.pid,
    sessions: status.sessions === undefined ? null : status.sessions,
    input: status.input === undefined ? null : status.input,
    realDesktop: status.realDesktop === undefined ? null : status.realDesktop,
    legacy: Boolean(status.legacy),
    note: status.note || null,
    tried: Array.isArray(status.tried) ? status.tried : [],
  }
}

/** 探测失败时的兜底 ServiceState（**绝不抛给调用方**）。 */
function unavailableState(reason) {
  return serviceState({
    running: false,
    port: null,
    backend: null,
    size: null,
    input: null,
    realDesktop: null,
    version: null,
    pid: null,
    sessions: null,
    missing: [],
    legacy: false,
    home: DISPLAY_HOME,
    log: VIEWER_LOG,
    token: readToken(),
    note: reason,
  })
}

/**
 * 对外通道①：**只探测，绝不拉起服务**（给 `display_panel_status`/`sessions` 用）。
 *
 * @param {boolean} [force] - true 时绕过 1.5s 缓存，强制重新探测。
 * @returns {Promise<object>} ServiceState。
 */
async function discover(force = false) {
  try {
    return serviceState(await probe({ force: force === true }))
  } catch (error) {
    return unavailableState(`探测失败：${String((error && error.message) || error)}`)
  }
}

/**
 * 对外通道②：服务没跑就拉起（给 `display_panel_open`/`run` 等用）。
 *
 * @param {object} [options] - 透传给内部实现（`{spawn:true}` 强制允许拉起）。
 * @returns {Promise<object>} ServiceState。
 */
async function ensureService(options) {
  try {
    return serviceState(await ensureRunning(options))
  } catch (error) {
    return unavailableState(`拉起服务失败：${String((error && error.message) || error)}`)
  }
}

/**
 * 停服务。**只停宿主自己拉起的那个**：别人的 systemd 服务不该被面板偷偷杀掉。
 *
 * @returns {Promise<{ok:boolean, stopped:boolean, pid?:number, error?:string, note?:string}>}
 */
async function stopService() {
  if (!childAlive()) {
    SHARED.child = null
    return { ok: true, stopped: false, note: '没有由本宿主拉起的显示器服务（外部启动的服务需自行停止）' }
  }
  const child = SHARED.child
  const pid = child.pid
  try {
    child.kill('SIGTERM')
  } catch (error) {
    return { ok: false, stopped: false, pid, error: String((error && error.message) || error) }
  }
  const deadline = Date.now() + 3000
  while (Date.now() < deadline && childAlive()) {
    await new Promise((resolve) => {
      const timer = setTimeout(resolve, 100)
      if (typeof timer.unref === 'function') timer.unref()
    })
  }
  if (childAlive()) {
    try {
      child.kill('SIGKILL')
    } catch {
      /* 已经不在了 */
    }
  }
  log('info', `已停止宿主拉起的显示器服务（pid ${pid}）`)
  return { ok: true, stopped: true, pid }
}

// ---------------------------------------------------------------------------
// 状态组装
// ---------------------------------------------------------------------------

/** `P/info` 的服务块。 */
function serviceBlock(status) {
  const running = Boolean(status.running)
  return {
    running,
    // 服务没跑时给"期望端口"（`<home>/port` 或 DSH_VIEW_PORT）：面板要能告诉用户
    // "它本该在哪个端口"，而 tools.js 走的是 serviceState()（那里没跑就是 null）。
    port: running ? status.port : readPortFile() || expectedPort() || DEFAULT_PORT,
    managed: childAlive(),
    backend: status.backend || defaultBackend(),
    size: status.size,
    version: status.version || VERSION,
    pid: status.pid === undefined ? null : status.pid,
    sessions: status.sessions === undefined ? null : status.sessions,
    legacy: Boolean(status.legacy),
    autoStart: MANAGED,
  }
}

/**
 * `P/info` 的完整响应（契约 §2）。
 *
 * **绝不含令牌**：只给端口、状态与日志路径。
 */
function infoPayload(status) {
  const backend = status.backend || defaultBackend()
  const realDesktop = status.realDesktop === null || status.realDesktop === undefined
    ? backend === 'win32' || backend === 'darwin'
    : status.realDesktop
  const enabled = status.input === null || status.input === undefined
    ? !(backend === 'win32' || backend === 'darwin')
    : status.input

  const missing = Array.isArray(status.missing) ? status.missing : []
  const hints = []
  if (!status.running) {
    hints.push(MANAGED ? '服务未运行：面板会自动拉起（或 POST /service {action:"start"}）' : '服务未运行：自动拉起已关闭（DSH_VIEW_MANAGED=0），请手工启动')
    // 探测结论（比如"8099 上那个服务不是本用户的"）必须留给用户看，否则只剩"没找到"。
    if (status.note) hints.push(status.note)
  } else if (status.note) {
    hints.push(status.note)
  }
  if (missing.length > 0) hints.push(`缺少依赖：${missing.map((item) => item && item.tool).filter(Boolean).join('、')}`)
  hints.push(`日志：${VIEWER_LOG}`)

  return {
    ok: true,
    service: serviceBlock(status),
    input: { enabled, realDesktop },
    missing,
    home: DISPLAY_HOME,
    viewer: {
      log: VIEWER_LOG,
      script: SERVICE_SCRIPT,
      hint: hints.join('；'),
    },
  }
}

/** 给透传响应补的 `service` 字段（客户端据此决定要不要显示"打开显示器"）。 */
function serviceField(status) {
  return serviceBlock(status)
}

// ---------------------------------------------------------------------------
// 路由
// ---------------------------------------------------------------------------

/** 网关守卫：未通过就写完响应并返回 true。 */
function rejected(connection, req, res) {
  try {
    const rejection = connection.requestRejection(req)
    if (rejection === undefined) return false
    res.writeHead(rejection, { 'content-type': 'text/plain; charset=utf-8' })
    res.end(rejection === 401 ? 'unauthorized' : 'forbidden')
    return true
  } catch (error) {
    fail(res, 500, `request guard failed: ${String((error && error.message) || error)}`)
    return true
  }
}

/**
 * 构造全部路由。
 *
 * 每个 handler 的最外层都是 try/catch：面板接口坏掉只能回 JSON 错误，
 * **绝不能把异常扔回宿主**。
 */
function makeRoutes({ connection }) {
  /**
   * 透传类接口的公共流程：守卫 → 校验 session → 拼上游 → 补 `service` → 回 JSON。
   *
   * @param {object} spec
   * @param {string} spec.suffix - 上游子路径（`state` / `display` / `input` / `exec` / `procs` / `kill`）。
   * @param {'GET'|'POST'} [spec.method] - **上游**用的方法（与浏览器用的方法无关，默认 GET）。
   * @param {boolean} [spec.forwardBody] - 是否把请求体原样转发给上游。
   */
  const passthrough = (spec) => async (req, res) => {
    try {
      if (rejected(connection, req, res)) return

      let body
      if (spec.forwardBody) {
        try {
          body = await readBody(req)
        } catch (error) {
          fail(res, error.status || 400, String((error && error.message) || error))
          return
        }
      }

      // session 一律取自查询串（契约 §2）；body 里的 `session` 只是客户的便利写法，
      // 只有当查询串没给、且 body 里的值**通过同一套校验**时才采用。
      let rawSession = queryParam(req, 'session')
      if (rawSession === undefined && body && body.length > 0) {
        const parsed = parseJson(body)
        const fromBody = parsed && typeof parsed === 'object' ? parsed.session : undefined
        if (typeof fromBody === 'string') rawSession = fromBody
      }
      const session = validSession(rawSession)
      if (session === null) {
        fail(
          res,
          400,
          rawSession === undefined ? 'missing session parameter' : `invalid session parameter: ${String(rawSession).slice(0, 64)}`,
          SESSION_HINT,
        )
        return
      }

      const status = await ensureService()
      if (!status.running) {
        fail(res, 503, 'display service is not running', status.note || `日志：${VIEWER_LOG}`)
        return
      }

      const upstreamMethod = spec.method || 'GET'
      const t = queryParam(req, 't')
      const timeout = spec.timeout || (spec.suffix === 'exec' && bodyIsWait(body) ? TIMEOUT_EXEC_WAIT_MS : TIMEOUT_DEFAULT_MS)
      const result = await upstream({
        port: status.port,
        pathname: `/s/${session}/${spec.suffix}`,
        query: t ? `t=${encodeURIComponent(t)}` : undefined,
        method: upstreamMethod,
        body: spec.forwardBody ? body : undefined,
        contentType: req.headers['content-type'],
        timeout,
      })

      if (!result.ok) {
        fail(res, 503, `upstream unreachable: ${result.error}`, `日志：${VIEWER_LOG}`)
        return
      }

      const parsed = parseJson(result.body)
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
        writeJson(res, result.status, { ...parsed, service: serviceField(status) })
        return
      }
      // 上游回了非 JSON（HTML 错误页等）：原样包成 JSON，客户端不必猜。
      writeJson(res, result.status || 502, {
        ok: false,
        error: 'unexpected upstream response',
        status: result.status,
        body: result.body.toString('utf8').slice(0, 2000),
        service: serviceField(status),
      })
    } catch (error) {
      fail(res, 500, String((error && error.message) || error))
    }
  }

  /** body 是否是 `{"wait":true,...}`（决定 exec 超时）。 */
  function bodyIsWait(buffer) {
    const parsed = parseJson(buffer)
    return Boolean(parsed && typeof parsed === 'object' && parsed.wait === true)
  }

  return [
    {
      kind: 'exact',
      path: `${BASE_PATH}/info`,
      handler: async (req, res) => {
        try {
          if (rejected(connection, req, res)) return
          const method = req.method || 'GET'
          if (method !== 'GET' && method !== 'POST') {
            fail(res, 405, 'method not allowed')
            return
          }
          let status
          if (method === 'POST') {
            const body = await readBody(req).catch(() => Buffer.alloc(0))
            const action = queryParam(req, 'action') || (parseJson(body) || {}).action
            if (action === 'restart') {
              await stopService()
              status = await ensureService({ spawn: true })
            } else {
              status = await discover(true)
              if (!status.running) status = await ensureService()
            }
          } else {
            status = await discover()
          }
          writeJson(res, 200, infoPayload(status))
        } catch (error) {
          fail(res, 500, String((error && error.message) || error))
        }
      },
    },
    {
      kind: 'exact',
      path: `${BASE_PATH}/frame`,
      handler: async (req, res) => {
        try {
          if (rejected(connection, req, res)) return
          if ((req.method || 'GET') !== 'GET') {
            fail(res, 405, 'method not allowed')
            return
          }
          const rawSession = queryParam(req, 'session')
          const session = validSession(rawSession)
          if (session === null) {
            fail(res, 400, rawSession === undefined ? 'missing session parameter' : `invalid session parameter: ${String(rawSession).slice(0, 64)}`, SESSION_HINT)
            return
          }

          const status = await ensureService()
          if (!status.running) {
            fail(res, 503, 'display service is not running', status.note || `日志：${VIEWER_LOG}`)
            return
          }

          const t = queryParam(req, 't')
          const result = await upstream({
            port: status.port,
            pathname: `/s/${session}/snapshot`,
            query: t ? `t=${encodeURIComponent(t)}` : undefined,
            timeout: TIMEOUT_FRAME_MS,
          })

          if (!result.ok) {
            fail(res, 503, `upstream unreachable: ${result.error}`, `日志：${VIEWER_LOG}`)
            return
          }
          if (result.status !== 200) {
            // 上游 503（还没有帧）/404/403 等：告诉客户端真实状态码 + 原因。
            fail(res, result.status || 503, `upstream returned ${result.status} for session ${session}`, summarizeUpstream(result))
            return
          }
          const contentType = String(result.headers['content-type'] || '')
          if (!contentType.startsWith('image/')) {
            fail(res, 502, `upstream did not return an image (content-type: ${contentType || 'none'})`, summarizeUpstream(result))
            return
          }
          if (!res.headersSent) {
            res.writeHead(200, {
              'content-type': 'image/jpeg',
              'content-length': String(result.body.length),
              'cache-control': 'no-store, no-cache, must-revalidate, max-age=0',
              pragma: 'no-cache',
              expires: '0',
            })
          }
          res.end(result.body)
        } catch (error) {
          fail(res, 500, String((error && error.message) || error))
        }
      },
    },
    { kind: 'exact', path: `${BASE_PATH}/state`, handler: passthrough({ suffix: 'state', method: 'GET' }) },
    { kind: 'exact', path: `${BASE_PATH}/display`, handler: passthrough({ suffix: 'display', method: 'GET' }) },
    { kind: 'exact', path: `${BASE_PATH}/input`, handler: passthrough({ suffix: 'input', method: 'POST', forwardBody: true }) },
    { kind: 'exact', path: `${BASE_PATH}/exec`, handler: passthrough({ suffix: 'exec', method: 'POST', forwardBody: true }) },
    // 下面两个不在契约 §2 的必需清单里，是给面板/脚本的便利接口（上游 §1.2 的 procs/kill）。
    // 注意上游 `procs` 是 **GET**、`kill` 是 POST —— 浏览器侧的方法不参与决定，别写反。
    { kind: 'exact', path: `${BASE_PATH}/procs`, handler: passthrough({ suffix: 'procs', method: 'GET' }) },
    { kind: 'exact', path: `${BASE_PATH}/kill`, handler: passthrough({ suffix: 'kill', method: 'POST', forwardBody: true }) },
    {
      kind: 'exact',
      path: `${BASE_PATH}/service`,
      handler: async (req, res) => {
        try {
          if (rejected(connection, req, res)) return
          if ((req.method || 'GET') !== 'POST') {
            fail(res, 405, 'method not allowed（POST {"action":"start|stop|restart|status"}）')
            return
          }
          const body = await readBody(req).catch(() => Buffer.alloc(0))
          const action = String(queryParam(req, 'action') || (parseJson(body) || {}).action || 'status')

          let status
          let extra = {}
          if (action === 'start') {
            status = await ensureService({ spawn: true })
          } else if (action === 'stop') {
            const stopped = await stopService()
            status = await discover(true)
            extra = { stopped }
          } else if (action === 'restart') {
            const stopped = await stopService()
            status = await ensureService({ spawn: true })
            extra = { stopped }
          } else if (action === 'status') {
            status = await discover(true)
          } else {
            fail(res, 400, `unknown action: ${action}`, 'action 只能是 start|stop|restart|status')
            return
          }
          writeJson(res, 200, { ...infoPayload(status), ...extra, action })
        } catch (error) {
          fail(res, 500, String((error && error.message) || error))
        }
      },
    },
    {
      // 可选能力（契约标注为可选）：没有它客户端就轮询。SSE 只推**状态**，不推帧。
      kind: 'exact',
      path: `${BASE_PATH}/events`,
      handler: async (req, res) => {
        let timer = null
        let closed = false
        try {
          if (rejected(connection, req, res)) return
          const rawSession = queryParam(req, 'session')
          const session = validSession(rawSession)
          if (session === null) {
            fail(res, 400, rawSession === undefined ? 'missing session parameter' : `invalid session parameter: ${String(rawSession).slice(0, 64)}`, SESSION_HINT)
            return
          }
          res.writeHead(200, {
            'content-type': 'text/event-stream; charset=utf-8',
            'cache-control': 'no-store',
            connection: 'keep-alive',
            'x-accel-buffering': 'no',
          })
          const send = (event, data) => {
            if (closed) return
            try {
              res.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`)
            } catch {
              closed = true
            }
          }
          const tick = async () => {
            if (closed) return
            try {
              const status = await probe()
              let state = null
              if (status.running) {
                const result = await upstream({ port: status.port, pathname: `/s/${session}/state`, timeout: TIMEOUT_PROBE_MS })
                state = result.ok ? parseJson(result.body) || null : null
              }
              send('status', { ok: true, service: serviceField(status), session, state })
            } catch (error) {
              send('status', { ok: false, error: String((error && error.message) || error), session })
            }
          }
          SHARED.sse.add(res)
          send('hello', { ok: true, session })
          await tick()
          timer = setInterval(tick, 2000)
          if (typeof timer.unref === 'function') timer.unref()
          req.on('close', () => {
            closed = true
            SHARED.sse.delete(res)
            if (timer) clearInterval(timer)
          })
        } catch (error) {
          if (timer) clearInterval(timer)
          SHARED.sse.delete(res)
          if (!res.headersSent) fail(res, 500, String((error && error.message) || error))
          else {
            try {
              res.end()
            } catch {
              /* 已经断了 */
            }
          }
        }
      },
    },
  ].map((route) => ({ ...route, key: route.path }))
}

/** 上游非 200 时，把它的 JSON 错误信息摘要出来给客户端。 */
function summarizeUpstream(result) {
  const parsed = parseJson(result.body)
  if (parsed && typeof parsed === 'object') {
    return typeof parsed.error === 'string' ? parsed.error : typeof parsed.hint === 'string' ? parsed.hint : JSON.stringify(parsed).slice(0, 300)
  }
  const text = result.body.toString('utf8').replace(/\s+/g, ' ').trim()
  return text ? text.slice(0, 300) : `HTTP ${result.status}`
}

// ---------------------------------------------------------------------------
// 插件入口
// ---------------------------------------------------------------------------

module.exports = {
  name: NAME,
  /**
   * ⚠️ **刻意不声明顶层 `inject`**。
   *
   * 顶层 `inject: ['webServer','connection']` 会让 DSH 的 loader 把这个插件标成
   * `pending (waiting for services: ...)`，而 loader 把 pending 当"未激活"→ **整个
   * profile 起不来**（实测：花名册里没有 `@deepseek-ai/dsh-web-app` 的 profile 会直接
   * `Error: dsh: plugin tree failed to load: dsh: 1 entry did not activate`）。
   * 对用户来说那是"装了这个插件把 DSH 弄挂了"，比"面板不显示"严重得多。
   *
   * 本插件对宿主的两个服务都**不是必需的**：没有 webServer/connection 就只是少一个
   * 面板，没有 tools 就只是少几个 AI 工具。所以全部走回调式 `ctx.inject(..., cb)`：
   * 服务什么时候就位、甚至永远不就位，都不影响插件激活，更不影响 profile 启动。
   * （官方 dsh-browser-panel 也是这个套路：顶层只声明它真正必需的，路由放在回调里。）
   */
  apply(ctx) {
    try {
      log('info', `apply() entered（home=${DISPLAY_HOME}, managed=${MANAGED}）`)

      let mounted = false
      let toolsMounted = false
      ctx.inject(['webServer', 'connection'], (webCtx) => {
        let routes = []
        try {
          routes = makeRoutes({ connection: webCtx.connection })
        } catch (error) {
          log('error', `构造路由失败：${String(error && error.message)}`)
          return
        }
        // 一次 effect 管全部路由：注册失败（比如路径撞车）只影响面板，不影响宿主启动。
        webCtx.effect(
          () => {
            const disposers = []
            // live patchReload 会让 effect 重跑，而 webServer.register 对同一
            // (kind, path) 会**抛错**（"duplicate exact route"）：记下已经注册过的
            // 路径，重跑时只注册缺的那些，并把这次拿到的 disposer 都收好。
            for (const key of claim(webCtx.webServer, routes.map((route) => route.key))) {
              const route = routes.find((item) => item.key === key)
              try {
                disposers.push(webCtx.webServer.register({ kind: route.kind, path: route.path, handler: route.handler }))
              } catch (error) {
                log('warn', `注册路由 ${route.path} 失败：${String(error && error.message)}`)
              }
            }
            mounted = true
            return () => {
              for (const dispose of disposers) {
                try {
                  dispose()
                } catch {
                  /* 卸载时的异常不值得影响宿主 */
                }
              }
            }
          },
          `${NAME}: routes (${BASE_PATH}/*)`,
        )
        log('info', `host half mounted on ${BASE_PATH}/{info,frame,state,display,input,exec,procs,kill,service,events}`)

        // 启动时**只探测，不拉起**：拉 Python + Xvfb 是"用户真的要看画面"时才该付的代价，
        // 而 harness 每次启动都去拉一遍会让所有人（包括不用面板的人）多背一个进程。
        // 真正的自动拉起发生在：POST /info（重新探测）、POST /service{start}、
        // 以及 /frame、/state、/input、/exec 这些"要用显示"的请求上。
        // 绝不 await：探测再慢也不能拖住 harness 启动。
        void discover()
          .then((status) => {
            if (status.running) log('info', `显示器服务在 127.0.0.1:${status.port}（managed=${status.managed}）`)
            else log('info', `显示器服务未运行：${status.note || ''}（打开面板或 POST ${BASE_PATH}/service 会拉起）`)
          })
          .catch((error) => {
            log('warn', `初始探测失败：${String(error && error.message)}`)
          })
      })

      // 两段式看门狗：慢启动不能报成"没有服务"（本机实测 webServer 偶尔要 >8s 才就位，
      // 8 秒就下结论会打出误导性的"面板接口不注册"，而它其实马上就挂上了）。
      const watchdogSoon = setTimeout(() => {
        if (!mounted) log('info', '启动 8 秒内还没看到 webServer/connection（可能只是慢启动；25 秒仍没有才是真没有）')
        if (!toolsMounted) log('info', '启动 8 秒内还没看到 tools 服务（同上）')
      }, 8000)
      const watchdog = setTimeout(() => {
        if (!mounted) {
          log('warn', '没有 webServer/connection 服务（这个 profile 大概没装 dsh-web-app）—— 面板接口不注册。'
            + '这只是少一个面板：插件照常激活、不影响 profile 启动。')
        }
        if (!toolsMounted) {
          log('warn', '没有 tools 服务 —— display_panel_* 工具不注册（面板与显示器服务不受影响）。')
        }
      }, 25000)
      for (const timer of [watchdogSoon, watchdog]) {
        if (typeof timer.unref === 'function') timer.unref()
      }

      // AI 用的工具（`display_panel_*`）：这条路必须存在 —— bash 工具的 /tmp 是私有
      // tmpfs，看不到服务建的 X socket；而宿主自己的 HTTP 路由是 cookie 鉴权的，
      // AI 用 curl 打不进去。工具直接在宿主进程内调服务，绕开这两堵墙。
      //
      // 单独一个 inject：`tools` 服务没就位时**只少工具**，路由照常挂载。
      ctx.inject(['tools'], (toolsCtx) => {
        try {
          const { defineTools } = require('./tools.js')
          const defs = defineTools({ discover, ensureService })
          for (const def of defs) {
            try {
              toolsCtx.effect(() => toolsCtx.tools.register(def), `${NAME}: ${def.name}`)
            } catch (error) {
              log('warn', `注册工具 ${def.name} 失败：${String(error && error.message)}`)
            }
          }
          toolsMounted = true
          log('info', `已注册 ${defs.length} 个工具：${defs.map((def) => def.name).join(', ')}`)
        } catch (error) {
          // 工具注册失败绝不能影响路由挂载（面板是插件的门面）。
          log('warn', `tools skipped: ${String((error && error.stack) || error)}`)
        }
      })

      // 插件卸载：关掉 SSE、停掉**我们拉起的**服务、清掉定时器。
      ctx.effect(
        () => () => {
          clearTimeout(watchdog)
          for (const res of Array.from(SHARED.sse)) {
            try {
              res.end()
            } catch {
              /* 已经断了 */
            }
          }
          SHARED.sse.clear()
          void stopService().catch(() => {})
        },
        `${NAME}: lifetime`,
      )
    } catch (error) {
      // 宿主半边出任何问题都只是"面板拿不到画面"。
      log('warn', `host half skipped: ${String((error && error.message) || error)}`)
    }
  },
}
