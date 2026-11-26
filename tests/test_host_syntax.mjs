import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const here = dirname(fileURLToPath(import.meta.url))
const repo = join(here, '..')

// 1. Dynamically import host.js to verify valid ESM syntax and exported helpers
const hostMod = await import(join(repo, 'plugin', 'host.js'))
assert.equal(hostMod.name, 'omnivision')
assert.equal(typeof hostMod.apply, 'function')
assert.equal(hostMod.isSilentReply('[SILENT]'), true)
assert.equal(hostMod.isSilentReply('  [silent]  '), true)
assert.equal(hostMod.isSilentReply(''), true)
assert.equal(hostMod.isSilentReply('哈哈这波太秀了'), false)

const evMsg = hostMod.formatCompanionEvent({
  mode: 'watch',
  window: 'PotPlayer',
  frame: '/tmp/sheet_1.jpg',
  transcript: '[00:01:12] 主角登场了',
  reasoningEffort: 2,
  budgetUsed: 3,
  budgetMax: 120,
})
assert.ok(evMsg.includes('[伴侣/追剧]'))
assert.ok(evMsg.includes('reasoning_effort=2'))
assert.ok(evMsg.includes('[SILENT]'))

// 2. Verify client.js parses cleanly with Function constructor (syntax check)
const clientSrc = readFileSync(join(repo, 'plugin', 'client.js'), 'utf8')
new Function('window', clientSrc)

console.log('ALL JS SYNTAX & HELPER ASSERTIONS PASSED')
