// 安装后提示：光有插件不够，还需要把显示器服务跑起来（否则面板只会显示"显示器还没有打开"）。
const fs = require('fs')
const path = require('path')
try {
  const unit = path.join(__dirname, '..', 'service', 'dsh-display-viewer.service')
  if (fs.existsSync(unit)) {
    console.log('[dsh-display-panel] 别忘了启动显示器服务：')
    console.log('[dsh-display-panel]   bash scripts/install-service.sh --user xgl   # 见 README')
  }
} catch (e) { /* 安装提示失败不该影响安装 */ }
