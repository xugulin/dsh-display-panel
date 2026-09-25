#!/usr/bin/env node
/**
 * dsh-display-panel 端到端性能测量 —— 契约 §5.1「指标（验收线）」的取证工具。
 *
 * 它量四件事（同一条 pipeline 的四个断面），**两个 lane 各自独立取证**：
 *
 * ```
 *   X 画面变化            服务抓帧/编码          网络             客户端画出来
 *   perf-anim.py  ──►  viewer 服务  ──►  MJPEG  ──►  面板 canvas
 *        │  (1)            │  (3)          │(2)            │  (1)(2)
 *        └── 变化时间戳 ─────┴── 帧头 X-DSH-Time ──────────┘
 * ```
 *
 * * **lane S（服务端，不需要浏览器）**：直接吃 `/s/<sid>/stream`，
 *   用「帧字节去重」数真正变化的内容帧，用 anim 日志的脉冲时间戳量变化延迟，
 *   用 `/proc/<pid>/stat` 量服务/ Xvfb / 靶程序 CPU，字节数按窗口统计。
 *   为什么要有它：队友改的是服务端管线，**量它不需要等浏览器**，
 *   而且它排除了浏览器/GPU 这个变量，能把"服务慢"和"客户端慢"分开定责。
 * * **lane B（客户端，Playwright + 真 Brave）**：在页面里对面板画面做
 *   **降采样指纹去重**（同一画面不重复计数），延迟用页面内 `performance.now()`
 *   与 anim 日志时间戳配对，字节数走 CDP `Network.dataReceived`（真实网络字节），
 *   CPU 走 CDP `SystemInfo.getProcessInfo`（真正的渲染进程）+ `/proc` 进程树兜底。
 *
 * 时间基准：anim 靶程序、服务、浏览器都在**同一台机器同一个 CLOCK_REALTIME** 上，
 * 所以「X 侧变化的 epoch 毫秒」与「客户端 Date.now()」可直接相减。
 * 这是全程唯一的时间同步假设，写在这里以备质疑。
 *
 * 用法：
 *   node tools/perf-panel.mjs --service-only [--baseline-service] [--seconds 20]
 *   node tools/perf-panel.mjs --url "http://127.0.0.1:19601/?token=…" [--seconds 20]
 *
 * 选项：
 *   --url URL            DSH Web UI 地址（带 `?token=`）→ 打开 lane B（真浏览器）
 *   --service-only       只跑 lane S（基线取证不需要 DSH 实例）
 *   --baseline-service   量**冻结的 0.3.4**（.verify/perf-baseline/viewer-0.3.4.py，端口 8504）
 *   --viewer PATH        指定要量的服务实现（默认：仓库里的 service/dsh-display-viewer.py）
 *   --service-port N     服务端口（默认 --baseline-service ? 8504 : 8503）
 *   --service-home DIR   服务的 DSH_DISPLAY_HOME（默认 .verify/perf-home[-baseline]）
 *   --attach             不自己起服务，只连已经在跑的那个
 *   --session SID        面板会话 id（默认 perf<label>）
 *   --seconds N          动态测量窗口（默认 20）
 *   --anim-fps N         靶画面变化率（默认 20，必须高于目标 fps 才量得出上限）
 *   --label L            本次运行的标签（进 JSON 与表格，默认按代码自动取名）
 *   --shots DIR          截图目录（默认 verify/shots-perf）
 *   --json FILE          报告 JSON 路径（默认 <shots>/perf-<label>.json）
 *   --phases LIST        lane S 的阶段（默认 fps,latency,static）
 *   --adversarial        额外跑对抗性检查（极档 / 流中断自愈 / 双标签 / 静止带宽）
 *   --keep               跑完不杀服务/靶程序（排查用）
 *   --headless 0         有头跑浏览器（排查渲染问题用）
 *
 * 退出码：0=全部达标；1=有指标未达 §5.1 目标线（打印差多少）；
 *         2=用法/环境错误；3=缺依赖（Playwright/Brave/Xvfb/libX11）。
 *
 * ⚠️ 只碰自己的东西：默认端口 8503/8504、`.verify/perf-*`。
 *    绝不去连用户的 8099/8100/19387 —— `--attach` 时也会显式核对端口白名单。
 */
import fs from 'node:fs'
import path from 'node:path'
import crypto from 'node:crypto'
import { spawn, spawnSync } from 'node:child_process'
import { fileURLToPath, pathToFileURL } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const REPO = path.resolve(HERE, '..')
const ANIM = path.join(HERE, 'perf-anim.py')
const VERIFY = path.join(REPO, '.verify')
const BASELINE_DIR = path.join(VERIFY, 'perf-baseline')
const BASELINE_VIEWER = path.join(BASELINE_DIR, 'viewer-0.3.4.py')
const PLAYWRIGHT = process.env.DSH_PERF_PLAYWRIGHT
  || '/home/xgl/deepseek-harness/node_modules/.pnpm/playwright@1.61.1/node_modules/playwright/index.mjs'
const BROWSER = process.env.DSH_PERF_BROWSER || '/opt/brave.com/brave-origin-beta/brave'

/** 我允许自己碰的端口（别人的服务一律不碰：8099/8100/19387/8501/8502/8521/19399/19501）。 */
const MY_PORTS = new Set([8503, 8504, 19601])

// ------------------------------------------------------------------ 参数
const argv = process.argv.slice(2)
const arg = (name, def = null) => {
  const i = argv.indexOf(name)
  return i >= 0 && i + 1 < argv.length ? argv[i + 1] : def
}
const flag = (name) => argv.includes(name)

const opts = {
  url: arg('--url'),
  serviceOnly: flag('--service-only') || !arg('--url'),
  baselineService: flag('--baseline-service'),
  viewer: arg('--viewer'),
  port: Number(arg('--service-port', flag('--baseline-service') ? '8504' : '8503')),
  home: arg('--service-home'),
  attach: flag('--attach'),
  session: arg('--session'),
  seconds: Number(arg('--seconds', '20')),
  animFps: Number(arg('--anim-fps', '20')),
  label: arg('--label'),
  shots: path.resolve(arg('--shots', path.join(REPO, 'verify', 'shots-perf'))),
  json: arg('--json'),
  phases: arg('--phases', 'fps,latency,static').split(',').map((s) => s.trim()).filter(Boolean),
  adversarial: flag('--adversarial'),
  keep: flag('--keep'),
  headless: arg('--headless', '1') !== '0',
  timeout: Number(arg('--timeout', '30000')),
}
if (flag('-h') || flag('--help')) {
  console.log(fs.readFileSync(fileURLToPath(import.meta.url), 'utf8').split('*/')[0].replace(/^#!.*\n/, ''))
  process.exit(0)
}
if (opts.viewer) opts.baselineService = opts.viewer === BASELINE_VIEWER ? true : opts.baselineService
opts.viewerPath = path.resolve(opts.viewer
  || (opts.baselineService ? BASELINE_VIEWER : path.join(REPO, 'service', 'dsh-display-viewer.py')))
/**
 * 服务 home 的候选位置：本仓库的 `.verify/` 优先，其次工作区根的 `.verify/`
 * （隔离 DSH 实例用的是工作区根那份 —— 两边都可能存在，别猜，存在 token 的那个算数）。
 */
const homeBase = opts.baselineService ? 'perf-home-baseline' : 'perf-home'
const homeCandidates = [path.join(VERIFY, homeBase), path.join(path.resolve(REPO, '..'), '.verify', homeBase)]
opts.home = path.resolve(opts.home
  || homeCandidates.find((p) => fs.existsSync(path.join(p, 'token')))
  || homeCandidates[0])
opts.session = opts.session || (opts.baselineService ? 'perfbase1' : 'perf1')
opts.label = opts.label || (opts.baselineService ? 'baseline-0.3.4' : 'current')
opts.shots = path.resolve(opts.shots)
opts.json = path.resolve(opts.json || path.join(opts.shots, `perf-${opts.label}.json`))
// 靶画面日志（每行一个 JSON，含"哪一刻画面真的变了"）跟 JSON 报告放一起，便于复核
opts.logDir = path.resolve(arg('--log-dir', path.dirname(opts.json)))
if (!opts.attach && !MY_PORTS.has(opts.port)) {
  console.error(`拒绝：端口 ${opts.port} 不在我的白名单 ${[...MY_PORTS].join('/')} 里（绝不碰别人的服务）`)
  process.exit(2)
}

fs.mkdirSync(opts.shots, { recursive: true })
fs.mkdirSync(opts.logDir, { recursive: true })
fs.mkdirSync(BASELINE_DIR, { recursive: true })

// ------------------------------------------------------------------ 小工具
const nowMs = (() => {
  const off = Date.now() - Number(process.hrtime.bigint() / 1000000n)
  return () => off + Number(process.hrtime.bigint() / 1000000n)
})()
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
const sha256 = (buf) => crypto.createHash('sha256').update(buf).digest('hex')
const short = (h, n = 12) => (h ? h.slice(0, n) : '—')
const fileSha = (p) => { try { return sha256(fs.readFileSync(p)) } catch { return null } }
const kb = (n) => `${(n / 1024).toFixed(1)}KB`
const ms = (n) => (n === null || n === undefined ? '—' : `${n.toFixed(1)}ms`)
const pct = (n) => (n === null || n === undefined ? '—' : `${n.toFixed(2)}%`)

function pcts(values, ps = [50, 95]) {
  const a = values.filter((v) => typeof v === 'number' && isFinite(v)).sort((x, y) => x - y)
  if (!a.length) return Object.fromEntries(ps.map((p) => [p, null]))
  const at = (p) => {
    const idx = (a.length - 1) * (p / 100)
    const lo = Math.floor(idx), hi = Math.ceil(idx)
    return lo === hi ? a[lo] : a[lo] + (a[hi] - a[lo]) * (idx - lo)
  }
  return Object.fromEntries(ps.map((p) => [p, at(p)]))
}

const LOG = []
function say(line = '') { LOG.push(line); console.log(line) }
function head(title) { say(''); say(`── ${title} ${'─'.repeat(Math.max(0, 64 - title.length))}`) }

async function httpJson(url, init = {}, timeoutMs = 8000, retry = true) {
  const ac = new AbortController()
  const timer = setTimeout(() => ac.abort(), timeoutMs)
  try {
    const r = await fetch(url, { ...init, signal: ac.signal, cache: 'no-store' })
    const text = await r.text()
    let json = null
    try { json = JSON.parse(text) } catch { /* 非 JSON（旧版 404 HTML 等） */ }
    // 旧版服务有个真 bug：POST 打了 404 但不读 body → keep-alive 连接上**下一个请求**
    // 被当成垃圾请求行，回 501 "Unsupported method ('{...}')"。
    // 这里用"换一条连接重试一次"绕开它（不是粉饰：报告里记 httpRetries）。
    if (retry && r.status >= 500) {
      return httpJson(url, { ...init, headers: { ...(init.headers || {}), connection: 'close' } },
        timeoutMs, false)
    }
    return { status: r.status, json, text: text.slice(0, 800), headers: r.headers }
  } catch (e) {
    return { status: 0, json: null, text: String(e.message || e), error: String(e.message || e) }
  } finally { clearTimeout(timer) }
}

// ------------------------------------------------------------------ 代码取证（这一轮量的是哪份代码）
function gitInfo() {
  const run = (a) => spawnSync('git', a, { cwd: REPO, encoding: 'utf8' }).stdout?.trim() || null
  return { head: run(['rev-parse', 'HEAD']), subject: run(['log', '-1', '--format=%s']),
    dirty: (run(['status', '--porcelain']) || '').split('\n').filter(Boolean) }
}
const code = {
  service: { path: opts.viewerPath, sha256: fileSha(opts.viewerPath) },
  host: { path: path.join(REPO, 'lib', 'index.js'), sha256: fileSha(path.join(REPO, 'lib', 'index.js')) },
  client: { path: path.join(REPO, 'lib', 'client.js'), sha256: fileSha(path.join(REPO, 'lib', 'client.js')) },
  anim: { path: ANIM, sha256: fileSha(ANIM) },
  baseline: {
    service: fileSha(BASELINE_VIEWER),
    host: fileSha(path.join(BASELINE_DIR, 'index-0.3.4.js')),
    client: fileSha(path.join(BASELINE_DIR, 'client-0.3.4.js')),
  },
  git: gitInfo(),
}
for (const k of ['service', 'host', 'client']) {
  code[k].frozenBaseline = code[k].sha256 !== null && code[k].sha256 === code.baseline[k]
}

// ------------------------------------------------------------------ 服务控制
function readFileTrim(p) { try { return fs.readFileSync(p, 'utf8').trim() } catch { return null } }

async function probeService(port, token) {
  const r = await httpJson(`http://127.0.0.1:${port}/health?k=${token}`, {}, 2500)
  return r.status === 200 && r.json?.ok === true ? r.json : null
}

async function ensureService() {
  const tokenFile = path.join(opts.home, 'token')
  const portFile = readFileTrim(path.join(opts.home, 'port'))
  const token = readFileTrim(tokenFile)
  const candidates = [opts.port, ...(portFile ? [Number(portFile)] : []), 8503, 8504]
    .filter((p, i, a) => MY_PORTS.has(p) && a.indexOf(p) === i)
  for (const p of candidates) {
    if (!token) break
    const health = await probeService(p, token)
    if (health) return { port: p, token, health, started: false, home: opts.home }
  }
  if (opts.attach) throw new Error(`--attach 但 ${candidates.join('/')} 上没有服务（home=${opts.home}）`)
  if (!fs.existsSync(opts.viewerPath)) throw new Error(`找不到服务实现 ${opts.viewerPath}`)
  const logPath = path.join(opts.home, 'perf-viewer.log')
  fs.mkdirSync(opts.home, { recursive: true })
  const fd = fs.openSync(logPath, 'a')
  const child = spawn('python3', [opts.viewerPath], {
    env: { ...process.env, DSH_DISPLAY_HOME: opts.home, DSH_VIEW_PORT: String(opts.port),
      DSH_VIEW_SIZE: process.env.DSH_VIEW_SIZE || '1600x1000', DSH_VIEW_LOG: logPath },
    detached: false, stdio: ['ignore', fd, fd],
  })
  say(`启动服务 pid=${child.pid} port=${opts.port} home=${opts.home}`)
  say(`       实现=${path.relative(REPO, opts.viewerPath)} sha256=${short(code.service.sha256)}`)
  for (let i = 0; i < 60; i++) {
    await sleep(250)
    const tk = readFileTrim(tokenFile) || token
    const h = tk ? await probeService(opts.port, tk) : null
    if (h) return { port: opts.port, token: tk, health: h, started: true, child, home: opts.home, log: logPath }
  }
  throw new Error(`服务起不来，看 ${logPath}`)
}

async function serviceExec(svc, sid, argvList, { wait = false, cwd = REPO, timeoutMs = 30000 } = {}) {
  return httpJson(`http://127.0.0.1:${svc.port}/s/${sid}/exec?k=${svc.token}`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ argv: argvList, cwd, wait }),
  }, timeoutMs)
}

async function serviceInput(svc, sid, event) {
  return httpJson(`http://127.0.0.1:${svc.port}/s/${sid}/input?k=${svc.token}`, {
    method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(event),
  }, 8000)
}

async function serviceStats(svc, sid) {
  const r = await httpJson(`http://127.0.0.1:${svc.port}/s/${sid}/stats?k=${svc.token}`, {}, 4000)
  return r.status === 200 && r.json ? r.json : null
}

async function serviceStreamConfig(svc, sid, cfg) {
  const r = await httpJson(`http://127.0.0.1:${svc.port}/s/${sid}/stream-config?k=${svc.token}`, {
    method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(cfg),
  }, 6000)
  return { status: r.status, json: r.json, text: r.text }
}

// ------------------------------------------------------------------ MJPEG 流客户端（自己解析 multipart）
function parseHeaders(str) {
  const h = {}
  for (const line of str.split('\r\n')) {
    const i = line.indexOf(':')
    if (i > 0) h[line.slice(0, i).trim().toLowerCase()] = line.slice(i + 1).trim()
  }
  return h
}

/**
 * 起一个 MJPEG 长连接，逐帧记账。
 * 去重口径：**逐帧 sha256**，只有与上一帧不同才算"内容帧"（契约 §5.1 的"去重后帧率"）。
 */
function startStream({ port, token, sid, onFrame, label = 'stream', retriesLeft = 1 }) {
  const frames = []
  let bytes = 0
  let state = { connected: false, status: 0, error: null, ended: false, contentType: null, retries: 0 }
  const ac = new AbortController()
  let lastHash = null
  const BOUND = Buffer.from('--frame')

  const done = (async () => {
    try {
      for (let attempt = 0; attempt <= retriesLeft; attempt++) {
        // ⚠️ 旧版服务的 keep-alive 连接会被"没读 body 的 POST 404"污染：
        //    同一连接上的下一个请求会拿到 501（method = 上一个 POST 的 body，实测见 PERF-REPORT）。
        //    第一次失败就换一条干净连接（Connection: close）重试，否则测出来的是这个 bug 而不是性能。
        const r = await fetch(`http://127.0.0.1:${port}/s/${sid}/stream?k=${token}`, {
          signal: ac.signal, cache: 'no-store',
          headers: attempt > 0 ? { connection: 'close' } : undefined,
        })
        state.status = r.status
        state.contentType = r.headers.get('content-type')
        if (r.status >= 500 && attempt < retriesLeft) {
          state.retries += 1
          try { r.body?.cancel() } catch { /* 忽略 */ }
          continue
        }
        if (!r.ok || !r.body) { state.error = `HTTP ${r.status}`; state.ended = true; return }
        state.connected = true
        let buf = Buffer.alloc(0)
        for await (const chunk of r.body) {
          const c = Buffer.from(chunk)
          bytes += c.length
          buf = buf.length ? Buffer.concat([buf, c]) : c
          for (;;) {
            const start = buf.indexOf(BOUND)
            if (start < 0) { if (buf.length > 8192) buf = buf.subarray(buf.length - 8192); break }
            if (start > 0) { buf = buf.subarray(start); continue }
            const hdrEnd = buf.indexOf('\r\n\r\n')
            if (hdrEnd < 0) { if (buf.length > 1 << 20) buf = buf.subarray(buf.length - 8192); break }
            const headers = parseHeaders(buf.subarray(0, hdrEnd).toString('latin1'))
            const len = Number(headers['content-length'] ?? -1)
            const pStart = hdrEnd + 4
            let pEnd
            if (len >= 0) {
              pEnd = pStart + len
              if (buf.length < pEnd) break
            } else {
              const next = buf.indexOf(BOUND, pStart)
              if (next < 0) break
              pEnd = next
            }
            const payload = buf.subarray(pStart, pEnd)
            const hash = sha256(payload)
            const frame = {
              t: nowMs(), bytes: payload.length, hash, changed: hash !== lastHash,
              seq: headers['x-dsh-seq'] ?? null, xTime: headers['x-dsh-time'] ? Number(headers['x-dsh-time']) : null,
              size: headers['x-dsh-size'] ?? null, cursor: headers['x-dsh-cursor'] ?? null,
              hasFrameHeaders: !!(headers['x-dsh-seq'] || headers['x-dsh-time']),
              jpegMagic: payload.length > 2 && payload[0] === 0xff && payload[1] === 0xd8,
            }
            lastHash = hash
            frames.push(frame)
            if (onFrame) onFrame(frame)
            buf = buf.subarray(pEnd)
          }
        }
        state.ended = true
        return
      }
    } catch (e) {
      if (!ac.signal.aborted) state.error = String(e.message || e)
      state.ended = true
    }
  })()
  return {
    frames, get bytes() { return bytes }, state, label,
    stop: async () => { ac.abort(); await done.catch(() => {}) },
    done,
  }
}

/**
 * 按窗口聚合一帧序列。
 * `from`/`to` 用 nowMs() 的时间轴；`changed` 是**跨窗口**判定的（去重只看前一帧）。
 */
function windowStats(frames, from, to) {
  const win = frames.filter((f) => f.t >= from && f.t <= to)
  const dur = Math.max(0.001, (to - from) / 1000)
  const changed = win.filter((f) => f.changed).length
  const bytes = win.reduce((s, f) => s + f.bytes, 0)
  const sizes = win.map((f) => f.bytes)
  return {
    seconds: dur, framesSent: win.length, contentFrames: changed,
    sentFps: win.length / dur, contentFps: changed / dur,
    bytes, bytesPerSec: bytes / dur,
    avgFrameBytes: sizes.length ? sizes.reduce((a, b) => a + b, 0) / sizes.length : null,
    maxFrameBytes: sizes.length ? Math.max(...sizes) : null,
  }
}

/** 把 anim 日志里的「变化时间戳」与帧到达时间配对 → 变化延迟。 */
function pairFlips(flips, frames, maxWaitMs = 4000) {
  const out = []
  for (const f of flips) {
    const t = f.t * 1000
    let base = null
    for (let i = frames.length - 1; i >= 0; i--) { if (frames[i].t <= t) { base = frames[i]; break } }
    let hit = null
    for (const fr of frames) {
      if (fr.t <= t) continue
      if (fr.t > t + maxWaitMs) break
      if (!base || fr.hash !== base.hash) { hit = fr; break }
    }
    out.push({ src: f.src, seq: f.seq, tFlip: t,
      latencyMs: hit ? hit.t - t : null,
      // 有帧头时顺手把"抓帧→到客户端"那一段拆出来（旧版没有帧头，这一项是 null）
      serverAgeMs: hit && hit.xTime ? hit.t - hit.xTime : null,
      frameBytes: hit ? hit.bytes : null })
  }
  return out
}

// ------------------------------------------------------------------ anim 靶程序
function readAnimLog(p) {
  try {
    return fs.readFileSync(p, 'utf8').split('\n').filter(Boolean).map((l) => {
      try { return JSON.parse(l) } catch { return null }
    }).filter(Boolean)
  } catch { return [] }
}

async function startAnim(svc, sid, args, logPath) {
  fs.writeFileSync(logPath, '')
  const r = await serviceExec(svc, sid, ['python3', ANIM, '--log', logPath, ...args], { timeoutMs: 15000 })
  if (!r.json?.ok) {
    // 兜底：直接 DISPLAY=:N 起（沙箱里 abstract socket 可用，契约 §4.1）
    const disp = await httpJson(`http://127.0.0.1:${svc.port}/s/${sid}/display?k=${svc.token}`)
    const d = disp.json?.display
    if (!d) throw new Error(`/exec 起不了靶程序：${r.text}`)
    const child = spawn('python3', [ANIM, '--log', logPath, ...args],
      { env: { ...process.env, DISPLAY: d }, detached: false, stdio: 'ignore' })
    await sleep(600)
    return { pid: child.pid, display: d, log: logPath, fallback: true }
  }
  await sleep(600)
  return { pid: r.json.pid, display: r.json.display, log: logPath, fallback: false }
}

async function killAnim(svc, sid, anim) {
  if (!anim) return
  try { await httpJson(`http://127.0.0.1:${svc.port}/s/${sid}/kill?k=${svc.token}`,
    { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ pid: anim.pid }) }, 4000) } catch { /* 已退出 */ }
  if (anim.fallback) { try { process.kill(anim.pid, 'SIGTERM') } catch { /* 已退出 */ } }
  // ⚠️ 必须等它**真的退出**：靶画面是满屏窗口，两个叠在一起时"抓到的是哪一个"不确定，
  //    后启动的那个未必在最上层（实测踩过：key 子阶段 6 次注入全部量不到新帧）。
  for (let i = 0; i < 40; i++) {
    await sleep(50)
    const alive = await httpJson(`http://127.0.0.1:${svc.port}/s/${sid}/procs?k=${svc.token}`, {}, 4000)
    const list = alive.json?.procs || []
    if (!list.some((p) => p.pid === anim.pid)) return
  }
}

/** 会话显示上还剩几个 /exec 起着的进程（收尸是否干净要用它断言）。 */
async function animProcs(svc, sid) {
  const r = await httpJson(`http://127.0.0.1:${svc.port}/s/${sid}/procs?k=${svc.token}`, {}, 4000)
  return r.json?.procs || []
}

/**
 * 可见性自检：靶画面到底有没有被**抓进画面**。
 * 为什么必须有：窗口叠着、靶程序被杀、显示不对 —— 表现都是"延迟样本全 null"，
 * 分不清是"服务慢"还是"根本没拍到"。这里用一次按键翻页 + 等新帧来判定。
 */
async function assertVisible(svc, sid, stream, logPath, label) {
  const n0 = bySrc(readAnimLog(logPath), 'key').length
  const f0 = stream.frames.length
  const hashBefore = f0 ? stream.frames[f0 - 1].hash : null
  await serviceInput(svc, sid, { t: 'key', k: 'a' })
  const rec = await waitFor(logPath, (rs) => bySrc(rs, 'key').length >= n0 + 1, 2000)
  const flipped = bySrc(rec, 'key').length >= n0 + 1
  let gotNew = false
  for (let i = 0; i < 40; i++) {
    await sleep(100)
    if (stream.frames.length > f0 && stream.frames[stream.frames.length - 1].hash !== hashBefore) { gotNew = true; break }
  }
  return { label, keyReached: flipped, newFrameAfterFlip: gotNew,
    framesBefore: f0, framesAfter: stream.frames.length }
}

/**
 * 等到 anim 日志满足条件为止（默认条件：出现 n 条"变化"记录）。
 * ⚠️ 别用"记录条数"数 key/pulse：日志里还有 init/expose 两种"变化"，
 *    混在一起会让等待立刻返回，于是延迟样本变成 0（踩过）。
 */
async function waitFor(logPath, cond, timeoutMs = 6000, pollMs = 50) {
  const t0 = Date.now()
  for (;;) {
    const rec = readAnimLog(logPath)
    if (cond(rec)) return rec
    if (Date.now() - t0 > timeoutMs) return rec
    await sleep(pollMs)
  }
}
const bySrc = (rec, src) => rec.filter((r) => r.src === src)

/** 靶画面的出生/死亡（含退出原因）：延迟样本为 null 时，先看它是不是被谁提前杀了。 */
function animExitInfo(logPath) {
  const rec = readAnimLog(logPath)
  const ready = rec.find((r) => r.event === 'READY')
  const done = rec.find((r) => r.event === 'DONE')
  return { readyT: ready?.t ?? null, doneT: done?.t ?? null,
    aliveSec: ready && done ? +(done.t - ready.t).toFixed(2) : null,
    reason: done?.reason ?? null, flips: done?.flips ?? null,
    drawErrs: rec.filter((r) => r.event === 'DRAWERR').length }
}

// ------------------------------------------------------------------ CPU 采样（/proc）
const CLK_TCK = (() => {
  const r = spawnSync('getconf', ['CLK_TCK'], { encoding: 'utf8' })
  const v = Number((r.stdout || '').trim())
  return Number.isFinite(v) && v > 0 ? v : 100
})()

function cpuTicks(pid) {
  try {
    const s = fs.readFileSync(`/proc/${pid}/stat`, 'utf8')
    const parts = s.slice(s.lastIndexOf(')') + 2).split(' ')
    return Number(parts[11]) + Number(parts[12])            // utime + stime
  } catch { return null }
}
function cpuSnapshot(pids) {
  const out = {}
  for (const p of pids) { const v = cpuTicks(p); if (v !== null) out[p] = v }
  return out
}
function cpuDelta(before, after, seconds) {
  const out = {}
  for (const p of Object.keys(after)) {
    if (before[p] === undefined) continue
    out[p] = ((after[p] - before[p]) / CLK_TCK) / seconds * 100   // 单核百分比
  }
  return out
}

/** 在 /proc 里找 cmdline 命中所有 needle 的进程（找 Xvfb 用）。 */
function findProcs(needles) {
  const out = []
  let dirs = []
  try { dirs = fs.readdirSync('/proc') } catch { return out }
  for (const d of dirs) {
    if (!/^\d+$/.test(d)) continue
    let cmd = ''
    try { cmd = fs.readFileSync(`/proc/${d}/cmdline`, 'utf8').replace(/\0/g, ' ').trim() } catch { continue }
    if (needles.every((n) => cmd.includes(n))) out.push({ pid: Number(d), cmd })
  }
  return out
}

// ------------------------------------------------------------------ 判定（§5.1）
const TARGETS = {
  contentFps: { min: 15, unit: 'fps', name: '内容 fps（去重后）', baseline: 2 },
  latencyP50: { max: 150, unit: 'ms', name: '变化延迟 p50', baseline: 600 },
  bytesStatic: { max: 5 * 1024, unit: 'B/s', name: '静止画面带宽', baseline: 50 * 1024 },
  serviceCpuDynamic: { max: 25, unit: '%', name: '服务单核 CPU（动态）', baseline: null },
  serviceCpuStatic: { max: 2, unit: '%', name: '服务单核 CPU（静止）', baseline: null },
}
const checks = []
function check(key, value, note = '') {
  const t = TARGETS[key]
  const ok = value === null || value === undefined ? null
    : (t.min !== undefined ? value >= t.min : value <= t.max)
  const targetTxt = t.min !== undefined ? `≥${t.min}${t.unit}` : `≤${t.max}${t.unit}`
  let gap = null
  if (ok === false) {
    gap = t.min !== undefined ? `${(t.min - value).toFixed(1)}${t.unit} 之差` : `超出 ${(value - t.max).toFixed(1)}${t.unit}`
  }
  checks.push({ key, name: t.name, value, target: targetTxt, ok, gap, note, baseline: t.baseline })
  return ok
}

// ------------------------------------------------------------------ lane S
async function laneService(svc, report) {
  const sid = opts.session
  report.laneS = report.laneS || {}
  head(`lane S（服务端，端口 ${svc.port}，session=${sid}）`)
  say(`服务版本 ${svc.health.version} backend=${svc.health.backend} size=${svc.health.size} pid=${svc.health.pid}`)

  const display = (await httpJson(`http://127.0.0.1:${svc.port}/s/${sid}/display?k=${svc.token}`)).json
  say(`会话显示 ${display?.display} （${display?.size}）`)
  const configured = await serviceStreamConfig(svc, sid, { quality: 70, fps: 15, scale: 1 })
  report.laneS.streamConfig = {
    requested: { quality: 70, fps: 15, scale: 1 }, status: configured.status,
    applied: configured.json || null,
    note: configured.status === 200 ? '服务接受 stream-config' : '服务无 /stream-config（旧版）：用它的固定参数',
  }
  say(`stream-config → HTTP ${configured.status}${configured.status === 200 ? ` ${JSON.stringify(configured.json)}` : '（旧版没有这个端点，按其固定参数测）'}`)

  const xvfb = findProcs(['Xvfb', display?.display || '###']).map((p) => p.pid)
  const pids = [svc.health.pid, ...xvfb]
  report.laneS.pids = { service: svc.health.pid, xvfb, display: display?.display }
  say(`CPU 采样对象：service=${svc.health.pid}${xvfb.length ? ` xvfb=${xvfb.join(',')}` : ''} (CLK_TCK=${CLK_TCK})`)

  const phases = {}
  report.laneS.phases = phases

  // ---------------------------------------------------------- 阶段 1：动态 fps/带宽/CPU
  if (opts.phases.includes('fps')) {
    const log = path.join(opts.logDir, `anim-fps-${opts.label}.jsonl`)
    const anim = await startAnim(svc, sid,
      ['--fps', String(opts.animFps), '--duration', String(opts.seconds + 12), '--label', opts.label], log)
    const flips0 = readAnimLog(log).filter((r) => r.seq !== undefined).length
    const stream = startStream({ port: svc.port, token: svc.token, sid })
    // 等第一次真的出帧（旧版启动后第一帧可能好几秒）
    for (let i = 0; i < 80 && !stream.frames.length; i++) await sleep(100)
    const t0 = nowMs()
    const samplePids = [...pids, anim.pid]
    const cpu0 = cpuSnapshot(samplePids)
    await sleep(opts.seconds * 1000)
    const cpu1 = cpuSnapshot(samplePids)
    const t1 = nowMs()
    const stats = await serviceStats(svc, sid)
    await stream.stop()
    const flips = readAnimLog(log).filter((r) => r.seq !== undefined)
    const w = windowStats(stream.frames, t0, t1)
    const cpu = cpuDelta(cpu0, cpu1, (t1 - t0) / 1000)
    const animPid = anim.pid
    const cpuAnim = cpu[animPid] ?? null
    phases.dynamic = {
      ...w, animFlips: flips.length - flips0, animFps: (flips.length - flips0) / ((t1 - t0) / 1000),
      serverStats: stats, cpuPct: { ...cpu, animator: cpuAnim, xvfbSum: xvfb.reduce((s, p) => s + (cpu[p] || 0), 0) },
      streamHeadersSeen: stream.frames.some((f) => f.hasFrameHeaders),
      jpegMagicOk: stream.frames.length ? stream.frames.every((f) => f.jpegMagic) : null,
      contentType: stream.state.contentType, streamStatus: stream.state.status, streamError: stream.state.error,
      streamRetries: stream.state.retries,
      firstFrameWaitMs: stream.frames.length ? stream.frames[0].t - t0 : null,
      animExit: animExitInfo(log),
      animator: { pid: animPid, display: anim.display, fallbackExec: anim.fallback, log },
    }
    say(`动态窗口 ${((t1 - t0) / 1000).toFixed(1)}s：发帧 ${w.framesSent}（${w.sentFps.toFixed(1)}/s）`
      + ` 内容帧 ${w.contentFrames}（**${w.contentFps.toFixed(2)} fps**）`
      + ` 带宽 ${kb(w.bytesPerSec)}/s 平均帧 ${w.avgFrameBytes ? kb(w.avgFrameBytes) : '—'}`)
    say(`        靶画面实际变化 ${(flips.length - flips0) / ((t1 - t0) / 1000)} fps；服务端 /stats `
      + (stats ? `fps=${stats.fps} captured=${stats.captured} encoded=${stats.encoded} skipped=${stats.skipped} bytesPerSec=${stats.bytesPerSec} mode=${stats.mode}` : '不可用（旧版没有 /stats）'))
    say(`        CPU：服务 ${pct(cpu[svc.health.pid])}｜Xvfb ${pct(xvfb.reduce((s, p) => s + (cpu[p] || 0), 0))}｜靶程序 ${pct(cpuAnim)}`)
    if (!opts.keep) await killAnim(svc, sid, anim)
  }

  // ---------------------------------------------------------- 阶段 2：变化延迟
  if (opts.phases.includes('latency')) {
    // 脉冲间隔**故意与抓帧周期（旧版 0.5s）互质**：0.61 与 0.5 每拍把相位推进 0.11s，
    // 14 拍正好把量化误差扫过 ~3 个周期 → p50/p95 才是分布估计，而不是"某一拍恰好差多少"。
    // （实测教训：间隔 1.4s + 8 拍时，p50 在三次独立运行里是 341/445/531ms —— 方差过大，
    //   因为 1.4 与 0.5 的相位只在两个值之间跳。）
    const N = 14, INTERVAL = 0.61
    const log = path.join(opts.logDir, `anim-lat-${opts.label}.jsonl`)
    const anim = await startAnim(svc, sid,
      ['--static', '--pulse-interval', String(INTERVAL), '--pulses', String(N),
        '--duration', String(N * INTERVAL + 8), '--label', opts.label], log)
    const stream = startStream({ port: svc.port, token: svc.token, sid })
    for (let i = 0; i < 80 && !stream.frames.length; i++) await sleep(100)
    await sleep(600)
    await waitFor(log, (rec) => bySrc(rec, 'pulse').length >= N, (N + 4) * INTERVAL * 1000)
    await sleep(500)
    const flips = readAnimLog(log).filter((r) => r.src === 'pulse')
    await stream.stop()
    const pairs = pairFlips(flips, stream.frames)
    const lats = pairs.map((p) => p.latencyMs).filter((v) => v !== null)
    const p = pcts(lats)
    // 收掉脉冲那台，**等它真的退出**，再起按键那台（两个满屏窗口重叠 = 量不到东西）
    await killAnim(svc, sid, anim)
    const leftover = await animProcs(svc, sid)
    // 同时用按键注入再量一次（含输入链路：POST /input → xdotool → 靶程序）
    const keyLog = path.join(opts.logDir, `anim-key-${opts.label}.jsonl`)
    const anim2 = await startAnim(svc, sid, ['--static', '--duration', '40', '--label', `${opts.label}-key`], keyLog)
    const stream2 = startStream({ port: svc.port, token: svc.token, sid })
    const t2Start = nowMs()
    for (let i = 0; i < 80 && !stream2.frames.length; i++) await sleep(100)
    await sleep(600)
    const keyDiag = {
      firstFrameWaitMs: stream2.frames.length ? stream2.frames[0].t - t2Start : null,
      framesBeforeFlips: stream2.frames.length, streamStatus: stream2.state.status,
      streamError: stream2.state.error, streamRetries: stream2.state.retries,
    }
    const visibility = await assertVisible(svc, sid, stream2, keyLog, 'key-phase')
    const keyLats = []
    const keyDetails = []
    for (let i = 0; i < (visibility.newFrameAfterFlip ? 6 : 2); i++) {
      const n0 = readAnimLog(keyLog).filter((r) => r.src === 'key').length
      const t0 = nowMs()
      await serviceInput(svc, sid, { t: 'key', k: 'a' })
      const t1 = nowMs()
      const got = await waitFor(keyLog, (rec) => bySrc(rec, 'key').length >= n0 + 1, 3000)
      const rec = got.filter((r) => r.src === 'key')[n0]
      // ⚠️ 这里**只记时间戳，不配对**：配对必须等新帧真的到了，
      //    在 flip 瞬间就配对必然全是 null（踩过：n=0 看着像"服务不响应按键"）。
      keyDetails.push({ tPost0: t0, tPostDone: t1, tFlip: rec ? rec.t * 1000 : null,
        inputPathMs: rec ? rec.t * 1000 - t0 : null, latencyMs: null, serverAgeMs: null })
      await sleep(900)
    }
    // 全部注入做完再配对：此时帧序列里已经有每一次翻页之后的新帧
    for (const d of keyDetails) {
      if (d.tFlip === null) continue
      const pr = pairFlips([{ t: d.tFlip / 1000, src: 'key', seq: 0 }], stream2.frames, 4000)[0]
      d.latencyMs = pr?.latencyMs ?? null
      d.serverAgeMs = pr?.serverAgeMs ?? null
      if (d.latencyMs !== null) keyLats.push(d.latencyMs)
    }
    phases.latency = {
      pulses: pairs, p50: p[50], p95: p[95], n: lats.length,
      headerAvailable: stream.frames.some((f) => f.hasFrameHeaders),
      serverAgeP50: pcts(pairs.map((x) => x.serverAgeMs).filter((v) => v !== null))[50] ?? null,
      animExit: animExitInfo(log), keyAnimExit: animExitInfo(keyLog),
      keyPulses: keyDetails, keyDiag, keyStreamFrames: stream2.frames.length, leftoverProcs: leftover.length, visibility,
      // 诊断用：帧到达/内容变化的时间线 vs 翻页时刻（配不上对时一眼看出卡在哪）
      keyTimeline: stream2.frames.slice(-60).map((f) => ({ t: Math.round(f.t), h: f.hash.slice(0, 6), ch: f.changed ? 1 : 0 })),
      keyFlips: readAnimLog(keyLog).filter((r) => r.src === 'key').map((r) => ({ t: Math.round(r.t * 1000), seq: r.seq })),
      keyAllSrcs: readAnimLog(keyLog).map((r) => r.src).join(','),
      keyP50: pcts(keyLats)[50] ?? null, keyP95: pcts(keyLats)[95] ?? null,
      keyInputPathP50: pcts(keyDetails.map((k) => k.inputPathMs).filter((v) => v !== null))[50] ?? null,
      note: stream.frames.some((f) => f.hasFrameHeaders)
        ? '帧头 X-DSH-Time 可用 → serverAgeMs = 帧到达 − 抓帧时刻'
        : '旧版无 X-DSH-* 帧头 → 只有客户端本地计时（serverAgeMs 为 null）',
    }
    say(`脉冲延迟（X 侧画面变化 → 帧到达客户端，n=${lats.length}/${pairs.length}）：`
      + `p50 **${ms(p[50])}** p95 ${ms(p[95])}`
      + (phases.latency.serverAgeP50 !== null ? `（其中服务抓帧→到达 p50 ${ms(phases.latency.serverAgeP50)}）` : '（无帧头，无法拆分服务段）'))
    say(`按键注入延迟（POST /input → 画面变化 → 帧到达，n=${keyLats.length}）：p50 ${ms(phases.latency.keyP50)}`
      + `；输入链路本身占 p50 ${ms(phases.latency.keyInputPathP50)}`)
    if (!opts.keep) { await killAnim(svc, sid, anim); await killAnim(svc, sid, anim2) }
  }

  // ---------------------------------------------------------- 阶段 3：静止带宽 + 静止 CPU
  if (opts.phases.includes('static')) {
    const log = path.join(opts.logDir, `anim-static-${opts.label}.jsonl`)
    const anim = await startAnim(svc, sid, ['--static', '--duration', String(opts.seconds + 10), '--label', opts.label], log)
    const stream = startStream({ port: svc.port, token: svc.token, sid })
    for (let i = 0; i < 80 && !stream.frames.length; i++) await sleep(100)
    await sleep(2500)                                    // 让旧版把"变化中"的帧消化掉
    const t0 = nowMs()
    const cpu0 = cpuSnapshot(pids)
    await sleep(opts.seconds * 1000)
    const cpu1 = cpuSnapshot(pids)
    const t1 = nowMs()
    const stats = await serviceStats(svc, sid)
    await stream.stop()
    const w = windowStats(stream.frames, t0, t1)
    const cpu = cpuDelta(cpu0, cpu1, (t1 - t0) / 1000)
    phases.static = { ...w, serverStats: stats, cpuPct: { ...cpu },
      animExit: animExitInfo(log),
      animFlips: readAnimLog(log).filter((r) => r.seq !== undefined).length }
    say(`静止窗口 ${(t1 - t0) / 1000}s：发帧 ${w.framesSent}（${w.sentFps.toFixed(1)}/s）`
      + ` 内容帧 ${w.contentFrames}（${w.contentFps.toFixed(2)} fps）`
      + ` 带宽 **${kb(w.bytesPerSec)}/s**` + (stats ? `；/stats.bytesPerSec=${stats.bytesPerSec}` : '（旧版无 /stats）'))
    say(`        CPU：服务 ${pct(cpu[svc.health.pid])}｜Xvfb ${pct(xvfb.reduce((s, p) => s + (cpu[p] || 0), 0))}`)
    if (!opts.keep) await killAnim(svc, sid, anim)
  }

  // ---------------------------------------------------------- 汇总
  const dyn = phases.dynamic, lat = phases.latency, sta = phases.static
  report.laneS.metrics = {
    contentFps: dyn ? dyn.contentFps : null,
    sentFps: dyn ? dyn.sentFps : null,
    serverStatsFps: dyn?.serverStats?.fps ?? null,
    latencyP50: lat ? lat.p50 : null,
    latencyP95: lat ? lat.p95 : null,
    bytesPerSecDynamic: dyn ? dyn.bytesPerSec : null,
    bytesPerSecStatic: sta ? sta.bytesPerSec : null,
    serviceCpuDynamic: dyn ? (dyn.cpuPct[svc.health.pid] ?? null) : null,
    serviceCpuStatic: sta ? (sta.cpuPct[svc.health.pid] ?? null) : null,
  }
  return report.laneS.metrics
}

// ------------------------------------------------------------------ 页面探针（canvas/img 去重指纹）
const PROBE_SRC = `(() => {
  if (window.__perfProbe) return 'already'
  const W = 64, H = 40
  const off = document.createElement('canvas'); off.width = W; off.height = H
  const octx = off.getContext('2d', { willReadFrequently: true })
  let prev = new Uint8ClampedArray(W * H * 4)
  let cur = new Uint8ClampedArray(W * H * 4)
  const st = { changes: [], samples: 0, kind: null, hash: null, errors: 0, lastError: null,
               hasPrev: false, startedAt: Date.now(), surfaceTag: null }
  window.__perfProbe = st
  const findSurface = () => {
    const cs = [...document.querySelectorAll('canvas')].filter(c => c.width > 32 && c.height > 32 && c.isConnected)
    if (cs.length) return { el: cs[0], kind: 'canvas' }
    const imgs = [...document.querySelectorAll('img')].filter(i => i.naturalWidth > 32 &&
      /^(blob:|data:)/.test(i.currentSrc || i.src || '') && i.isConnected)
    if (imgs.length) return { el: imgs[imgs.length - 1], kind: 'img' }
    return null
  }
  const tick = () => {
    try {
      const s = findSurface()
      if (s) {
        st.kind = s.kind
        st.surfaceTag = s.el.tagName + (s.kind === 'canvas' ? ' ' + s.el.width + 'x' + s.el.height : '')
        octx.drawImage(s.el, 0, 0, W, H)
        const d = octx.getImageData(0, 0, W, H).data
        let h = 2166136261, diff = 0, maxd = 0
        for (let i = 0; i < d.length; i += 4) {
          const q = ((d[i] >> 3) << 10) ^ ((d[i + 1] >> 3) << 5) ^ (d[i + 2] >> 3)
          h = Math.imul(h ^ q, 16777619) >>> 0
          if (st.hasPrev) {
            const dd = Math.abs(d[i] - prev[i]) + Math.abs(d[i + 1] - prev[i + 1]) + Math.abs(d[i + 2] - prev[i + 2])
            if (dd > 30) diff++
            if (dd > maxd) maxd = dd
          }
        }
        cur.set(d)
        const tmp = prev; prev = cur; cur = tmp
        st.hasPrev = true
        st.samples++
        const changed = st.hash !== null && h !== st.hash && diff >= 2
        st.hash = h
        if (changed) st.changes.push({ t: Date.now(), diff, maxd })
      }
    } catch (e) { st.errors++; st.lastError = String(e && e.message || e) }
    st.raf = requestAnimationFrame(tick)
  }
  tick()
  return 'installed'
})()`

// ------------------------------------------------------------------ lane B
async function laneBrowser(report) {
  head('lane B（客户端，Playwright + 真 Brave）')
  if (!fs.existsSync(PLAYWRIGHT)) throw Object.assign(new Error(`Playwright 不存在：${PLAYWRIGHT}`), { code: 3 })
  if (!fs.existsSync(BROWSER)) throw Object.assign(new Error(`浏览器不存在：${BROWSER}`), { code: 3 })
  const { chromium } = await import(pathToFileURL(PLAYWRIGHT).href)
  const browser = await chromium.launch({
    executablePath: BROWSER, headless: opts.headless,
    args: ['--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage'],
  })
  const context = await browser.newContext({ viewport: { width: 1500, height: 950 } })
  const page = await context.newPage()
  const pageErrors = []
  const pluginHttp = []
  let sid = null
  const T0 = Date.now()
  page.on('pageerror', (e) => pageErrors.push(String(e.message).slice(0, 200)))
  page.on('response', (r) => {
    const u = r.url()
    if (u.includes('/api/dsh-display-panel/')) {
      pluginHttp.push({ ms: Date.now() - T0, path: u.replace(/^https?:\/\/[^/]+/, '').split('?')[0], status: r.status() })
      const m = u.match(/[?&]session=([^&]+)/)
      if (m && !sid) sid = decodeURIComponent(m[1])
    }
  })
  let shotSeq = 0
  const shot = async (name) => {
    shotSeq += 1
    const f = path.join(opts.shots, `${String(shotSeq).padStart(2, '0')}-${opts.label}-${name}.png`)
    try { await page.screenshot({ path: f }); return f } catch { return null }
  }
  const api = async (p, o = {}) => page.evaluate(async ([p, o]) => {
    const r = await fetch(p, { method: o.method || 'GET',
      headers: o.body ? { 'content-type': 'application/json' } : undefined,
      body: o.body ? JSON.stringify(o.body) : undefined, cache: 'no-store' })
    const text = await r.text()
    let json = null
    try { json = JSON.parse(text) } catch { /* 非 JSON */ }
    return { status: r.status, json, text: text.slice(0, 1000) }
  }, [p, o])
  const clickText = async (t, exact = true) => {
    const loc = page.getByText(t, { exact }).first()
    try {
      if (await loc.count() === 0) return false
      await loc.click({ timeout: 8000 })
      await page.waitForTimeout(1200)
      return true
    } catch { return false }
  }

  // CDP：真实网络字节 + 真实渲染进程 CPU
  const cdp = await context.newCDPSession(page)
  await cdp.send('Network.enable')
  const reqUrl = new Map()
  const netEvents = []
  cdp.on('Network.requestWillBeSent', (e) => reqUrl.set(e.requestId, e.request.url))
  cdp.on('Network.dataReceived', (e) => {
    const u = reqUrl.get(e.requestId) || ''
    netEvents.push({ t: nowMs(), url: u, len: e.encodedDataLength || 0 })
  })
  const netBytes = (from, to) => {
    const win = netEvents.filter((e) => e.t >= from && e.t <= to)
    const panel = win.filter((e) => e.url.includes('/api/dsh-display-panel/'))
    return {
      all: win.reduce((s, e) => s + e.len, 0),
      panel: panel.reduce((s, e) => s + e.len, 0),
      byPath: panel.reduce((acc, e) => {
        const p = e.url.replace(/^https?:\/\/[^/]+/, '').split('?')[0]
        acc[p] = (acc[p] || 0) + e.len
        return acc
      }, {}),
    }
  }
  const procInfo = async () => {
    try { return await cdp.send('SystemInfo.getProcessInfo') } catch { return null }
  }
  const rendererCpu = async () => {
    const info = await procInfo()
    if (!info) return null
    const r = info.processInfo.filter((p) => p.type === 'renderer')
    return r.reduce((s, p) => s + (p.cpuTime || 0), 0)
  }
  const subtreePids = () => {
    if (!browser.process()) return []
    const root = browser.process().pid
    const kids = new Map()
    for (const d of fs.readdirSync('/proc')) {
      if (!/^\d+$/.test(d)) continue
      try {
        const s = fs.readFileSync(`/proc/${d}/stat`, 'utf8')
        const ppid = Number(s.slice(s.lastIndexOf(')') + 2).split(' ')[1])
        if (!kids.has(ppid)) kids.set(ppid, [])
        kids.get(ppid).push(Number(d))
      } catch { /* 进程刚退出 */ }
    }
    const out = [root]
    for (let i = 0; i < out.length; i++) for (const c of kids.get(out[i]) || []) out.push(c)
    return out
  }

  try {
    // ---------------------------------------------------------- 打开 UI / 建会话 / 进面板
    await page.goto(opts.url, { waitUntil: 'load', timeout: opts.timeout })
    await page.waitForTimeout(4000)
    await clickText('稍后配置', false)
    let created = await clickText('新会话', false)
    if (!created) {
      const wsName = await page.evaluate(() => {
        const b = [...document.querySelectorAll('button')].find((e) => /workspace/i.test(e.className || ''))
        return b ? b.innerText.trim() : null
      })
      if (wsName) { await clickText(wsName, true); created = await clickText('新会话', false) }
    }
    const box = page.locator('textarea, [contenteditable="true"]').first()
    await box.waitFor({ state: 'visible', timeout: opts.timeout })
    await box.click()
    await box.type('性能测量（perf-panel.mjs）')
    await page.keyboard.press('Enter')
    await page.waitForTimeout(9000)
    await clickText('显示器', true)
    await page.waitForTimeout(4000)
    // 会话 id：既从网络请求里取，也从 /state 兜底
    if (!sid) {
      const st = await api('/api/dsh-display-panel/info')
      sid = st.json?.session || null
    }
    if (!sid) throw new Error('拿不到面板会话 id（/frame?session= 与 /info 都没有）')
    report.laneB = { url: opts.url, session: sid, uiUrl: page.url(), panelOpened: true }
    say(`面板已打开 session=${sid}`)

    await page.evaluate(PROBE_SRC)
    const probeInfo = await page.evaluate(() => ({
      kind: window.__perfProbe?.kind, samples: window.__perfProbe?.samples,
      surfaceTag: window.__perfProbe?.surfaceTag,
    }))
    if (!probeInfo.kind) {
      await page.waitForTimeout(5000)
      const again = await page.evaluate(() => ({ kind: window.__perfProbe?.kind, samples: window.__perfProbe?.samples }))
      say(`⚠️ 探针还没找到画面元素（samples=${again.samples}）—— 继续，但 fps 可能测不到`)
    } else {
      say(`探针已挂在 ${probeInfo.surfaceTag}（kind=${probeInfo.kind}）`)
    }

    // ---------------------------------------------------------- 起靶画面
    const animLog = path.join(opts.logDir, `anim-browser-${opts.label}.jsonl`)
    const startAnimViaApi = async (args, log) => {
      fs.writeFileSync(log, '')
      const r = await api(`/api/dsh-display-panel/exec?session=${encodeURIComponent(sid)}`, {
        method: 'POST', body: { argv: ['python3', ANIM, '--log', log, ...args], cwd: REPO, wait: false },
      })
      if (!r.json?.ok) throw new Error(`面板会话 /exec 起靶程序失败：${r.text}`)
      await page.waitForTimeout(800)
      return r.json
    }
    const killViaApi = async (pid) => {
      try { await api(`/api/dsh-display-panel/kill?session=${encodeURIComponent(sid)}`,
        { method: 'POST', body: { pid } }) } catch { /* 已退出 */ }
    }

    const phases = report.laneB.phases = {}
    const svcPid = (await api(`/api/dsh-display-panel/info`)).json?.service?.pid ?? null
    const statsProbe = async () => (await api(`/api/dsh-display-panel/stats?session=${encodeURIComponent(sid)}`))

    // ---------------------------------------------------------- B1 动态：内容 fps / 带宽 / CPU
    {
      const ex = await startAnimViaApi(['--fps', String(opts.animFps), '--duration', String(opts.seconds + 15),
        '--label', opts.label], animLog)
      await api(`/api/dsh-display-panel/stream-config?session=${encodeURIComponent(sid)}`,
        { method: 'POST', body: { quality: 70, fps: 15, scale: 1 } }).catch(() => {})
      await page.evaluate(() => { window.__perfProbe.changes.length = 0 })
      const cpuSnap = cpuSnapshot([svcPid, ...subtreePids()].filter(Boolean))
      const rc0 = await rendererCpu()
      const t0 = nowMs()
      await page.waitForTimeout(opts.seconds * 1000)
      const t1 = nowMs()
      const rc1 = await rendererCpu()
      const cpuAfter = cpuSnapshot(Object.keys(cpuSnap).map(Number))
      const cpu = cpuDelta(cpuSnap, cpuAfter, (t1 - t0) / 1000)
      const probs = await page.evaluate(() => window.__perfProbe.changes.slice())
      const probeState = await page.evaluate(() => ({ samples: window.__perfProbe.samples, kind: window.__perfProbe.kind, errors: window.__perfProbe.errors }))
      const flips = readAnimLog(animLog).filter((r) => r.seq !== undefined)
      const net = netBytes(t0, t1)
      const stats = await statsProbe()
      const dur = (t1 - t0) / 1000
      phases.dynamic = {
        seconds: dur, canvasChanges: probs.length, contentFps: probs.length / dur,
        probeSamples: probeState.samples, probeFps: probeState.samples / dur, probeKind: probeState.kind,
        probeErrors: probeState.errors,
        animFlips: flips.length, animFps: flips.length / dur,
        panelBytesPerSec: net.panel / dur, allBytesPerSec: net.all / dur, bytesByPath: net.byPath,
        serviceCpuPct: svcPid ? (cpu[svcPid] ?? null) : null,
        browserCpuPct: Object.values(cpu).reduce((a, b) => a + b, 0) - (svcPid ? (cpu[svcPid] || 0) : 0),
        rendererCpuPct: rc0 !== null && rc1 !== null ? (rc1 - rc0) / dur * 100 : null,
        serverStats: stats.json, serverStatsStatus: stats.status,
      }
      await shot('dynamic')
      say(`动态窗口 ${dur.toFixed(1)}s：canvas 内容帧 ${probs.length}（**${(probs.length / dur).toFixed(2)} fps**，`
        + `rAF 采样 ${probeState.samples} 次/surface=${probeState.kind}）；靶画面 ${flips.length}（${(flips.length / dur).toFixed(1)} fps）`)
      say(`        客户端实收面板字节 ${kb(net.panel / dur)}/s（全页 ${kb(net.all / dur)}/s）`
        + `；/stats ` + (stats.json ? `fps=${stats.json.fps} bytesPerSec=${stats.json.bytesPerSec} mode=${stats.json.mode} quality=${stats.json.quality} fps_cfg=${stats.json.fps}` : `不可用(HTTP ${stats.status})`))
      say(`        CPU：服务 ${pct(phases.dynamic.serviceCpuPct)}｜渲染进程 ${pct(phases.dynamic.rendererCpuPct)}｜Brave 全树 ${pct(phases.dynamic.browserCpuPct)}`)
      if (!opts.keep) await killViaApi(ex.pid)
    }

    // ---------------------------------------------------------- B2 变化延迟（脉冲 + canvas / 探针流交叉验证）
    {
      const N = 14, INTERVAL = 0.61        // 同 lane S：与抓帧周期互质，扫相位
      await startAnimViaApi(['--static', '--pulse-interval', String(INTERVAL), '--pulses', String(N),
        '--duration', String(N * INTERVAL + 8), '--label', opts.label], animLog)
      // 页面内自带一条 MJPEG 观察连接：与面板**互不干扰**地看同一条流，用来交叉验证延迟
      await page.evaluate(async (url) => {
        const st = window.__perfStream = { frames: [], bytes: 0, status: 0, ct: null, error: null, headers: null, parsed: 0 }
        try {
          const r = await fetch(url, { cache: 'no-store' })
          st.status = r.status; st.ct = r.headers.get('content-type')
          const reader = r.body.getReader()
          let buf = new Uint8Array(0)
          const B = new TextEncoder().encode('--frame')
          const idxOf = (hay, needle, from) => {
            outer: for (let i = from; i <= hay.length - needle.length; i++) {
              for (let j = 0; j < needle.length; j++) if (hay[i + j] !== needle[j]) continue outer
              return i
            }
            return -1
          }
          for (;;) {
            const { value, done } = await reader.read()
            if (done) break
            st.bytes += value.length
            const nb = new Uint8Array(buf.length + value.length)
            nb.set(buf); nb.set(value, buf.length); buf = nb
            for (;;) {
              const s = idxOf(buf, B, 0)
              if (s < 0) break
              if (s > 0) { buf = buf.subarray(s); continue }
              let he = -1
              for (let i = 0; i + 3 < buf.length; i++) {
                if (buf[i] === 13 && buf[i + 1] === 10 && buf[i + 2] === 13 && buf[i + 3] === 10) { he = i; break }
              }
              if (he < 0) break
              const head = new TextDecoder('latin1').decode(buf.subarray(0, he))
              const h = {}
              for (const line of head.split('\r\n')) { const i = line.indexOf(':'); if (i > 0) h[line.slice(0, i).trim().toLowerCase()] = line.slice(i + 1).trim() }
              const len = Number(h['content-length'] ?? -1)
              const ps = he + 4
              let pe
              if (len >= 0) { pe = ps + len; if (buf.length < pe) break } else { const n2 = idxOf(buf, B, ps); if (n2 < 0) break; pe = n2 }
              let hash = 2166136261
              for (let i = ps; i < pe; i++) hash = Math.imul(hash ^ buf[i], 16777619) >>> 0
              st.frames.push({ t: Date.now(), bytes: pe - ps, hash, seq: h['x-dsh-seq'] ?? null,
                xTime: h['x-dsh-time'] ? Number(h['x-dsh-time']) : null, size: h['x-dsh-size'] ?? null })
              st.parsed++
              if (!st.headers && (h['x-dsh-seq'] || h['x-dsh-time'])) st.headers = h
              buf = buf.subarray(pe)
            }
            if (buf.length > 4 << 20) buf = buf.subarray(buf.length - (1 << 20))
          }
        } catch (e) { st.error = String(e && e.message || e) }
      }, `/api/dsh-display-panel/stream?session=${encodeURIComponent(sid)}`).catch(() => {})
      await page.waitForTimeout(1500)
      await page.evaluate(() => { window.__perfProbe.changes.length = 0 })
      await waitFor(animLog, (rec) => bySrc(rec, 'pulse').length >= N, (N + 4) * INTERVAL * 1000)
      await page.waitForTimeout(800)
      const flips = readAnimLog(animLog).filter((r) => r.src === 'pulse')
      const probs = await page.evaluate(() => window.__perfProbe.changes.slice())
      const streamState = await page.evaluate(() => ({
        frames: window.__perfStream?.frames?.slice(-400) || [], bytes: window.__perfStream?.bytes,
        status: window.__perfStream?.status, ct: window.__perfStream?.ct, error: window.__perfStream?.error,
        headers: window.__perfStream?.headers, parsed: window.__perfStream?.parsed,
      }))
      // canvas 侧配对：第一次像素变化发生在 tFlip 之后
      const pairs = flips.map((f) => {
        const t = f.t * 1000
        const hit = probs.find((p) => p.t > t)
        return { seq: f.seq, tFlip: t, latencyMs: hit ? hit.t - t : null, diff: hit?.diff ?? null }
      })
      const lats = pairs.map((p) => p.latencyMs).filter((v) => v !== null)
      // 探针流侧配对（独立连接，含帧头 X-DSH-Time）
      const spairs = pairFlips(flips, streamState.frames.map((f) => ({ ...f, t: f.t, hash: String(f.hash), xTime: f.xTime })))
      const slats = spairs.map((p) => p.latencyMs).filter((v) => v !== null)
      const canvasP = pcts(lats), streamP = pcts(slats)
      phases.latency = {
        n: lats.length, pulses: flips.length, p50: canvasP[50], p95: canvasP[95], pairs,
        probeStream: { status: streamState.status, contentType: streamState.ct, frames: streamState.parsed,
          bytes: streamState.bytes, error: streamState.error,
          hasFrameHeaders: !!streamState.headers, sampleHeader: streamState.headers,
          p50: streamP[50], p95: streamP[95],
          serverAgeP50: pcts(spairs.map((x) => x.serverAgeMs).filter((v) => v !== null))[50] ?? null },
        clientOverheadP50: canvasP[50] !== null && streamP[50] !== null ? canvasP[50] - streamP[50] : null,
      }
      await shot('latency')
      say(`变化延迟（X 画面变化 → canvas 画出来，n=${lats.length}/${flips.length}）：p50 **${ms(canvasP[50])}** p95 ${ms(canvasP[95])}`)
      say(`        独立流观察同一条流：p50 ${ms(streamP[50])}（HTTP ${streamState.status}，帧头 `
        + `${streamState.headers ? `有（X-DSH-Time）→ 抓帧→到达 p50 ${ms(phases.latency.probeStream.serverAgeP50)}` : '无（旧版）'}）`
        + `；客户端解码+绘制开销 ≈ ${ms(phases.latency.clientOverheadP50)}`)
      if (!opts.keep) await killViaApi((await api(`/api/dsh-display-panel/procs?session=${encodeURIComponent(sid)}`)).json?.procs?.slice(-1)[0]?.pid)
    }

    // ---------------------------------------------------------- B3 按键注入延迟（含输入链路）
    {
      await startAnimViaApi(['--static', '--duration', '60', '--label', `${opts.label}-key`], animLog)
      await page.waitForTimeout(1200)
      const keyLats = []
      const details = []
      for (let i = 0; i < 6; i++) {
        await page.evaluate(() => { window.__perfProbe.changes.length = 0 })
        const n0 = readAnimLog(animLog).filter((r) => r.src === 'key').length
        const tPost0 = nowMs()
        const r = await api(`/api/dsh-display-panel/input?session=${encodeURIComponent(sid)}`,
          { method: 'POST', body: { t: 'key', k: 'a' } })
        const tPost1 = nowMs()
        const got = await waitFor(animLog, (rec) => bySrc(rec, 'key').length >= n0 + 1, 3000)
        const rec = got.filter((x) => x.src === 'key')[n0]
        if (!rec) { details.push({ error: 'no flip', inputStatus: r.status }); continue }
        const tFlip = rec.t * 1000
        const probs = await page.evaluate(() => window.__perfProbe.changes.slice())
        const hit = probs.find((p) => p.t > tFlip)
        const lat = hit ? hit.t - tFlip : null
        details.push({ inputStatus: r.status, inputPathMs: tFlip - tPost0, postMs: tPost1 - tPost0, latencyMs: lat })
        if (lat !== null) keyLats.push(lat)
        await page.waitForTimeout(900)
      }
      const kp = pcts(keyLats)
      phases.keyLatency = { n: keyLats.length, p50: kp[50], p95: kp[95], details,
        inputPathP50: pcts(details.map((d) => d.inputPathMs).filter((v) => v !== null))[50] ?? null }
      say(`按键注入延迟（/input → 画面变化 → canvas，n=${keyLats.length}）：p50 ${ms(kp[50])} p95 ${ms(kp[95])}`
        + `；其中输入链路 p50 ${ms(phases.keyLatency.inputPathP50)}`)
      const procs = (await api(`/api/dsh-display-panel/procs?session=${encodeURIComponent(sid)}`)).json?.procs || []
      if (!opts.keep) await killViaApi(procs.slice(-1)[0]?.pid)
    }

    // ---------------------------------------------------------- B4 静止带宽 + CPU
    {
      await startAnimViaApi(['--static', '--duration', String(opts.seconds + 10), '--label', opts.label], animLog)
      await page.waitForTimeout(3000)
      const cpuSnap = cpuSnapshot([svcPid, ...subtreePids()].filter(Boolean))
      const rc0 = await rendererCpu()
      await page.evaluate(() => { window.__perfProbe.changes.length = 0 })
      const t0 = nowMs()
      await page.waitForTimeout(opts.seconds * 1000)
      const t1 = nowMs()
      const rc1 = await rendererCpu()
      const cpuAfter = cpuSnapshot(Object.keys(cpuSnap).map(Number))
      const cpu = cpuDelta(cpuSnap, cpuAfter, (t1 - t0) / 1000)
      const probs = await page.evaluate(() => window.__perfProbe.changes.slice())
      const net = netBytes(t0, t1)
      const stats = await statsProbe()
      const dur = (t1 - t0) / 1000
      phases.static = {
        seconds: dur, canvasChanges: probs.length, contentFps: probs.length / dur,
        panelBytesPerSec: net.panel / dur, allBytesPerSec: net.all / dur, bytesByPath: net.byPath,
        serviceCpuPct: svcPid ? (cpu[svcPid] ?? null) : null,
        rendererCpuPct: rc0 !== null && rc1 !== null ? (rc1 - rc0) / dur * 100 : null,
        serverStats: stats.json, serverStatsStatus: stats.status,
      }
      await shot('static')
      say(`静止窗口 ${dur.toFixed(1)}s：canvas 变化 ${probs.length} 次（${(probs.length / dur).toFixed(2)} fps）`
        + `；客户端实收面板字节 **${kb(net.panel / dur)}/s**` + (stats.json ? `；/stats.bytesPerSec=${stats.json.bytesPerSec}` : ''))
      say(`        CPU：服务 ${pct(phases.static.serviceCpuPct)}｜渲染进程 ${pct(phases.static.rendererCpuPct)}`)
      if (!opts.keep) {
        const procs = (await api(`/api/dsh-display-panel/procs?session=${encodeURIComponent(sid)}`)).json?.procs || []
        await killViaApi(procs.slice(-1)[0]?.pid)
      }
    }

    // ---------------------------------------------------------- 对抗性检查
    if (opts.adversarial) {
      phases.adversarial = await adversarial(context, page, api, { sid, animLog, startAnimViaApi, killViaApi, shot, svcPid, subtreePids, rendererCpu, nowMs, netBytes })
    }

    report.laneB.metrics = {
      contentFps: phases.dynamic?.contentFps ?? null,
      sentFps: phases.dynamic?.serverStats?.fps ?? null,
      serverStatsFps: phases.dynamic?.serverStats?.fps ?? null,
      latencyP50: phases.latency?.p50 ?? null,
      latencyP95: phases.latency?.p95 ?? null,
      bytesPerSecDynamic: phases.dynamic?.panelBytesPerSec ?? null,
      bytesPerSecStatic: phases.static?.panelBytesPerSec ?? null,
      serviceCpuDynamic: phases.dynamic?.serviceCpuPct ?? null,
      serviceCpuStatic: phases.static?.serviceCpuPct ?? null,
      rendererCpuDynamic: phases.dynamic?.rendererCpuPct ?? null,
      browserCpuDynamic: phases.dynamic?.browserCpuPct ?? null,
    }
    report.laneB.pluginHttp = pluginHttp.slice(-200)
    report.laneB.pageErrors = pageErrors
  } finally {
    if (!opts.keep) await browser.close().catch(() => {})
    else say('（--keep：浏览器留着不关，自己收尸）')
  }
  return report.laneB.metrics
}

// ------------------------------------------------------------------ 对抗性检查
async function adversarial(context, page, api, ctx) {
  const { sid, animLog, startAnimViaApi, killViaApi, shot } = ctx
  const enc = encodeURIComponent(sid)
  const out = {}
  head('对抗性检查')

  // ---- 1) 极档 quality=1 / fps=1 / scale=0.25：还能不能用、有没有按合同降档
  {
    await startAnimViaApi(['--fps', '10', '--duration', '70', '--label', `${opts.label}-extreme`], animLog)
    await page.waitForTimeout(2500)
    const cfg = await api(`/api/dsh-display-panel/stream-config?session=${enc}`,
      { method: 'POST', body: { quality: 1, fps: 1, scale: 0.25 } })
    await page.waitForTimeout(4000)
    await page.evaluate(() => { window.__perfProbe.changes.length = 0 })
    const t0 = Date.now()
    await page.waitForTimeout(12000)
    const dur = (Date.now() - t0) / 1000
    const probs = await page.evaluate(() => window.__perfProbe.changes.slice())
    const stats = (await api(`/api/dsh-display-panel/stats?session=${enc}`)).json
    const pix = await page.evaluate(() => {
      const c = document.querySelector('canvas')
      if (!c) return null
      const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data
      let nonblack = 0
      for (let i = 0; i < d.length; i += 4) if (d[i] > 12 || d[i + 1] > 12 || d[i + 2] > 12) nonblack++
      return { w: c.width, h: c.height, nonblack, total: d.length / 4 }
    })
    out.extremeConfig = {
      request: { quality: 1, fps: 1, scale: 0.25 }, status: cfg.status, applied: cfg.json,
      stats, contentFps: probs.length / dur, canvasNonBlack: pix,
      downsized: stats?.scale === 0.25 || /0\.25/.test(JSON.stringify(cfg.json || {})),
      usable: probs.length > 0 && !!pix && pix.nonblack > 50,
    }
    say(`极档 quality=1/fps=1/scale=0.25：HTTP ${cfg.status} applied=${JSON.stringify(cfg.json)}`)
    say(`        /stats=${JSON.stringify(stats)}`)
    say(`        面板可用性：contentFps=${(probs.length / dur).toFixed(2)} 非黑像素=${pix ? pix.nonblack + '/' + pix.total : '—'}`
      + ` → ${out.extremeConfig.usable ? '可用（画面仍在动）' : '⚠️ 不可用/画面静止'}`)
    await shot('extreme')
    await api(`/api/dsh-display-panel/stream-config?session=${enc}`, { method: 'POST', body: { quality: 70, fps: 15, scale: 1 } }).catch(() => {})
  }

  // ---- 2) 流被中断后自愈（服务重启）
  {
    const before = await page.evaluate(() => window.__perfProbe?.changes?.length ?? 0)
    const rst = await api(`/api/dsh-display-panel/service`, { method: 'POST', body: { action: 'restart' } })
    say(`服务重启（宿主 /service action=restart）：HTTP ${rst.status} ${rst.text.slice(0, 200)}`)
    let recovered = null
    const t0 = Date.now()
    for (let i = 0; i < 40; i++) {
      await page.waitForTimeout(1000)
      const n = await page.evaluate(() => window.__perfProbe?.changes?.length ?? 0)
      if (n > before + 3) { recovered = Date.now() - t0; break }
    }
    const pix = await page.evaluate(() => {
      const c = document.querySelector('canvas')
      if (!c) return null
      const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data
      let nonblack = 0
      for (let i = 0; i < d.length; i += 4) if (d[i] > 12 || d[i + 1] > 12 || d[i + 2] > 12) nonblack++
      return { nonblack, total: d.length / 4 }
    })
    out.restartHeal = { actionStatus: rst.status, response: rst.text.slice(0, 300), recoveredAfterMs: recovered, canvas: pix }
    say(`        自愈：画面重新变化用时 ${recovered === null ? '⚠️ >40s 未恢复' : recovered + 'ms'}；canvas 非黑 ${pix ? pix.nonblack + '/' + pix.total : '—'}`)
    await shot('after-restart')
    const procs = (await api(`/api/dsh-display-panel/procs?session=${enc}`)).json?.procs || []
    if (!opts.keep) await killViaApi(procs.slice(-1)[0]?.pid)
  }

  // ---- 3) 两个标签同看一个会话
  {
    await startAnimViaApi(['--fps', '10', '--duration', '60', '--label', `${opts.label}-tabs`], animLog)
    await page.waitForTimeout(2500)
    const page2 = await context.newPage()
    const errs2 = []
    page2.on('pageerror', (e) => errs2.push(String(e.message).slice(0, 160)))
    try {
      await page2.goto(page.url(), { waitUntil: 'load', timeout: opts.timeout })
      await page2.waitForTimeout(5000)
      await page2.evaluate(PROBE_SRC)
      await page2.waitForTimeout(6000)
      const p2 = await page2.evaluate(() => ({ kind: window.__perfProbe?.kind, changes: window.__perfProbe?.changes?.length ?? 0,
        samples: window.__perfProbe?.samples ?? 0, surface: window.__perfProbe?.surfaceTag }))
      const p1 = await page.evaluate(() => ({ kind: window.__perfProbe?.kind, changes: window.__perfProbe?.changes?.length ?? 0 }))
      out.twoTabs = { page1: p1, page2: p2, page2Errors: errs2, sameUrl: page.url() }
      say(`双标签：tab1 ${p1.kind} changes=${p1.changes}｜tab2 ${p2.kind} changes=${p2.changes} (surface=${p2.surface})`
        + ` → ${p2.changes > 3 ? '✅ 两个标签都在动' : '⚠️ tab2 没画面（可能没自动进同一个会话，见 JSON）'}`)
      await page2.screenshot({ path: path.join(opts.shots, `${opts.label}-tab2.png`) }).catch(() => {})
    } catch (e) {
      out.twoTabs = { error: String(e.message) }
      say(`双标签：⚠️ ${e.message}`)
    } finally { await page2.close().catch(() => {}) }
    const procs = (await api(`/api/dsh-display-panel/procs?session=${enc}`)).json?.procs || []
    if (!opts.keep) await killViaApi(procs.slice(-1)[0]?.pid)
  }

  // ---- 4) 宿主重启（会话跨宿主进程存活）
  out.hostRestart = { skipped: true, why: '宿主重启需要外部重启 DSH 进程；由报告手工核对（脚本内不重启别人的宿主）' }
  return out
}

// ------------------------------------------------------------------ main
const report = {
  tool: 'tools/perf-panel.mjs', label: opts.label, startedAt: new Date().toISOString(),
  options: { ...opts, url: opts.url ? opts.url.replace(/token=[^&]+/, 'token=***') : null },
  host: { hostname: spawnSync('hostname', { encoding: 'utf8' }).stdout?.trim(), cpus: (await import('node:os')).cpus().length,
    platforms: process.platform },
  code, targets: TARGETS, checks: [], error: null,
}
let exitCode = 0
let svc = null

try {
  svc = await ensureService()
  report.service = { port: svc.port, home: svc.home, started: svc.started,
    health: { ...svc.health, pid: svc.health.pid }, viewer: path.relative(REPO, opts.viewerPath) }
  const mS = await laneService(svc, report)
  let mB = null
  if (opts.url) {
    try {
      mB = await laneBrowser(report)
    } catch (e) {
      report.laneBError = String(e.message || e)
      say(`⚠️ lane B 失败：${e.message}`)
      if (e.code === 3) exitCode = 3
    }
  } else {
    say('')
    say('（没有 --url：只跑了 lane S。客户端侧指标 = 未测，见报告"未测项"）')
  }

  // ---------------------------------------------------------------- 判定
  head('判定（契约 §5.1 验收线）')
  const primary = mB || mS
  const via = mB ? '客户端 canvas（lane B）' : '服务端流（lane S）'
  say(`口径：${via}`)
  const rows = [
    ['contentFps', primary.contentFps, `去重后内容帧率（${via}）`],
    ['latencyP50', primary.latencyP50, `X 侧画面变化 → 画出来（${via}）`],
    ['bytesStatic', primary.bytesPerSecStatic, primary.bytesPerSecStatic !== null ? '静止画面带宽（客户端实收）' : '未测'],
    ['serviceCpuDynamic', primary.serviceCpuDynamic, '服务进程单核 CPU（动态 15fps 档）'],
    ['serviceCpuStatic', primary.serviceCpuStatic, '服务进程单核 CPU（静止）'],
  ]
  say('')
  say('指标                              基线(0.3.4)      目标        实测            结论')
  say('─'.repeat(88))
  for (const [key, value, note] of rows) {
    const ok = check(key, value === undefined ? null : value, note)
    const t = TARGETS[key]
    const base = t.baseline === null ? '—' : (t.unit === 'B/s' ? kb(t.baseline) + '/s' : `${t.baseline}${t.unit}`)
    const shown = value === null || value === undefined ? '未测'
      : (t.unit === 'B/s' ? `${kb(value)}/s` : `${value.toFixed(2)}${t.unit}`)
    const verdict = ok === null ? '未测' : ok ? 'PASS' : `FAIL（${checks[checks.length - 1].gap}）`
    say(`${t.name.padEnd(28)} ${base.padEnd(14)} ${(t.min !== undefined ? '≥' + t.min : '≤' + t.max).padEnd(10)} ${shown.padEnd(14)} ${verdict}`)
  }
  const failed = checks.filter((c) => c.ok === false)
  report.checks = checks
  report.summary = { laneS: mS, laneB: mB, failed: failed.map((c) => c.key), pass: failed.length === 0 && checks.some((c) => c.ok === true) }
  if (failed.length) {
    say('')
    say(`❌ 未达 §5.1：${failed.map((c) => `${c.name} ${c.value?.toFixed?.(2)}（目标 ${c.target}，${c.gap}）`).join('；')}`)
    if (exitCode === 0) exitCode = 1
  } else {
    say('')
    say('✅ 本次测得的口径内全部达标'
      + (mB ? '' : '（注意：客户端侧未测 —— 面板真实观感还要 lane B 才算数）'))
  }
} catch (e) {
  report.error = String(e.stack || e.message || e)
  say(`❌ ${e.message}`)
  if (exitCode === 0) exitCode = e.code === 3 ? 3 : 2
} finally {
  report.finishedAt = new Date().toISOString()
  report.log = LOG
  try { fs.writeFileSync(opts.json, JSON.stringify(report, null, 2)) } catch (e) { console.error('写 JSON 失败', e) }
  say('')
  say(`JSON: ${opts.json}`)
  if (svc?.started && !opts.keep) {
    // 收尸：只杀自己起的服务 + 它的 Xvfb（按显示号精确匹配，绝不误伤别人的）
    const disp = report.laneS?.pids?.display
    try { svc.child?.kill('SIGTERM') } catch { /* 已退出 */ }
    if (disp) {
      const xv = findProcs(['Xvfb', disp])
      for (const p of xv) { try { process.kill(p.pid, 'SIGTERM') } catch { /* 已退出 */ } }
      if (xv.length) say(`已收尸：服务 + Xvfb ${disp}（${xv.map((p) => p.pid).join(',')}）`)
    }
  }
}
process.exit(exitCode)
