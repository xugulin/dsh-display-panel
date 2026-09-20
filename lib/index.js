/**
 * dsh-display-panel — 宿主半边。
 *
 * 只做一件事：把**本用户**的显示器服务端口与访问令牌交给浏览器。
 *
 * 为什么必须由宿主来做：服务监听 127.0.0.1，**同一台机器上的其他用户也能连上**；
 * 服务因此要求请求带 `?k=<token>`，令牌放在本用户的 `~/.cache/dsh-display/token`
 * （目录 700，别的用户读不到）。浏览器里的插件读不到文件、也不知道自己的 uid，
 * 但**宿主进程就是以该用户身份运行的** —— 由它读出来、经宿主自己的接口（并复用宿主
 * 既有的请求守卫 `connection.requestRejection`）转交给浏览器即可。
 *
 * 整段包在 try/catch 里：出任何问题都只是"面板拿不到端口"，**绝不能影响 harness 启动**。
 */
'use strict'

const fs = require('fs')
const os = require('os')
const path = require('path')

const NAME = 'dsh-display-panel'
const ROUTE = '/api/dsh-display-panel/info'
const HOME_DIR = process.env.DSH_DISPLAY_HOME || path.join(os.homedir(), '.cache', 'dsh-display')

function readText(file) {
  try {
    return fs.readFileSync(file, 'utf8').trim()
  } catch (error) {
    return ''
  }
}

function readInfo() {
  const port = Number.parseInt(readText(path.join(HOME_DIR, 'port')), 10)
  return {
    home: HOME_DIR,
    port: Number.isFinite(port) && port > 0 ? port : null,
    token: readText(path.join(HOME_DIR, 'token')) || null,
  }
}

module.exports = {
  name: NAME,
  apply(ctx) {
    try {
      // 与 dsh-browser-panel 同一套挂载方式：等 webServer 与 connection 就位再注册，
      // 并复用宿主自己的请求守卫 —— 不自己造 auth（造错了就是"任意网页都能读令牌"）。
      ctx.inject(['webServer', 'connection'], (webCtx) => {
        const route = {
          kind: 'exact',
          path: ROUTE,
          handler: (req, res) => {
            try {
              const rejection = webCtx.connection.requestRejection(req)
              if (rejection !== undefined) {
                res.writeHead(rejection, { 'content-type': 'text/plain; charset=utf-8' })
                res.end(rejection === 401 ? 'unauthorized' : 'forbidden')
                return
              }
              const body = JSON.stringify(readInfo())
              res.writeHead(200, {
                'content-type': 'application/json; charset=utf-8',
                'cache-control': 'no-store',
              })
              res.end(body)
            } catch (error) {
              res.writeHead(500, { 'content-type': 'application/json; charset=utf-8' })
              res.end(JSON.stringify({ error: String((error && error.message) || error) }))
            }
          },
        }
        webCtx.effect(() => webCtx.webServer.register(route), `${NAME}: ${ROUTE}`)
        console.log(`[${NAME}] host half mounted on ${ROUTE}`)
      })
    } catch (error) {
      console.warn(`[${NAME}] host half skipped:`, error)
    }
  },
}
