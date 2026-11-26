/**
 * @local/dsh-omnivision —— 通用视觉层「照像机」
 *
 * 把 sidecar（Python：PIL 抓帧 + RapidOCR/ONNX 文字层 + 块差分 + 场景指纹缓存）
 * 包装成一个工具 `vision_look`：
 *   看一帧 → 返回「场景 + 带坐标的元素清单 + 变化区域 + 截图路径」，
 *   同一界面第二次直接走缓存（0 OCR，几十毫秒）。
 *
 * 设计取舍：
 *   - 只返回文本清单 + 截图路径；要真看图，用 read_image 读 frame 路径（按需，省 token）。
 *   - 点按不在这里重复实现：元素已带 center，直接喂 cv_act / coach_click。
 */

import { spawn } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import { existsSync, readFileSync, statSync, writeFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { dirname, join } from 'node:path'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'

export const name = 'omnivision'

const HERE = dirname(fileURLToPath(import.meta.url))
const SIDECAR = join(HERE, 'sidecar', 'omnivisiond.py')
const CONFIG_FILE = join(HERE, 'omnivision.config.json')

/** 安装器写的本地配置（数据目录等）。没这个文件就用默认值。 */
function readFileConfig() {
  try {
    if (!existsSync(CONFIG_FILE)) return {}
    const data = JSON.parse(readFileSync(CONFIG_FILE, 'utf8'))
    return data && typeof data === 'object' ? data : {}
  } catch {
    return {}
  }
}

function readPromptFile(filename, fallback = '') {
  try {
    const p = join(HERE, 'prompts', filename)
    if (existsSync(p)) return String(readFileSync(p, 'utf8')).trim()
  } catch {}
  return fallback
}

export function isSilentReply(text) {
  const s = String(text ?? '').trim()
  return s === '' || /^\[\s*SILENT\s*\]$/i.test(s)
}

export function formatCompanionEvent(ev) {
  const mode = String(ev?.mode || 'watch')
  const isWatch = mode === 'watch'
  const tag = isWatch ? '[伴侣/追剧]' : `[伴侣/游戏:${mode}]`
  const lines = [
    `${tag} 窗口「${ev?.window || 'foreground'}」｜建议推理强度 reasoning_effort=${ev?.reasoningEffort ?? (isWatch ? 2 : 15)}｜本小时配额 ${ev?.budgetUsed ?? '?'}/${ev?.budgetMax ?? '?'}`,
  ]
  if (ev?.frame) {
    lines.push(
      isWatch
        ? `2x2时间序宫格图：${ev.frame}（请直接用原生视觉 read_image 查看）`
        : `最新游戏画面：${ev.frame}（请直接用原生视觉 read_image 查看；极小数值看不清时调 vision_ocr_crop）`,
    )
  }
  if (ev?.transcript) {
    lines.push(`近期台词/音频转写：\n${ev.transcript}`)
  }
  const proto = isWatch
    ? readPromptFile('watch_protocol.md')
    : readPromptFile('game_protocol.md')
  if (proto) {
    lines.push(`--- 感官协议 ---\n${proto.replaceAll('{{window_title}}', String(ev?.window || 'foreground'))}`)
  }
  return lines.join('\n')
}


// ---------------------------------------------------------------- Config（settings 页自动渲染用）

const PROFILE_VALUES = ['energy', 'standard', 'performance']

function loadSchemastery() {
  try {
    const dir = process.env.DSH_PROFILE_DIR || join(homedir(), '.dsh', 'profiles', 'desktop')
    const requireFromProfile = createRequire(join(dir, 'package.json'))
    const mod = requireFromProfile('@deepseek-ai/schemastery')
    return mod && (mod.default || mod)
  } catch {
    return null
  }
}

const z = loadSchemastery()

//: profile 标 volatile：设置页改动后由 settings 服务走「不重载插件」的路径写回，
//: 我们通过 .get() 读实时值并同步给 sidecar。
export const Config = z
  ? z.object({
      profile: z
        .union([z.const('energy'), z.const('standard'), z.const('performance')])
        .volatile()
        .description('性能档位：energy 省电 / standard 标准 / performance 性能'),
    })
  : undefined

const DEFAULT_DATA_DIR = join(homedir(), '.dsh', 'omnivision')

const DEFAULTS = {
  python: '',
  framesDir: '',
  dataDir: DEFAULT_DATA_DIR,
  bootTimeoutMs: 20000,
  callTimeoutMs: 60000,
  maxElements: 40,
  minScore: 0.5,
  profile: 'standard',
}

const LOOK_OUTPUT = {
  schema: {
    type: 'object',
    additionalProperties: true,
    properties: { text: { type: 'string' }, data: { type: 'object' } },
    required: ['text'],
  },
  render: (_args, value) => [{ type: 'text', text: String(value?.text ?? '') }],
}

function resolvePython(cfg) {
  if (cfg?.python) return cfg.python
  if (process.env.DSH_OMNIVISION_PYTHON) return process.env.DSH_OMNIVISION_PYTHON
  const venv = join(cfg?.dataDir || DEFAULTS.dataDir, 'venv', 'Scripts', 'python.exe')
  if (existsSync(venv)) return venv
  return process.platform === 'win32' ? 'python' : 'python3'
}

// ---------------------------------------------------------------- sidecar 进程

class Sidecar {
  constructor(log, cfg) {
    this.log = log
    this.cfg = cfg
    this.proc = undefined
    this.port = 0
    this.screen = undefined
    this.stderr = ''
    this.starting = undefined
    this.python = resolvePython(cfg)
  }

  get base() {
    return `http://127.0.0.1:${this.port}`
  }

  async ensure() {
    if (this.port && this.proc && this.proc.exitCode === null) return this
    if (this.starting) return this.starting
    this.starting = this._start().finally(() => {
      this.starting = undefined
    })
    return this.starting
  }

  async _start() {
    if (!existsSync(SIDECAR)) throw new Error(`sidecar 脚本不存在：${SIDECAR}`)
    const python = this.python
    if (!existsSync(python) && python !== 'python' && python !== 'python3') {
      throw new Error(`找不到 Python：${python}（可用 DSH_OMNIVISION_PYTHON 指定）`)
    }
    const dataDir = String(this.cfg?.dataDir || DEFAULTS.dataDir).replace(/\\/g, '/')
    const args = [SIDECAR, '--port', '0', '--data-dir', dataDir]
    const wanted = this.cfg?.runtime?.profile
    const profileArg = ['energy', 'standard', 'performance'].includes(String(wanted)) ? String(wanted) : 'standard'
    args.push('--profile', profileArg)
    if (this.cfg?.framesDir) args.push('--frames-dir', String(this.cfg.framesDir).replace(/\\/g, '/'))
    this.log?.info?.(`启动视觉 sidecar：${python} ${args.join(' ')}`)
    const proc = spawn(python, args, { cwd: HERE, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] })
    this.proc = proc
    proc.stderr?.on('data', (chunk) => {
      this.stderr = (this.stderr + String(chunk)).slice(-6000)
    })
    const first = await new Promise((resolve, reject) => {
      let buf = ''
      const timer = setTimeout(
        () => reject(new Error(`sidecar 启动超时（${this.cfg?.bootTimeoutMs ?? DEFAULTS.bootTimeoutMs}ms）。stderr：${this.stderr.slice(-800)}`)),
        this.cfg?.bootTimeoutMs ?? DEFAULTS.bootTimeoutMs,
      )
      proc.stdout?.on('data', (chunk) => {
        buf += String(chunk)
        const idx = buf.indexOf('\n')
        if (idx >= 0) {
          clearTimeout(timer)
          resolve(buf.slice(0, idx).trim())
        }
      })
      proc.once('error', (err) => {
        clearTimeout(timer)
        reject(new Error(`无法启动 python（${python}）：${err.message}`))
      })
      proc.once('exit', (code) => {
        clearTimeout(timer)
        reject(new Error(`sidecar 启动即退出（code=${code}）。stderr：${this.stderr.slice(-800)}`))
      })
    })
    let info
    try {
      info = JSON.parse(first)
    } catch {
      throw new Error(`sidecar 启动输出无法解析：${String(first).slice(0, 300)}`)
    }
    this.port = info.port
    this.screen = info.screen
    this.codeMtime = Number(info.codeMtime) || 0
    this.log?.info?.(`视觉 sidecar 就绪：端口 ${this.port}，屏幕 ${(info.screen || []).join('x')}，加载 ${info.bootMs}ms`)
    return this
  }

  async call(path, body, timeoutMs, opts) {
    await this.ensure()
    const init = body === undefined
      ? { method: 'GET' }
      : { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) }
    const limit = timeoutMs ?? (this.cfg?.callTimeoutMs ?? DEFAULTS.callTimeoutMs)
    const allowRetry = opts?.retryOnNetErr !== false
    let res
    for (let attempt = 0; attempt < (allowRetry ? 2 : 1); attempt++) {
      try {
        res = await fetch(this.base + path, { ...init, signal: AbortSignal.timeout(limit) })
        break
      } catch (err) {
        const msg = String(err?.message ?? err)
        const isTransient = /fetch failed|ECONNREFUSED|ECONNRESET|UND_ERR_SOCKET|socket/i.test(msg)
        if (attempt === 0 && allowRetry && isTransient) {
          if (!this.proc || this.proc.exitCode !== null) this.port = 0
          await new Promise((r) => setTimeout(r, 300))
          await this.ensure()
          continue
        }
        throw new Error(`视觉 sidecar 调用失败（${path}）：${msg}。stderr：${this.stderr.slice(-400)}`)
      }
    }
    const text = await res.text()
    let data
    try {
      data = text ? JSON.parse(text) : {}
    } catch {
      throw new Error(`视觉 sidecar 返回非 JSON（${path}）：${text.slice(0, 300)}`)
    }
    if (!res.ok || (data.ok === false && opts?.allowNotOk !== true)) {
      throw new Error(`视觉失败（${path}）：${data.error ?? text.slice(0, 300)}`)
    }
    return data
  }

  stop() {
    const proc = this.proc
    this.port = 0
    this.proc = undefined
    if (!proc) return
    try {
      proc.kill()
    } catch {
      /* 可能已退出 */
    }
  }
}

// ---------------------------------------------------------------- 渲染

const arr = (v) => (Array.isArray(v) ? v : [])

function renderLook(args, d) {
  const els = arr(d.elements)
  const changes = arr(d.changes)
  const stats = d.stats && typeof d.stats === 'object' ? d.stats : {}
  const ocrOn = args?.ocr !== false
  const iconN = els.filter((e) => e.source === 'icon').length
  const hotN = els.filter((e) => e.interactive).length
  const mixed = `（文字 ${els.length - iconN} / 图标 ${iconN}，可点 ${hotN}）`
  const head = [
    `场景 ${d.scene || 'unknown'}`,
    `元素 ${els.length}${mixed}${ocrOn ? `（原始 ${stats.rawCount ?? '?'} 块）` : '（本次没跑 OCR）'}`,
    `变化 ${changes.length} 块`,
    d.cached ? '缓存命中' : '新解析',
    `${d.elapsedMs}ms`,
  ].join('｜')

  const lines = [head]
  if (changes.length) {
    const top = changes.slice(0, 3).map((c) => `${c.box?.join(',')}(${Math.round((c.score ?? 0) * 100)}%)`)
    lines.push(`变化区域：${top.join('  ')}${changes.length > 3 ? ` …共 ${changes.length} 块` : ''}`)
  }
  if (els.length) {
    lines.push('元素（box = x,y,w,h；中心点可直接喂 cv_act）：')
    for (const e of els) {
      const star = e.interactive ? '*' : ' '
      const c = e.center ? `中心(${e.center[0]},${e.center[1]})` : ''
      const label = e.source === 'icon' ? '(无字图标)' : `「${e.label}」`
      const low = e.score != null && e.score < 0.75 ? ` ?${Math.round(e.score * 100)}` : ''
      lines.push(`${star}${e.id} (${(e.box || []).join(',')}) ${c} ${label}${low}`)
    }
  } else {
    lines.push('这一帧没读到文字元素（可能是纯图标界面，或 OCR 被关了）。')
  }
  lines.push(`帧：${d.frame || '(没存)'}　（要真看图：read_image 这个路径）`)
  if (d.markedFrame) lines.push(`标注图：${d.markedFrame}　（带 id，可拿去 vision_understand{frame:"…"} 问答引用）`)
  if (!d.cached && stats.ocrMs) {
    const iconPart = stats.iconsMs
      ? ` / 图标 ${stats.iconsMs}ms（${stats.iconEngine || '?'}：并入控件 ${stats.iconsBlended ?? 0} 个，独立图标 ${stats.iconsStandalone ?? 0} 个）`
      : ''
    const cachePart = stats.cache === 'boxes' ? `｜框缓存命中（${stats.boxHit ?? '?'} 框，0 次识别）` : ''
    lines.push(`耗时明细：抓帧 ${stats.grabMs}ms / 检测 ${stats.detMs}ms / 识别 ${stats.recMs}ms${iconPart}${cachePart}`)
  }
  return lines.join('\n')
}

function renderUiTree(args, d) {
  const els = arr(d.elements)
  const win = d.window && typeof d.window === 'object' ? d.window : {}
  const stats = d.stats && typeof d.stats === 'object' ? d.stats : {}
  const uiaN = els.filter((e) => e.source === 'uia').length
  const ocrN = els.filter((e) => e.source === 'ocr').length

  if (d.source === 'ocr') {
    const head = `UIA 无树 → 回退 OCR｜${d.fallback || '未知原因'}｜元素 ${els.length}｜${d.elapsedMs}ms`
    const lines = [head]
    if (win.title) lines.push(`窗口：${win.title}${win.app ? `（${win.app}）` : ''}`)
    for (const e of els.slice(0, 40)) {
      const c = e.center ? `中心(${e.center[0]},${e.center[1]})` : ''
      lines.push(`${e.id} (${(e.box || []).join(',')}) ${c} 「${e.label}」`)
    }
    if (els.length > 40) lines.push(`…共 ${els.length} 个，只列前 40`)
    if (d.frame) lines.push(`帧：${d.frame}`)
    return lines.join('\n')
  }

  const head = [
    `UIA 树｜节点 ${stats.nodes ?? '?'}（可交互 ${uiaN}${ocrN ? ` + OCR 补 ${ocrN}` : ''}）`,
    `${d.elapsedMs}ms（预算 ${d.budgetMs}ms，depth ${d.depth ?? '?'}）`,
    d.truncated ? '⚠ 超预算已截断' : '完整',
    `uiautomation ${d.uiaVersion || '?'}`,
  ].join('｜')
  const lines = [head]
  if (d.note) lines.push(`⚠ ${d.note}`)
  if (win.title) lines.push(`窗口：${win.title}${win.app ? `（${win.app}）` : ''}${win.pid ? ` pid ${win.pid}` : ''}`)
  const actMap = new Map(arr(d.actions).map((x) => [x.id, x]))
  for (const e of els.slice(0, 60)) {
    const en = e.enabled === false ? '✗禁用' : e.enabled === true ? '✓' : '?'
    const sel = e.selected === null || e.selected === undefined ? '未知' : e.selected ? '选中' : '未选'
    const focus = e.focus === true ? '有焦点' : ''
    const act = actMap.get(e.id)
    const actTxt = act ? (act.blocked ? '动作=禁用' : `动作=${act.action}`) : ''
    const label = e.name ? `「${e.name}」` : '(无名)'
    lines.push(`${e.id} ${e.role} (${(e.box || []).join(',')}) 中心(${(e.center || []).join(',')}) ${en} 选中=${sel}${actTxt ? ' ' + actTxt : ''}${focus ? ' ' + focus : ''} ${label}`)
  }
  if (els.length > 60) lines.push(`…共 ${els.length} 个，只列前 60`)

  const rels = arr(d.relations)
  if (d.relationsSkipped === 'too_many') {
    lines.push('关系：元素超过 30 个，已跳过（relationsSkipped: too_many）')
  } else if (rels.length) {
    const shown = rels.slice(0, 10).map((r) =>
      r.rel === 'labelFor' ? `${r.label} → ${r.b}（labelFor, gap ${r.gap}）` : `${r.a} ${r.rel} ${r.b}（gap ${r.gap}）`)
    lines.push(`关系（${rels.length} 条）：${shown.join('；')}${rels.length > 10 ? ' …' : ''}`)
  }
  const unresolved = arr(d.labelForUnresolved)
  if (unresolved.length) lines.push(`⚠ 标签未识别：${unresolved.join(', ')}（不猜，按未知处理）`)
  return lines.join('\n')
}

function renderAsk(args, d) {
  const usage = d.usage && typeof d.usage === 'object' ? d.usage : {}
  const tokens = usage.total_tokens ? `｜tokens ${usage.prompt_tokens ?? '?'}/${usage.completion_tokens ?? '?'}（总 ${usage.total_tokens}）` : ''
  const q = d.question || args?.question || ''
  const status = d.refused ? '拒绝' : d.cached ? '缓存命中（0 请求）' : `${d.elapsedMs ?? '?'}ms`
  const head = `问答模式｜后端 ${d.backend || '?'}｜${status}${d.refused ? '' : tokens}`

  if (d.refused) {
    return [head, `Q: ${q}`, `A: 无法回答 —— ${d.reason || '这个后端不支持问答'}`].join('\n')
  }
  if (d.degraded) {
    return [head, `Q: ${q}`, `A: （失败）${d.error || '模型调用失败'}`, `帧：${d.frame || '(没存)'}`].join('\n')
  }
  return [
    head,
    `Q: ${q}`,
    `A: ${d.answer || '(模型没给回答)'}`,
    `帧：${d.frame || '(没存)'}`,
  ].join('\n')
}

function renderUnderstand(args, d) {
  const els = arr(d.elements)
  const confirmed = els.filter((e) => e.confident)
  const usage = d.usage && typeof d.usage === 'object' ? d.usage : {}
  const tokens = usage.total_tokens ? `tokens ${usage.prompt_tokens ?? '?'}/${usage.completion_tokens ?? '?'}（总 ${usage.total_tokens}）` : ''
  const head = [
    `场景 ${d.scene || 'unknown'}`,
    d.summary ? `「${d.summary}」` : '',
    `元素 ${els.length}｜模型确认 ${confirmed.length}`,
    `后端 ${d.backend || '?'}`,
    d.cached ? '语义缓存命中（0 请求）' : (d.backend === 'off' ? '没发请求（off）' : '本轮请求过模型'),
    `${d.elapsedMs}ms`,
  ].filter(Boolean).join('｜')

  const lines = [head]
  if (d.degraded) {
    const why = d.error || '模型调用失败'
    lines.push(`⚠ 降级：${why}——下面是原始 OCR 结果，没清洗`)
  }
  if (arr(d.attempts).length) {
    lines.push(`回退过程：${arr(d.attempts).map((x) => `${x.backend} 失败（${String(x.error || '').slice(0, 60)}）`).join(' → ')}`)
  }
  if (els.length) {
    lines.push('元素（✓ = 模型对照截图确认过；无 ✓ = 原样保留）：')
    for (const e of els) {
      const mark = e.confident ? '✓' : ' '
      const c = e.center ? `中心(${e.center[0]},${e.center[1]})` : ''
      const label = e.source === 'icon' ? '(无字图标)' : `「${e.label}」`
      lines.push(`${mark}${e.id} (${(e.box || []).join(',')}) ${c} ${label}`)
    }
  } else {
    lines.push('这一帧没读到文字元素。')
  }
  lines.push(`帧：${d.frame || '(没存)'}　${d.model ? `模型 ${d.model}` : ''}${tokens ? `｜${tokens}` : ''}`)
  if (!d.cached && d.latencyMs != null) lines.push(`模型耗时 ${d.latencyMs}ms（总耗时含截图+OCR）`)
  return lines.join('\n')
}

function renderStatus(d) {
  const ocr = d.ocr || {}
  const cache = d.cache || {}
  const sem = d.semantic || {}
  const semCache = sem.cache || {}
  const loc = sem.local || {}
  return [
    `视觉层就绪方式：${ocr.ready ? 'RapidOCR/ONNX' : '未加载'}`,
    `屏幕：${(d.screen || []).join('x')}（原点 ${(d.origin || []).join(',')}）｜Python ${d.python}`,
    `OCR 初始化：${ocr.initMs}ms｜宽高比钳制 ${ocr.maxWhRatio}`,
    ...(ocr.cache
      ? [
          `OCR 缓存：命中率 ${ocr.cache.hitRate ?? 0}%（命中 ${ocr.cache.cacheHits ?? 0} 次 / 全量 ${ocr.cache.fullRuns ?? 0} 次）｜det ${ocr.cache.detLimit ?? '?'}｜框缓存 ${ocr.cache.entries ?? 0} 条`,
        ]
      : []),
    `元素缓存：${cache.size}/${cache.cap} 条，命中 ${cache.hits}，未命中 ${cache.misses}`,
    `语义后端：${sem.backend || '?'}｜本地 ${loc.healthy ? '就绪' : '未起'}（${loc.endpoint || '?'}）｜启动 ${loc.bootMs ?? '?'}ms`,
    `云端：${sem.model || '?'}｜key ${sem.keyPresent ? '已就绪' : '缺失'}（${sem.keySource}）｜请求 ${sem.calls ?? 0} 次、失败 ${sem.errors ?? 0} 次`,
    ...(d.usage && d.usage.budget
      ? [
          `本月模型：调用 ${d.usage.cloudCalls ?? 0} 次｜tokens ≈ ${d.usage.totalTokens ?? 0}｜≈ ${d.usage.budget.estimatedCredits ?? 0} / ${d.usage.budget.monthlyCredits ?? 20000} Credits（${d.usage.budget.percent ?? 0}%）`,
          ...(d.usage.budget.warn
            ? [`⚠ 套餐用量已到 ${d.usage.budget.percent}%（本地估算：tokens/${d.usage.budget.tokensPerCredit}；官方无用量 API，数字偏大说明该省着用了）`]
            : []),
        ]
      : []),
    `ONNX 线程：${(d.onnx || {}).threads ?? '?'}（${(d.onnx || {}).source === 'env' ? '环境变量' : '自动'}，${(d.onnx || {}).cpuCount ?? '?'} 核）`,
    d.configProfile
      ? `档位：配置 ${d.configProfile}｜实际生效 ${d.appliedProfile || d.profile || '?'}${d.profileMismatch ? ' ⚠ 不一致' : ''}`
      : `档位：${['energy', 'standard', 'performance'].includes(String(d.profile)) ? d.profile : 'standard'}` + `（watch ${({ energy: 5, standard: 2, performance: 1 })[String(d.profile)] ?? 2}s + textTrack ${String(d.profile) === 'energy' ? 'off' : 'on'}` + `｜语义默认 ${String(d.profile) === 'energy' ? 'off' : String(d.profile) === 'performance' ? 'cloud' : 'local'}）`,
    `图标层：${(d.icons || {}).modelPresent ? 'OmniParser ONNX 就绪' : '模型缺失（会退 OpenCV）'}｜${(d.icons || {}).modelPath || ''}`,
    `结构层：${(d.uia || {}).available ? `uiautomation ${(d.uia || {}).version} 就绪` : 'UIA 不可用（会回退 OCR）'}｜钉死 ${(d.uia || {}).pin || '2.0.29'}`,
    `语义缓存：${semCache.size ?? 0}/${semCache.cap ?? 0} 条，命中 ${semCache.hits ?? 0}，未命中 ${semCache.misses ?? 0}`,
    `本次进程：调用 ${d.calls} 次，已运行 ${d.uptimeS}s`,
    ...(d.companion
      ? [
          `伴侣底座：模式 ${d.companion.mode || 'passive'}｜窗口锁 ${d.companion.lockedTitle || d.companion.windowMatch || '无'}｜采集 ${d.companion.sensor?.backend || 'pil'}｜音频 ${d.companion.audio?.running ? d.companion.audio.asrEngine : '未启'}｜急停 ${d.companion.driver?.emergencyStopped ? '已触发' : '正常'}｜本小时配额 ${d.companion.budget?.usedThisHour ?? 0}/${d.companion.budget?.maxPerHour ?? 120}`,
        ]
      : []),
    `截图目录：${d.framesDir}`,
  ].join('\n')
}

function renderProfileText(d) {
  if (!d || d.ok === false) return `档位切换失败：${(d && d.error) || '未知错误'}`
  const changed = typeof d.oldThreads === 'number' && d.oldThreads !== d.threads
  const lines = [`档位：${d.profile}｜ONNX 线程 ${d.threads}${changed ? `（${d.oldThreads} → ${d.threads}）` : ''}`]
  if (d.rebuilt) lines.push('OCR 引擎已按新档位重建（约 1 秒），不用重启 DSH')
  lines.push(`watch 默认：${d.profile === 'energy' ? '5s + textTrack off' : d.profile === 'performance' ? '1s + textTrack on' : '2s + textTrack on'}`)
  lines.push(`语义默认：${d.profile === 'energy' ? 'off' : d.profile === 'performance' ? 'cloud' : 'local'}`)
  return lines.join('\n')
}

// ---------------------------------------------------------------- 插件主体

export function apply(ctx, config) {
  const fileCfg = readFileConfig()
  const cfg = { ...DEFAULTS, ...fileCfg, ...(config ?? {}) }

  const legacyProfile = () => {
    try {
      const raw = String(readFileSync(CONFIG_FILE, 'utf8')).replace(/^\uFEFF/, '')
      const j = JSON.parse(raw)
      return PROFILE_VALUES.includes(String(j?.profile)) ? String(j.profile) : ''
    } catch {
      return ''
    }
  }
  // 优先级：cordis config（volatile 实时值）> 旧 omnivision.config.json > standard
  const readLiveProfile = () => {
    let raw
    try {
      const v = cfg.profile
      raw = v && typeof v.get === 'function' ? v.get() : v
    } catch {}
    if (PROFILE_VALUES.includes(String(raw))) return String(raw)
    return legacyProfile() || 'standard'
  }
  const runtime = { profile: readLiveProfile() }
  const profile = ['energy', 'standard', 'performance'].includes(String(cfg.profile))
    ? String(cfg.profile)
    : 'standard'
  const side = new Sidecar(ctx.logger?.('omnivision') ?? ctx.logger, cfg)

  function persistProfileLegacy(profile) {
    try {
      let cur = {}
      if (existsSync(CONFIG_FILE)) cur = JSON.parse(String(readFileSync(CONFIG_FILE, 'utf8')).replace(/^\uFEFF/, ''))
      cur.profile = profile
      writeFileSync(CONFIG_FILE, JSON.stringify(cur, null, 2), 'utf8')
      return 'json'
    } catch {
      return 'none'
    }
  }

  // 写回 cordis config（settings 页读的就是它）；拿不到 configEditor 就退回旧 JSON
  async function persistProfile(profile) {
    try {
      const editor = ctx.get && ctx.get('configEditor')
      if (editor && typeof editor.entries === 'function' && typeof editor.edit === 'function') {
        const entry = editor.entries().find((e) => {
          const id = String(e?.options?.id || '')
          const name = String(e?.options?.name || '')
          return id === '@local/dsh-omnivision' || name === '@local/dsh-omnivision' || id.endsWith('/omnivision')
        })
        if (entry) {
          await editor.edit(entry, (current) => ({ ...(current || {}), profile }))
          return 'config'
        }
      }
    } catch (e) {
      ctx.logger?.warn?.('omnivision: 写 cordis config 失败：' + String(e?.message ?? e))
    }
    return persistProfileLegacy(profile)
  }

  // 切档：先热切 sidecar（立即生效），再持久化（settings 页/重启后保持）
  async function applyProfile(profile, { persist = true } = {}) {
    const p = String(profile || '').toLowerCase()
    if (!PROFILE_VALUES.includes(p)) return { ok: false, error: '档位只能是 energy / standard / performance' }
    const d = await side.call('/profile', { profile: p }, 120000)
    if (d && d.ok) runtime.profile = p
    if (persist && d && d.ok) d.persisted = await persistProfile(p)
    return d
  }

  // 设置页改档位 → settings/document-updated（只带 ns+revision）→ 读实时值同步 sidecar
  try {
    ctx.effect(() =>
      ctx.on('settings/document-updated', () => {
        const p = readLiveProfile()
        if (p !== runtime.profile) void applyProfile(p, { persist: false }).catch(() => {})
      }),
    )
  } catch {}

  // 迁移：老用户只在 omnivision.config.json 里写过 profile、cordis config 里没有 → 补写一份
  try {
    const raw = cfg.profile
    const cordisHas = PROFILE_VALUES.includes(String(raw && typeof raw.get === 'function' ? raw.get() : raw))
    if (!cordisHas && legacyProfile()) void persistProfile(legacyProfile())
  } catch {}

  ctx.effect(() => () => side.stop())

  // ---------------- 余光（watch）：sidecar 差分事件 → followup 注入 ----------------
  const watch = {
    running: false,
    timer: null,
    agent: null,
    sessionId: null,
    lastEventId: 0,
    injectedSeqs: new Set(),
    cfg: null,
    startedAt: 0,
    lastError: '',
    stats: { polls: 0, events: 0, injected: 0, failed: 0, skipped: 0 },
  }

  function serviceOf(name) {
    try {
      if (typeof ctx.get === 'function') return ctx.get(name) || null
    } catch {}
    try { return ctx[name] || null } catch {}
    return null
  }

  // 手工填的时间：数字当 Unix 毫秒；"23:00" 当今天/明天那个时刻
  function parseWhen(v) {
    if (v === undefined || v === null || v === '') return undefined
    if (typeof v === 'number' && Number.isFinite(v)) return Math.floor(v)
    const s = String(v).trim()
    if (/^\d+$/.test(s)) return Number(s)
    const m = s.match(/^(\d{1,2}):(\d{2})$/)
    if (m) {
      const now = new Date()
      const t = new Date(now.getFullYear(), now.getMonth(), now.getDate(), Number(m[1]), Number(m[2]), 0, 0)
      if (t.getTime() <= now.getTime()) t.setDate(t.getDate() + 1)
      return t.getTime()
    }
    return undefined
  }

  function stopWatchTimer() {
    if (watch.timer) {
      clearInterval(watch.timer)
      watch.timer = null
    }
    watch.running = false
  }

  function watchText(ev) {
    const boxes = Array.isArray(ev.boxes) ? ev.boxes : []
    const box = boxes[0] && Array.isArray(boxes[0].box) ? boxes[0].box : null
    const where = box ? `区域 ${box[0]},${box[1]} 附近` : '整屏'
    const score = typeof ev.score === 'number' ? ev.score.toFixed(2) : '?'
    let text = `[余光] 屏幕变了（score ${score}，${where}，窗口「${ev.window || '未知'}」），帧：${ev.frame || '(没存)'}`
    const tc = Array.isArray(ev.textChanges) ? ev.textChanges : []
    if (tc.length) {
      const parts = tc.slice(0, 3).map((x) =>
        x.op === 'update' ? `${x.from} → ${x.to}` : x.op === 'add' ? `+${x.text}` : `-${x.text}`)
      text += `\n文字：${parts.join(' / ')}`
    }
    return text
  }

  async function watchAgent() {
    if (watch.agent && typeof watch.agent.followup === 'function') return watch.agent
    try {
      const sc = serviceOf('sessionController')
      if (sc && typeof sc.resolveAgent === 'function' && watch.sessionId) {
        const r = await sc.resolveAgent(watch.sessionId)
        if (r && r.agent) {
          watch.agent = r.agent
          return r.agent
        }
      }
    } catch (e) {
      watch.lastError = String((e && e.message) || e)
    }
    return null
  }

  async function injectWatch(text) {
    const agent = await watchAgent()
    if (!agent) {
      watch.stats.skipped += 1
      return false
    }
    const message = {
      id: randomUUID(),
      role: 'user',
      content: [{ type: 'text', text }],
      source: { kind: 'omnivision-watch' },
    }
    try {
      await agent.followup(message)
      watch.stats.injected += 1
      try {
        const sessions = serviceOf('sessions')
        if (sessions && typeof sessions.flush === 'function') await sessions.flush(agent.session)
      } catch {}
      return true
    } catch (e) {
      watch.stats.failed += 1
      watch.lastError = String((e && e.message) || e)
      return false
    }
  }

  async function pollWatch() {
    if (!watch.running) return
    try {
      const r = await side.call('/watch/events', { after: watch.lastEventId, limit: 10 }, 8000)
      watch.stats.polls += 1
      const evs = arr(r.events)
      for (const ev of evs) {
        if (typeof ev.seq === 'number') {
          if (watch.injectedSeqs.has(ev.seq)) continue
          watch.injectedSeqs.add(ev.seq)
          watch.lastEventId = Math.max(watch.lastEventId, ev.seq)
        }
        watch.stats.events += 1
        let text = watchText(ev)
        if (watch.cfg && watch.cfg.autoUnderstand === true && ev.frame) {
          try {
            const u = await side.call('/understand', { frame: ev.frame, backend: 'local' }, 120000)
            const scene = String((u && u.scene) || '')
            const summary = String((u && u.summary) || '')
            if (scene || summary) text += `\n画面：${scene}${scene && summary ? '｜' : ''}${summary}`
          } catch (e) {
            watch.lastError = String((e && e.message) || e)
          }
        }
        await injectWatch(text)
      }
    } catch (e) {
      watch.lastError = String((e && e.message) || e)
    }
  }

  function renderWatch(d) {
    const cfg = (d && d.cfg) || {}
    const stats = (d && d.stats) || {}
    const head = [
      d && d.running ? '余光 运行中' : '余光 已停止',
      `采样 ${cfg.intervalMs ?? '?'}ms`,
      `阈值 ${cfg.threshold ?? '?'}`,
      `冷却 ${cfg.cooldownMs ?? '?'}ms`,
      `配额 ${d?.perHour ?? 0}/${cfg.maxPerHour ?? '?'}/h`,
      `textTrack ${cfg.textTrack === false ? 'off' : 'on'}`,
      `档位 ${d?.profile || cfg.profile || 'standard'}`,
    ].join('｜')
    const lines = [head]
    if (cfg.windowMatch) lines.push(`只盯窗口：${cfg.windowMatch}`)
    if (cfg.quietUntil) lines.push(`静默到：${new Date(cfg.quietUntil).toLocaleString('zh-CN')}`)
    lines.push(
      `事件：累计 ${stats.events ?? 0}｜队列 ${d?.queued ?? 0}｜最近 score ${d?.lastScore ?? 0}（${d?.lastChanged ?? 0} 块）`,
    )
    const st = stats || {}
    lines.push(
      `过滤：静止 ${st.droppedStatic ?? 0}｜冷却 ${st.droppedCooldown ?? 0}｜配额 ${st.droppedQuota ?? 0}｜窗口 ${st.droppedWindow ?? 0}｜静默 ${st.droppedQuiet ?? 0}`,
    )
    const ts = (d && d.textStats) || {}
    if (ts.ocrRuns || ts.add || ts.remove || ts.update || ts.skippedLarge) {
      lines.push(`文字：+${ts.add ?? 0} / -${ts.remove ?? 0} / ~${ts.update ?? 0}（OCR ${ts.ocrRuns ?? 0} 次，大区域跳过 ${ts.skippedLarge ?? 0}）`)
    }
    if (watch.stats.injected || watch.stats.failed || watch.stats.skipped) {
      lines.push(`注入：成功 ${watch.stats.injected}｜失败 ${watch.stats.failed}｜跳过 ${watch.stats.skipped}`)
    }
    if (watch.lastError) lines.push(`⚠ 最近错误：${watch.lastError}`)
    return lines.join('\n')
  }

  // ---------------- 伴侣引擎（v0.15.1：追剧 + 游戏代打状态机） ----------------
  const companion = {
    running: false,
    polling: false,
    mode: 'passive',
    timer: null,
    agent: null,
    sessionId: null,
    lastSeq: 0,
    injectedSeqs: new Set(),
    stats: { polls: 0, events: 0, injected: 0, skipped: 0, failed: 0, transientRetries: 0 },
    consecutiveErrors: 0,
    lastError: '',
  }

  function stopCompanionTimer() {
    if (companion.timer) {
      clearInterval(companion.timer)
      companion.timer = null
    }
    companion.running = false
    companion.polling = false
  }

  ctx.effect(() => () => stopCompanionTimer())

  async function companionAgent() {
    if (companion.agent && typeof companion.agent.followup === 'function') return companion.agent
    try {
      const sc = serviceOf('sessionController')
      if (sc && typeof sc.resolveAgent === 'function' && companion.sessionId) {
        const r = await sc.resolveAgent(companion.sessionId)
        if (r && r.agent) {
          companion.agent = r.agent
          return r.agent
        }
      }
    } catch (e) {
      companion.lastError = String((e && e.message) || e)
    }
    return null
  }

  async function injectCompanionEvent(ev) {
    const agent = await companionAgent()
    if (!agent) {
      companion.stats.skipped += 1
      return false
    }
    const text = formatCompanionEvent(ev)
    if (isSilentReply(text)) {
      companion.stats.skipped += 1
      return false
    }
    const message = {
      id: randomUUID(),
      role: 'user',
      content: [{ type: 'text', text }],
      source: {
        kind: 'omnivision-companion',
        mode: ev?.mode || companion.mode,
        reasoningEffort: ev?.reasoningEffort,
      },
    }
    try {
      await agent.followup(message)
      companion.stats.injected += 1
      try {
        const sessions = serviceOf('sessions')
        if (sessions && typeof sessions.flush === 'function') await sessions.flush(agent.session)
      } catch {}
      return true
    } catch (e) {
      companion.stats.failed += 1
      companion.lastError = String((e && e.message) || e)
      return false
    }
  }

  async function pollCompanion() {
    if (!companion.running || companion.polling) return
    companion.polling = true
    try {
      await side.ensure()
      const r = await side.call('/companion/events', { after: companion.lastSeq, limit: 5 }, 8000)
      companion.stats.polls += 1
      companion.consecutiveErrors = 0
      companion.lastError = ''
      const evs = Array.isArray(r?.events) ? r.events : []
      for (const ev of evs) {
        if (!companion.running) break
        if (typeof ev.seq === 'number') {
          if (companion.injectedSeqs.has(ev.seq)) continue
          companion.injectedSeqs.add(ev.seq)
          companion.lastSeq = Math.max(companion.lastSeq, ev.seq)
        }
        companion.stats.events += 1
        await injectCompanionEvent(ev)
      }
    } catch (e) {
      const msg = String((e && e.message) || e)
      companion.consecutiveErrors += 1
      const isTransient = /fetch failed|ECONNREFUSED|ECONNRESET|UND_ERR_SOCKET|aborted|timeout/i.test(msg)
      if (isTransient && companion.consecutiveErrors < 3) {
        companion.stats.transientRetries += 1
      } else {
        companion.lastError = msg
      }
    } finally {
      companion.polling = false
    }
  }

  function startCompanionPoller(seq0 = 0) {
    stopCompanionTimer()
    companion.lastSeq = Number.isFinite(Number(seq0)) ? Number(seq0) : 0
    companion.injectedSeqs = new Set()
    companion.consecutiveErrors = 0
    companion.lastError = ''
    companion.running = true
    companion.timer = setInterval(() => { void pollCompanion() }, 1000)
    if (companion.timer && typeof companion.timer.unref === 'function') companion.timer.unref()
  }

  function renderCompanionStatus(d) {
    const b = d?.budget || {}
    const drv = d?.driver || {}
    const aud = d?.audio || {}
    const rfx = d?.reflex || {}
    const lines = [
      `伴侣模式：${d?.mode || 'passive'}｜窗口锁：${d?.lockedTitle || d?.windowMatch || '未锁定'}｜推荐 reasoning_effort=${d?.recommendedReasoningEffort ?? 10}`,
      `预算熔断：本小时 ${b.usedThisHour ?? 0}/${b.maxPerHour ?? 120} 帧（约 ${b.estTokensThisHour ?? 0} tokens，熔断跳过 ${b.budgetSkips ?? 0}）`,
      `采集：${d?.sensor?.backend || 'pil'}（静帧跳过 ${d?.sensor?.staticSkips ?? 0}）｜音频：${aud.running ? `运行中(${aud.asrEngine})` : '未运行'}`,
      `游戏驱动：${drv.emergencyStopped ? '⛔ 已急停（发 /omnivision reset 复位）' : '就绪'}${drv.dryRun ? ' [dryRun]' : ''}｜快反射：${rfx.running ? `${rfx.preset}(${rfx.reflexMode}, ${rfx.dryRun ? 'dryRun仅记录' : 'LIVE实操'}, 触发${rfx.triggers}次)` : '未运行'}`,
    ]
    if (d?.bargeInPaused) lines.push('⏸ 当前处于 Barge-in 暂停状态（用户插话优先）')
    if (d?.reflexWarning) lines.push(`⚠ 快反射警告：${d.reflexWarning}`)
    if (companion.lastError) lines.push(`⚠ 最近错误：${companion.lastError}`)
    return lines.join('\n')
  }

  ctx.inject(['tools'], (toolsCtx) => {
    const tools = toolsCtx.tools

    tools.register({
      name: 'vision_look',
      description:
        '通用视觉层：截一帧，返回「场景 + 带坐标的文字元素清单 + 变化区域 + 截图路径」。' +
        '第一次看某界面要跑 OCR（全屏约 2 秒），同一界面再看不花 OCR、几十毫秒（cached）。' +
        '元素带 center，可直接喂 cv_act / coach_click 点；要真看图就用 read_image 读返回的 frame 路径。' +
        'region 默认整屏，也可指定窗口关键词或矩形。',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          region: { type: 'string', description: '看哪儿：full（默认整屏）/ smart（推荐：前台窗口，取不到回退全屏）/ foreground / window:关键词 / "x,y,w,h"' },
          rect: { type: 'array', items: { type: 'number' }, description: '直接给绝对矩形 [x,y,w,h]，与 region 二选一' },
          ocr: { type: 'boolean', description: '是否跑文字识别，默认 true；只想看变化区域时传 false 更快' },
          icons: { type: 'boolean', description: '补「可点控件」候选：OmniParser icon_detect（纯 ONNX，约 70-100ms），默认 false' },
          iconEngine: { type: 'string', description: '图标引擎：auto（默认，模型优先）/ model（只用模型）/ opencv（只用轮廓兜底）' },
          maxElements: { type: 'number', description: `最多返回几个元素，默认 ${DEFAULTS.maxElements}` },
          minScore: { type: 'number', description: `识别置信度下限，默认 ${DEFAULTS.minScore}` },
          saveFrame: { type: 'boolean', description: '是否把截图存成 jpg 供 read_image 看，默认 true' },
          markIds: { type: 'boolean', description: '另存一张带 id 标注的截图（Set-of-Mark，红框+白底黑字 id），原帧不动；默认 false' },
          sceneHint: { type: 'string', description: '手动指定场景名（跳过自动判别）' },
        },
      },
      output: LOOK_OUTPUT,
      async execute(args) {
        const a = args && typeof args === 'object' ? args : {}
        const body = {
          region: Array.isArray(a.rect) && a.rect.length === 4 ? a.rect : a.region ?? 'full',
          ocr: a.ocr !== false,
          icons: a.icons === true,
          iconEngine: a.iconEngine ? String(a.iconEngine) : 'auto',
          maxElements: Number.isFinite(a.maxElements) ? a.maxElements : DEFAULTS.maxElements,
          minScore: Number.isFinite(a.minScore) ? a.minScore : DEFAULTS.minScore,
          saveFrame: a.saveFrame !== false,
        }
        if (a.markIds === true) body.markIds = true
        if (a.sceneHint) body.sceneHint = String(a.sceneHint)
        const d = await side.call('/look', body)
        return { text: renderLook(a, d), data: d }
      },
    })

    tools.register({
      name: 'vision_understand',
      description:
        '语义层：先 vision_look 拿元素，再把「缩略截图 + 元素清单」发给 qwen4-max，让它对照截图纠正 OCR 错字、判场景、写一句概括。' +
        '会真的发一次网络请求（默认只在显式调用这个工具时才发）。同一界面第二次走语义缓存、0 请求。' +
        '失败会降级成原始元素并标 degraded，不阻塞。',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          region: { type: 'string', description: '看哪儿：full（默认整屏）/ smart（推荐：前台窗口，取不到回退全屏）/ foreground / window:关键词 / "x,y,w,h"' },
          rect: { type: 'array', items: { type: 'number' }, description: '绝对矩形 [x,y,w,h]' },
          question: { type: 'string', description: '看图问答：传了它就走问答模式（只支持 cloud），返回自然语言回答；不传则保持原来的清洗行为，一字不变' },
          frame: { type: 'string', description: '用一张已存在的截图做理解或问答（绝对路径），不截屏；复核历史帧/不想动屏幕时用' },
          maxElements: { type: 'number', description: '送进模型的元素上限，默认 40（越多越贵）' },
          minScore: { type: 'number', description: 'OCR 置信度下限，默认 0.5' },
          icons: { type: 'boolean', description: '是否带上无字图标候选，默认 false' },
          model: { type: 'string', description: 'cloud 后端模型名，默认 qwen4-max（或环境变量 DSH_OMNIVISION_MODEL）' },
          backend: { type: 'string', description: '语义后端：local（默认，本地 llama-server）/ cloud（百炼，要 key）/ off（只 OCR 不联网）' },
          thumbWidth: { type: 'number', description: '送进模型的截图宽度，默认 1024；小字要看清就调大（1280~1600），省钱就调小（640~800）' },
          timeoutS: { type: 'number', description: '单次请求超时秒数，默认 90' },
        },
      },
      output: LOOK_OUTPUT,
      async execute(args) {
        const a = args && typeof args === 'object' ? args : {}
        const body = {
          region: Array.isArray(a.rect) && a.rect.length === 4 ? a.rect : a.region ?? 'full',
          maxElements: Number.isFinite(a.maxElements) ? a.maxElements : DEFAULTS.maxElements,
          minScore: Number.isFinite(a.minScore) ? a.minScore : DEFAULTS.minScore,
          icons: a.icons === true,
        }
        if (a.model) body.model = String(a.model)
        if (a.backend) body.backend = String(a.backend)
        else body.backend = runtime.profile === 'energy' ? 'off' : runtime.profile === 'performance' ? 'cloud' : 'local'
        if (a.frame) body.frame = String(a.frame)
        if (a.question) body.question = String(a.question)
        if (Number.isFinite(a.thumbWidth)) body.thumbWidth = a.thumbWidth
        if (Number.isFinite(a.timeoutS)) body.timeoutS = a.timeoutS
        const d = await side.call('/understand', body, 240000)
        return { text: d.mode === 'ask' ? renderAsk(a, d) : renderUnderstand(a, d), data: d }
      },
    })

    tools.register({
      name: 'ui_tree',
      description:
        '结构层：读 Windows UI Automation 树，拿到「控件名 + 角色 + 坐标 + enabled + selected」——纯本机、零模型、约 50–270ms。' +
        '和 vision_look 的关系：UIA 有树就用它（可交互控件准得快），没树（如桌宠窗口）自动回退 OCR 并标 source:"ocr"。' +
        'enabled=false 能直接读（禁用按钮）；selected 读不到时是 null（未知），别当 false。',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          region: { type: 'string', description: 'foreground（默认，前台窗口）/ smart（推荐：前台窗口，取不到回退全屏）/ window:关键词 / full（所有可见窗口，预算共享）' },
          depth: { type: 'number', description: '遍历深度，默认 12（实测 Edge 12 层 344 节点/301ms；浅层树用 8 更快）' },
          filter: { type: 'string', description: 'interactive（默认，只收可交互控件）/ all（全部节点）' },
          budgetMs: { type: 'number', description: '时间预算，默认 300ms；超了就返回已读到的部分并标 truncated' },
          withOcr: { type: 'boolean', description: '是否用 OCR 补 UIA 没覆盖的文字（会多花 1–2s），默认 false' },
        },
      },
      output: LOOK_OUTPUT,
      async execute(args) {
        const a = args && typeof args === 'object' ? args : {}
        const body = {
          region: a.region ? String(a.region) : 'foreground',
          filter: a.filter ? String(a.filter) : 'interactive',
        }
        if (Number.isFinite(a.depth)) body.depth = a.depth
        if (Number.isFinite(a.budgetMs)) body.budgetMs = a.budgetMs
        if (a.withOcr === true) body.withOcr = true
        const d = await side.call('/ui_tree', body, 20000)
        return { text: renderUiTree(a, d), data: d }
      },
    })

    // ---------------- 动作层：定位 → 执行 → 验证 ----------------
    function renderAction(d) {
      if (d && d.batch && Array.isArray(d.steps)) {
        const lines = [`连招 ${d.steps.length} 步｜${d.ok ? '全部完成' : `失败在第 ${d.failedAt + 1} 步`}｜${d.elapsedMs ?? '?'}ms`]
        for (const st of d.steps) {
          const mark = st.ok && st.changed !== false ? '✓' : '✗'
          lines.push(
            ` ${mark} [${st.index}] ${st.action}${st.changed === true ? ' (changed)' : ''}${st.retried ? ' (重试过)' : ''}` +
              (st.note ? `：${st.note}` : '') +
              (st.elapsedMs != null ? `（${st.elapsedMs}ms）` : ''),
          )
        }
        if (d.failedAt != null && d.failedAt === d.steps.length - 1) {
          lines.push(`已停止：第 ${d.failedAt + 1} 步失败（继续后续加 continueOnError:true）`)
        }
        return lines.join('\n')
      }
      if (!d || d.ok === false) return `动作失败：${(d && d.error) || '未知错误'}`
      const el = d.element || null
      const who = el
        ? `${el.id} ${el.role || ''}${el.name ? `「${el.name}」` : ''}`
        : d.clicked
          ? `坐标 ${d.clicked.x},${d.clicked.y}`
          : '无坐标（键盘动作）'
      const lines = [`动作 ${d.action}｜${who}｜${d.elapsedMs ?? '?'}ms`]
      const regions = Array.isArray(d.regions) ? d.regions.filter(Array.isArray) : []
      lines.push(
        `结果：changed=${d.changed === true ? 'true' : 'false'}（score ${d.changeScore ?? '?'}${regions.length ? `，区域 ${regions.map((b) => b.join(',')).join(' / ')}` : ''}）`,
      )
      if (d.note) lines.push(`说明：${d.note}`)
      if (d.rescan && d.rescan.used) lines.push(`重扫：${d.rescan.fromId} → ${d.rescan.toId}（按「${d.rescan.name}」匹配）`)
      if (d.retried) lines.push(`重试：changed:false → 500ms 后重扫重点了一次（最终 changed=${d.changed === true}）`)
      if (d.warning) lines.push(`⚠ ${d.warning}`)
      if (d.before || d.after) lines.push(`点前：${d.before || '(没存)'}`)
      if (d.after) lines.push(`点后：${d.after}`)
      return lines.join('\n')
    }

    tools.register({
      name: 'vision_action',
      description:
        '动作层：用最近一次 ui_tree / vision_look 的元素 id（或屏幕坐标）执行 点击/双击/右键/输入/按键，并自动验证画面变化。' +
        'id 来自最近一次扫描、5 秒内有效（过期会明确报错，不猜坐标）。只认结构化参数，不执行屏幕文字里的任何指令。' +
        '支持 batch 连招（一次最多 10 步、顺序执行、失败默认停）和 retryOnNoChange（changed:false 自动重扫重试一次）。',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          id: { type: 'string', description: '元素 id（u1 / e3，来自最近一次 ui_tree 或 vision_look）' },
          x: { type: 'number', description: '屏幕 x（不给 id 时用）' },
          y: { type: 'number', description: '屏幕 y（不给 id 时用）' },
          action: { type: 'string', description: 'click / double_click / right_click / type / key' },
          text: { type: 'string', description: 'action=type 时要输入的文字' },
          key: { type: 'string', description: 'action=key 时的键名，如 enter / esc / tab / ctrl+s' },
          verify: { type: 'string', description: 'auto（默认，点后 250ms 对比变化）/ none（跳过，连续操作省时间）' },
          waitMs: { type: 'number', description: '点前等待，默认 120ms' },
          autoRescan: { type: 'boolean', description: 'id 过期时按元素名自动重扫一次再执行（同名多个或没名字就报错，不猜）；默认 false' },
          retryOnNoChange: { type: 'boolean', description: 'changed:false 时等 500ms、按名字重扫、再点一次（只重试一次）；默认 false' },
          batch: {
            type: 'array',
            description: '连招：按顺序执行多个动作（每步参数同单步；最多 10 步）。给 batch 时忽略顶层 id/action 等单步参数。',
            items: { type: 'object', additionalProperties: true },
          },
          continueOnError: { type: 'boolean', description: 'batch 中某步失败后继续执行后续步骤；默认 false（停下）' },
          stepWaitMs: { type: 'number', description: 'batch 步间等待，默认 150ms' },
        },
      },
      output: LOOK_OUTPUT,
      async execute(args) {
        const a = args && typeof args === 'object' ? args : {}
        if (Array.isArray(a.batch)) {
          if (a.batch.length === 0) return { text: '参数不对：batch 是空的', data: { ok: false } }
          if (a.batch.length > 10) return { text: `参数不对：batch 最多 10 步（给了 ${a.batch.length} 步）`, data: { ok: false } }
          const body = { batch: a.batch }
          if (a.continueOnError === true) body.continueOnError = true
          if (Number.isFinite(a.stepWaitMs)) body.stepWaitMs = a.stepWaitMs
          const d = await side.call('/act', body, 60000, { allowNotOk: true })
          return { text: renderAction(d), data: d }
        }
        const action = String(a.action || '').trim().toLowerCase()
        const allowed = ['click', 'double_click', 'right_click', 'type', 'key']
        if (!allowed.includes(action)) {
          return { text: `参数不对：action 只能是 ${allowed.join(' / ')}`, data: { ok: false } }
        }
        const hasId = typeof a.id === 'string' && a.id.trim() !== ''
        const hasXY = Number.isFinite(a.x) && Number.isFinite(a.y)
        if (!hasId && !hasXY && action !== 'key') {
          return { text: '参数不对：要么给 id（来自最近一次扫描），要么给 x/y；action=key 可以都不给', data: { ok: false } }
        }
        if (action === 'type' && (typeof a.text !== 'string' || a.text === '')) {
          return { text: '参数不对：action=type 需要 text', data: { ok: false } }
        }
        if (action === 'key' && (typeof a.key !== 'string' || a.key.trim() === '')) {
          return { text: '参数不对：action=key 需要 key（如 enter / esc / ctrl+s）', data: { ok: false } }
        }
        const body = { action }
        if (hasId) body.id = a.id.trim()
        if (hasXY) {
          body.x = Math.round(a.x)
          body.y = Math.round(a.y)
        }
        if (a.text !== undefined) body.text = String(a.text)
        if (a.key !== undefined) body.key = String(a.key)
        if (a.verify) body.verify = String(a.verify)
        if (Number.isFinite(a.waitMs)) body.waitMs = a.waitMs
        if (a.autoRescan === true) body.autoRescan = true
        if (a.retryOnNoChange === true) body.retryOnNoChange = true
        const d = await side.call('/act', body, 30000)
        return { text: renderAction(d), data: d }
      },
    })

    tools.register({
      name: 'vision_watch',
      description:
        '余光：后台盯屏幕变化（纯差分，不调模型），变了就往当前会话投一条 [余光] 消息（agent.followup）。' +
        '默认关闭；静止屏幕零事件。刹车：冷却 cooldownMs、每小时上限 maxPerHour、只看指定窗口 windowMatch、静默到 quietUntil。',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          action: { type: 'string', description: 'start / stop / status / snap（默认 status）' },
          intervalMs: { type: 'number', description: '采样间隔，默认 2000ms（300–60000）' },
          threshold: { type: 'number', description: '变化比例阈值，默认 0.02（至少 2% 的块变了才算；另有≥8 块的最小变化量）' },
          cooldownMs: { type: 'number', description: '两条注入的最小间隔，默认 45000ms' },
          maxPerHour: { type: 'number', description: '每小时最多注入几条，默认 40' },
          windowMatch: { type: 'string', description: '只盯标题包含此关键词的窗口（空=任意）' },
          quietUntil: { type: 'string', description: '静默到这个时刻：Unix 毫秒，或 "23:00" 这种写法' },
          autoUnderstand: { type: 'boolean', description: '每次变化追加 vision_understand 摘要（默认 false，费 token）' },
          textTrack: { type: 'boolean', description: '变化时对变化区域做一次 OCR，报「哪行字变了」；默认 true（textTrack:false 退化成纯像素事件）' },
          profile: { type: 'string', description: '档位：energy / standard / performance（不传沿用当前档位；显式给的单项参数优先于档位）' },
        },
      },
      output: LOOK_OUTPUT,
      async execute(args, run) {
        const a = args && typeof args === 'object' ? args : {}
        const action = String(a.action || 'status').toLowerCase()
        if (action === 'start') {
          watch.agent = (run && run.agent) || null
          const sess = watch.agent && watch.agent.session
          watch.sessionId = (sess && ((sess.header && sess.header.id) || sess.id)) || null
          const body = {}
          for (const k of ['intervalMs', 'threshold', 'cooldownMs', 'maxPerHour', 'windowMatch']) {
            if (a[k] !== undefined && a[k] !== null && a[k] !== '') body[k] = a[k]
          }
          if (a.textTrack === false) body.textTrack = false
          if (a.profile) body.profile = String(a.profile)
          const quiet = parseWhen(a.quietUntil)
          if (quiet !== undefined) body.quietUntil = quiet
          const st = await side.call('/watch/start', body, 15000)
          watch.cfg = Object.assign({}, st.cfg || {}, { autoUnderstand: a.autoUnderstand === true })
          // 起点必须是「当前队列末尾」，否则 stop→start 会把旧事件再注入一遍。
          let baseSeq = Number(st.seq)
          if (!Number.isFinite(baseSeq)) {
            const s0 = await side.call('/watch/status', {}, 8000)
            baseSeq = Number(s0.seq)
          }
          watch.lastEventId = Number.isFinite(baseSeq) ? baseSeq : 0
          watch.injectedSeqs = new Set()
          watch.startedAt = Date.now()
          watch.stats = { polls: 0, events: 0, injected: 0, failed: 0, skipped: 0 }
          watch.lastError = ''
          stopWatchTimer()
          watch.running = true
          const pollMs = Math.max(300, Math.min(1000, Number(watch.cfg.intervalMs) || 2000))
          watch.timer = setInterval(() => { void pollWatch() }, pollMs)
          if (watch.timer && typeof watch.timer.unref === 'function') watch.timer.unref()
          const status = await side.call('/watch/status', {}, 8000)
          return { text: renderWatch(status), data: status }
        }
        if (action === 'stop') {
          stopWatchTimer()
          const st = await side.call('/watch/stop', {}, 8000)
          const status = await side.call('/watch/status', {}, 8000)
          return { text: renderWatch(status), data: status }
        }
        if (action === 'snap') {
          const d = await side.call('/watch/snap', {}, 15000)
          const lines = [`余光快照｜${d.frame || '(没存)'}`]
          if (d.window) lines.push(`窗口：${d.window}`)
          if (typeof d.score === 'number') lines.push(`相对上一帧：score ${d.score.toFixed(3)}`)
          return { text: lines.join('\n'), data: d }
        }
        const status = await side.call('/watch/status', {}, 8000)
        return { text: renderWatch(status), data: status }
      },
    })

    tools.register({
      name: 'vision_profile',
      description:
        '查看/切换性能档位（energy / standard / performance）。切换会按新线程数重建 OCR 引擎（约 1 秒），不重启 DSH，并写回 omnivision.config.json。',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          action: { type: 'string', description: 'get（默认）/ set' },
          profile: { type: 'string', description: 'energy / standard / performance（action=set 时必填）' },
        },
      },
      output: LOOK_OUTPUT,
      async execute(args) {
        const a = args && typeof args === 'object' ? args : {}
        const action = String(a.action || 'get').toLowerCase()
        if (action === 'set') {
          const pp = String(a.profile || '').toLowerCase()
          if (!['energy', 'standard', 'performance'].includes(pp)) {
            return { text: '档位只能是 energy / standard / performance', data: { ok: false } }
          }
          const d = await applyProfile(pp, { persist: true })
          return { text: renderProfileText(d), data: d }
        }
        const h = await side.call('/health')
        return {
          text: renderProfileText({ ok: true, profile: h.profile, threads: (h.onnx || {}).threads, oldThreads: null, rebuilt: false }),
          data: h,
        }
      },
    })

    tools.register({
      name: 'vision_status',
      description: '看通用视觉层自己的状态：Python/OCR 是否就绪、屏幕尺寸、缓存命中计数、截图目录。排查「为什么没识别出来」先用它。',
      parameters: { type: 'object', additionalProperties: false, properties: {} },
      output: LOOK_OUTPUT,
      async execute() {
        const d = await side.call('/health')
        d.configProfile = readLiveProfile()
        d.appliedProfile = runtime.profile
        d.profileMismatch = d.configProfile !== d.appliedProfile
        let text = renderStatus(d)
        if (d.profileMismatch) text += `\n⚠ 配置档位 ${d.configProfile} ≠ 实际生效 ${d.appliedProfile}（设置页刚改完时会短暂不一致）`
        try {
          const disk = statSync(join(HERE, 'sidecar', 'omnivisiond.py')).mtimeMs / 1000
          d.sidecarCodeMtime = side.codeMtime || 0
          d.diskCodeMtime = Math.round(disk * 10) / 10
          if (d.sidecarCodeMtime && Math.abs(disk - d.sidecarCodeMtime) > 1) {
            d.codeStale = true
            const stale = `\n⚠ sidecar 跑的还是旧代码（进程 ${new Date(d.sidecarCodeMtime * 1000).toLocaleTimeString('zh-CN')}，磁盘 ${new Date(disk * 1000).toLocaleTimeString('zh-CN')}）——重启 DSH 让 Python 换新`
            text += stale
          }
        } catch {}
        return { text, data: d }
      },
    })

    tools.register({
      name: 'companion_mode',
      description:
        '切换全感官伴侣模式（适配 DeepSeek-V4.1-Flash 原生多模态）：passive（待机）/ watch（陪追剧：2x2宫格+系统音频转写）/ game_turn（慢节奏回合/卡牌代打）/ game_action（动作游戏快慢双系统）。',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          mode: { type: 'string', description: 'passive / watch / game_turn / game_action' },
          windowMatch: { type: 'string', description: '目标窗口标题关键词（隐私与安全白名单锁定，强烈建议填写）' },
          maxFramesPerHour: { type: 'number', description: '每小时最大推帧预算上限，默认 120' },
          sheetIntervalS: { type: 'number', description: '追剧拼图推送间隔（秒），默认 14' },
          diffThreshold: { type: 'number', description: '64x40 帧差阈值，默认 4.5' },
          enableAudio: { type: 'boolean', description: 'watch 模式是否启动 WASAPI 系统内录转写，默认 true' },
          reflexPreset: { type: 'string', description: 'game_action 模式下同时加载的快反射预设 id（如 generic_hp_potion）' },
          reflexMode: { type: 'string', description: '快反射子模式，如 normal / dodge_priority / paused' },
          reflexDryRun: { type: 'boolean', description: '快反射是否干跑（默认 true 仅检测记录不真按键；传 false 才真按键）' },
        },
        required: ['mode'],
      },
      output: LOOK_OUTPUT,
      async execute(args, run) {
        const a = args && typeof args === 'object' ? args : {}
        const mode = String(a.mode || 'passive').toLowerCase()
        if (run?.agent) {
          companion.agent = run.agent
          const sess = run.agent.session
          companion.sessionId = (sess && ((sess.header && sess.header.id) || sess.id)) || companion.sessionId
        }
        const d = await side.call('/companion/mode', a, 20000, { allowNotOk: true })
        if (d?.ok) {
          companion.mode = d.mode || mode
          if (companion.mode === 'passive') {
            stopCompanionTimer()
          } else {
            startCompanionPoller(d.eventSeq || 0)
          }
        }
        return { text: d?.ok ? renderCompanionStatus(d) : `切换模式失败：${d?.error || '未知错误'}`, data: d }
      },
    })

    tools.register({
      name: 'companion_watch_frame',
      description: '立即抓取当前窗口画面拼成 2x2 时间戳宫格图，并附带近期系统音频转写台词（供原生视觉直读，不经过千问/OCR 中间层）。',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          sinceTs: { type: 'number', description: '只返回该 Unix 秒时间戳之后的台词（默认 0 返回最近 12 句）' },
          limit: { type: 'number', description: '最多返回几句台词，默认 12' },
        },
      },
      output: LOOK_OUTPUT,
      async execute(args) {
        const a = args && typeof args === 'object' ? args : {}
        const d = await side.call('/companion/contact_sheet', a, 15000, { allowNotOk: true })
        if (!d?.ok) return { text: `抓帧失败：${d?.error || '未知错误'}`, data: d }
        const lines = [
          `已生成 2x2 宫格图（含 ${d.framesPacked} 帧）：${d.frame}`,
          `窗口：${d.window}｜建议 reasoning_effort=${d.reasoningEffort}`,
        ]
        if (d.transcript) lines.push(`近期台词：\n${d.transcript}`)
        return { text: lines.join('\n'), data: d }
      },
    })

    tools.register({
      name: 'companion_game_act',
      description:
        '游戏专用硬件扫描码执行器（DirectInput Set-1）：支持 tap/hold/mouse_rel/mouse_click/sequence/release_all/emergency_stop/reset_stop。自带前台窗口锁、5秒单键上限、30秒连招上限与全局 F12 急停。仅限单机/允许自动化场景。',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          action: { type: 'string', description: 'tap / hold / mouse_rel / mouse_click / sequence / release_all / emergency_stop / reset_stop' },
          key: { type: 'string', description: '按键名（如 w/a/s/d/space/enter/esc/f1/up）' },
          durationMs: { type: 'number', description: '按住时长毫秒（硬上限 5000ms）' },
          dx: { type: 'number', description: 'mouse_rel 相对水平位移（3D 视角转向）' },
          dy: { type: 'number', description: 'mouse_rel 相对垂直位移' },
          steps: {
            description: 'mouse_rel 时为插值步数（数字）；sequence 时为动作步骤数组（最多 20 步）',
          },
          button: { type: 'string', description: 'mouse_click 键位：left / right / middle' },
          holdMs: { type: 'number', description: 'mouse_click 按下时长毫秒' },
          dryRun: { type: 'boolean', description: '干跑测试模式（不发送真实键鼠事件，返回校验与模拟日志）' },
          reason: { type: 'string', description: 'emergency_stop 触发原因说明' },
        },
        required: ['action'],
      },
      output: LOOK_OUTPUT,
      async execute(args) {
        const a = args && typeof args === 'object' ? args : {}
        const d = await side.call('/companion/game/act', a, 35000, { allowNotOk: true })
        if (!d?.ok) return { text: `游戏操作未执行：${d?.error || '未知错误'}`, data: d }
        const text = `游戏执行 OK｜${d.action || a.action}${d.key ? ` key=${d.key}` : ''}${d.durationMs ? ` ${d.durationMs}ms` : ''}${d.clamped ? ' (已触发5s安全截断)' : ''}${d.dryRun ? ' [dryRun]' : ''}`
        return { text, data: d }
      },
    })

    tools.register({
      name: 'companion_reflex',
      description: '动作游戏本地快系统（20-60 FPS ROI 规则反射层）：list（列出游戏预设）/ start / stop / set_mode / status。',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          action: { type: 'string', description: 'list / start / stop / set_mode / status' },
          preset: { type: 'string', description: '预设 id（如 generic_hp_potion）' },
          mode: { type: 'string', description: '反射子模式（如 normal / dodge_priority / paused）' },
          dryRun: { type: 'boolean', description: '干跑模式（默认 true：只检测并记录触发，不发送真实按键；确认规则无误后传 false 才真按键）' },
        },
        required: ['action'],
      },
      output: LOOK_OUTPUT,
      async execute(args) {
        const a = args && typeof args === 'object' ? args : {}
        const op = String(a.action || 'status').toLowerCase()
        const d = await side.call(`/companion/reflex/${op}`, a, 15000, { allowNotOk: true })
        if (!d?.ok) return { text: `快反射失败：${d?.error || '未知错误'}`, data: d }
        if (op === 'list') {
          const items = (d.presets || []).map((p) => `- ${p.id}: ${p.name} [${p.genre}] (${p.rules} 条规则)`).join('\n')
          return { text: items || '暂无游戏预设（可在 presets/games/ 添加 JSON）', data: d }
        }
        return { text: `快反射状态：${d.running ? '运行中' : '已停止'}｜预设 ${d.preset || '无'}｜模式 ${d.reflexMode || 'normal'}｜${d.dryRun === false ? 'LIVE实操' : 'dryRun仅记录'}｜累计触发 ${d.triggers ?? 0} 次`, data: d }
      },
    })

    tools.register({
      name: 'vision_ocr_crop',
      description: '按需局部高清放大 OCR：只裁剪屏幕矩形 [x,y,w,h] 放大 2 倍后跑 RapidOCR，专用于核对游戏血条数字、技能冷却、极小字幕，避免全屏 OCR 噪声。',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          rect: { type: 'array', items: { type: 'number' }, description: '绝对像素矩形 [x, y, w, h]' },
          scale: { type: 'number', description: '放大倍数（1.0~3.0），默认 2.0' },
          minScore: { type: 'number', description: 'OCR 置信度阈值，默认 0.45' },
        },
        required: ['rect'],
      },
      output: LOOK_OUTPUT,
      async execute(args) {
        const a = args && typeof args === 'object' ? args : {}
        const d = await side.call('/ocr_crop', a, 15000, { allowNotOk: true })
        if (!d?.ok) return { text: `局部 OCR 失败：${d?.error || '未知错误'}`, data: d }
        return { text: `局部 OCR [${(d.rect || []).join(',')}]：${d.text || '(未识别到文字)'}\n裁切图：${d.frame}`, data: d }
      },
    })
  })

  const commands = serviceOf('commands')
  if (commands && typeof commands.register === 'function') {
    const dispose = commands.register({
    name: 'omnivision',
    description: 'OmniVision 全感官伴侣层：档位 / 模式切换(追剧/代打) / 急停与复位 / 余光 / 状态',
    input: { hint: '[mode passive|watch|game_turn|game_action <窗口>] | [stop|reset] | [bargein pause|resume] | [preset list|stop|<id>] | [profile ...] | [watch on|off] | [status]' },
    handler: async (invocation) => {
      const raw = String(invocation?.rawInput ?? '').trim()
      const parts = raw.split(/\s+/).filter(Boolean)
      const sub = (parts[0] || 'status').toLowerCase()
      try {
        if (sub === 'mode') {
          const mode = (parts[1] || 'status').toLowerCase()
          if (mode === 'status') {
            const st = await side.call('/companion/status', {}, 8000)
            return { kind: 'success', text: renderCompanionStatus(st) }
          }
          if (invocation?.agent) {
            companion.agent = invocation.agent
            const sess = invocation.agent.session
            companion.sessionId = (sess && ((sess.header && sess.header.id) || sess.id)) || companion.sessionId
          }
          const windowMatch = parts.slice(2).join(' ').trim()
          const body = { mode }
          if (windowMatch) body.windowMatch = windowMatch
          const d = await side.call('/companion/mode', body, 20000, { allowNotOk: true })
          if (!d?.ok) return { kind: 'error', text: '切换伴侣模式失败：' + (d?.error || '未知错误') }
          companion.mode = d.mode || mode
          if (companion.mode === 'passive') stopCompanionTimer()
          else startCompanionPoller(d.eventSeq || 0)
          return { kind: 'success', text: renderCompanionStatus(d) }
        }
        if (sub === 'stop' || sub === 'estop') {
          stopCompanionTimer()
          const d = await side.call('/companion/game/act', { action: 'emergency_stop', reason: 'SLASH_CMD' }, 8000, { allowNotOk: true })
          await side.call('/companion/mode', { mode: 'passive' }, 8000, { allowNotOk: true }).catch(() => {})
          companion.mode = 'passive'
          return { kind: 'success', text: `⛔ 已触发一键急停：松开所有按键并切回 passive 待机（释放：${(d?.released || []).join(', ') || '无'}）。恢复游戏控制请执行 /omnivision reset` }
        }
        if (sub === 'reset' || sub === 'unlock') {
          const d = await side.call('/companion/game/act', { action: 'reset_stop' }, 8000, { allowNotOk: true })
          return { kind: d?.ok ? 'success' : 'error', text: d?.ok ? '✅ 已解除游戏急停锁定，可重新切入代打模式' : `复位失败：${d?.error || '未知错误'}` }
        }
        if (sub === 'bargein') {
          const op = (parts[1] || 'pause').toLowerCase()
          const dur = Number(parts[2]) || 20
          const d = await side.call('/companion/barge_in', { action: op, durationS: dur }, 8000)
          return { kind: 'success', text: d.paused ? `⏸ 已暂停自动推帧 ${dur} 秒（插话优先）` : '▶ 已恢复自动推帧' }
        }
        if (sub === 'preset') {
          const op = (parts[1] || 'list').toLowerCase()
          if (op === 'list') {
            const d = await side.call('/companion/reflex/list', {}, 8000)
            const txt = (d.presets || []).map((p) => `- ${p.id}: ${p.name} [${p.genre}]`).join('\n') || '暂无预设'
            return { kind: 'success', text: txt }
          }
          if (op === 'stop') {
            await side.call('/companion/reflex/stop', {}, 8000)
            return { kind: 'success', text: '已停止本地快反射引擎' }
          }
          const isLive = parts.slice(2).some((x) => x.toLowerCase() === 'live')
          const rfxMode = parts[2] && !['live', 'dry', 'dryrun'].includes(parts[2].toLowerCase()) ? parts[2] : 'normal'
          const d = await side.call('/companion/reflex/start', { preset: parts[1], mode: rfxMode, dryRun: !isLive }, 10000, { allowNotOk: true })
          return { kind: d?.ok ? 'success' : 'error', text: d?.ok ? `已启动快反射预设：${d.preset} (${d.reflexMode}｜${d.dryRun ? 'dryRun 仅记录，加 live 才真按键' : 'LIVE 实操'})` : `启动预设失败：${d?.error}` }
        }
                if (sub === 'profile') {
          const pp = (parts[1] || '').toLowerCase()
          if (!['energy', 'standard', 'performance'].includes(pp)) {
            return { kind: 'error', text: '用法：/omnivision profile energy|standard|performance' }
          }
          const d = await side.call('/profile', { profile: pp }, 120000)
          if (d.ok) persistProfile(pp)
          return { kind: d.ok ? 'text' : 'error', text: renderProfileText(d) }
        }
        if (sub === 'watch') {
          const op = (parts[1] || 'status').toLowerCase()
          if (op === 'on') {
            if (invocation?.agent) watch.agent = invocation.agent
            const sess = watch.agent && watch.agent.session
            watch.sessionId = (sess && ((sess.header && sess.header.id) || sess.id)) || watch.sessionId
            const st = await side.call('/watch/start', {}, 15000)
            watch.cfg = { ...(st.cfg || {}) }
            const seq0 = Number(st.seq)
            watch.lastEventId = Number.isFinite(seq0) ? seq0 : 0
            watch.injectedSeqs = new Set()
            stopWatchTimer()
            watch.running = true
            const pollMs = Math.max(300, Math.min(1000, Number(watch.cfg.intervalMs) || 2000))
            watch.timer = setInterval(() => { void pollWatch() }, pollMs)
            if (watch.timer && typeof watch.timer.unref === 'function') watch.timer.unref()
          } else if (op === 'off') {
            stopWatchTimer()
            await side.call('/watch/stop', {}, 8000)
          }
          const st = await side.call('/watch/status', {}, 8000)
          return { kind: 'success', text: renderWatch(st) }
        }
        const h = await side.call('/health')
        h.configProfile = readLiveProfile()
        h.appliedProfile = runtime.profile
        h.profileMismatch = h.configProfile !== h.appliedProfile
        return { kind: 'success', text: renderStatus(h) }
      } catch (e) {
        return { kind: 'error', text: 'OmniVision 命令失败：' + String(e?.message ?? e) }
      }
    },
  })
    ctx.effect(() => () => { try { dispose?.() } catch {} })
  }
}
