window.__ModuleLoader__.load({
  id: '@local/dsh-omnivision',
  factory(require) {
    const React = require('react')
    const h = React.createElement
    const { useEffect, useState } = React

    const PROFILES = [
      { id: 'energy', label: '省电', hint: '5s / 文字关 / 1 线程' },
      { id: 'standard', label: '标准', hint: '2s / 文字开 / 4 线程' },
      { id: 'performance', label: '性能', hint: '1s / 文字开 / 全核' },
    ]
    const VALID_PROFILES = ['energy', 'standard', 'performance']

    const MODES = [
      { id: 'passive', label: '待机', hint: '停止后台循环并释放按键' },
      { id: 'watch', label: '陪追剧', hint: '2x2宫格 + WASAPI音频 + [SILENT]低打扰' },
      { id: 'game_turn', label: '慢代打', hint: '回合/卡牌/Galgame 原生直看' },
      { id: 'game_action', label: '动作代打', hint: '快慢双系统 + 硬件扫描码驱动' },
    ]

    function isSilentText(text) {
      const s = String(text || '').trim()
      return !s || /^\[\s*SILENT\s*\]$/i.test(s)
    }

    function parseLiveStateFromText(text) {
      const s = String(text || '')
      const out = {}
      const mApplied = s.match(/实际生效\s+(energy|standard|performance)/i)
      const mCfg = s.match(/档位[：:]\s*(?:配置\s+)?(energy|standard|performance)/i)
      if (mApplied) out.profile = mApplied[1].toLowerCase()
      else if (mCfg) out.profile = mCfg[1].toLowerCase()
      const mMode = s.match(/(?:伴侣底座[：:]\s*模式|伴侣模式[：:])\s*(passive|watch|game_turn|game_action)/i)
      if (mMode) out.mode = mMode[1].toLowerCase()
      return out
    }

    function currentSessionId(ctx) {
      try {
        const sessions = ctx.get && ctx.get('sessions')
        const list = sessions && sessions.list
        const snap = (list && list.getSnapshot && list.getSnapshot()) || (sessions && sessions.getSnapshot && sessions.getSnapshot())
        if (!snap) return null
        if (typeof snap.current === 'string') return snap.current
        if (snap.current && typeof snap.current.id === 'string') return snap.current.id
        if (typeof snap.currentId === 'string') return snap.currentId
        const byId = snap.byId || {}
        const order = Array.isArray(snap.order) ? snap.order : Object.keys(byId)
        return order.length ? order[order.length - 1] : null
      } catch {
        return null
      }
    }

    function DanmakuLayer({ items }) {
      if (!items || !items.length) return null
      return h('div', {
        style: {
          position: 'fixed',
          left: '50%',
          bottom: 72,
          transform: 'translateX(-50%)',
          zIndex: 35,
          pointerEvents: 'none',
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          gap: 6,
          maxWidth: '72vw',
        },
      }, items.map((it) => h('div', {
        key: it.id,
        style: {
          padding: '6px 14px',
          borderRadius: 999,
          background: 'rgba(14, 18, 28, 0.78)',
          color: '#f5f8ff',
          border: '1px solid rgba(130, 180, 255, 0.35)',
          boxShadow: '0 4px 16px rgba(0,0,0,0.35)',
          font: '13px/1.45 Segoe UI, system-ui, sans-serif',
          backdropFilter: 'blur(6px)',
          textAlign: 'center',
        },
      }, it.text)))
    }

    function Panel(props) {
      const ctx = props.ctx
      const [open, setOpen] = useState(false)
      const [busy, setBusy] = useState('')
      const [last, setLast] = useState('')
      const [mode, setMode] = useState('passive')
      const [winKw, setWinKw] = useState('')
      const [danmakuOn, setDanmakuOn] = useState(true)
      const [danmakus, setDanmakus] = useState([])
      const PROFILE_KEY = 'omnivision.profile'
      const [profile, setProfile] = useState('standard')

      function syncProfile(nextVal) {
        if (!VALID_PROFILES.includes(nextVal)) return
        setProfile(nextVal)
        try { localStorage.setItem(PROFILE_KEY, nextVal) } catch {}
      }

      function pushDanmaku(text) {
        if (!danmakuOn || isSilentText(text)) return
        const id = Date.now() + '-' + Math.random().toString(36).slice(2, 6)
        const clean = String(text).trim().slice(0, 90)
        setDanmakus((prev) => [...prev.slice(-2), { id, text: clean }])
        setTimeout(() => {
          setDanmakus((prev) => prev.filter((x) => x.id !== id))
        }, 6500)
      }

      useEffect(() => {
        let alive = true
        async function pull() {
          try {
            const d = await ctx.remote?.settings?.describe?.()
            const list = Array.isArray(d?.namespaces) ? d.namespaces : []
            const ns = list.find((n) => {
              const id = String((n && (n.ns || n.id || n.name)) || '')
              return id === 'omnivision' || id === '@local/dsh-omnivision' || id.endsWith('/omnivision')
            })
            if (ns) {
              const raw = ns.value && ns.value.profile
              const resolved = VALID_PROFILES.includes(String(raw)) ? String(raw) : 'standard'
              if (alive) syncProfile(resolved)
              return
            }
          } catch {}
          if (alive) {
            setProfile('standard')
          }
        }
        void pull()
        let off
        try {
          off = ctx.remote?.$on?.('settings/document-updated', () => { void pull() })
        } catch {}
        return () => {
          alive = false
          try { off?.() } catch {}
        }
      }, [])

      async function run(cmd, label) {
        const sid = currentSessionId(ctx)
        if (!sid) {
          setLast('先在左侧打开一个会话，再点这里')
          return
        }
        setBusy(label)
        setLast('')
        try {
          const r = await ctx.remote?.commands?.execute(sid, cmd, [])
          const res = r && r.result
          const text = typeof r === 'string' ? r : (res && typeof res.text === 'string' ? res.text : (r && (r.text || r.message)) || '')
          const parsed = parseLiveStateFromText(text)
          if (parsed.profile) syncProfile(parsed.profile)
          if (parsed.mode) setMode(parsed.mode)
          const summary = text ? String(text).split('\n').slice(0, 3).join(' · ') : '已执行'
          setLast(summary)
          if (label === 'mode' || label === 'stop') pushDanmaku(summary)
        } catch (e) {
          setLast('执行失败：' + String((e && e.message) || e))
        } finally {
          setBusy('')
        }
      }

      const pillStyle = {
        position: 'fixed', right: 20, bottom: 118, zIndex: 40,
        display: 'flex', alignItems: 'center', gap: 8,
        padding: '6px 12px', borderRadius: 999,
        background: 'var(--dsw-alias-bg-elevated, rgba(20,22,30,.92))',
        color: 'var(--dsw-alias-text-primary, #e8eefc)',
        border: '1px solid var(--dsw-alias-border-subtle, rgba(255,255,255,.16))',
        boxShadow: '0 6px 20px rgba(0,0,0,.28)', cursor: 'pointer',
        font: '12px/1.4 Segoe UI, system-ui, sans-serif', userSelect: 'none',
      }
      const cardStyle = {
        position: 'fixed', right: 20, bottom: 158, zIndex: 40, width: 292,
        background: 'var(--dsw-alias-bg-elevated, rgba(20,22,30,.96))',
        color: 'var(--dsw-alias-text-primary, #e8eefc)',
        border: '1px solid var(--dsw-alias-border-subtle, rgba(255,255,255,.16))',
        borderRadius: 12, padding: 12, boxShadow: '0 10px 30px rgba(0,0,0,.35)',
        font: '12px/1.5 Segoe UI, system-ui, sans-serif',
      }
      const row = { display: 'flex', gap: 6, marginBottom: 8 }
      const btn = (active, danger = false) => ({
        flex: 1, padding: '6px 4px', borderRadius: 8, cursor: 'pointer',
        background: danger
          ? '#c93434'
          : active
            ? 'var(--dsw-alias-bg-accent, #2f6feb)'
            : 'var(--dsw-alias-bg-subtle, rgba(255,255,255,.06))',
        color: '#f5f8ff',
        border: danger ? '1px solid rgba(255,140,140,.45)' : '1px solid var(--dsw-alias-border-subtle, rgba(255,255,255,.16))',
        font: '12px/1.4 Segoe UI, system-ui, sans-serif',
      })

      const dotColor = mode === 'passive' ? '#49c26b' : mode === 'watch' ? '#4fa8ff' : '#f5a623'

      return h(React.Fragment, null,
        danmakuOn && h(DanmakuLayer, { items: danmakus }),
        open && h('div', { style: cardStyle },
          h('div', { style: { display: 'flex', justifyContent: 'space-between', alignItems: 'center', fontWeight: 600, marginBottom: 8 } },
            h('span', null, 'OmniVision v0.15.3 伴侣底座'),
            h('span', { style: { fontSize: 11, opacity: .7 } }, mode),
          ),
          h('div', { style: row }, MODES.map((m) => h('button', {
            key: m.id,
            style: btn(mode === m.id),
            title: m.hint,
            onClick: () => {
              setMode(m.id)
              const suffix = m.id !== 'passive' && winKw.trim() ? ' ' + winKw.trim() : ''
              void run('/omnivision mode ' + m.id + suffix, 'mode')
            },
          }, m.label))),
          h('div', { style: { marginBottom: 8 } },
            h('input', {
              value: winKw,
              onChange: (e) => setWinKw(e.target.value),
              placeholder: '锁定窗口关键词（如 PotPlayer / 游戏名）',
              style: {
                width: '100%', boxSizing: 'border-box', padding: '5px 8px', borderRadius: 6,
                background: 'rgba(255,255,255,.06)', color: '#e8eefc',
                border: '1px solid rgba(255,255,255,.16)', font: '12px/1.4 Segoe UI, system-ui, sans-serif',
              },
            }),
          ),
          h('div', { style: row },
            h('button', {
              style: btn(false, true),
              title: '立即松开所有游戏按键并切回待机（全局热键 F12）',
              onClick: () => {
                setMode('passive')
                void run('/omnivision stop', 'stop')
              },
            }, busy === 'stop' ? '急停中…' : '⛔ 一键急停 (F12)'),
            h('button', {
              style: btn(danmakuOn),
              onClick: () => setDanmakuOn(!danmakuOn),
            }, danmakuOn ? '弹幕层:开' : '弹幕层:关'),
          ),
          h('div', { style: row }, PROFILES.map((p) => h('button', {
            key: p.id, style: btn(profile === p.id), title: p.hint,
            onClick: () => {
              syncProfile(p.id)
              void run('/omnivision profile ' + p.id, 'profile')
            },
          }, p.label))),
          h('div', { style: row },
            h('button', { style: btn(false), onClick: () => void run('/omnivision watch on', 'watch') }, busy === 'watch' ? '…' : '开余光'),
            h('button', { style: btn(false), onClick: () => void run('/omnivision watch off', 'watch') }, '关余光'),
            h('button', { style: btn(false), onClick: () => void run('/omnivision bargein pause 20', 'bargein') }, '插话暂停'),
          ),
          last && h('div', { style: { opacity: .88, whiteSpace: 'pre-wrap', marginBottom: 6 } }, last),
          h('div', { style: { display: 'flex', justifyContent: 'space-between', opacity: .6 } },
            h('span', { onClick: () => void run('/omnivision status', 'status'), style: { cursor: 'pointer' } }, '同步状态'),
            h('span', { onClick: () => void run('/omnivision reset', 'reset'), style: { cursor: 'pointer' } }, '解除急停'),
            h('span', { onClick: () => void run('/omnivision preset list', 'preset'), style: { cursor: 'pointer' } }, '游戏预设'),
          ),
        ),
        h('div', { style: pillStyle, onClick: () => setOpen(!open), title: 'OmniVision 全感官伴侣面板' },
          h('span', { style: { width: 8, height: 8, borderRadius: 4, background: dotColor, display: 'inline-block' } }),
          h('span', null, mode !== 'passive' ? 'OmniVision · ' + mode : 'OmniVision · ' + profile),
        ),
      )
    }

    return {
      inject: ['slots', 'remote', 'remote.commands', 'remote.settings'],
      apply(ctx) {
        ctx.slots.inject('shell.overlay', function* () {
          yield ctx.slots.register({ name: 'shell.overlay', id: 'omnivision-panel', order: 900 }, (props) => h(Panel, { ...props, ctx }))
        })
      },
    }
  },
})
