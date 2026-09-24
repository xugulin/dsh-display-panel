/**
 * 一条命令看清"显示器服务现在是什么状态、下一步该做什么"。
 *
 *   node scripts/hint.js
 *
 * 为什么要有它：0.2.x 的 scripts/hint.js 是一段**没接任何钩子**的脚手架，
 * 里面还硬编码了 `--user xgl`（别人跑就是错的提示）；而这两件事都必须说清楚：
 *   1. **0.3.0 起不用手工起服务** —— 宿主半边在面板打开时会自动拉起
 *      （`DSH_VIEW_MANAGED=0` 可关），所以这里只报状态，不指挥用户去装 systemd；
 *   2. 想让它常驻（省掉冷启动）才需要 `scripts/install-service.sh`。
 *
 * 这个文件**刻意不挂在任何 npm 生命周期钩子上**（安装期的钩子一旦失败会让 `npm i` 直接失败，
 * 而这个包要能在没有 Python、没有 Xvfb 的机器上装得上）。想看提示就手工跑它。
 *
 * 只用 Node 内置模块：本机 HTTP 探测 + 读几个文件，不联网、不装依赖、1.5 秒超时。
 */
'use strict'

const fs = require('fs')
const http = require('http')
const os = require('os')
const path = require('path')
const { spawnSync } = require('child_process')

const HOME = process.env.DSH_DISPLAY_HOME || path.join(os.homedir(), '.cache', 'dsh-display')
const PORT_HINT = process.env.DSH_VIEW_PORT || '8099'

function read(file) {
  try {
    return fs.readFileSync(path.join(HOME, file), 'utf8').trim()
  } catch (error) {
    return ''
  }
}

function get(port, token) {
  return new Promise((resolve) => {
    const req = http.get(
      { host: '127.0.0.1', port, path: `/health?k=${encodeURIComponent(token)}`, timeout: 1500 },
      (res) => {
        let body = ''
        res.setEncoding('utf8')
        res.on('data', (chunk) => { body += chunk })
        res.on('end', () => resolve({ status: res.statusCode, body }))
      },
    )
    req.on('timeout', () => { req.destroy(); resolve(null) })
    req.on('error', () => resolve(null))
  })
}

async function main() {
  console.log('[dsh-display-panel] 运行目录 DSH_DISPLAY_HOME = ' + HOME)

  const token = read('token')
  console.log('  令牌文件 token：' + (token ? `有（${token.length} 字符，别的本机用户读不到）` : '没有（服务还没起过）'))
  const portFile = read('port')
  if (portFile) console.log('  服务自报端口 port：' + portFile)

  const py = spawnSync(process.env.DSH_VIEW_PYTHON || 'python3', ['-V'], { encoding: 'utf8' })
  const pyVersion = (py.stdout || py.stderr || '').trim()
  console.log('  python3：' + (py.status === 0 ? pyVersion : '找不到 —— 宿主会在 /info 的 missing[] 里明说'))

  const ports = [...new Set([portFile, PORT_HINT].filter(Boolean))]
  let found = null
  for (const port of ports) {
    const res = await get(Number(port), token)
    // 403 = 端口上有人但令牌不对（老版本服务没有 /health，返回 404 也算"有服务"）
    if (res && res.status !== 0) { found = { port, ...res }; break }
  }

  if (!found) {
    console.log('  服务：127.0.0.1 的 ' + ports.join(' / ') + ' 都没有应答')
    console.log('')
    console.log('  通常**什么都不用做**：打开 DSH 的「显示器」标签，宿主半边会自动拉起服务。')
    console.log('  想让它常驻（省掉冷启动）：bash scripts/install-service.sh')
    console.log('  想先看看为什么没起来：    bash scripts/install-service.sh --status')
    return
  }

  console.log(`  服务：端口 ${found.port} 有应答（HTTP ${found.status}）`)
  try {
    const health = JSON.parse(found.body)
    console.log(`        后端 ${health.backend} · ${health.size} · ${health.version} · PID ${health.pid} · 会话 ${health.sessions}`)
    if (Array.isArray(health.missing) && health.missing.length > 0) {
      console.log('        ⚠ 缺依赖（面板会显示同样的清单）：')
      for (const m of health.missing) console.log(`          ${m.tool} —— ${m.why}（安装：${m.package}）`)
    }
  } catch (error) {
    console.log('        （这个服务没有 /health，是 0.3.0 之前的版本：接口仍可用，但没法诊断/自动拉起）')
  }
  console.log('')
  console.log('  面板：DSH Web UI 里的「显示器」标签（不用记端口，也不用带令牌）')
}

main().catch((error) => {
  // 提示脚本绝不能把调用方弄崩 —— 出问题只打一行
  console.log('[dsh-display-panel] 检查失败：' + (error && error.message ? error.message : error))
})
