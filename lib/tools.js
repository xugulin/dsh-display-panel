/**
 * dsh-display-panel — 给 AI 用的工具（`display_panel_*`）。
 *
 * 为什么必须有这一层：插件的卖点是"AI 把自己的 GUI 活干在**一台独立显示**上，
 * 人在面板里看着"，但**没有工具**时这条路是断的：
 *
 *   * bash 工具跑在沙箱里，`/tmp` 是私有 tmpfs —— 服务进程建的
 *     `/tmp/.X11-unix/X<n>` 在沙箱里**根本看不到**，`DISPLAY=:148 程序` 必然
 *     `Unable to open display`（本机实测）；
 *   * 宿主自己的 HTTP 路由是**同源 + cookie 鉴权**的，AI 用 curl 打不进去（403）。
 *
 * 所以由插件在宿主进程内注册工具，直接走"宿主 → 显示器服务"的内部通道。
 * 工具都作用在**调用方自己的会话**上（会话号取自 `exec.agent.id`），
 * 两个会话不可能操作到对方的显示。
 *
 * 实现约束：**不引入任何 npm 依赖**（插件目录解析不到 `@deepseek-ai/*`），
 * 所以工具定义用普通对象手写（`{name, description, parameters, output, execute}`），
 * 只依赖宿主注入的 `tools` 服务与全局 `fetch`。
 *
 * @module dsh-display-panel/tools
 */
'use strict'

/** 工具名前缀：与官方 `browser_panel_*` 并列，重名会让插件加载失败，所以自带命名空间。 */
const PREFIX = 'display_panel_'

/** 上游（显示器服务）请求超时：跑程序的活可能慢，给足；纯读接口给短一点。 */
const TIMEOUT_MS = 30_000

/** 把任意值收拾成"可无损 JSON 的**对象**"。 */
function clean(value) {
  let out
  try {
    out = JSON.parse(JSON.stringify(value === undefined ? null : value))
  } catch {
    out = null
  }
  // ⚠️ 输出 schema 是 object 根（见 jsonOutput 的注释），宿主会按它校验返回值：
  //    返回 null / 数组 / 标量会被判成 INVALID_TOOL_OUTPUT。所以这里兜成对象。
  if (out === null || typeof out !== 'object' || Array.isArray(out)) return { value: out }
  return out
}

/** 渲染成单块文本（模型看的）。 */
function asText(value) {
  return [{ type: 'text', text: typeof value === 'string' ? value : JSON.stringify(value, undefined, 1) }]
}

/**
 * 统一的输出声明。
 *
 * ⚠️ `output.schema` 必须是**受支持的 JSON Schema**：DSH 的 `tools.register` 会跑
 * `assertSupportedJsonSchema`，而它**不认 `{type:'json'}`**（实测报
 * "schema.type must be one of object/array/string/number/integer/boolean/null"），
 * 而且它还会用这个 schema **校验返回值** —— 所以只能声明成对象根，
 * 工具返回值也必须是对象（由 `clean()` 保证）。
 */
const jsonOutput = () => ({ schema: { type: 'object', additionalProperties: true }, render: (_args, value) => asText(value) })

/** 会话 id 校验：与服务端、宿主半边同一套规则（防注入/防穿越）。 */
const SESSION_RE = /^[A-Za-z0-9._-]{1,64}$/

/**
 * 取本次工具调用所属的会话 id。
 *
 * 没有会话（服务内部/UI/命令路径触发）时**必须报错**而不是退化：显示是"每会话一台"的，
 * 退化就等于共用一台显示，正是这个插件要消灭的串扰。
 *
 * @param exec - 工具执行上下文。
 * @returns 会话 id。
 */
function sessionOf(exec) {
  const id = exec && exec.agent && exec.agent.id
  if (typeof id !== 'string' || id === '') {
    throw new Error(`${PREFIX}* 工具必须在某个会话里调用；这次调用没有归属会话`)
  }
  if (!SESSION_RE.test(id)) throw new Error(`会话 id 不合法：${JSON.stringify(id)}`)
  return id
}

/** 取会话号的宽松版本（用于不依赖会话的接口，取不到就返回空串）。 */
function optionalSessionOf(exec) {
  const id = exec && exec.agent && exec.agent.id
  return typeof id === 'string' && SESSION_RE.test(id) ? id : ''
}

/**
 * 组装所有工具定义。
 *
 * @param options - 宿主半边提供的通道。
 * @param options.discover - `(force?: boolean) => Promise<{running, port, token, backend, size, missing, home, managed}>`
 *   探测（必要时重新探测）显示器服务；**不得**创建会话。
 * @param options.ensureService - `() => Promise<...>` 同上，但会在服务未运行时尝试拉起。
 * @param options.logger - 可选日志器（默认 console）。
 * @returns 工具定义数组，交给 `ctx.tools.register` 逐个注册。
 */
function defineTools({ discover, ensureService, logger = console }) {
  if (typeof discover !== 'function' || typeof ensureService !== 'function') {
    throw new TypeError('defineTools 需要 discover 与 ensureService 两个通道函数')
  }

  /**
   * 发一个上游请求。
   *
   * @param options.session - 会话 id（可空）。
   * @param options.path - 服务端路径，例如 `/state`、`/snapshot`、`/exec`。
   * @param options.method - HTTP 方法。
   * @param options.body - JSON body。
   * @param options.raw - true 时返回 Buffer（抓帧用），否则解析 JSON。
   * @param options.ensure - true 时先确保服务在跑。
   * @param options.timeoutMs - 覆盖默认超时。
   * @returns `{status, headers, json?, buffer?}`。
   */
  async function callService({ session = '', path, method = 'GET', body, raw = false, ensure = true, timeoutMs = TIMEOUT_MS }) {
    const state = ensure ? await ensureService() : await discover()
    if (state === undefined || state === null || state.running !== true || !state.port) {
      const missing = Array.isArray(state && state.missing) ? state.missing : []
      const why = missing.length > 0
        ? `缺少依赖：${missing.map((m) => `${m.tool}（${m.why}；装：${m.package}）`).join('、')}`
        : '显示器服务没有在运行，且自动拉起失败'
      const error = new Error(`${why}。可以看日志：${(state && state.log) || '<home>/viewer.log'}`)
      error.code = 'SERVICE_UNAVAILABLE'
      throw error
    }
    const base = `http://127.0.0.1:${state.port}`
    const prefix = session ? `/s/${encodeURIComponent(session)}` : ''
    // ⚠️ path 自己可能已经带查询串（例如 `/snapshot?quality=90`）。那种情况下再拼 `?k=…`
    //    会变成 `?quality=90?k=…` —— 令牌被当成前一个参数的值，服务端判 403，
    //    而 raw 调用只看到一堆"不像 JPEG 的字节"（实测：截图工具写出 197 字节的假 .jpg）。
    const query = state.token ? `${path.includes('?') ? '&' : '?'}k=${encodeURIComponent(state.token)}` : ''
    const url = `${base}${prefix}${path}${query}`
    const response = await fetch(url, {
      method,
      headers: body === undefined ? undefined : { 'content-type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(timeoutMs),
    })
    const out = { status: response.status, headers: response.headers }
    if (raw) {
      out.buffer = Buffer.from(await response.arrayBuffer())
    } else {
      const text = await response.text()
      try {
        out.json = text ? JSON.parse(text) : null
      } catch {
        out.json = { ok: response.ok, text: text.slice(0, 4000) }
      }
    }
    if (response.status >= 400 && !raw) {
      const detail = out.json && (out.json.error || out.json.hint)
      const error = new Error(`显示器服务返回 ${response.status}${detail ? `：${detail}` : ''}`)
      error.code = 'SERVICE_ERROR'
      error.status = response.status
      throw error
    }
    return out
  }

  /** 会话显示的状态（显示号/后端/尺寸/窗口数/光标）。 */
  async function sessionState(session) {
    const { json } = await callService({ session, path: '/state' })
    return json || {}
  }

  // ------------------------------------------------------------------ 工具
  const status = {
    name: `${PREFIX}status`,
    description:
      "Report this session's display: whether the viewer service is running (and on which port), the backend "
      + '(x11 = a private per-session Xvfb; win32/darwin = the machine\'s real desktop), the display number, '
      + 'whether mouse/keyboard injection is enabled, how many windows are on it, and any missing dependency. '
      + 'Call this first when unsure whether the display is usable.',
    parameters: { type: 'object', properties: {}, additionalProperties: false },
    output: jsonOutput(),
    execute: async (_args, exec) => {
      const session = optionalSessionOf(exec)
      const service = await discover()
      const out = {
        service: {
          running: service.running === true,
          port: service.port || null,
          managed: service.managed === true,
          backend: service.backend || null,
          size: service.size || null,
          missing: service.missing || [],
          log: service.log || null,
          home: service.home || null,
        },
        session: session || null,
      }
      if (session && service.running === true) {
        try {
          const state = await sessionState(session)
          out.display = {
            display: state.display || null,
            backend: state.backend || null,
            size: state.size || null,
            windows: typeof state.windows === 'number' ? state.windows : null,
            idle: state.idle === true,
            input: state.input !== false,
            realDesktop: state.realDesktop === true,
            cursor: state.cursor || null,
            tooltip: state.tooltip || null,
          }
          out.hint = state.realDesktop === true
            ? '这是**真实桌面**后端：注入会操作真人正在用的鼠标键盘；也可能只是只读（input=false）。'
            : `在会话显示上跑程序建议用 ${PREFIX}run（由服务自己拉起，能拿退出码与输出；DISPLAY=:N 今天也可用，但依赖沙箱不隔离网络）。`
        } catch (error) {
          out.displayError = String((error && error.message) || error)
        }
        // 新管线（契约 §5.5）的实时指标：fps / 抓帧方式 / 编码档 / 带宽。
        // 旧版服务没有 /stats —— 那就不要这个字段，绝不因为缺能力而报错。
        try {
          const { json: stats } = await callService({ session, path: '/stats', timeoutMs: 8000 })
          if (stats && typeof stats === 'object') {
            out.pipeline = {
              fps: typeof stats.fps === 'number' ? Math.round(stats.fps * 10) / 10 : null,
              mode: stats.mode || null,
              quality: stats.quality ?? null,
              scale: stats.scale ?? null,
              bytesPerSec: typeof stats.bytesPerSec === 'number' ? Math.round(stats.bytesPerSec) : null,
              captureMs: stats.captureMs ?? null,
              encodeMs: stats.encodeMs ?? null,
              skipped: stats.skipped ?? null,
              reason: stats.reason || null,
            }
          }
        } catch (error) {
          /* 旧版服务或 /stats 不可用：不影响 status 的其余部分 */
        }
      }
      return clean(out)
    },
  }

  const open = {
    name: `${PREFIX}open`,
    description:
      'Make sure the viewer service for this DSH host is running (start it on demand) and return its address. '
      + 'The display itself is created per session on first use.',
    parameters: { type: 'object', properties: {}, additionalProperties: false },
    output: jsonOutput(),
    execute: async () => {
      const service = await ensureService()
      return clean({
        ok: service.running === true,
        port: service.port || null,
        backend: service.backend || null,
        size: service.size || null,
        managed: service.managed === true,
        missing: service.missing || [],
        log: service.log || null,
      })
    },
  }

  const sessions = {
    name: `${PREFIX}sessions`,
    description: 'List the per-session displays this viewer service currently holds (session id, display, backend).',
    parameters: { type: 'object', properties: {}, additionalProperties: false },
    output: jsonOutput(),
    execute: async () => {
      // 索引页是 HTML（给人看的），这里退化成"服务状态 + 本会话"，避免为解析 HTML 写正则。
      const service = await discover()
      return clean({ running: service.running === true, port: service.port || null, index: service.port ? `http://127.0.0.1:${service.port}/` : null })
    },
  }

  const run = {
    name: `${PREFIX}run`,
    description:
      "Run a shell command **on this session's own display** and return its pid (or, with wait=true, its exit code "
      + 'and output). Prefer this over `DISPLAY=:N cmd`: the program is started by the viewer process itself, so it '
      + 'always lands on the right display regardless of sandbox or network isolation, and you get the pid, the exit '
      + 'code and the output back. (`DISPLAY=:N` also works today — X11 falls back to the abstract socket — but it '
      + 'depends on the sandbox not isolating the network namespace.) The command inherits DISPLAY '
      + '(and QT_QPA_PLATFORM=xcb on Linux).',
    parameters: {
      type: 'object',
      properties: {
        command: { type: 'string', description: 'Shell command to run on that display, e.g. "xterm" or "python3 app.py".' },
        wait: { type: 'boolean', description: 'Wait for it to finish and return exit code + stdout/stderr (default false).' },
        cwd: { type: 'string', description: 'Working directory for the command.' },
        timeoutSeconds: { type: 'number', description: 'With wait=true: kill the command after this many seconds (default 25).' },
      },
      required: ['command'],
      additionalProperties: false,
    },
    output: jsonOutput(),
    execute: async (args, exec) => {
      const session = sessionOf(exec)
      const command = String((args && args.command) || '').trim()
      if (command === '') throw new Error('command 不能为空')
      const wait = args && args.wait === true
      const timeoutSeconds = Math.min(300, Math.max(1, Number((args && args.timeoutSeconds) || 25)))
      const { json } = await callService({
        session,
        path: '/exec',
        method: 'POST',
        ensure: true,
        timeoutMs: wait ? (timeoutSeconds + 10) * 1000 : TIMEOUT_MS,
        body: {
          argv: ['/bin/sh', '-lc', command],
          cwd: args && typeof args.cwd === 'string' && args.cwd !== '' ? args.cwd : undefined,
          wait,
          timeoutSeconds,
        },
      })
      return clean(json)
    },
  }

  const procs = {
    name: `${PREFIX}procs`,
    description: "List (and optionally kill) the programs this panel started on this session's display.",
    parameters: {
      type: 'object',
      properties: {
        killPid: { type: 'number', description: 'Kill this pid instead of listing.' },
      },
      additionalProperties: false,
    },
    output: jsonOutput(),
    execute: async (args, exec) => {
      const session = sessionOf(exec)
      const killPid = args && Number(args.killPid)
      if (Number.isFinite(killPid) && killPid > 0) {
        const { json } = await callService({ session, path: '/kill', method: 'POST', body: { pid: killPid } })
        return clean(json)
      }
      const { json } = await callService({ session, path: '/procs' })
      return clean(json)
    },
  }

  const screenshot = {
    name: `${PREFIX}screenshot`,
    description:
      "Save a JPEG snapshot of this session's display to a file and return its path, so you can look at the "
      + 'picture (read the file with the image reader) or hand the path to a person. Snapshots are taken at '
      + 'higher quality than the live stream, so small text stays readable.',
    parameters: {
      type: 'object',
      properties: {
        path: { type: 'string', description: 'Where to write the JPEG (default: a temp file).' },
        quality: { type: 'number', description: 'JPEG quality 1..100 (default 90; the live stream runs cooler).' },
      },
      additionalProperties: false,
    },
    output: jsonOutput(),
    execute: async (args, exec) => {
      const session = sessionOf(exec)
      // 截图默认走高质量档（流里的默认 70 不适合看小字）。服务端不支持该参数时会被忽略，
      // 行为退回默认档 —— 不做版本探测，保持工具简单。
      const wanted = Number(args && args.quality)
      const quality = Number.isFinite(wanted) ? Math.min(100, Math.max(1, Math.round(wanted))) : 90
      const { buffer, status } = await callService({
        session,
        path: `/snapshot?quality=${quality}`,
        raw: true,
        timeoutMs: 8000,
      })
      if (!buffer || buffer.length === 0) throw new Error('抓帧为空（显示上可能还没有程序在跑）')
      // ⚠️ 别把错误响应当成图片写下去：服务端拒绝时（403/404/503）body 是 JSON，
      //    写进 .jpg 会得到一个"看起来成功、打开是坏图"的文件（实测踩过：197 字节的假 JPEG）。
      const isJpeg = buffer[0] === 0xFF && buffer[1] === 0xD8
      if (!isJpeg) {
        const text = buffer.toString('utf8', 0, 300)
        throw new Error(`抓帧失败：HTTP ${status}，返回的不是 JPEG —— ${text}`)
      }
      const os = require('os')
      const path = require('path')
      const fs = require('fs')
      const target = args && typeof args.path === 'string' && args.path !== ''
        ? args.path
        : path.join(os.tmpdir(), `dsh-display-${session}-${Date.now()}.jpg`)
      await fs.promises.writeFile(target, buffer)
      return clean({ path: target, bytes: buffer.length, quality, jpeg: true })
    },
  }

  const input = {
    name: `${PREFIX}input`,
    description:
      "Inject one mouse/keyboard event into this session's display (same channel the human's panel uses). "
      + 'Coordinates are normalized 0..1 of the picture. Use it to drive a GUI you started with '
      + `${PREFIX}run. For anything a person must do (login, QR code, CAPTCHA), ask them instead.`,
    parameters: {
      type: 'object',
      properties: {
        action: { type: 'string', enum: ['click', 'move', 'down', 'up', 'wheel', 'text', 'key'], description: 'Event type.' },
        x: { type: 'number', description: 'Normalized 0..1 horizontal position (click/move/down/up/wheel).' },
        y: { type: 'number', description: 'Normalized 0..1 vertical position.' },
        button: { type: 'number', description: '1=left (default), 2=middle, 3=right.' },
        dy: { type: 'number', description: 'Wheel delta (positive = scroll down).' },
        text: { type: 'string', description: 'Text for action=text (CJK is fine).' },
        key: { type: 'string', description: 'DOM key name for action=key, e.g. Enter, Backspace, ArrowUp, or ctrl+a.' },
      },
      required: ['action'],
      additionalProperties: false,
    },
    output: jsonOutput(),
    execute: async (args, exec) => {
      const session = sessionOf(exec)
      const action = String((args && args.action) || '')
      const event = { t: action }
      if (args && typeof args.x === 'number') event.x = args.x
      if (args && typeof args.y === 'number') event.y = args.y
      if (args && typeof args.button === 'number') event.b = args.button
      if (args && typeof args.dy === 'number') event.dy = args.dy
      if (args && typeof args.text === 'string') event.s = args.text
      if (args && typeof args.key === 'string') event.k = args.key
      if (['click', 'down', 'up', 'move'].includes(action) && (typeof event.x !== 'number' || typeof event.y !== 'number')) {
        throw new Error(`${action} 需要 x 与 y（0..1 归一化坐标）`)
      }
      if (action === 'text' && typeof event.s !== 'string') throw new Error('text 需要 s')
      if (action === 'key' && typeof event.k !== 'string') throw new Error('key 需要 k')
      if (action === 'wheel' && typeof event.dy !== 'number') event.dy = 120
      const { json } = await callService({ session, path: '/input', method: 'POST', body: event })
      return clean(json)
    },
  }


  const close = {
    name: `${PREFIX}close`,
    description:
      "Close this session's display: stop its X server and release the display number. "
      + 'The next frame/stream/exec request opens a fresh one automatically, so this is safe — '
      + 'use it when you are done looking at the display to give the resources back.',
    parameters: { type: 'object', properties: {}, additionalProperties: false },
    output: jsonOutput(),
    execute: async (_args, exec) => {
      const session = sessionOf(exec)
      const { json } = await callService({ session, path: '/close', method: 'POST' })
      const out = clean(json)
      return clean({
        ok: out.ok !== false,
        session,
        closed: out.removed === true || out.closed === true,
        upstream: out,
        hint: '显示器已关闭；下一次拉帧/注入/跑程序会自动重新打开（新会话显示是干净的）。',
      })
    },
  }

  if (logger && typeof logger.log === 'function') {
    logger.log(`[dsh-display-panel] tools: ${[status, open, sessions, run, procs, screenshot, input, close].map((t) => t.name).join(', ')}`)
  }
  return [status, open, sessions, run, procs, screenshot, input, close]
}

module.exports = { defineTools, PREFIX, SESSION_RE }
