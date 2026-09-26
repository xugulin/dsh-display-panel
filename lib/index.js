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
 * 2.5. **流式透传**（0.4.0 新增）：`/stream` 把上游的 MJPEG 长连接**逐 chunk** 转给浏览器
 *    （`pipe`，不攒帧、不设 Content-Length），客户端断开就立刻断上游 —— 见 proxyStream()。
 * 3. **服务生命周期**：没起就自动拉起 `service/dsh-display-viewer.py`
 *    （`detached` + `unref`，不随宿主退出），并支持 start/stop/restart/status。
 *
 * 设计约束（与 docs/CONTRACT.md §2/§4 对应）：
 *
 * * 每个路由都走 `ctx.connection.requestRejection(req)` —— 复用宿主自己的鉴权，
 *   不自己造一套（造错了就是"任意网页都能读令牌"）。
 * * **令牌永不出现在响应里**：只在本文件内部拼上游 URL 用。
 * * 所有**短**上游请求带超时（探测 2s / frame 3s / 默认 5s）：上游卡住不能把宿主的事件循环拖死。
 *   **长连接（`/stream`）是例外**：只在"上游还没出第一个字节"时有超时，出了数据之后再也不能砍 ——
 *   静止画面按契约几乎不发帧（§5.1 目标 ≤5KB/s），拿超时去砍它等于把省电设计砍掉。
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

/**
 * **行 id**（= `cordis.patch.yml` 里那一行的 `id`）。**不是包名。**
 *
 * ⚠️ 这两个域**必须分清**，混用会让设置卡片彻底不可用（真踩过）：
 *   * `dsh-settings` 的 `describe()`／`update()` 与客户端的 `configForms.get()`
 *     都只认 **profile entry id**（`entries().find(row => row.options.id === ns)`）；
 *   * 而卡片槽位 `plugins.bundle.config` 的 key 是**包名**。
 *
 * 本插件的行 id 是 `display-panel`（见 `cordis.patch.yml` 的 `- id:`），
 * 包名是 `dsh-display-panel` —— **两个字符串都要用，用在哪一处不能猜**：
 * 混用的后果不是报错，而是"卡片显示出来、但里面没有字段、保存还提示已保存"
 * （0.1.7 的 `get()` 找不到 entry 就返回一个 status:'unavailable' 的空表单）。
 *
 * 0.1.5 / 0.1.6 的 `settings.register(ns, ...)` 用同一个字符串当 namespace
 * （取值只需满足 `^[a-z][a-z0-9-]*$`，`display-panel` 合规；这一代是全新功能，
 * 没有历史 namespace 要兼容）。
 */
const SETTINGS_NAMESPACE = 'display-panel'

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

/**
 * 流式代理的**首字节**等待上限（毫秒）。
 *
 * 只覆盖"上游一个字节都还没出"的阶段：会话可能是现拉的（首次要起 Xvfb，一两秒很正常），
 * 所以比常规请求宽。**一旦开始出数据就再也没有超时** —— 静止画面按契约几乎不发帧
 * （§5.1 目标 ≤5KB/s），拿超时去砍它等于把省电设计砍掉。
 */
const TIMEOUT_STREAM_FIRST_BYTE_MS = 8000

/** 上游没有在流时，最多收多少字节的错误体来做诊断（上游错误体是小的 JSON/HTML）。 */
const MAX_UPSTREAM_ERROR_BYTES = 16 * 1024

/**
 * `/stream` 上游回这些码时，我们**换一条全新连接重试一次**。
 *
 * 为什么 404 也要重试：这类码有两种来源 ——
 *  ① 上游**真的**没有 `/stream`（旧版服务）→ 正确答复就是 404/501；
 *  ② keep-alive 连接被**上一个 POST 的残留 body** 污染了（旧版服务对 `POST /stream-config`
 *     回 404 时**不读请求体**，body 留在 socket 里；下一条复用它的请求，"请求行"就变成
 *     那段 JSON，上游于是回 400/501，方法名就是 body 内容 —— 实测复现过）。
 * 两者从状态码上分不开，代价却差得远：误判成①会让客户端**静默退回轮询**（画面变卡，
 * 用户以为"这版没优化"）。所以宁可多打一次本地回环请求，也要把②排除掉。
 */
const STREAM_RETRY_STATUSES = new Set([400, 404, 405, 501])
const MAX_STREAM_ATTEMPTS = 2

/**
 * 上游回了这些码 ⇒ "这条连接上的请求可能没被正常处理"（尤其在我们发过 body 时）。
 * 见 upstream() 里"旧版服务 404 不读 body"那段：这种连接**绝不能回连接池**。
 */
const SUSPECT_UPSTREAM_STATUSES = new Set([400, 404, 405, 501])

/**
 * `/stream-config` 的参数范围（契约 §5.2）。
 *
 * 只做"是数字 + 在范围内"的校验，**不额外要求整数**：上游自己会 floor/取整，
 * 宿主多判一层只会把本来能用的请求挡在门外（少一层自己的规矩，就少一处三方不一致）。
 */
const STREAM_CONFIG_FIELDS = {
  quality: { min: 1, max: 100 },
  fps: { min: 1, max: 30 },
  scale: { min: 0.25, max: 1 },
}

/** 参数不合法时的统一提示（范围写在响应里，客户端不必翻文档）。 */
const STREAM_CONFIG_HINT = 'quality 1..100、fps 1..30、scale 0.25..1.0，至少给一个'
  + '（例如 {"quality":70,"fps":15,"scale":1}）'

/** 上游没有 `/stream`（旧版服务）时给客户端的提示：这是一条**正常**的降级路径，不是错误。 */
const STREAM_LEGACY_HINT = '上游显示器服务不支持 /stream（旧版）：客户端应退回轮询 GET '
  + `${BASE_PATH}/frame（契约 §5.3 兼容路径），或把显示器服务升级到 0.4.0+`

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

// ---------------------------------------------------------------------------
// 设置卡片（0.1.7+ 的 Config / 更早的 settings.register）
// ---------------------------------------------------------------------------

/**
 * 从 DSH 自己的安装目录解析 `@deepseek-ai/schemastery`（同步，一次）。
 *
 * 为什么不能 `require('@deepseek-ai/schemastery')`：本插件装在
 * `$DSH_HOME/profiles/<p> /node_modules` 里，而 schemastery 是 **DSH 包的依赖**，
 * 不在 profile 的 node_modules 链上（实测 `import` / `require.resolve` 双双
 * `MODULE_NOT_FOUND`）。它是 `dsh-settings` 判定"哪些字段可实时编辑"用的
 * （`volatileForm()` 要拿 `schema.dict`／`meta.volatile`／`toJSON()`），
 * 所以设置卡片**必须**拿到真身，不能自己造一个长得像的对象。
 *
 * 定位靠"锚点 + 逐级向上找 node_modules"：
 *  ① `require.main.filename` —— DSH 宿主进程里就是 dsh 的入口脚本；
 *  ② `process.argv[1]` —— 同上的兜底；
 *  ③ `DSH_INSTALL_ANCHOR` —— 显式覆盖（测试或非标准安装）。
 * 符号链接要先 `realpathSync`：`~/.npm-global/bin/dsh` 是指向
 * `~/.npm-global/lib/node_modules/@deepseek-ai/dsh/lib/bin.js` 的软链，
 * 不解析就永远找不到旁边的 node_modules（实测踩过）。
 *
 * @returns {object|null} schemastery 的默认导出，或 null（拿不到就别声明 Config）。
 */
function loadSchemastery() {
  const anchors = []
  const push = (value) => {
    if (typeof value === 'string' && value.length > 0 && !anchors.includes(value)) anchors.push(value)
  }
  try {
    if (require.main && require.main.filename) push(require.main.filename)
  } catch {
    /* 某些加载形态下没有 require.main */
  }
  // ⚠️ 只接受**绝对路径**的 argv[1]：进程从别处启动时 argv[1] 可能是相对的，
  // path.dirname 会给出 '.'，于是"当前目录下的 node_modules"进了搜索链 ——
  // 万一那里有一份别的 schemastery，就会加载到与宿主不同的实例（volatile 支持
  // 可能不一致）。宁可退回"没有 Config"，也不猜。
  const argv1 = process.argv && process.argv[1]
  if (typeof argv1 === 'string' && path.isAbsolute(argv1)) push(argv1)
  push(env('DSH_INSTALL_ANCHOR'))
  for (const anchor of anchors) {
    let dir
    try {
      dir = path.dirname(fs.realpathSync(anchor))
    } catch {
      dir = path.dirname(anchor)
    }
    for (let depth = 0; depth < 12; depth += 1) {
      const candidate = path.join(dir, 'node_modules', '@deepseek-ai', 'schemastery', 'lib', 'index.cjs')
      try {
        if (fs.existsSync(candidate)) {
          const loaded = require(candidate)
          const schema = loaded && (loaded.default || loaded)
          if (schema && typeof schema.object === 'function') return schema
        }
      } catch (error) {
        log('warn', `找到 schemastery 但加载失败（${candidate}）：${String((error && error.message) || error)}`)
        break
      }
      const up = path.dirname(dir)
      if (up === dir) break
      dir = up
    }
  }
  return null
}

const Schema = loadSchemastery()

/**
 * 标一个字段为"可实时编辑"。
 *
 * `.volatile()` 是 dsh 0.1.7 起 schemastery 才有的扩展：0.1.5/0.1.6 的
 * schemastery 里**完全没有这个方法**，直接调用会抛 `TypeError`，而这个
 * `Config` 是在模块加载期构造的 —— 抛出去就是"这一行整条起不来"
 * （0.1.5 的 loader 还会连带回滚整个 group）。所以探测着调，没有就当普通字段。
 *
 * @param {object} node - schemastery 构造出的一段 schema。
 * @returns {object} 同一个节点（支持时已标 volatile）。
 */
function live(node) {
  return node && typeof node.volatile === 'function' ? node.volatile() : node
}

/**
 * 分辨率写法：`1600x1000` / `1600X1000` / `1280*720` 都收，也允许只给宽。
 *
 * 用正则而不是 `z.string()` 裸放：GUI 里填错了要在**保存时**就报错，
 * 而不是等宿主把 `abc` 当尺寸传给服务、服务启动失败才发现。
 */
const SIZE_PATTERN = /^\d{2,5}\s*[xX*]\s*\d{2,5}$|^\d{3,5}$/

/**
 * 三项设置的默认值。
 *
 * 与服务端的默认值**必须一致**（服务是独立进程，可能被用户单独安装/常驻，
 * 那边的默认值在 `service/dsh-display-viewer.py` 里）；改这里就得改那里。
 *
 * ⚠️ 这三个默认值**不能写进 schema 的 `.default()`**（真踩过）：写了之后
 * `resolveConfig` 会把它们填进 `fiber.config`，于是 `configField()` **永远**
 * 拿不到 `undefined` —— `resolveDisplayConfig()` 里那段"回落环境变量"就成了
 * 死代码，`DSH_VIEW_IDLE_MINUTES` / `DSH_VIEW_INPUT` 这类既有用法**静默失效**。
 * 所以字段一律 `required(false)`，默认值只在这里、在运行时兜底。
 */
const DEFAULT_SIZE = '1600x1000'
const DEFAULT_IDLE_MINUTES = 30
const DEFAULT_INPUT = false

/**
 * 本行的设置字段（三代的**同一个契约**）。
 *
 * 字段名与 `cordis.patch.yml`／`settings.yaml` 里的键一一对应，所以同一份
 * 用户值在"新版 Config"与"旧版 settings namespace"两条路上都能读出来。
 *
 * 全是 `required(false)`：**没设过 = undefined**，宿主据此回落到环境变量 ——
 * 这样 `DSH_VIEW_SIZE=...` 这类既有用法（脚本、systemd 单元里写的）继续有效，
 * 而 GUI 里改过的值优先。两者都不会把对方吃掉。
 *
 * 取不到 schemastery 时整体是 undefined：cordis 的 `resolveConfig` 在
 * `runtime.Config` 缺席时**原样放行**（`vendor/cordis/src/fiber.ts`），
 * 于是插件照常工作，只是没有表单 —— 这是刻意选的降级方向。
 */
const Config = Schema === null ? undefined : buildSettingsSchema()

/**
 * 构造设置字段的 schema（新版 `Config` 与旧版 namespace **共用同一份形状**）。
 *
 * 0.1.5/0.1.6 的 schemastery 没有 `.volatile()`：:func:`live` 探测着调，
 * 那两代退化成普通字段（值照样存、照样读，只是不参与"实时 HMR"那一套）。
 *
 * @returns {object} schemastery 的 object schema。
 */
function buildSettingsSchema() {
  return Schema.object({
    /** 每会话显示分辨率，例如 `1600x1000`（对应服务端的 `DSH_VIEW_SIZE`）。 */
    size: live(Schema.string().pattern(SIZE_PATTERN).required(false)),
    /** 空闲多少分钟回收会话与 Xvfb；`0` = 不回收（对应 `DSH_VIEW_IDLE_MINUTES`）。 */
    idleMinutes: live(Schema.number().min(0).max(10080).step(1).required(false)),
    /** win32/darwin 上是否允许把事件注进**真实**键鼠（对应 `DSH_VIEW_INPUT`）。 */
    inputEnabled: live(Schema.boolean().required(false)),
  })
}

/**
 * 读一个 Config 字段在**两代宿主**下的当前值。
 *
 * dsh 0.1.7 把 volatile 字段交给 `apply()` 时是 `Volatile<T>` 引用（`.get()`），
 * 更早的宿主（以及刚声明还没被编辑过的字段）给的是普通值。两种都要认。
 *
 * @param {unknown} value - `config` 上的一个字段。
 * @returns {unknown} 当前的普通值。
 */
function valueOf(value) {
  return value && typeof value.get === 'function' ? value.get() : value
}

/** 从 `config` 里取一个字段（`Config` 缺席时 `config` 可能是任意东西）。 */
function configField(config, key) {
  if (!config || typeof config !== 'object') return undefined
  return valueOf(config[key])
}

/** 校验并归一化分辨率写法；不合法回 undefined（调用方继续回落）。 */
function normalizeSize(raw) {
  const text = String(raw === undefined || raw === null ? '' : raw).trim()
  if (!SIZE_PATTERN.test(text)) return undefined
  const parts = text.split(/\s*[xX*]\s*/)
  const width = Number.parseInt(parts[0], 10)
  const height = parts.length > 1 ? Number.parseInt(parts[1], 10) : undefined
  if (!Number.isInteger(width) || width < 64 || width > 20000) return undefined
  if (height !== undefined && (!Number.isInteger(height) || height < 64 || height > 20000)) return undefined
  return height === undefined ? `${width}x${width}` : `${width}x${height}`
}

/**
 * 三项"服务端配置"在宿主侧的**生效值**（GUI 设置优先，环境变量兜底）。
 *
 * 这三项都要让**显示器服务**知道，而服务是独立进程、只吃环境变量，所以宿主
 * 拉起服务时把它们塞进子进程环境（见 :func:`spawnService`）。服务端自己的默认值
 * 在这里被复制了一份 —— 只有一份真相是不可能的（服务可能被独立安装/常驻），
 * 但两边的默认值必须一致，改一处就得改另一处。
 *
 * @param {object} config - `apply()` 收到的 Config。
 * @returns {{size: string, idleMinutes: number, input: boolean}} 三项都一定有权值。
 */
function resolveDisplayConfig(config) {
  const out = {}
  out.size = normalizeSize(configField(config, 'size'))
    || normalizeSize(env('DSH_VIEW_SIZE')) || DEFAULT_SIZE
  const idleRaw = configField(config, 'idleMinutes')
  const envIdle = Number.parseInt(String(env('DSH_VIEW_IDLE_MINUTES') ?? ''), 10)
  const idle = idleRaw === undefined || idleRaw === null || idleRaw === ''
    ? envIdle
    : Number(idleRaw)
  out.idleMinutes = Number.isFinite(idle) && idle >= 0 ? Math.floor(idle) : DEFAULT_IDLE_MINUTES
  const inputRaw = configField(config, 'inputEnabled')
  if (typeof inputRaw === 'boolean') out.input = inputRaw
  else if (env('DSH_VIEW_INPUT') !== undefined) {
    out.input = ['1', 'true', 'yes', 'on'].includes(String(env('DSH_VIEW_INPUT')).toLowerCase())
  } else out.input = DEFAULT_INPUT
  return out
}

/**
 * 当前生效的服务端配置（`spawnService` 每次拉起前刷新一次）。
 *
 * 为什么用"可变模块级变量"而不是把 config 一路传下去：拉起服务的路径有三条
 * （自动拉起 / `/service?action=start` / 配置改动后重启），每条都在不同的
 * closure 里，沿途透传参数要改十几个函数签名；而这份值本来就是"进程级的一小块状态"。
 */
let DISPLAY_CONFIG = null

/**
 * 宿主设置服务（两代各一个引用），供 `/config` 路由把面板上的动作写回配置。
 *
 * dsh ≥ 0.1.7：`ctx.settings` 是 SettingsForms，`update(ns, patch)` 按**行 id** 合并；
 * dsh ≤ 0.1.6：`settings.register()` 返回的 scope，`update(patch)` 按 **namespace** 合并。
 * 两者都是**可选的**：拿不到就没有 `/config`，客户端会如实说"这个宿主版本不支持"
 * —— 而不是假装写完（那会让用户以为注入开了，其实没开）。
 */
let SETTINGS_SERVICE = null
let LEGACY_SETTINGS_SCOPE = null

/** 由 `apply()` 写入最新配置（GUI 改完立刻反映到下一次拉起/重启）。 */
function setDisplayConfig(config) {
  DISPLAY_CONFIG = resolveDisplayConfig(config)
  return DISPLAY_CONFIG
}

/** 当前的生效值（没被 `apply()` 初始化过就现算一次）。 */
function displayConfig() {
  if (DISPLAY_CONFIG === null) DISPLAY_CONFIG = resolveDisplayConfig(undefined)
  return DISPLAY_CONFIG
}

/**
 * 把生效值折成服务端认的环境变量。
 *
 * 只输出**明确生效**的项：没设置的项不写进环境，服务端就会用自己的默认值
 * （而不是被宿主用一个"猜的默认值"钉死）。
 *
 * @param {object} resolved - :func:`resolveDisplayConfig` 的结果。
 * @returns {Record<string,string>} 要合并进子进程环境的键值。
 */
function displayConfigEnv(resolved) {
  const out = {}
  if (!resolved || typeof resolved !== 'object') return out
  if (resolved.size) out.DSH_VIEW_SIZE = resolved.size
  if (resolved.idleMinutes !== undefined) out.DSH_VIEW_IDLE_MINUTES = String(resolved.idleMinutes)
  if (resolved.input !== undefined) out.DSH_VIEW_INPUT = resolved.input ? '1' : '0'
  return out
}

/**
 * 把一组设置值写回宿主配置（两代各一条路）。
 *
 * 写的是**配置本身**（不是内存里的临时值）：dsh ≥ 0.1.7 落到
 * `<profileDir>/cordis.patch.yml` 那一行的 config 里，dsh ≤ 0.1.6 落到
 * `settings.yaml` 的 namespace 段里 —— 都**持久**，重启 DSH 还在。
 *
 * ⚠️ 这里的写入对用户是"改了你的配置文件"，所以：
 * ① 只接受 caller 已经逐个校验过的三个键（`/config` 路由负责校验）；
 * ② 拿不到设置面时**如实回 false**，绝不在内存里假装改成功 ——
 *    那会让用户以为注入开了，而面板点下去仍然只读。
 *
 * 返回 true 只表示"已提交给宿主设置面"：真正的落盘是异步的，失败只能记日志
 * （宿主设置面的写入没有同步 API）。
 *
 * @param {object} patch - 已校验的字段（size / idleMinutes / inputEnabled）。
 * @returns {boolean} 是否提交成功（未提交时调用方回 501）。
 */
async function applySettingsPatch(patch) {
  if (SETTINGS_SERVICE && typeof SETTINGS_SERVICE.update === 'function') {
    try {
      await SETTINGS_SERVICE.update(SETTINGS_NAMESPACE, patch)
      log('info', `设置已写入行配置 ${SETTINGS_NAMESPACE}：${JSON.stringify(patch)}`)
      return true
    } catch (error) {
      log('warn', `写行配置失败（行 id=${SETTINGS_NAMESPACE}）：${String((error && error.message) || error)}`)
      return false
    }
  }
  // 兜底：0.1.5/0.1.6 的 `ctx.settings` 同样有 `update(ns, patch)`，所以正常情况下
  // 上面第一个分支就覆盖了那两代 —— 这一条留给"settings 服务捕获失败、但 scope
  // 拿到了"的极端情形（scope 自己的 `update(patch)` 只吃一个参数）。
  if (LEGACY_SETTINGS_SCOPE && typeof LEGACY_SETTINGS_SCOPE.update === 'function') {
    try {
      await LEGACY_SETTINGS_SCOPE.update(patch)
      log('info', `设置已写入 namespace ${SETTINGS_NAMESPACE}：${JSON.stringify(patch)}`)
      return true
    } catch (error) {
      log('warn', `写 namespace 失败：${String((error && error.message) || error)}`)
      return false
    }
  }
  return false
}

/**
 * 写完之后从宿主读回**真正的当前值**（`describe()` 是宿主自己解析出来的结果）。
 *
 * 为什么不直接回 `DISPLAY_CONFIG`：那份缓存在 `apply()` 与 `loader/volatile-update`
 * 时才刷新，写完立刻读会拿到**写之前**的旧值 —— 客户端拿到一个"没变化"的响应，
 * 很难判断到底写进去没有。读不到（老版本、或这一代没有 describe）就回 null，
 * 由调用方决定怎么表达"不确定"。
 *
 * @returns {{size?: string, idleMinutes?: number, inputEnabled?: boolean}|null} 宿主侧现值。
 */
function readSettingsBack() {
  try {
    if (!SETTINGS_SERVICE || typeof SETTINGS_SERVICE.describe !== 'function') return null
    const rows = SETTINGS_SERVICE.describe()
    if (!Array.isArray(rows)) return null
    const row = rows.find((item) => item && item.ns === SETTINGS_NAMESPACE)
    if (!row || !row.value || typeof row.value !== 'object') return null
    const out = {}
    for (const key of ['size', 'idleMinutes', 'inputEnabled']) {
      if (row.value[key] !== undefined) out[key] = row.value[key]
    }
    return out
  } catch (error) {
    log('warn', `读回设置失败：${String((error && error.message) || error)}`)
    return null
  }
}

/**
 * 在 **0.1.5 / 0.1.6** 上注册这项设置的 namespace（0.1.7+ 什么都不做）。
 *
 * 三代的差别（已按 DSH 源码逐 tag 核对）：
 *   * 0.1.5-alpha.1 ~ 0.1.6-alpha.1：设置面是客户端服务 `settingsScope` + 卡片槽位
 *     `settings.plugin.item`；
 *   * 0.1.6-alpha.2：宿主/客户端设置面**逐字节没变**，只有卡片槽位换成了
 *     `plugins.bundle.config` 一族；
 *   * 0.1.7-alpha.1 起：`ctx.settings.register` **被删**，改成导出 `Config`。
 *
 * ⚠️ 最大的坑：`settings` 这个**服务名在三代里都存在**，但 0.1.7 上它的语义
 * 已经换成 SettingsForms，`register` 不在了。所以 `ctx.inject(['settings'], cb)`
 * 在 0.1.7 上**照样会触发**，进到回调里才发现没有 register。<br>
 * 因此这里必须做**方法级探测**，而不是靠服务名判断版本。
 *
 * @param {object} settings - `ctx.settings`（调用方已经从 `ctx.inject` 里拿到）。
 * @param {object} config - `apply()` 收到的 Config（`scope.get()` 拿不到值时的兜底）。
 */
function registerLegacySettings(settings, config) {
  if (Schema === null || !settings) return
  try {
    if (typeof settings.register !== 'function') {
      // 0.1.7+：register 已被行 Config 取代 —— 这是**正常路径**，不是错误。
      log('info', 'settings.register 不存在（0.1.7+ 用行 Config 提供设置面）')
      return
    }
    let schema
    try {
      schema = buildSettingsSchema()
    } catch (error) {
      log('warn', `旧版设置 schema 构造失败：${String((error && error.message) || error)}`)
      return
    }
    // ⚠️ 第三个参数（options）只有 `base` / `applies` / `validate` 三项
    // （0.1.5-alpha.1 与 0.1.6-alpha.2 逐字节相同），**没有** name/description ——
    // 早先传了这两个键，虽然会被忽略（无害），但注释在说谎，所以干脆不传。
    // 表单标题由 GUI 那边用行 id / namespace 显示。
    const scope = settings.register(SETTINGS_NAMESPACE, schema)
    LEGACY_SETTINGS_SCOPE = scope || null
    const syncFromScope = () => {
      try {
        const values = scope && typeof scope.get === 'function' ? scope.get() : undefined
        setDisplayConfig(values === undefined ? config : values)
      } catch (error) {
        log('warn', `读取旧版设置失败：${String((error && error.message) || error)}`)
      }
    }
    syncFromScope()
    if (scope && typeof scope.watch === 'function') {
      try {
        scope.watch(syncFromScope)
      } catch (error) {
        log('warn', `监听旧版设置失败：${String((error && error.message) || error)}`)
      }
    }
    log('info', `已注册旧版设置 namespace「${SETTINGS_NAMESPACE}」（dsh ≤ 0.1.6 的设置卡片用）`)
  } catch (error) {
    log('warn', `旧版设置注册失败（不影响面板）：${String((error && error.message) || error)}`)
  }
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
  /** 活跃的 MJPEG 流（`{session,port,destroy()}`；卸载时统一断掉上游）。 */
  streams: new Set(),
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

/**
 * 把一批路径**还回**登记表（effect 卸载时调用）。
 *
 * ⚠️ 为什么必须有：`claim()` 是"注册过就别再注册"的登记表（因为 `webServer.register`
 * 对同一 (kind, path) 会抛 "duplicate exact route"）。但 effect 卸载时如果不把路径还回去，
 * 那么 effect 一旦**重跑**（live patchReload、宿主重载插件），claim() 会认为"早就注册过"
 * 而一个都不注册 —— 上一轮的 disposer 已经把路由卸掉了，于是面板没有任何接口可用，
 * 只能重启 DSH 才能恢复（perf-host 在实测 patchReload 时发现的隐患）。
 *
 * 还回去之后：重跑 = 重新注册，语义正确；同一时刻也不会重复注册。
 */
function release(webServer, keys) {
  const set = SHARED.registry.get(webServer)
  if (!set) return
  for (const key of keys) set.delete(key)
  if (set.size === 0) SHARED.registry.delete(webServer)
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
 * 拼上游查询串：额外参数在前，**令牌永远由宿主最后拼上**（§1：服务监听 127.0.0.1，
 * 同机其它用户也能连，必须带 `k`）。`upstream()` 与流式代理共用，免得两处拼法跑偏。
 */
function upstreamSearch(query, token) {
  const params = []
  if (query) params.push(query)
  if (token) params.push(`k=${encodeURIComponent(token)}`)
  return params.length > 0 ? `?${params.join('&')}` : ''
}

/**
 * 一次上游请求。
 *
 * 用 `http.request`（而不是 fetch）是为了**零依赖 + 完全控制**：超时用
 * `AbortController`，响应体自己收成 Buffer（帧最大也就几百 KB）。
 * 任何失败都以 `{ok:false}` 返回，**不抛** —— 调用方只需要看 `ok`。
 *
 * ⚠️ 这是**短请求** helper：它把整个 body 收完才 resolve。**MJPEG 长连接不能走这里**
 * （见 proxyStream()），否则在客户端看来就是"永远没有响应"。
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
 * @param {boolean} [options.freshConnection] - 用**独立连接**（`agent:false`，不进 keep-alive
 *   池、也不从池里取）。凡是"上游可能不认识这个路径/方法"的请求都该带上：旧版服务对
 *   未知 POST 回 404 时**不读请求体**，那条连接就带着残留字节，谁复用谁遭殃。
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
    freshConnection = false,
  } = options
  const token = options.token !== undefined ? options.token : readToken()
  const search = upstreamSearch(query, token)
  const sentBody = Boolean(body && body.length > 0)

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
          ...(freshConnection ? { agent: false } : {}),
          headers: {
            accept: 'application/json, image/jpeg, */*',
            ...(sentBody
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
            /**
             * ⚠️ **旧版服务 404 不读 body ⇒ 这条 keep-alive 连接已经被污染**：
             * 我们发出去的请求体还留在对端 socket 的接收缓冲里，下一条复用这条连接的请求，
             * 请求行会变成那段 JSON —— 上游回 400/501（501 的"方法名"就是 body 内容），
             * 或者干脆路径乱掉。实测：不处理的话，紧接着的 `GET /frame` 会拿到 501，
             * 面板直接黑掉。
             *
             * 所以："我们发过 body" + "上游回的可疑码" ⇒ **这条连接不许回池子**。
             * `shouldKeepAlive=false` 让 Node 在收尾时自己 destroy（正常时机），
             * 再补一发 `destroy()` 兜底（已经回池的也照样清掉）。
             */
            if (sentBody && SUSPECT_UPSTREAM_STATUSES.has(res.statusCode)) {
              try {
                req.shouldKeepAlive = false
                req.destroy()
              } catch {
                /* 连接已经不在了 */
              }
            }
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

/**
 * 流式透传：把上游 `/s/<sid>/stream` 的 MJPEG **逐 chunk** 转给浏览器（契约 §5.3）。
 *
 * 为什么不能走 upstream()：那个 helper 是"收完整个 body 再 resolve"的语义。拿它代理
 * MJPEG 长连接等于**永远不返回** —— 上游不结束它就不结束，客户端连第一帧都拿不到；
 * 就算上游偶尔结束，也会把整段流攒进内存。
 *
 * 这里每一条都对应一种踩过的坏法：
 *
 * * `pipe()` 而不是自己听 `data` 攒帧：管道自带**背压** —— 浏览器读得慢就压住上游
 *   （暂停 socket），内存不会涨；攒帧则是"越卡越吃内存"。
 * * **不设 `Content-Length`**（流本来没有长度），让 Node 走 chunked。
 * * `flushHeaders()`：`fetch()` 在**收到响应头**时就 resolve。不先冲头的话客户端要等到
 *   上游出第一帧才拿到 response —— 首帧延迟白加一个抓帧周期，而且"上游不支持流"这个
 *   结论也要拖到那时才得出（客户端就得多等一个超时才能退回轮询）。
 * * 客户端一断（`res` 的 close 且响应没写完）**立刻 `destroy()` 上游请求**：上游是
 *   Python 的 `ThreadingHTTPServer`，每会话一个线程往 socket 写；宿主不断开，那个线程
 *   会一直对着空气写（旧版实现里就是这个坑，要等会话超时才收）。
 * * **上游非 200 不当 500**：旧版服务没有 `/stream`（404/405/501），必须如实回 404/501
 *   + hint，客户端才知道要退回轮询 `/frame`（不能让它以为"宿主坏了"）。
 * * **上游回 400/404/405/501 时换新连接重试一次**（见 STREAM_RETRY_STATUSES）：
 *   旧版服务 404 不读 body 会把 keep-alive 连接污染成 400/501，不重试就会把"连接被污染"
 *   误判成"上游不支持流"，客户端于是静默退回轮询。
 * * 上游超时/断流一定打一行日志（含转发量与耗时），别静默。
 * * `agent: false`：给这条流一条**独占** socket（随请求一起销毁）。默认的 keep-alive
 *   池会把连接留着复用，客户端断开后 `ss` 里还能看到残留的 ESTABLISHED。
 *
 * ⚠️ **不预判"旧版就一定没有 /stream"**：控制台仓库那份 viewer 也是旧版（没有 `/health`），
 * 但它**有** `/stream`。预判会让本来能流的连接被降级成轮询 —— 直接试，按上游的真实答复决定。
 *
 * @param {object} res - 浏览器的原生响应（流写在这里；客户端断开也由它的 `close` 感知）。
 * @param {object} spec
 * @param {number} spec.port - 上游端口。
 * @param {string} spec.session - **已校验**的会话 id（调用方负责校验）。
 */
function proxyStream(res, spec) {
  const { port, session } = spec
  const startedAt = Date.now()
  const label = `stream ${session}`
  const entry = { session, port, startedAt, destroy: null }
  SHARED.streams.add(entry)

  let upReq = null
  let done = false
  let clientGone = false
  let chunks = 0
  let bytes = 0
  let firstByteMs = null
  let firstByteTimer = null
  /** 已经开过几条上游连接（1 = 首次；2 = 换新连接重试那一次）。 */
  let attempts = 0

  /** 收场：清定时器 + 从活跃表摘掉。**幂等**（多处可能同时触发）。 */
  const settle = () => {
    if (done) return
    done = true
    if (firstByteTimer !== null) {
      clearTimeout(firstByteTimer)
      firstByteTimer = null
    }
    SHARED.streams.delete(entry)
  }

  /** 断开上游。不抛。 */
  const killUpstream = () => {
    try {
      if (upReq && !upReq.destroyed) upReq.destroy()
    } catch {
      /* 已经断了 */
    }
  }

  /** 插件卸载时由 SHARED.streams 调用：把这一路彻底收掉。 */
  entry.destroy = () => {
    clientGone = true
    killUpstream()
    try {
      if (!res.writableEnded) res.end()
    } catch {
      /* 已经断了 */
    }
    settle()
  }

  /** 诊断用摘要：转发量 + 耗时 + 首字节延迟（和 /stats 对不上时一眼能看出卡在哪）。 */
  const summary = () => `${chunks} chunk/${bytes}B，${Date.now() - startedAt}ms`
    + (firstByteMs === null ? '，从未出数据' : `，首字节 ${firstByteMs}ms`)

  /**
   * 客户端断开：**从第一毫秒就听着**。
   *
   * 必须在建连之前就挂上：客户端在"上游还没回头"的阶段断线（关标签页、切走）是最常见的
   * 断法之一。如果只在"上游响应回调"里挂 close，这条上游连接就要一直挂到首字节超时
   * （8 秒）才被收掉 —— 恰好是"把服务拖死"的那类悬挂连接。
   */
  res.on('close', () => {
    if (done) return
    if (res.writableFinished) {
      settle()
      return
    }
    clientGone = true
    log(
      'info',
      `[${label}] 客户端断开（${chunks > 0 ? `已转发 ${summary()}` : '上游还没回头'}）`
      + `—— 立刻断开上游 127.0.0.1:${port}`,
    )
    killUpstream()
    settle()
  })
  res.on('error', () => {
    clientGone = true
    killUpstream()
    settle()
  })

  /**
   * 上游没有在流（非 200 / 不是 multipart）：把它的（小）错误体包成统一的 JSON 错误形状。
   * 404/405/501 一律映射成 **404/501 + STREAM_LEGACY_HINT**，这是"客户端该退回轮询"的信号。
   */
  const replyNotStreaming = (status, contentType, body, why) => {
    const fallback = status === 501 ? 501 : 404
    const legacyish = status === 404 || status === 405 || status === 501
    if (res.headersSent) {
      try {
        res.end()
      } catch {
        /* 客户端已经跑了 */
      }
    } else if (legacyish) {
      fail(res, fallback, `upstream has no /stream (HTTP ${status}) for session ${session}`, STREAM_LEGACY_HINT)
    } else if (status === 200) {
      // 200 但不是 multipart：上游把别的页面当 /stream 回了（老路由器会这样）。
      fail(
        res,
        502,
        `upstream /stream did not return MJPEG (content-type: ${contentType || 'none'})`,
        STREAM_LEGACY_HINT,
      )
    } else {
      fail(
        res,
        status || 502,
        `upstream returned ${status} for /stream (session ${session})`,
        `${summarizeUpstream({ status, body })}${why ? `；${why}` : ''}`,
      )
    }
    log(
      'info',
      `[${label}] 上游没有出流：HTTP ${status}${contentType ? ` ${contentType}` : ''}${why ? `（${why}）` : ''}`
      + ` —— 已回 ${legacyish ? fallback : status || 502} 给客户端`,
    )
    settle()
  }

  // 首字节超时：**只覆盖"还没开始出数据"的阶段**（见函数头注释）。
  firstByteTimer = setTimeout(() => {
    if (done || chunks > 0) return
    log(
      'warn',
      `[${label}] 上游 127.0.0.1:${port} 在 ${TIMEOUT_STREAM_FIRST_BYTE_MS}ms 内没有出任何数据`
      + `（会话刚建/Xvfb 起不来/抓帧失败？）—— 断开上游，让客户端重连`,
    )
    killUpstream()
    if (res.headersSent) {
      try {
        res.end()
      } catch {
        /* 已经断了 */
      }
    } else {
      fail(res, 504, `upstream produced no data within ${TIMEOUT_STREAM_FIRST_BYTE_MS}ms`, `日志：${VIEWER_LOG}`)
    }
    settle()
  }, TIMEOUT_STREAM_FIRST_BYTE_MS)
  if (typeof firstByteTimer.unref === 'function') firstByteTimer.unref()

  /**
   * 开一条到上游 `/stream` 的连接。**可能被调用两次**（第二次是"换新连接重试"，
   * 见下面 reply() 里那段）：每次都是 `agent:false` 的全新连接，不复用任何池化 socket。
   */
  const openUpstream = () => {
    attempts += 1
    try {
      upReq = http.request(
        {
          host: '127.0.0.1',
          port,
          path: `/s/${session}/stream${upstreamSearch(undefined, readToken())}`,
          method: 'GET',
          // 独占 socket：客户端一断，destroy() 就是真的把连接关掉（见函数头注释）。
          // 顺带把"复用一条被污染的 keep-alive 连接"这条路彻底堵死（Node 会带 Connection: close）。
          agent: false,
          headers: {
            accept: 'multipart/x-mixed-replace, image/jpeg, */*',
            'cache-control': 'no-store',
            connection: 'close',
          },
        },
        (up) => {
          const status = up.statusCode || 0
          const contentType = String(up.headers['content-type'] || '')

          if (status !== 200 || !contentType.startsWith('multipart/')) {
            // 非流响应体很小：收下来（有上限）再回 JSON。上游不结束时也要有个了断。
            const parts = []
            let size = 0
            let replied = false
            const reply = (why) => {
              if (replied) return
              replied = true
              if (capTimer !== null) clearTimeout(capTimer)
              const body = Buffer.concat(parts)
              // 可疑答复（"这条路径/方法在此上游不成立"）且还没重试过：换一条**全新连接**再试一次。
              // 旧版服务 404 不读 body 会把 keep-alive 连接污染成 400/501，这条重试就是为它准备的。
              if (attempts < MAX_STREAM_ATTEMPTS && STREAM_RETRY_STATUSES.has(status)) {
                killUpstream()
                log(
                  'info',
                  `[${label}] 上游第 ${attempts} 次回 HTTP ${status}${contentType ? ` ${contentType}` : ''}`
                  + `${why ? `（${why}）` : ''} —— 可能是旧版服务 404 没读 body 污染了 keep-alive 连接，`
                  + '换一条全新连接重试一次（若仍不是流，才按"上游不支持 /stream"回 404/501）',
                )
                openUpstream()
                return
              }
              replyNotStreaming(status, contentType, body, why)
            }
            const capTimer = setTimeout(() => reply(`上游 ${TIMEOUT_PROBE_MS}ms 内没有结束错误响应`), TIMEOUT_PROBE_MS)
            if (typeof capTimer.unref === 'function') capTimer.unref()
            up.on('data', (chunk) => {
              if (size >= MAX_UPSTREAM_ERROR_BYTES) return
              parts.push(chunk)
              size += chunk.length
            })
            up.on('end', () => reply(null))
            up.on('aborted', () => reply('上游提前断开'))
            up.on('error', (error) => reply(`上游读取失败：${String((error && error.message) || error)}`))
            return
          }

          // ---- 200 + multipart：开始逐 chunk 透传（到此为止一个字都还没写给客户端）----
          up.on('data', (chunk) => {
            if (chunks === 0) {
              firstByteMs = Date.now() - startedAt
              if (firstByteTimer !== null) {
                clearTimeout(firstByteTimer)
                firstByteTimer = null
              }
            }
            chunks += 1
            bytes += chunk.length
          })
          up.on('error', (error) => {
            if (clientGone) return
            log('warn', `[${label}] 上游流中断：${String((error && error.message) || error)}（已转发 ${summary()}）`)
            try {
              res.end()
            } catch {
              /* 已经断了 */
            }
            settle()
          })
          up.on('aborted', () => {
            if (clientGone) return
            log('warn', `[${label}] 上游流被中止（已转发 ${summary()}）`)
          })
          up.on('end', () => {
            log('info', `[${label}] 上游结束（已转发 ${summary()}）`)
            settle()
          })

          // 客户端断开由 proxyStream 开头那个 close 处理器统一负责（从头到尾只有一处）。
          if (clientGone) return   // 上游回头的这一瞬间客户端已经走了：别再往 res 里写

          if (!res.headersSent) {
            res.writeHead(200, {
              // **原样**透传（含 `boundary=frame`）：客户端按同一个 boundary 解析 part。
              'content-type': contentType,
              'cache-control': 'no-store, no-cache, must-revalidate, max-age=0',
              pragma: 'no-cache',
              expires: '0',
              // 反向代理（nginx 等）默认会攒缓冲：攒了 MJPEG 就变成"一卡一卡"。
              'x-accel-buffering': 'no',
              // 上游说它这条连接不复用（EOF 结尾的流、或服务自己要收场）→ 我们也不暗示可以复用，
              // 免得下游把"已经断掉的上游"当成"还活着的 keep-alive"。
              ...(String(up.headers.connection || '').toLowerCase() === 'close'
                ? { connection: 'close' }
                : {}),
            })
            try {
              res.flushHeaders()
            } catch {
              /* 客户端已经跑了 */
            }
          }
          up.pipe(res)
        },
      )
    } catch (error) {
      log('warn', `[${label}] 无法连上游 127.0.0.1:${port}：${String((error && error.message) || error)}`)
      if (!res.headersSent) {
        fail(res, 503, `upstream request failed: ${String((error && error.message) || error)}`, `日志：${VIEWER_LOG}`)
      }
      settle()
      return
    }

    upReq.on('error', (error) => {
      if (done) return
      log('warn', `[${label}] 上游连接失败 127.0.0.1:${port}：${String((error && error.message) || error)}`)
      if (res.headersSent) {
        try {
          res.end()
        } catch {
          /* 已经断了 */
        }
      } else {
        fail(res, 503, `upstream unreachable: ${String((error && error.message) || error)}`, `日志：${VIEWER_LOG}`)
      }
      settle()
    })
    upReq.end()
    // 建连期间客户端就走了（close 处理器的 killUpstream 那时还是空操作）：别把这条上游留着。
    if (clientGone) killUpstream()
  }

  openUpstream()
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
        // 设置卡片的三项：GUI 里改过的值在这里覆盖掉继承来的环境变量，
        // 没设过的项不写（让服务端用自己的默认值）。
        ...displayConfigEnv(displayConfig()),
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
async function ensureRunning({ spawn: allowSpawn, force = false } = {}) {
  // ⚠️ force 也要透给 probe()：restart 的流程是「先停掉自己的服务，再确保有服务」，
  //    而 probe() 有 1.5 秒的状态缓存 —— 不强制刷新的话，刚被我们杀掉的服务还会以
  //    "running: true" 的姿态从缓存里回来，于是 restart 变成"停了但没起"
  //    （实测：/info 报着 8402/running，而 8402 根本没人听）。
  let status = await probe({ force })

  const canSpawn = allowSpawn === undefined ? MANAGED : allowSpawn === true && MANAGED

  // 只要看到**本插件的服务**（有 /health）在跑，就把"升级尝试过"的标记清掉：
  // 否则我们的服务一旦停掉（重启、崩溃、被手工 kill），宿主会因为标记还在而
  // 永远不再把它拉起来，静静回落到旧版服务上 —— POST /service {action:"restart"}
  // 就会变成"停掉新的、用回旧的"，非常反直觉（实测踩到）。
  if (status.running && status.legacy !== true) SHARED.upgradeAttempted = false

  // 只有旧版服务在跑（没有 /health、/exec、cursor）：能用但缺能力。
  // 允许自动拉起时，把**本插件的服务**也起起来（它会顺延到下一个空闲端口），
  // 然后重新探测 —— probe() 优先本插件的服务，于是 exec/光标/诊断都能用上。
  // force=true（restart 路径）时无视"只尝试一次"的标记：调用方明确要求重来一遍。
  if (status.running && status.legacy === true && canSpawn && (force || !SHARED.upgradeAttempted)) {
    SHARED.upgradeAttempted = true
    log('info', `检测到旧版显示器服务（127.0.0.1:${status.port}，无 /health）—— 尝试另外拉起本插件的服务以获得完整能力`)
    const spawned = spawnService()
    if (spawned.ok) {
      // ⚠️ 这里不能直接用 waitForService()：旧版服务仍在应答，probe() 会立刻返回"running"，
      //    于是升级判定会在本插件的服务起来之前就放弃。要明确等到**有 /health 的**那个。
      const deadline = Date.now() + SPAWN_WAIT_MS
      while (Date.now() < deadline) {
        const next = await probe({ force: true })
        if (next.running && next.legacy !== true) {
          SHARED.upgradeAttempted = false
          return next
        }
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
    {
      /**
       * 契约 §5.3：MJPEG 长连接（面板画面的主路径）。
       *
       * ⚠️ 这是**唯一**不能走 `passthrough()`/`upstream()` 的路由：那两个 helper 都要
       * "收完整个 body"，用在长连接上等于永远不返回。具体见 proxyStream()。
       */
      kind: 'exact',
      path: `${BASE_PATH}/stream`,
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
          // 从这里开始响应由 proxyStream 全权负责（它知道什么时候能回 JSON、什么时候已经在流里了）。
          proxyStream(res, { port: status.port, session })
        } catch (error) {
          fail(res, 500, String((error && error.message) || error))
        }
      },
    },
    // 契约 §5.5：观测接口，纯 JSON 透传（上游没有就如实回它的状态码，不是 500）。
    { kind: 'exact', path: `${BASE_PATH}/stats`, handler: passthrough({ suffix: 'stats', method: 'GET' }) },
    {
      /**
       * 契约 §5.2：`POST {quality,fps,scale}` → 上游 `/s/<sid>/stream-config`。
       *
       * 宿主这一层只做**范围校验**（非法值 400 + 把范围写进 hint）：上游拿到的永远是
       * 合法数字，于是"客户端一个小笔误"不会变成服务端的异常路径。
       */
      kind: 'exact',
      path: `${BASE_PATH}/stream-config`,
      handler: async (req, res) => {
        try {
          if (rejected(connection, req, res)) return
          if ((req.method || 'GET') !== 'POST') {
            fail(res, 405, 'method not allowed（POST {"quality":70,"fps":15,"scale":1}）')
            return
          }
          const rawSession = queryParam(req, 'session')
          const session = validSession(rawSession)
          if (session === null) {
            fail(res, 400, rawSession === undefined ? 'missing session parameter' : `invalid session parameter: ${String(rawSession).slice(0, 64)}`, SESSION_HINT)
            return
          }

          let body
          try {
            body = await readBody(req)
          } catch (error) {
            fail(res, error.status || 400, String((error && error.message) || error))
            return
          }
          const parsed = parseJson(body)
          if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
            fail(res, 400, 'invalid JSON body', `body 形如 {"quality":70,"fps":15,"scale":1}；${STREAM_CONFIG_HINT}`)
            return
          }

          // 只挑契约里的三个字段转发（未知字段直接丢掉：上游不认识它们，留着只会误导）。
          const config = {}
          for (const [key, range] of Object.entries(STREAM_CONFIG_FIELDS)) {
            if (!(key in parsed)) continue
            const raw = parsed[key]
            // 数字字符串也收（`curl -d '{"fps":"15"}'` 是很自然的写法），别的一律 NaN。
            const value = typeof raw === 'number'
              ? raw
              : typeof raw === 'string' && raw.trim() !== '' ? Number(raw) : Number.NaN
            if (!Number.isFinite(value)) {
              fail(res, 400, `${key} must be a number`, `${key} 的合法范围是 ${range.min}..${range.max}；${STREAM_CONFIG_HINT}`)
              return
            }
            if (value < range.min || value > range.max) {
              fail(res, 400, `${key} out of range: ${value}`, `${key} 的合法范围是 ${range.min}..${range.max}；${STREAM_CONFIG_HINT}`)
              return
            }
            config[key] = value
          }
          if (Object.keys(config).length === 0) {
            fail(res, 400, 'no known config field', STREAM_CONFIG_HINT)
            return
          }

          const status = await ensureService()
          if (!status.running) {
            fail(res, 503, 'display service is not running', status.note || `日志：${VIEWER_LOG}`)
            return
          }

          const payload = Buffer.from(JSON.stringify(config), 'utf8')
          const result = await upstream({
            port: status.port,
            pathname: `/s/${session}/stream-config`,
            method: 'POST',
            body: payload,
            contentType: 'application/json; charset=utf-8',
            // ⚠️ **必须独占连接**：旧版服务没有 /stream-config，它对未知 POST 回 404 时
            //    **不读请求体** —— 这条 keep-alive 连接就带着残留 body 回到池子里，
            //    下一条复用它的请求会被污染成 400/501（实测：紧接着的 GET /stream 会 501，
            //    客户端于是静默退回轮询）。用 agent:false 让这条连接活一次就结束。
            freshConnection: true,
          })
          if (!result.ok) {
            fail(res, 503, `upstream unreachable: ${result.error}`, `日志：${VIEWER_LOG}`)
            return
          }
          const answer = parseJson(result.body)
          if (answer && typeof answer === 'object' && !Array.isArray(answer)) {
            // ⚠️ 上游成功时回的是 `{ok:true,config:{quality,fps,scale}}`（**合并后的当前档**）。
            //    所以这里的字段名要避开 `config`：拿我们发过去的那三个值去覆盖它，会把
            //    "没改的那两个现在是什么"抹掉（客户端读到的 config 就不完整了）。
            //    只在旧版上游没给 config 时，才用我们发过去的值兜底。
            const extra = answer.config === undefined ? { config } : {}
            writeJson(res, result.status || 200, { ...answer, ...extra, service: serviceField(status) })
            return
          }
          writeJson(res, result.status || 502, {
            ok: false,
            error: 'unexpected upstream response',
            status: result.status,
            body: result.body.toString('utf8').slice(0, 2000),
            config,
            service: serviceField(status),
            hint: result.status === 404 || result.status === 501
              ? STREAM_LEGACY_HINT
              : `上游 ${status.port} 的 /stream-config 回了非 JSON；日志：${VIEWER_LOG}`,
          })
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
    // 关闭**这个会话的显示器**（回收 Xvfb + 释放显示号）。对应上游 §1.2 的 `POST /s/<sid>/close`：
    // 面板的「关闭显示器」按钮与 display_panel_close 工具都走它。
    // ⚠️ 用 POST 而不是上游等价的 DELETE：DELETE 在部分链路/客户端上会被吃掉 body 或触发预检差异，
    //    POST + 明确路径在浏览器与 curl 下行为一致。
    { kind: 'exact', path: `${BASE_PATH}/close`, handler: passthrough({ suffix: 'close', method: 'POST', forwardBody: true }) },
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
            // force：明确要求"重来一遍"。否则"机器上还跑着别的项目的旧版 viewer"这条
            // 会让 restart 变成"停掉我们的、用回旧的"（实测踩到）。
            status = await ensureService({ spawn: true, force: true })
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
      /**
       * 写设置（可选能力）：面板上的"打开注入"确认之后走这里。
       *
       * 为什么不让客户端自己写配置：宿主配置的写入口在**宿主进程**里
       * （`ctx.settings.update`），浏览器不该也不能直接碰；而且写完之后
       * "重启服务让新环境变量生效"这一步也在宿主侧最顺。
       *
       * 只认三个键、逐个校验：这是一条**写**路由，宁可回 400 也不要把
       * 来路不明的字段合并进用户的 profile 配置里。
       */
      kind: 'exact',
      path: `${BASE_PATH}/config`,
      handler: async (req, res) => {
        try {
          if (rejected(connection, req, res)) return
          if ((req.method || 'GET') !== 'POST') {
            fail(res, 405, 'method not allowed（POST {"size"?, "idleMinutes"?, "inputEnabled"?, "restart"?}）')
            return
          }
          let body
          try {
            body = await readBody(req)
          } catch (error) {
            // readBody 用 error.status 表达 413（体太大）—— 直接降级成 400 会把
            // "你发的太大"说成"你的 JSON 不对"，排查时完全是两个方向。
            fail(res, Number(error && error.status) || 400, String((error && error.message) || error))
            return
          }
          const input = parseJson(body)
          if (!input || typeof input !== 'object' || Array.isArray(input)) {
            fail(res, 400, 'body 必须是 JSON 对象')
            return
          }
          const patch = {}
          if (Object.prototype.hasOwnProperty.call(input, 'size')) {
            const size = normalizeSize(input.size)
            if (size === undefined) {
              fail(res, 400, 'size 要写成 宽x高（例如 1600x1000）', '也允许只给宽：1600')
              return
            }
            patch.size = size
          }
          if (Object.prototype.hasOwnProperty.call(input, 'idleMinutes')) {
            const minutes = Number(input.idleMinutes)
            if (!Number.isInteger(minutes) || minutes < 0 || minutes > 10080) {
              fail(res, 400, 'idleMinutes 要是 0..10080 的整数（分钟）')
              return
            }
            patch.idleMinutes = minutes
          }
          if (Object.prototype.hasOwnProperty.call(input, 'inputEnabled')) {
            if (typeof input.inputEnabled !== 'boolean') {
              fail(res, 400, 'inputEnabled 要是布尔值')
              return
            }
            patch.inputEnabled = input.inputEnabled
          }
          if (Object.keys(patch).length === 0) {
            fail(res, 400, '没有任何可写字段', '可用：size、idleMinutes、inputEnabled')
            return
          }
          if (!(await applySettingsPatch(patch))) {
            // 501：宿主**没有**可用的设置面。这里必须如实回错 ——
            // 回 200 会让客户端提示"已保存"，而配置里什么都没变（真踩过）。
            fail(res, 501,
              '这个宿主版本不支持从面板写设置（或行 id 对不上）',
              '请在「设置 → 插件 → 显示器面板」里改，或设 DSH_VIEW_* 环境变量后重启 DSH')
            return
          }
          // 这三项由**服务进程**消费，写完必须重启服务才生效
          // （`restart:false` 留给"只想落配置、稍后自己重启"的调用方）。
          let service = null
          if (input.restart !== false) {
            await stopService()
            service = await ensureService({ spawn: true, force: true })
          }
          // 读回宿主侧的真值；读不到就回 null（**不要**用缓存的那份假装是现值）。
          const config = readSettingsBack()
          writeJson(res, 200, {
            ok: true,
            config,
            service: service === null ? null : serviceField(service),
          })
        } catch (error) {
          fail(res, 400, String((error && error.message) || error))
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
   * 设置字段的 schema（dsh ≥ 0.1.7 的"行 Config"）。
   *
   * 0.1.7 起插件的设置**就是它的行 Config**：`dsh-settings` 的 `describe()` 会
   * 把每个 live entry 的 `Config` 投影成一张表单，浏览器半边再按行 id
   * （= 包名 `dsh-display-panel`）取它。取不到 schemastery 时这里是 `undefined`，
   * cordis 原样放行、插件照常工作，只是没有表单 —— 见 :func:`loadSchemastery`。
   *
   * 0.1.5 / 0.1.6 的宿主不认这个导出（`runtime.Config` 只是挂着没人看，
   * `vendor/cordis/src/fiber.ts` 在两代里都是 `if (!runtime.Config) return config`），
   * 那两代的设置面走 `settings.register(...)`，见 :func:`registerLegacySettings`。
   */
  Config,
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
  apply(ctx, config) {
    try {
      log('info', `apply() entered（home=${DISPLAY_HOME}, managed=${MANAGED}）`)
      const resolved = setDisplayConfig(config)
      if (Object.keys(resolved).length > 0) {
        log('info', `设置卡片生效值：${JSON.stringify(resolved)}`)
      }
      /**
       * 设置面：**一次注入、两件事**。
       *
       * `settings` 这个服务在三代 DSH 里都存在，但语义不同：
       *   * ≥ 0.1.7 是 SettingsForms（`update(ns, patch)` 写行 Config）→ 捕获它，
       *     供 `/config` 路由用；
       *   * ≤ 0.1.6 有 `register(ns, schema)` → 注册 namespace，供旧版设置卡片用。
       * 用**方法探测**区分（靠服务名判版本会在 0.1.7 上踩空 —— 名字还在、方法没了）。
       */
      try {
        ctx.inject(['settings'], (settingsCtx) => {
          try {
            const settings = settingsCtx && settingsCtx.settings
            if (settings && typeof settings.update === 'function') {
              SETTINGS_SERVICE = settings
              log('info', '已捕获 settings 服务（面板可以写配置了）')
            }
            registerLegacySettings(settings, config)
          } catch (error) {
            log('warn', `设置面初始化失败（不影响面板）：${String((error && error.message) || error)}`)
          }
        })
      } catch (error) {
        log('warn', `settings 注入失败（不影响面板）：${String((error && error.message) || error)}`)
      }

      /**
       * 配置在 GUI 里被改过之后，`Config` 里那些 `Volatile` 引用会被就地更新并
       * 触发 `loader/volatile-update`。这里重新读一遍，好让"下一次拉起服务"
       * 立刻用上新值 —— 否则用户改了设置、点「重启」，服务拿到的还是旧环境变量。
       */
      try {
        ctx.on('loader/volatile-update', () => {
          try {
            const fiber = ctx.fiber
            const next = fiber && fiber.config
            if (next && typeof next === 'object') setDisplayConfig(next)
          } catch (error) {
            log('warn', `读取更新后的配置失败：${String((error && error.message) || error)}`)
          }
        })
      } catch (error) {
        log('warn', `监听配置变更失败：${String((error && error.message) || error)}`)
      }

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
            const claimed = claim(webCtx.webServer, routes.map((route) => route.key))
            for (const key of claimed) {
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
              // 把路径还回登记表：这样 effect 重跑时能重新注册（见 release() 注释）
              release(webCtx.webServer, claimed)
            }
          },
          `${NAME}: routes (${BASE_PATH}/*)`,
        )
        log('info', `host half mounted on ${BASE_PATH}/{info,frame,stream,stats,stream-config,state,display,input,exec,procs,kill,service,events}`)

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

      // 插件卸载：关掉 SSE、断掉活跃的 MJPEG 流、停掉**我们拉起的**服务、清掉定时器。
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
          // 活跃的流必须连**上游**一起断：只关客户端的话，上游那个写线程会一直挂着。
          for (const stream of Array.from(SHARED.streams)) {
            try {
              stream.destroy()
            } catch {
              /* 已经断了 */
            }
          }
          SHARED.streams.clear()
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
