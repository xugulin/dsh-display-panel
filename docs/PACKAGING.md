# 打包与发布（files 的取舍，以及为什么）

这份文档回答一个问题：**`npm pack` 到底该带哪些东西，为什么**。
它不是给用户看的（用户看 [README](../README.md)），是给改这个包的人看的 —— 因为"多带一个目录 / 少带一个目录"
都会在发布之后才暴露：用户拿到包里 README 让他跑 `tools/selftest.py`，而 `tools/` 没打进去。

## 1. 实际发布的内容

`files` 白名单（[package.json](../package.json)）：

```json
"files": ["README.md", "cordis.patch.yml", "docs", "lib", "scripts", "service", "tools"]
```

| 条目 | 带什么 | 为什么带 |
|---|---|---|
| `README.md` | 说明文档 | npm 页面 + 用户第一眼看到的东西 |
| `cordis.patch.yml` | 把插件挂进 profile 花名册的 patch | `package.json` 的 `dsh.bundle.patch` 指着它，**少了插件就装不进 profile** |
| `lib` | `index.js`（宿主半边）、`client.js`（面板）、`tools.js`（给 AI 的工具） | 插件本体。`dsh.client.platform=web` 会让 `./client` 在浏览器里加载 |
| `service` | Python 显示器服务、自检脚本、systemd 单元**模板**、`windows/` 脚本 | 宿主拉起的就是 `service/dsh-display-viewer.py`；少了它面板永远没有画面。注意：模板文件名沿用旧名 `dsh-display-viewer.service`，而 `scripts/install-service.sh` 渲染后**安装**的单元名是 `dsh-display-panel-viewer.service`（避开别人已有的同名单元，见第 6 节的安全说明） |
| `scripts` | 安装/卸载服务、发布脚本、公共函数 | README 让用户跑 `scripts/install-service.sh`（尤其没有 systemd 用户总线的机器） |
| `tools` | `selftest.py`、`e2e-panel.mjs`、`xtarget.py` | README「自测」一节让用户跑它们 —— **文档里出现的命令，包里必须真有这个文件** |
| `docs` | `CONTRACT.md`、`ANALYSIS.md`、`PACKAGING.md` | README 直接链到 `docs/CONTRACT.md`；链一个不存在的文件比不链更糟。契约也是排查时的"真实行为"依据 |

**旧版为什么不对**（0.2.x）：`files` 里同时写了 `scripts` 和 `scripts/publish-npm.sh`、
`service` 和 `service/selfcheck.py`、`service/windows` —— 父目录已经覆盖了子路径，
重复项没有任何作用（npm 会去重，但读的人会以为"这两个是特例，得单独列"）。
现在的规则一句话：**只列顶层目录，特例（单个文件）只在它不在任何目录里时才列。**

## 2. 刻意不带的内容

| 不带 | 原因 |
|---|---|
| `verify/`、`.verify/` | 验证报告与本机跑测试的临时目录 —— 是**证据**不是**产品**，留在仓库里 |
| `node_modules/`、`__pycache__/`、`*.pyc` | `.gitignore` 已排除；`prepack` 再清一次（`rm -rf service/__pycache__ lib/__pycache__ scripts/__pycache__ tools/__pycache__`），避免本机跑过测试后把字节码打进包里 |
| `.github/` | CI 配置对本包用户没用（npm 会忽略 `.github`，这里也不显式列） |
| `LICENSE` | 即使不在 `files` 里，npm 也**总是**带上 `LICENSE` / `README` / `package.json` |

## 3. npm scripts 的纪律：除了 `prepack` 不许有钩子

```json
"scripts": {
  "prepack": "rm -rf service/__pycache__ …",
  "selfcheck": "python3 service/selfcheck.py",
  "test": "python3 tools/selftest.py"
}
```

* **只允许 `prepack`**：它在"打包/发布"时跑，不在用户安装时跑。
* **不加 `postinstall` / `prepare`**：这个包要在没有网络、没有 Python、甚至没有 `Xvfb` 的机器上
  也能装得上；任何安装期钩子失败都会让 `npm i` 直接失败。需要人做的事（装依赖、起服务）
  由**面板和 README** 提示，不由安装脚本代劳。
* `test` / `selfcheck` 是给人手工跑的（`npm test`），CI 里也是显式调用 —— 不挂在安装链上。

## 4. 版本号必须与冻结契约一致

`tools/selftest.py` 有一条静态断言：**`package.json` 的 version 必须等于
`docs/CONTRACT.md` 标题里的版本**（本轮 = `0.3.0`）。改协议忘了改版本号，自测会直接报：

```
FAIL  package.json 版本与契约版本一致  — package.json=0.2.3 契约=0.3.0
```

发布前建议按顺序跑：

```sh
python3 service/selfcheck.py      # 服务内部不变式
python3 tools/selftest.py         # 端到端（缺 GUI 依赖时动态段 SKIP，静态段照跑）
npm pack --dry-run                # 内容清单（本 README 第 1 节就是它的输出）
bash scripts/publish-npm.sh       # 会先列内容、要你确认，再 npm publish --access public
```

## 5. 安装路径（实测记录，2026-09）

| 方式 | 命令 | 结果 |
|---|---|---|
| 开发用（软链） | `dsh plugin --profile <p> add link:/abs/path/dsh-display-panel` | ✅ 在 `$DSH_HOME/profiles/<p>/package.json` 生成 `link:` 依赖 + `node_modules` 软链，并**自动**把包名加进 `dsh.profile.bundles` |
| 本地包 | `dsh plugin --profile <p> add file:/abs/path/dsh-display-panel-0.3.0.tgz` | ✅ 装完 `node_modules/dsh-display-panel/` 里有 `lib/ service/ scripts/ tools/ docs/`（README 让用户跑的脚本都在） |
| npm | `dsh plugin --profile <p> add dsh-display-panel` | 同 `file:`，只是从 registry 取 |

`<p>` 是 profile 名；profile 目录 = `$DSH_HOME/profiles/<p>/`（桌面版 `$DSH_HOME` 在它自己的目录下，
CLI 默认 `~/.dsh`）。插件装在 **profile** 里，换 profile 要重装一次。

## 6. 单元名：为什么装出来的名字和模板文件不一样

* 模板文件叫 `service/dsh-display-viewer.service`（历史名，内容里用 `@PYTHON@` / `@HERE@` 占位）；
* 脚本渲染后**安装**的名字是 **`dsh-display-panel-viewer.service`**，并在 `[Unit]` 里插一行
  `X-DSH-Display-Panel=1` 标记（systemd 会忽略 `X-` 开头的键）。

原因是真实的踩坑：同一个用户下**早已存在** `~/.config/systemd/user/dsh-display-viewer.service`，
指向**另一份 checkout** 的 viewer，而且还在跑、占着 8099。旧脚本会按同名直接覆盖它 ——
用户重启后面板就悄悄换成了另一份代码，甚至起不来。现在的规则：

| 动作 | 判据 | 不是本包时 |
|---|---|---|
| 安装 | 同名单元带标记，或 `ExecStart` 指向本包的 `service/dsh-display-viewer.py` | **拒绝覆盖**并打印对策（换名 / 先处理旧单元 / `--force`） |
| `--stop`、卸载 | 同上，且单元里的 `DSH_DISPLAY_HOME` == 本次运行目录（没写则必须是默认目录） | 只报告，**不停、不删** |

想检查有没有装错、装重：`bash scripts/install-service.sh --status`（它会列出发现的老单元）。
