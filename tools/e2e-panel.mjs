#!/usr/bin/env node
/**
 * dsh-display-panel 端到端验收（**真浏览器**，不是 mock）：Playwright + Brave。
 *
 * 它验的是"用户视角"的那条主线：
 *   1. 打开 DSH Web UI →（首次）点「稍后配置」→ 选工作区 → 新建会话
 *   2. 发一条消息（**必须有这一步**：空会话不显示「对话/轨迹/显示器」标签，实测踩过）
 *   3. 点「显示器」标签 → 面板渲染出 <canvas>（不是 iframe）
 *   4. 面板经同源代理拉到真 JPEG（GET /api/dsh-display-panel/frame → 200 + image/jpeg + FFD8 魔数）
 *   5. 在画面上点一下 → POST /api/dsh-display-panel/input 发出，且坐标是 0..1 归一化
 *   6. 在面板上打字 → 同样有 input 请求带上我们输入的字符
 *   7. 全程截图；失败打印**页面 console 错误 + 请求失败清单 + 404 清单**
 *
 * 用法：
 *   node tools/e2e-panel.mjs --url "http://127.0.0.1:19399/?token=..." [选项]
 *
 * 选项：
 *   --url URL          DSH Web UI 地址（**要带 ?token=**，即 `dsh web` 启动日志里那一行）
 *   --shot-dir DIR     截图与 JSON 报告目录（默认 ./verify/shots）
 *   --timeout MS       单个 UI 步骤超时（默认 30000）
 *   --headless 0       有头运行（排查渲染问题用）
 *   --with-target      额外验证"注入真的落到那台显示上"：经宿主 /exec 拉起
 *                      tools/xtarget.py，再断言点击/打字到达靶程序（需要显示号可写）
 *   --xtarget PATH     靶程序路径（默认 <repo>/tools/xtarget.py）
 *   --allow-skip       缺依赖时也返回 0（默认返回 3，避免 CI 里静默变绿）
 *
 * 退出码：0=全过；1=有失败；3=缺依赖（Playwright/浏览器不存在）
 *
 * ⚠️ 它**不会**自己起 DSH 实例：给什么 URL 就连什么。请用隔离实例 + 隔离
 *    DSH_DISPLAY_HOME（例如 .verify/e2e-home/dsh-display），别连用户正在用的那个。
 */
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const REPO = path.resolve(HERE, '..')
const PLAYWRIGHT = process.env.DSH_E2E_PLAYWRIGHT
  || '/home/xgl/deepseek-harness/node_modules/.pnpm/playwright@1.61.1/node_modules/playwright/index.mjs'
const BROWSER = process.env.DSH_E2E_BROWSER || '/opt/brave.com/brave-origin-beta/brave'

// ----------------------------------------------------------------- 参数
const argv = process.argv.slice(2)
const arg = (name, def = null) => {
  const i = argv.indexOf(name)
  return i >= 0 && i + 1 < argv.length ? argv[i + 1] : def
}
const flag = (name) => argv.includes(name)
const opts = {
  url: arg('--url'),
  shotDir: path.resolve(arg('--shot-dir', path.join(REPO, 'verify', 'shots'))),
  timeout: Number(arg('--timeout', '30000')),
  headless: arg('--headless', '1') !== '0',
  withTarget: flag('--with-target'),
  xtarget: arg('--xtarget', path.join(REPO, 'tools', 'xtarget.py')),
  allowSkip: flag('--allow-skip'),
}
if (!opts.url || flag('-h') || flag('--help')) {
  console.log(fs.readFileSync(fileURLToPath(import.meta.url), 'utf8').split('*/')[0])
  process.exit(opts.url ? 0 : 2)
}
fs.mkdirSync(opts.shotDir, { recursive: true })

const report = { url: opts.url, startedAt: new Date().toISOString(), steps: [], info: {}, diagnostics: {} }
const step = (name, ok, detail = '') => {
  report.steps.push({ name, ok: !!ok, detail })
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? '  — ' + detail : ''}`)
}
const info = (name, detail) => {
  report.steps.push({ name, ok: null, detail })
  console.log(`INFO  ${name}${detail ? '  — ' + detail : ''}`)
}

// ----------------------------------------------------------------- 依赖
if (!fs.existsSync(PLAYWRIGHT)) {
  console.log(`SKIP(缺依赖)  Playwright 不存在：${PLAYWRIGHT}`)
  console.log('             可用 DSH_E2E_PLAYWRIGHT=<path/to/playwright/index.mjs> 指定')
  process.exit(opts.allowSkip ? 0 : 3)
}
if (!fs.existsSync(BROWSER)) {
  console.log(`SKIP(缺依赖)  浏览器不存在：${BROWSER}`)
  console.log('             可用 DSH_E2E_BROWSER=<path/to/chrome> 指定')
  process.exit(opts.allowSkip ? 0 : 3)
}
const { chromium } = await import(pathToFileURL(PLAYWRIGHT).href)

// ----------------------------------------------------------------- 采集
const consoleErrors = []
const pageErrors = []
const failedRequests = []
const notFound = []
const frames = []
const inputs = []          // 状态码
const postBodies = []
const pluginHttp = []      // {url, status} —— 按 URL 归因，比 console 文本可靠
const timeline = []        // {ms, path, status}：/frame 有没有重试，一眼看出
const T0 = Date.now()

const browser = await chromium.launch({
  executablePath: BROWSER,
  headless: opts.headless,
  args: ['--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage'],
})
const page = await browser.newPage({ viewport: { width: 1500, height: 950 } })
let shotSeq = 0
const shot = async (name) => {
  shotSeq += 1
  const file = path.join(opts.shotDir, `${String(shotSeq).padStart(2, '0')}-${name}.png`)
  try { await page.screenshot({ path: file }); return file } catch { return null }
}
page.on('console', (m) => { if (m.type() === 'error') consoleErrors.push(m.text().slice(0, 300)) })
page.on('pageerror', (e) => pageErrors.push(String(e.message).slice(0, 300)))
page.on('requestfailed', (r) => failedRequests.push(`${r.method()} ${r.url().slice(0, 160)} — ${r.failure()?.errorText}`))
page.on('request', (r) => {
  if (r.url().includes('/api/dsh-display-panel/input')) postBodies.push(r.postData() || '')
})
page.on('response', async (r) => {
  const u = r.url()
  if (r.status() === 404) notFound.push(u.slice(0, 200))
  if (u.includes('/api/dsh-display-panel/frame')) {
    const rec = { status: r.status(), type: r.headers()['content-type'] || '', url: u, jpeg: false }
    const m = u.match(/session=([^&]+)/)
    if (m) report.info.session = decodeURIComponent(m[1])
    if (rec.status === 200 && /image\/jpeg/.test(rec.type)) {
      try {
        const body = await r.body()
        rec.jpeg = body && body.length > 2 && body[0] === 0xff && body[1] === 0xd8
        rec.bytes = body ? body.length : 0
      } catch { /* body 可能已被丢弃，魔数检查不是必须的 */ }
    }
    frames.push(rec)
  }
  if (u.includes('/api/dsh-display-panel/input')) inputs.push(r.status())
  if (u.includes('/api/dsh-display-panel/')) {
    const p = u.replace(/^https?:\/\/[^/]+/, '')
    pluginHttp.push({ url: p, status: r.status() })
    timeline.push({ ms: Date.now() - T0, path: p.split('?')[0], status: r.status() })
  }
})

/** 在页面里调宿主接口（同源、带 cookie）—— 与面板走同一条路。 */
const api = async (p, o = {}) => page.evaluate(async ([p, o]) => {
  const r = await fetch(p, {
    method: o.method || 'GET',
    headers: o.body ? { 'content-type': 'application/json' } : undefined,
    body: o.body ? JSON.stringify(o.body) : undefined,
    cache: 'no-store',
  })
  const text = await r.text()
  let json = null
  try { json = JSON.parse(text) } catch { /* 非 JSON */ }
  return { status: r.status, json, text: text.slice(0, 1500) }
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

let bail = null
try {
  // ------------------------------------------------------------- 1. 打开
  await page.goto(opts.url, { waitUntil: 'load', timeout: opts.timeout })
  await page.waitForTimeout(4000)
  step('打开 DSH Web UI', true, page.url())
  if (await clickText('稍后配置', false)) info('首次配置弹窗已跳过', '点了「稍后配置」')
  await shot('opened')

  // ------------------------------------------------------------- 2. 建会话
  let created = await clickText('新会话', false)
  if (!created) {
    // 有些布局要先选工作区才有「新会话」
    const wsName = await page.evaluate(() => {
      const b = [...document.querySelectorAll('button')].find((e) => /workspace/i.test(e.className || ''))
      return b ? b.innerText.trim() : null
    })
    if (wsName) { await clickText(wsName, true); created = await clickText('新会话', false) }
  }
  step('进入新建会话', created, created ? '点了「新会话」' : '找不到「新会话」按钮')

  const box = page.locator('textarea, [contenteditable="true"]').first()
  await box.waitFor({ state: 'visible', timeout: opts.timeout })
  await box.click()
  await box.type('面板端到端探针（e2e-panel.mjs）')
  await page.keyboard.press('Enter')
  await page.waitForTimeout(9000)                       // 等消息落库 + 标签渲染
  const tabs = await page.evaluate(() => [...document.querySelectorAll('button,[role=tab]')]
    .map((e) => (e.innerText || '').trim()))
  const hasPanelTab = tabs.includes('显示器')
  step('会话视图出现「显示器」标签', hasPanelTab,
    '标签=' + JSON.stringify(tabs.filter((t) => ['对话', '轨迹', '显示器'].includes(t))))
  await shot('tabs')

  // ------------------------------------------------------------- 3. 打开面板
  step('点击「显示器」标签', await clickText('显示器', true))
  await page.waitForTimeout(4000)

  const canvasCount = await page.locator('canvas').count()
  const iframeCount = await page.locator('iframe').count()
  step('面板渲染出 canvas（契约 §3：不再用 iframe）',
    canvasCount > 0 && iframeCount === 0, `canvas=${canvasCount} iframe=${iframeCount}`)
  await page.waitForTimeout(2500)

  // ------------------------------------------------------------- 4. 画面
  const jpegOk = frames.filter((f) => f.status === 200 && /image\/jpeg/.test(f.type))
  const magicOk = jpegOk.some((f) => f.jpeg)
  step('同源 /frame 返回真 JPEG（200 + image/jpeg + FFD8）',
    jpegOk.length >= 3 && magicOk,
    `200-JPEG=${jpegOk.length}/${frames.length} 首帧=${jpegOk[0] ? jpegOk[0].bytes + 'B' : '无'} 魔数ok=${magicOk}`)
  step('会话 id 从 /frame?session= 取到（不是状态条上的截断 id）',
    !!report.info.session, String(report.info.session))

  const painted = await page.evaluate(() => {
    const c = document.querySelector('canvas')
    if (!c) return null
    const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data
    let nonBlack = 0
    for (let i = 0; i < d.length; i += 4) {
      if (d[i] > 12 || d[i + 1] > 12 || d[i + 2] > 12) nonBlack += 1
    }
    return { w: c.width, h: c.height, nonBlack, total: d.length / 4 }
  })
  if (opts.withTarget) {
    step('canvas 上画出了画面（非全黑）', painted && painted.nonBlack > 50, JSON.stringify(painted))
  } else {
    info('canvas 像素统计（未 --with-target 时显示通常是全黑，不作断言）', JSON.stringify(painted))
  }
  await shot('panel-live')

  // ------------------------------------------------------------- 5. 点击 → 输入事件
  const dispState = report.info.session
    ? await api(`/api/dsh-display-panel/state?session=${encodeURIComponent(report.info.session)}`)
    : { json: null }
  const dims = String(dispState.json?.size || '1024x768').split('x').map(Number)
  const bb = await page.locator('canvas').first().boundingBox()
  // 画面按 contain 居中（客户端会抠掉黑边），点击必须落在**画面矩形**里
  const scale = Math.min(bb.width / dims[0], bb.height / dims[1])
  const drawW = dims[0] * scale
  const drawH = dims[1] * scale
  const offX = (bb.width - drawW) / 2
  const offY = (bb.height - drawH) / 2
  const fx = 0.3, fy = 0.45
  const clickAt = { x: bb.x + offX + drawW * fx, y: bb.y + offY + drawH * fy }
  info('画面几何', `canvas=${Math.round(bb.width)}x${Math.round(bb.height)} 显示=${dims.join('x')} ` +
    `contain=${Math.round(drawW)}x${Math.round(drawH)} offset=(${Math.round(offX)},${Math.round(offY)}) 点击=(${Math.round(clickAt.x)},${Math.round(clickAt.y)})`)
  await page.mouse.click(clickAt.x, clickAt.y)
  await page.waitForTimeout(1500)

  const clickEvents = postBodies.map((b) => { try { return JSON.parse(b) } catch { return null } }).filter(Boolean)
  const clickEv = clickEvents.find((e) => e.t === 'click' || e.t === 'down' || e.t === 'up')
  const inRange = (e) => e && typeof e.x === 'number' && typeof e.y === 'number'
    && e.x >= 0 && e.x <= 1 && e.y >= 0 && e.y <= 1
  step('点画面 → POST /input 且坐标 0..1 归一化',
    !!clickEv && inRange(clickEv) && inputs.length > 0 && inputs.every((s) => s === 200),
    `事件=${JSON.stringify(clickEvents.slice(0, 3))} 状态=${JSON.stringify(inputs.slice(0, 5))}`)
  const geomOk = clickEv && Math.abs(clickEv.x - fx) < 0.05 && Math.abs(clickEv.y - fy) < 0.05
  step('点击几何换算正确（点画面 30%,45% → 客户端算出 ≈0.30,0.45）', geomOk,
    `期望≈(0.30,0.45) 实际=(${clickEv ? clickEv.x : '?'},${clickEv ? clickEv.y : '?'})`)

  // ------------------------------------------------------------- 6. 打字
  const typed = 'hello123'
  const before = postBodies.length
  await page.keyboard.type(typed, { delay: 60 })
  await page.waitForTimeout(2000)
  const typedEvents = postBodies.slice(before).map((b) => { try { return JSON.parse(b) } catch { return null } }).filter(Boolean)
  const gotText = typedEvents.filter((e) => e.t === 'text').map((e) => e.s).join('')
    + typedEvents.filter((e) => e.t === 'key' && typeof e.k === 'string').map((e) => e.k).join('')
  step('在面板上打字 → 有 input 事件带上输入的字符', gotText.includes(typed) || gotText.includes('hello'),
    `发出=${JSON.stringify(typedEvents.slice(0, 14))}`)
  await shot('panel-typed')

  // ------------------------------------------------------------- 7. 可选：真的落到显示上
  if (opts.withTarget) {
    const sid = report.info.session
    const log = path.join(opts.shotDir, 'xtarget.log')
    try { fs.unlinkSync(log) } catch { /* 首次运行没有这个文件 */ }
    const ex = await api(`/api/dsh-display-panel/exec?session=${encodeURIComponent(sid)}`, {
      method: 'POST',
      body: { argv: ['/usr/bin/python3', opts.xtarget, log, '--label', 'E2E', '--timeout', '90'],
              cwd: opts.shotDir, wait: false },
    })
    step('宿主 /exec 在会话显示上拉起靶程序', ex.status === 200 && ex.json?.ok === true,
      JSON.stringify(ex.json).slice(0, 200))
    await page.waitForTimeout(2500)
    const st = await api(`/api/dsh-display-panel/state?session=${encodeURIComponent(sid)}`)
    step('该显示上出现窗口（/state.windows>0）', (st.json?.windows || 0) > 0,
      `windows=${st.json?.windows} display=${st.json?.display}`)
    await page.mouse.click(clickAt.x, clickAt.y)
    await page.waitForTimeout(1200)
    await page.keyboard.type(typed, { delay: 60 })
    await page.waitForTimeout(1500)
    const cat = await api(`/api/dsh-display-panel/exec?session=${encodeURIComponent(sid)}`, {
      method: 'POST', body: { argv: ['/bin/cat', log], wait: true },
    })
    const out = cat.json?.stdout || ''
    report.info.targetLog = out.slice(-1200)
    const m = (out.split('\n').filter((l) => l.includes('BUTTON ')).pop() || '').match(/x=(\d+) y=(\d+)/)
    const lastInput = clickEvents.filter((e) => typeof e.x === 'number').pop()
    if (lastInput && m) {
      const expX = Math.round(lastInput.x * dims[0])
      const expY = Math.round(lastInput.y * dims[1])
      const okPx = Math.abs(Number(m[1]) - expX) <= 3 && Math.abs(Number(m[2]) - expY) <= 3
      step('点击真的落到显示上（像素误差 ≤3）', okPx,
        `客户端发出=(${lastInput.x},${lastInput.y}) 期望≈(${expX},${expY}) 靶程序收到=(${m[1]},${m[2]})`)
    } else {
      step('点击真的落到显示上', false, `靶程序日志片段：${out.slice(-200)}`)
    }
    step('键盘输入真的落到显示上', out.includes(typed),
      (out.split('\n').filter((l) => l.includes('LINE')).pop() || out.slice(-120)).slice(0, 200))
    await shot('panel-with-target')
  }

  // ------------------------------------------------------------- 8. 诊断
  // 归因要按 **URL**，不能按 console 文本：浏览器会把任何一个 4xx/5xx 都打成
  // "Failed to load resource"（连 DSH 自己的 /open-in-app/icon/filemanager 404 也算），
  // 而契约 §1.2 **明确允许** /frame 在没有帧时先回 503 —— 那是设计，不是故障。
  const pluginBad = pluginHttp.filter((r) => r.status >= 400 && r.status !== 503)
  const frame503 = pluginHttp.filter((r) => r.status === 503 && /\/frame/.test(r.url))
  const other503 = pluginHttp.filter((r) => r.status === 503 && !/\/frame/.test(r.url))
  step('插件接口没有 4xx/5xx（/frame 首帧 503 属契约允许，且必须随后恢复）',
    pluginBad.length === 0 && other503.length === 0 && jpegOk.length >= 3,
    `非503错误=${JSON.stringify(pluginBad.slice(0, 3))} /frame 503=${frame503.length}（随后 200 JPEG=${jpegOk.length}）` +
    ` 其它503=${JSON.stringify(other503.slice(0, 3))}`)
  step('页面没有 JS 异常（pageerror）', pageErrors.length === 0,
    pageErrors.length ? JSON.stringify(pageErrors.slice(0, 3)) : '无')
  info('页面 console 错误（含非插件来源，供排查）',
    consoleErrors.length ? JSON.stringify(consoleErrors.slice(0, 4)) : '无')
} catch (error) {
  bail = error
  step('脚本跑完全部步骤', false, String((error && error.stack) || error).slice(0, 700))
  await shot('failure')
} finally {
  report.diagnostics = {
    consoleErrors: consoleErrors.slice(0, 20),
    pageErrors: pageErrors.slice(0, 20),
    failedRequests: failedRequests.slice(0, 20),
    notFound: [...new Set(notFound)].slice(0, 20),
    frameSample: frames.slice(-5),
    inputStatuses: inputs.slice(0, 20),
    timeline: timeline.filter((e) => /\/frame/.test(e.path)).slice(-40),
  }
  await browser.close()
  const file = path.join(opts.shotDir, 'e2e-report.json')
  fs.writeFileSync(file, JSON.stringify(report, null, 2))
  console.log('\n---- 诊断（页面侧）----')
  console.log('console 错误：' + (consoleErrors.length ? JSON.stringify(consoleErrors.slice(0, 5)) : '无'))
  console.log('pageerror：' + (pageErrors.length ? JSON.stringify(pageErrors.slice(0, 5)) : '无'))
  console.log('请求失败：' + (failedRequests.length ? JSON.stringify(failedRequests.slice(0, 5)) : '无'))
  console.log('404：' + (notFound.length ? JSON.stringify([...new Set(notFound)].slice(0, 5)) : '无'))
  const ftl = timeline.filter((e) => /\/frame/.test(e.path))
  console.log(`/frame 请求 ${ftl.length} 次：` + JSON.stringify(ftl.slice(0, 8).map((e) => `${e.ms}ms:${e.status}`))
    + (ftl.length > 8 ? ` … 末次 ${ftl[ftl.length - 1].ms}ms:${ftl[ftl.length - 1].status}` : ''))
  const passed = report.steps.filter((s) => s.ok === true).length
  const failed = report.steps.filter((s) => s.ok === false)
  console.log('\n== 汇总 ==')
  console.log(`${passed} 通过 / ${failed.length} 失败（截图与报告：${opts.shotDir}）`)
  for (const f of failed) console.log(`  失败：${f.name}  — ${f.detail}`)
  console.log(failed.length === 0 ? '结果：PASS（退出码 0）' : '结果：FAIL（退出码 1）')
  if (bail) console.log(String(bail).slice(0, 400))
  process.exit(failed.length === 0 ? 0 : 1)
}
