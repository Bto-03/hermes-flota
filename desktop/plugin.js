// Flota for Hermes Desktop and the Webapp: your fleet of machines on a radar.
//
// The Hermes host is the flagship in the centre; every machine in the Flota config is a ship
// around it. Ships under way (online) are in colour, docked ones (asleep) grey, ones raising
// anchor (waking) amber with signal flowing toward them. Each ship carries its crew: the Hermes
// agents that run there, asleep while it is docked, awake when it is under way, working while
// subagents run on it. The side panel lists the flagship's scheduled jobs and which ship each
// one will wake. Click a ship for its details and Wake / Sleep / Power off / Remove.
// Data comes from the plugin's own backend (dashboard/plugin_api.py) through ctx.rest.
//
// Disk plugin rules: plain ESM, no JSX, only these three imports.
import {
  Button, ConfirmDialog, CopyButton, Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader,
  DialogTitle, host, Input, PALETTE_AREA, queryClient, ROUTES_AREA, SegmentedControl, SIDEBAR_NAV_AREA,
  STATUSBAR_AREAS, useQuery
} from '@hermes/plugin-sdk'
import { useEffect, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'flota'
const PATH = '/flota'
const KEY = [ID, 'status']
const SCHEDULE_KEY = [ID, 'schedule']
const POLL_MS = 5000
const SCHEDULE_POLL_MS = 30000
const CREW_CAP = 4 // compact cluster: this many avatars, then +N
const RING_CAP = 12 // expanded ring
const SEAT_GAP = 27 // min distance between two ring seats (an expanded avatar is ~21 wide)
const FLEETS_KEY = [ID, 'fleets']
const NET_KEY = [ID, 'net']
const NET_POLL_MS = 30000
const ROLE_STORE = 'flota.role'
const EMPTY_SCHEDULE = 'No scheduled jobs. Ask Hermes to schedule one (e.g. "every night at 2 run the backup on escritorio") and Flota will wake that machine for it.'
const SETUP_HINT = 'hermes flota setup <name> --host <ip> --user <user>'
const BACKEND_OFF = 'Enable the Flota plugin on this Hermes (Plugins) and restart its gateway.'
const UPDATE_NOTE = 'This Flota backend is older and cannot do this yet. Update the Flota plugin on this Hermes and restart its gateway.'
const MOOD_TEXT = { asleep: 'asleep', awake: 'awake', working: 'working', waking: 'waking up' }
const MACHINE_TIP = 'Machine registered in this fleet'
const NO_BOTS = 'no bots on this machine'
const PLUGINS_PAGE = '/capabilities?tab=plugins&plugin=flota'

/** A ship's hostname when it says more than its fleet name: 'desk-pc' for 'escritorio'. */
export function hostLabel(m) {
  const h = String((m && m.hostname) || '').trim()
  return h && h.toLowerCase() !== String((m && m.name) || '').toLowerCase() ? h : ''
}

// --- pure helpers (exported for tests) -------------------------------------------------------

/** 'up' | 'waking' | 'down' for one /status row. */
export function nodeState(m) {
  if (m.waking) return 'waking'
  return m.up ? 'up' : 'down'
}

/** Which glyph to draw: 'laptop' | 'server' | 'desktop', from the description, name or OS. */
export function machineKind(m) {
  const text = `${m.description || ''} ${m.name || ''}`.toLowerCase()
  if (/laptop|notebook|port[aá]til|macbook|thinkpad|xps|surface/.test(text)) return 'laptop'
  if (/server|servidor|\bnas\b|rack|proxmox|\bnuc\b|raspberry|\bpi\b|homelab|vps/.test(text)) return 'server'
  if (/desktop|\bpc\b|tower|torre|workstation|sobremesa|gaming|imac/.test(text)) return 'desktop'
  return (m.os || '').toLowerCase() === 'macos' ? 'laptop' : 'desktop'
}

/** Short status shown under a ship. */
export function statusLabel(m) {
  const state = nodeState(m)
  if (state === 'waking') return 'Raising anchor…'
  if (state === 'up') return m.busy > 0 ? `Under way · ${m.busy} working` : 'Under way'
  if (m.wake_error) return 'Wake failed'
  return m.wake === 'none' ? 'Docked · offline' : 'Docked · asleep'
}

/** The crew of a ship: its Hermes agents (the profiles whose terminal is this machine; at least
 *  flota-<name>) plus one member per subagent running there. Agents sleep while the ship is
 *  docked, blink awake while it wakes, are awake while it is under way; running subagents work.
 *  Returns { mood, members: [{ name, role, mood }] (at most `cap`), extra, total, working }. */
export function crew(m, cap = CREW_CAP) {
  const state = nodeState(m)
  const mood = state === 'down' ? 'asleep' : state === 'waking' ? 'waking' : 'awake'
  // Newer backends send the real crew: every bot (profile) that lives on the machine, with avatar.
  // Flota's own SSH access profile (ship_hand) is plumbing, not a bot: an empty list stays empty.
  if (Array.isArray(m.crew) && (m.crew.length || m.crew_known !== false)) {
    const all = botsOf(m.crew, state)
    const members = all.slice(0, cap)
    const working = all.filter(b => b.mood === 'working').length
    return { mood, members, hidden: all.slice(members.length), extra: all.length - members.length, total: all.length, working, avatars: true, all }
  }
  const names = Array.isArray(m.agents) && m.agents.length ? m.agents : [m.profile || `flota-${m.name}`]
  const busy = Math.max(0, Number(m.busy) || 0)
  const agents = names.map(name => ({ name, role: 'agent', mood }))
  const subs = Array.from({ length: busy }, (_, i) => ({ name: `subagent ${i + 1}`, role: 'subagent', mood: state === 'up' ? 'working' : mood }))
  // The ship's own agent first, then the workers, then any other agents: a capped row still shows both kinds.
  const all = [...agents.slice(0, 1), ...subs, ...agents.slice(1)]
  const members = all.slice(0, cap)
  return { mood, members, hidden: all.slice(cap), extra: all.length - members.length, total: all.length, working: state === 'up' ? busy : 0, avatars: false, all }
}

/** Backend bots -> crew members. A bot whose machine is raising anchor blinks awake. */
export function botsOf(list, shipState = 'up') {
  return (Array.isArray(list) ? list : []).filter(b => b && (b.name || b.id) && !b.ship_hand).map(b => {
    const raw = String(b.state || '').toLowerCase()
    let mood = raw === 'working' ? 'working' : raw === 'awake' ? 'awake' : 'asleep'
    if (mood === 'asleep' && shipState === 'waking') mood = 'waking'
    return { id: String(b.id || b.name), name: String(b.name || b.id), role: 'bot', mood,
      avatar: typeof b.avatar === 'string' && /^(data:image\/|https?:)/.test(b.avatar) ? b.avatar : null, color: b.color || null,
      remote: b.remote === true, machine: b.machine || null, hostname: b.hostname || null, profile: b.profile || null }
  })
}

/** A bot's identity colour: its own, or a hue derived from its name (stable across reloads). */
export function botColor(bot) {
  if (bot && bot.color) return bot.color
  let h = 7
  for (const ch of String((bot && (bot.name || bot.id)) || '?')) h = (h * 31 + ch.codePointAt(0)) >>> 0
  return `oklch(0.72 0.13 ${h % 360})`
}

/** First letter or digit of a name, upper-cased: 'flota-torre' -> 'F', 'éclair' -> 'É'. */
export function botInitial(name) {
  const m = /[\p{L}\p{N}]/u.exec(String(name || ''))
  return m ? m[0].toUpperCase() : '?'
}

/** Angles for n seats on a ring around a node, leaving a gap of `gap` radians centred on
 *  `gapAt` (where the node's label is). Seats spread a quarter turn apart at most, centred opposite
 *  the gap; a big crew fills the whole arc. */
export function ringAngles(n, gapAt = Math.PI / 2, gap = 1.2) {
  if (n <= 0) return []
  const arc = 2 * Math.PI - gap
  const step = n === 1 ? 0 : Math.min(arc / (n - 1), (2 * Math.PI) / Math.max(n, 4))
  const mid = gapAt + Math.PI
  return Array.from({ length: n }, (_, i) => mid + (i - (n - 1) / 2) * step)
}

/** Where a node's crew sits. Compact: a little overlapping stack in a row at (x0.., y); expanded:
 *  a ring of radius R around the node. Returns { compact: [{x,y}], ring: [{x,y}], R, shown }. */
export function crewSeats({ count, r, rowY, rowX = 0, anchor = 'middle', edge = 0, labelAbove = false, step = 12, hub = false }) {
  const shown = Math.min(count, RING_CAP)
  const vis = Math.min(count, CREW_CAP)
  const width = (vis - 1) * step + (count > vis ? 20 : 0)
  const x0 = rowX + (anchor === 'start' ? -edge + 8 : anchor === 'end' ? edge - 8 - width : -width / 2)
  const compact = Array.from({ length: shown }, (_, i) => ({ x: x0 + Math.min(i, vis - 1) * step, y: rowY }))
  const gap = hub ? 1.75 : 1.25
  // A big crew gets a wider ring, so neighbours (and their lock / chat badges) never overlap.
  const R = Math.max(hub ? r + 26 : r + 24, shown > 1 ? (SEAT_GAP * (shown - 1)) / (2 * Math.PI - gap) : 0)
  const gapAt = hub || !labelAbove ? Math.PI / 2 : -Math.PI / 2
  const ring = ringAngles(shown, gapAt, gap).map(a => ({ x: R * Math.cos(a), y: R * Math.sin(a) }))
  return { compact, ring, R, shown, vis, moreX: x0 + (vis - 1) * step + 13 }
}

/** The flagship's compact cluster goes above it, at the first bearing no link uses. */
export function hubCrewAnchor(linkAngles = [], hubR = 50) {
  const dist = (a, b) => { const d = Math.abs(a - b) % (2 * Math.PI); return Math.min(d, 2 * Math.PI - d) }
  const cands = [-90, -62, -118, -36, -144].map(d => (d * Math.PI) / 180)
  const a = cands.find(c => linkAngles.every(l => dist(c, l) > 0.42)) ?? cands[0]
  const R = hubR + 22
  return { a, x: R * Math.cos(a), y: R * Math.sin(a) }
}

// --- reaching a bot's chat -------------------------------------------------------------------
// A bot opens its chat only when this app has a door to the Hermes it lives on. Flagship bots
// (and a ship's own flagship-side profiles) live on the backend this page runs against. A ship's
// remote bots live on the ship's own Hermes: reachable only through a registered connection
// (Hermes Desktop: Settings → Gateways; the Webapp: its saved connections) whose host or name
// matches the ship. Everything comes from public SDK calls: host.connections(), host.agents(),
// host.profileRoutes(), host.activeConnectionId(). None of them carry secrets.

export const SETTINGS_GATEWAYS = '/settings?tab=gateway'
const IPV4 = /^\d{1,3}(\.\d{1,3}){3}$/

/** 'ssh://me@Torre.local:22/x', 'me@10.0.0.3', 'TORRE.' -> 'torre.local', '10.0.0.3', 'torre'. */
export function hostOf(value) {
  let t = String(value || '').trim().toLowerCase()
  if (!t) return ''
  if (/^[a-z][a-z0-9+.-]*:\/\//.test(t)) {
    try { t = new URL(t).hostname } catch { t = t.replace(/^[a-z][a-z0-9+.-]*:\/\//, '').split('/')[0] }
  }
  t = t.replace(/^[^@/]*@/, '').split('/')[0].replace(/^\[|\]$/g, '')
  if ((t.match(/:/g) || []).length === 1) t = t.replace(/:\d*$/, '')
  return t.replace(/\.$/, '')
}

/** Every name a host answers to: itself, and for a DNS name its first label ('torre.example.ts.net' -> 'torre'). */
export function hostKeys(values) {
  const keys = new Set()
  for (const v of values || []) {
    const h = hostOf(v)
    if (!h) continue
    keys.add(h)
    if (!IPV4.test(h) && !h.includes(':') && h.includes('.')) keys.add(h.split('.')[0])
  }
  keys.delete('localhost'); keys.delete('127.0.0.1'); keys.delete('::1')
  return [...keys]
}

/** The connections this app knows, normalised: [{ id, kind, label, keys }]. Registry rows
 *  (host.connections()) give host / url / label; roster sources (host.agents()) at least a label. */
export function knownLinks({ connections, sources } = {}) {
  const out = new Map()
  for (const c of Array.isArray(connections) ? connections : []) {
    if (!c || !c.id) continue
    out.set(String(c.id), { id: String(c.id), kind: c.kind || 'remote', label: String(c.label || c.id), keys: hostKeys([c.host, c.url, c.label]) })
  }
  for (const s of Array.isArray(sources) ? sources : []) {
    const id = s && String(s.connectionId || s.id || '')
    if (!id) continue
    const prev = out.get(id)
    const keys = hostKeys([s.label, s.connectionLabel])
    if (prev) prev.keys = [...new Set([...prev.keys, ...keys])]
    else out.set(id, { id, kind: s.kind || s.connectionKind || 'remote', label: String(s.label || id), keys })
  }
  return [...out.values()]
}

/** The registered connection that reaches ship `m` (its hostname, address or name), or null.
 *  The local source is this app's own machine, never a ship. */
export function linkFor(m, links) {
  const want = new Set(hostKeys([m && m.hostname, m && m.host, m && m.name]))
  if (!want.size) return null
  return (links || []).find(l => l.kind !== 'local' && l.id !== 'local' && l.keys.some(k => want.has(k))) || null
}

const profileOf = b => String((b && (b.profile || b.id || b.name)) || 'default')

function routeOn(routes, connectionId, profile) {
  return (routes || []).find(r => r && r.connectionId === connectionId && (r.targetProfile === profile || r.profile === profile)) || null
}

/** Can this app open `bot`'s chat? `m` is its ship (null for the flagship's / this machine's own
 *  crew); `net` = { links, routes, activeId, ready }. Returns { ok, route, profile, via, hint }:
 *  `route` is a host.profileRoutes()-shaped descriptor, or null for the page's own backend. */
export function chatAccess(bot, m, net) {
  const profile = profileOf(bot)
  const routes = (net && net.routes) || []
  if (!bot || !bot.remote) {
    // Lives on the backend this page talks to.
    const activeId = net && net.activeId
    const route = activeId ? routeOn(routes, activeId, profile)
      : (routes.find(r => r && r.mode === 'local' && (r.targetProfile === profile || r.profile === profile)) || null)
    return { ok: true, route, profile, via: 'here', hint: 'Open chat' }
  }
  const machine = (m && m.name) || bot.machine || 'its machine'
  const name = bot.name || profile
  const link = linkFor(m || { name: bot.machine, hostname: bot.hostname }, net && net.links)
  if (link) {
    const route = routeOn(routes, link.id, profile) || { connectionId: link.id, mode: 'remote', profile, targetProfile: profile }
    return { ok: true, route, profile, via: link.label, hint: `Open chat · via ${link.label}` }
  }
  if (net && net.ready === false) return { ok: false, route: null, profile, via: null, pending: true, hint: 'Checking this app’s connections…' }
  return { ok: false, route: null, profile, via: null,
    hint: `Connect ${machine} in Hermes Desktop (Settings → Gateways) to chat with ${name}` }
}

const withTimeout = (p, ms) => Promise.race([p, new Promise((_, reject) => setTimeout(() => reject(new Error('timeout')), ms))])

/** The stored id of a profile's chat in a profiles.list result: its Bot Chat, else its newest session. */
export function chatSessionId(result, target) {
  const rows = result && Array.isArray(result.profiles) ? result.profiles : []
  const row = rows.find(r => r && r.name === target)
  if (!row) return null
  const canon = row.canonical_session
  return (canon && (canon.resolved_id || canon.id)) || (row.last_session && row.last_session.id) || null
}

/** Open `bot`'s chat through the SDK: its Bot Chat / newest session (host.openSession), else a
 *  fresh chat with it (host.newChat). A locked bot does nothing. Returns { ok, how, sid? }. */
export async function openBotChat(bot, access, sdk = host, { timeoutMs = 8000 } = {}) {
  if (!access || !access.ok) return { ok: false, how: 'locked' }
  const route = access.route ? { ...access.route } : null
  const target = route ? route.targetProfile || route.profile : access.profile
  let sid = null
  try {
    const call = route
      ? (typeof sdk.requestProfile === 'function' ? sdk.requestProfile(route, 'profiles.list', {}, timeoutMs, { spawnPriority: 'foreground' }) : null)
      : (typeof sdk.request === 'function' ? sdk.request('profiles.list', {}) : null)
    if (call) sid = chatSessionId(await withTimeout(call, timeoutMs), target)
  } catch { /* fall through to a fresh chat */ }
  if (sid && typeof sdk.openSession === 'function') {
    try {
      await sdk.openSession(sid, route ? { route, profile: route.profile, intent: 'in-place' } : { profile: access.profile, intent: 'in-place' })
      return { ok: true, how: 'session', sid }
    } catch { /* a stale id: start a fresh chat instead */ }
  }
  if (typeof sdk.newChat === 'function') {
    sdk.newChat(route || access.profile)
    return { ok: true, how: 'new' }
  }
  return { ok: false, how: 'unsupported' }
}

/** What the app knows about its connections, for chatAccess. Each read is optional: an older
 *  Desktop or a Webapp without saved connections just knows less. */
export async function loadNet(sdk = host) {
  const tryCall = async fn => { try { return typeof fn === 'function' ? await fn() : null } catch { return null } }
  const [connections, roster, routes] = await Promise.all([
    tryCall(sdk.connections && (() => sdk.connections())),
    tryCall(sdk.agents && (() => sdk.agents())),
    tryCall(sdk.profileRoutes && (() => sdk.profileRoutes()))
  ])
  let activeId = null
  try { activeId = (typeof sdk.activeConnectionId === 'function' && sdk.activeConnectionId()) || null } catch { /* older SDK */ }
  return { links: knownLinks({ connections, sources: roster && roster.sources }), routes: Array.isArray(routes) ? routes : [], activeId, ready: true }
}

/** One line for an asleep bot whose machine can be reached. */
export function sleepNote(bot, m) {
  if (!m || !bot || nodeState(m) !== 'down') return ''
  return `${m.name} is asleep — Flota will wake it when it gets a task`
}

/** A newer backend: /status carries `role` and `fleet` (the one fleet this machine is a ship in). */
export const reportsRole = status => !!status && typeof status === 'object' && 'fleet' in status && 'role' in status

/** The fleet this machine belongs to: /status `fleet`, else the first of GET /fleets, else null. */
export function fleetOf(status, fleets) {
  if (status && status.fleet && status.fleet.flagship) return status.fleet
  return (Array.isArray(fleets) && fleets[0]) || null
}

/** Which screen to show: 'offline' | 'loading' | 'choose' | 'flagship' | 'join' | 'ship'.
 *  Machines make a flagship; joined fleets make a ship; with neither, the saved role decides,
 *  and without a role the chooser asks. `pinJoin` keeps the join wizard up until it finishes. */
export function viewMode({ status, fleets, role, pinJoin = false }) {
  if (!status) return 'loading'
  if (pinJoin) return 'join'
  const machines = Array.isArray(status.machines) ? status.machines : []
  if (reportsRole(status)) {
    // A backend that reports its role and its one fleet is the authority.
    const fleet = fleetOf(status, fleets)
    if (role === 'flagship') return 'flagship'
    if (role === 'ship') return fleet ? 'ship' : 'join'
    return machines.length ? 'flagship' : fleet ? 'ship' : 'choose'
  }
  if (machines.length) return 'flagship'
  if (role === 'flagship') return 'flagship'
  if (fleets == null) return 'loading'
  if (fleets.length) return 'ship'
  if (role === 'ship') return 'join'
  return 'choose'
}

/** The role in force: what the user just picked, else the backend's, else the one kept locally
 *  (for a backend without /role). */
export function effectiveRole(status, picked, stored) {
  if (picked) return picked
  if (reportsRole(status)) return status.role === 'flagship' || status.role === 'ship' ? status.role : null
  if (status && (status.role === 'flagship' || status.role === 'ship')) return status.role
  if (status && status.role === null) return stored || null
  return stored || null
}

/** POST /machines {setup}, /ships, /join -> [{ ok, text }]: lines are ✓ unless they say they failed; problems are ✗. */
export function setupLog(result) {
  const out = []
  const FAIL = /^(✗|×|x\s|error|failed|fail:|could not|couldn't|cannot|can't|unreachable|no route)/i
  for (const line of (result && Array.isArray(result.lines) ? result.lines : [])) {
    const text = String(line).trim()
    if (!text) continue
    out.push({ ok: !FAIL.test(text), text: text.replace(/^(✓|✔|✗|×)\s*/, '') })
  }
  for (const p of (result && Array.isArray(result.problems) ? result.problems : [])) out.push({ ok: false, text: String(p).replace(/^(✗|×)\s*/, '') })
  return out
}

/** Best-effort peek into an invite code for the flagship's name ('flota1.<base64 json>' or plain base64 json). */
export function decodeInvite(code) {
  const raw = String(code || '').trim()
  if (!raw) return null
  for (const part of [raw, ...raw.split(/[.:_]/)].filter(x => x.length > 8)) {
    try {
      const b64 = part.replace(/-/g, '+').replace(/_/g, '/')
      const text = typeof atob === 'function' ? atob(b64 + '==='.slice((b64.length + 3) % 4)) : ''
      const data = JSON.parse(text)
      if (data && typeof data === 'object') return { flagship: data.flagship || data.hub || data.name || null, expires_at: data.expires_at || data.exp || null }
    } catch { /* not this part */ }
  }
  return null
}

/** expires_at (ISO, epoch seconds or ms) -> ms, or null. */
export function toMs(t) {
  if (t == null || t === '') return null
  if (typeof t === 'number') return t < 1e12 ? t * 1000 : t
  const n = Number(t)
  if (Number.isFinite(n)) return toMs(n)
  const d = Date.parse(t)
  return Number.isFinite(d) ? d : null
}

/** 'expires in 9:41' | 'expires in 2 h 5 m' | 'expired'. */
export function countdown(until, now = Date.now()) {
  const s = Math.floor((until - now) / 1000)
  if (!Number.isFinite(s)) return ''
  if (s <= 0) return 'expired'
  if (s >= 3600) return `expires in ${Math.floor(s / 3600)} h ${Math.floor((s % 3600) / 60)} m`
  return `expires in ${Math.floor(s / 60)}:${pad2(s % 60)}`
}

/** GET /join/check -> rows [{ id, label, ok (true|false|null), detail, fix, optional }]. Without the
 *  endpoint, the same checklist with fixes for this OS and ok = null (check it yourself). */
export function joinChecks(data, os) {
  if (data && Array.isArray(data.checks)) {
    return data.checks.map((c, i) => ({ id: String(c.id || i), label: String(c.label || c.id || 'check'), ok: c.ok === true ? true : c.ok === false ? false : null,
      detail: c.detail || '', fix: c.fix || '', optional: !!c.optional }))
  }
  const o = String((data && data.os) || os || 'linux').toLowerCase()
  const k = o === 'darwin' ? 'macos' : o
  const pick = map => map[k] || map.linux
  return [
    { id: 'ssh', label: 'SSH server running on this machine', ok: null, detail: '', optional: false, fix: pick({
      linux: 'sudo systemctl enable --now sshd   (Debian/Ubuntu: sudo apt install openssh-server)',
      macos: 'System Settings → General → Sharing → turn on Remote Login',
      windows: 'Settings → System → Optional features → add “OpenSSH Server”, then in PowerShell: Start-Service sshd' }) },
    { id: 'port', label: 'Port 22 reachable from the flagship', ok: null, detail: '', optional: false, fix: pick({
      linux: 'as root: ufw allow 22/tcp   (or open TCP 22 in your firewall)',
      macos: 'System Settings → Network → Firewall: allow incoming connections for Remote Login',
      windows: 'New-NetFirewallRule -Name sshd -Protocol TCP -LocalPort 22 -Action Allow -Direction Inbound' }) },
    { id: 'network', label: 'Same network or Tailscale as the flagship', ok: null, detail: '', optional: false,
      fix: 'Put both machines on the same LAN, or install Tailscale on both and sign in to the same tailnet (tailscale.com/download).' },
    { id: 'wol', label: 'Wake-on-LAN (optional: lets the flagship wake this machine)', ok: null, detail: '', optional: true, fix: pick({
      linux: 'Enable it in the BIOS/UEFI, then as root: ethtool -s <interface> wol g',
      macos: 'System Settings → Energy (or Battery → Options) → Wake for network access',
      windows: 'Device Manager → your network adapter → Power Management → Allow this device to wake the computer' }) }
  ]
}

/** GET /join/check `wake_options` -> [{ id, ok, label }] ('wol' | 'wol_via' | 'timer' | 'none'). */
export function wakeOptions(data) {
  const WAKE = { wol: 'Wake-on-LAN from the flagship', wol_via: 'Wake-on-LAN relayed by another machine', timer: 'A wake timer on this machine', none: 'Not woken: the flagship only uses it while it is on' }
  return (data && Array.isArray(data.wake_options) ? data.wake_options : []).filter(o => o && o.id)
    .map(o => ({ id: String(o.id), ok: o.ok === true, label: String(o.label || WAKE[o.id] || o.id) }))
}

// --- schedule (pure) -------------------------------------------------------------------------

const DAYS = ['Sundays', 'Mondays', 'Tuesdays', 'Wednesdays', 'Thursdays', 'Fridays', 'Saturdays']
const pad2 = n => String(n).padStart(2, '0')

/** '0 2 * * *' -> 'Every day at 02:00', '30 3 * * 1' -> 'Mondays at 03:30', 'every 6h' -> 'Every 6 h'.
 *  Anything it does not recognise comes back as it was. */
export function humanSchedule(text) {
  const raw = String(text || '').trim()
  if (!raw || raw === '?') return 'Unscheduled'
  const every = /^every\s+(\d+)\s*(m|min|minutes?|h|hours?|d|days?)$/i.exec(raw)
  if (every) {
    const n = Number(every[1])
    const unit = every[2][0].toLowerCase()
    if (unit === 'd') return n === 1 ? 'Every day' : `Every ${n} days`
    return n === 1 ? (unit === 'h' ? 'Every hour' : 'Every minute') : `Every ${n} ${unit === 'h' ? 'h' : 'min'}`
  }
  const f = raw.split(/\s+/)
  if (f.length !== 5) return raw
  const [mi, ho, dom, mon, dow] = f
  const step = /^\*\/(\d+)$/
  if (step.test(mi) && ho === '*' && dom === '*' && mon === '*' && dow === '*') return `Every ${step.exec(mi)[1]} min`
  if (/^\d+$/.test(mi) && step.test(ho) && dom === '*' && mon === '*' && dow === '*') return `Every ${step.exec(ho)[1]} h`
  if (/^\d+$/.test(mi) && ho === '*' && dom === '*' && mon === '*' && dow === '*') return `Hourly at :${pad2(mi)}`
  if (!/^\d+$/.test(mi) || !/^\d+$/.test(ho) || mon !== '*') return raw
  const at = `${pad2(ho)}:${pad2(mi)}`
  if (dom === '*' && dow === '*') return `Every day at ${at}`
  if (dom === '*' && (dow === '1-5' || dow === 'mon-fri')) return `Weekdays at ${at}`
  if (dom === '*' && (dow === '0,6' || dow === '6,0' || dow === 'sat,sun')) return `Weekends at ${at}`
  if (dom === '*' && /^[0-7](,[0-7])*$/.test(dow)) {
    const days = dow.split(',').map(d => DAYS[Number(d) % 7])
    return `${days.length > 2 ? days.map(d => d.slice(0, 3)).join(', ') : days.join(' and ')} at ${at}`
  }
  if (/^\d+$/.test(dom) && dow === '*') return `Day ${dom} of each month at ${at}`
  return raw
}

/** Time until `ts` (ms): 'in 2 h 10 m', 'in 3 d 4 h', 'in 5 m', 'due now', 'overdue'. */
export function relTime(ts, now = Date.now()) {
  const s = (ts - now) / 1000
  if (s < -120) return 'overdue'
  if (s < 60) return 'due now'
  const total = Math.ceil(s / 60)
  const d = Math.floor(total / 1440)
  const h = Math.floor((total % 1440) / 60)
  const m = total % 60
  if (d) return h ? `in ${d} d ${h} h` : `in ${d} d`
  if (h) return m ? `in ${h} h ${m} m` : `in ${h} h`
  return `in ${m} m`
}

/** Local wall-clock of `ts`: '02:00', 'tomorrow 02:00', 'Mon 03:30', 'Nov 20, 09:00'. */
export function localWhen(ts, now = Date.now()) {
  const d = new Date(ts)
  const time = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  const day = x => { const c = new Date(x); c.setHours(0, 0, 0, 0); return c.getTime() }
  const days = Math.round((day(ts) - day(now)) / 86400000)
  if (days === 0) return time
  if (days === 1) return `tomorrow ${time}`
  if (days > 1 && days < 7) return `${d.toLocaleDateString([], { weekday: 'short' })} ${time}`
  return `${d.toLocaleDateString([], { month: 'short', day: 'numeric' })}, ${time}`
}

/** Machine names mentioned as whole words in a job (name, prompt, skills), in order of mention.
 *  Same rule as the backend: 'mini-pc' names mini, 'flota-torre' names torre, 'torretas' does not. */
export function jobMachines(job, names) {
  const text = [job.name, job.prompt || job.prompt_preview, ...(job.skills || [])].filter(Boolean).join(' ').toLowerCase()
  const hits = []
  for (const name of names || []) {
    const esc = name.toLowerCase().replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
    const at = text.search(new RegExp(`(?<![a-z0-9])${esc}(?![a-z0-9])`))
    if (at >= 0) hits.push([at, name])
  }
  return hits.sort((a, b) => a[0] - b[0]).map(h => h[1])
}

/** GET /schedule (or the gateway's cron.manage list) -> rows soonest first, paused and unscheduled last:
 *  [{ id, name, schedule, human, next (ms | null), enabled, paused, machine, machines }]. */
export function scheduleRows(data, machineNames) {
  const jobs = (data && Array.isArray(data.jobs)) ? data.jobs : []
  const rows = jobs.map(j => {
    const machines = Array.isArray(j.machines) ? j.machines : jobMachines(j, machineNames)
    const machine = j.machine !== undefined ? j.machine : (machines[0] || null)
    const next = j.next_run_at ? Date.parse(j.next_run_at) : NaN
    const enabled = j.enabled !== false
    const schedule = String(j.schedule || '?')
    return { id: String(j.id || j.job_id || j.name || ''), name: String(j.name || 'cron job'), schedule, human: humanSchedule(schedule),
      next: Number.isFinite(next) && enabled ? next : null, enabled, paused: !enabled || j.state === 'paused',
      machine, machines, state: j.state || 'scheduled' }
  })
  return rows.sort((a, b) => (a.next == null) - (b.next == null) || (a.next || 0) - (b.next || 0) || a.name.localeCompare(b.name))
}

/** The first upcoming job that will wake a ship: { row, machine } or null. */
export function nextWake(rows, now = Date.now()) {
  const row = (rows || []).find(r => r.machine && r.next != null && r.next > now - 120000)
  return row ? { row, machine: row.machine } : null
}

/** 600 -> '10 min', 5400 -> '1 h 30 min', 0 -> ''. */
export function formatDuration(seconds) {
  const s = Math.round(Number(seconds) || 0)
  if (s <= 0) return ''
  if (s < 60) return `${s} s`
  const h = Math.floor(s / 3600)
  const min = Math.round((s % 3600) / 60)
  return h ? (min ? `${h} h ${min} min` : `${h} h`) : `${min} min`
}

const OS_NAMES = { linux: 'Linux', macos: 'macOS', darwin: 'macOS', windows: 'Windows' }
export const osName = os => OS_NAMES[(os || '').toLowerCase()] || os || 'unknown'

export function wakeText(m) {
  if (m.wake === 'wol') return m.mac ? `Wake-on-LAN · ${m.mac}` : 'Wake-on-LAN'
  if (m.wake === 'wol_via') return `Wake-on-LAN via ${m.via || '?'}`
  if (m.wake === 'command') return 'Custom command'
  return 'Not woken by Flota'
}

export function sleepText(m) {
  const how = m.custom_sleep ? 'custom command' : m.sleep
  if (!how || how === 'none') return 'Stays on'
  const after = formatDuration(m.sleep_after)
  return after ? `${how} after ${after} idle` : `${how}, only on request`
}

/** The ctx.rest error -> { status, message, problems }. Electron hands us "…409: {json}". */
export function parseError(err) {
  const text = String((err && err.message) || err || 'Unknown error')
  const match = /(\d{3}):\s*([\s\S]*)$/.exec(text)
  const status = Number((err && err.statusCode) || (match && match[1])) || 0
  let message = match ? match[2] : text
  let problems = []
  let needs
  let fleet
  try {
    const body = JSON.parse(message)
    const detail = body && 'detail' in body ? body.detail : body
    if (detail && typeof detail === 'object') {
      message = detail.message || detail.error || JSON.stringify(detail)
      problems = Array.isArray(detail.problems) ? detail.problems : []
      needs = detail.needs || undefined
      fleet = detail.fleet || undefined
    } else if (detail != null) {
      message = String(detail)
    }
    // Flota's 409s also carry these at the top level, beside a plain-text detail.
    if (body && typeof body === 'object') {
      needs = needs || body.needs || undefined
      fleet = fleet || body.fleet || undefined
    }
  } catch { /* not JSON: keep the text */ }
  return { status, message: message || text, problems, needs, fleet }
}

/** What kind of failure an error is, so the page never calls a backend 'older' by mistake:
 *  'plugin-off'    404 "Plugin not found": the Hermes (or the profile) this request reached does
 *                  not run Flota. Desktop sends plugin REST to the profile you last chatted with,
 *                  so a bot profile on the flagship's connection answers this too.
 *  'route-missing' 404 of a route while the plugin answers: a genuinely older Flota backend.
 *  'unreachable'   no answer at all (bridge, timeout, gateway down, 502-504).
 *  'auth' | 'server' | 'conflict' | 'other'.  `raw` keeps the full text for a details toggle. */
export function classifyError(err) {
  const e = err && typeof err === 'object' && 'status' in err && 'message' in err && !(err instanceof Error) ? err : parseError(err)
  const raw = String((err && (err.raw || err.message)) || err || '')
  return { ...e, kind: errKind(e, raw), raw }
}

function errKind(e, raw = '') {
  if (e.kind) return e.kind
  const s = Number(e.status) || 0
  if (s === 404) return /plugin not found/i.test(`${e.message} ${raw}`) ? 'plugin-off' : 'route-missing'
  if (s === 401 || s === 403) return 'auth'
  if (s === 409) return 'conflict'
  if (s >= 502 && s <= 504) return 'unreachable'
  if (s >= 500) return 'server'
  if (!s) return 'unreachable'
  return 'other'
}

/** Only a 404 of the route itself, with the plugin answering, means an older Flota backend. */
export const isOlderBackend = e => !!e && errKind(e, e.raw) === 'route-missing'

/** The REST scope plugin calls go to right now: { connectionId, profile, label, local }. */
export function currentScope(sdk = host, net = null) {
  let connectionId = null
  let profile = null
  try { connectionId = (typeof sdk.activeConnectionId === 'function' && sdk.activeConnectionId()) || null } catch { /* older SDK */ }
  try { profile = (sdk.state && sdk.state.profile && typeof sdk.state.profile.get === 'function' && sdk.state.profile.get()) || null } catch { /* older SDK */ }
  const id = connectionId || 'local'
  const link = ((net && net.links) || []).find(l => l.id === id)
  const local = id === 'local' || !!(link && link.kind === 'local')
  return { connectionId: id, profile: profile || 'default', label: link && !local ? link.label : local ? 'this PC' : id, local }
}

// --- reaching the flagship from any connection -----------------------------------------------
// ctx.rest always goes to the app's ACTIVE connection and profile (Desktop api/plugins.ts
// pluginRest -> profileScoped + connectionScoped); PluginRestOptions has no route field, so a
// plugin cannot send REST to another connection. What it can do: ask every connection's default
// profile, over the gateway JSON-RPC door host.requestProfile(route, 'command.dispatch'), whether
// it runs Flota (the /flota slash command answers 'Usage: /flota … Machines: a, b'), and then make
// that connection the active one with host.ensureAgent(connectionId, profile).

/** Routes worth asking: each connection's default profile (Flota is normally enabled there),
 *  never the scope that just failed. Dialing a profile can cold-start its backend, so no bots. */
export function probeCandidates(routes, scope) {
  const seen = new Set()
  const out = []
  for (const r of Array.isArray(routes) ? routes : []) {
    if (!r || !r.connectionId || !r.profile) continue
    if ((r.targetProfile || r.profile) !== 'default' && r.profile !== 'default') continue
    const key = `${r.connectionId}::${r.profile}`
    if (seen.has(key)) continue
    seen.add(key)
    if (scope && r.connectionId === scope.connectionId && r.profile === scope.profile) continue
    out.push(r)
  }
  return out.slice(0, 6)
}

/** '/flota' usage text -> machine names ('Machines: a, b' / 'none configured' -> []). */
export function usageMachines(text) {
  const m = /Machines:\s*(.*)$/m.exec(String(text || ''))
  if (!m || /none configured/i.test(m[1])) return []
  return m[1].split(',').map(s => s.trim()).filter(Boolean)
}

/** Ask the other connections whether they run Flota. Returns { supported, tried, found } where
 *  found = { route, label, machines, sameConnection } (a flagship with ships first), or null. */
export async function findFlagship(sdk = host, net = null, scope = null, { timeoutMs = 6000 } = {}) {
  if (!sdk || typeof sdk.requestProfile !== 'function') return { supported: false, tried: 0, found: null }
  const cands = probeCandidates(net && net.routes, scope)
  const hits = (await Promise.all(cands.map(async route => {
    try {
      const res = await withTimeout(sdk.requestProfile(route, 'command.dispatch', { name: 'flota', arg: 'help' }, timeoutMs), timeoutMs + 500)
      const out = String((res && (res.output != null ? res.output : res.text)) || '')
      return /\/flota\b/.test(out) ? { route, machines: usageMachines(out) } : null
    } catch { return null }
  }))).filter(Boolean)
  const best = hits.find(h => h.machines.length) || hits[0] || null
  if (!best) return { supported: true, tried: cands.length, found: null }
  const link = ((net && net.links) || []).find(l => l.id === best.route.connectionId)
  const local = best.route.connectionId === 'local' || (link && link.kind === 'local')
  return { supported: true, tried: cands.length, found: { ...best, label: link && !local ? link.label : local ? 'this PC' : best.route.connectionId,
    sameConnection: !!scope && scope.connectionId === best.route.connectionId } }
}

/** Make `route` the app's active connection, so ctx.rest reaches it (host.ensureAgent). */
export async function reachRoute(route, sdk = host) {
  if (!route) throw new Error('No route')
  if (typeof sdk.ensureAgent !== 'function') throw new Error('This Hermes Desktop cannot switch connections from a plugin yet. Switch it in the app’s connection menu.')
  await sdk.ensureAgent(route.connectionId === 'local' ? null : route.connectionId, route.profile || 'default')
  return true
}

const REACH_STORE = 'flota.flagshipRoute'
export const readReach = () => { try { const v = typeof localStorage !== 'undefined' && localStorage.getItem(REACH_STORE); return v ? JSON.parse(v) : null } catch { return null } }
const writeReach = route => { try { if (typeof localStorage !== 'undefined') localStorage.setItem(REACH_STORE, JSON.stringify({ connectionId: route.connectionId, profile: route.profile })) } catch { /* private mode */ } }
export const sameRoute = (a, b) => !!a && !!b && a.connectionId === b.connectionId && (a.profile || 'default') === (b.profile || 'default')

/** Positions for n machines around the hub. Returns { width, height, cx, cy, hubR, r, rings, nodes }.
 *  1..12 machines sit on one ellipse; more use two rings, the outer one offset half a step. */
export function layoutRing(n) {
  const two = n > 12
  const width = two ? 1040 : 1000
  const height = two ? 760 : 640
  const cx = width / 2
  const cy = height / 2
  const r = two ? 28 : 33
  const rings = []
  if (n > 0 && !two) {
    const rx = Math.min(370, 250 + 20 * n)
    rings.push({ count: n, rx, ry: Math.min(205, rx * 0.6), offset: 0 })
  } else if (two) {
    const inner = Math.round(n * 0.4)
    rings.push({ count: inner, rx: 230, ry: 140, offset: 0 })
    rings.push({ count: n - inner, rx: 430, ry: 280, offset: 0.5 })
  }
  const nodes = []
  const TAU = 2 * Math.PI
  let inner = null
  for (const ring of rings) {
    const k = ring.count
    const step = TAU / k
    // One machine sits to the right; an even count straddles the vertical axis so no label hides a link.
    const start = k === 1 ? 0 : -Math.PI / 2 + (k % 2 === 0 ? Math.PI / k : 0) + ring.offset * step
    for (let i = 0; i < k; i++) {
      const a = start + i * step
      const node = { x: cx + ring.rx * Math.cos(a), y: cy + ring.ry * Math.sin(a), angle: a }
      if (inner) {
        // An outer link bends through the nearest gap between two inner machines instead of under one.
        const g = Math.round((a - inner.start) / inner.step - 0.5)
        const ga = inner.start + (g + 0.5) * inner.step
        node.via = { x: cx + inner.rx * Math.cos(ga), y: cy + inner.ry * Math.sin(ga) }
      }
      nodes.push(node)
    }
    inner = { start, step, rx: ring.rx, ry: ring.ry }
  }
  // Crop the view to what is drawn (plus room for labels) so a small fleet fills the card.
  // Symmetric around the hub, which always stays in the middle.
  const outer = rings[rings.length - 1] || { rx: 0, ry: 0 }
  const hw = Math.max(400, outer.rx + 14, ...nodes.map(p => Math.abs(p.x - cx) + r + 80))
  const hh = Math.max(250, outer.ry + 14, ...nodes.map(p => Math.abs(p.y - cy) + r + 84)) // crew row + two label lines
  const view = { x: Math.round(cx - hw), y: Math.round(cy - hh), w: Math.round(2 * hw), h: Math.round(2 * hh) }
  return { width, height, cx, cy, hubR: 50, r, rings, nodes, view }
}

// --- styles ----------------------------------------------------------------------------------
// Disk plugins are not scanned by Tailwind, so layout and the SVG animations live here, scoped
// under .flota-*. Colours are theme variables only, so every theme (light or dark) reskins it.

export const CSS = `
.flota-page{box-sizing:border-box;display:flex;flex-direction:column;gap:14px;height:100%;min-height:0;overflow:auto;padding:22px 26px;color:var(--ui-text-primary)}
.flota-head{display:flex;align-items:flex-end;gap:16px;flex-wrap:wrap}
.flota-title{font-size:22px;font-weight:650;letter-spacing:-.01em;line-height:1.1}
.flota-sub{margin-top:4px;font-size:13px;color:var(--ui-text-tertiary)}
.flota-head-right{margin-left:auto;display:flex;align-items:center;gap:10px;font-size:12px;color:var(--ui-text-tertiary)}
.flota-summary{display:flex;gap:6px;flex-wrap:wrap}
.flota-pill{display:inline-flex;align-items:center;gap:6px;padding:2px 9px;border-radius:999px;font-size:11.5px;background:var(--ui-bg-quaternary);color:var(--ui-text-secondary)}
.flota-pill i{width:7px;height:7px;border-radius:50%;background:currentColor;display:inline-block}
.flota-pill.up{color:var(--ui-green)}.flota-pill.waking{color:var(--ui-yellow)}.flota-pill.down{color:var(--ui-text-tertiary)}
.flota-body{display:grid;grid-template-columns:minmax(0,1fr) 340px;gap:16px;min-height:0;flex:1}
.flota-col{display:flex;flex-direction:column;gap:12px;min-height:0;overflow:auto;padding:1px;margin:-1px}
@media (max-width:980px){.flota-body{grid-template-columns:1fr}.flota-col{overflow:visible}}
.flota-card{border-radius:10px;background:var(--ui-bg-card);box-shadow:inset 0 0 0 1px var(--ui-stroke-quaternary)}
.flota-mapwrap{position:relative;min-height:440px;overflow:hidden;background:none}
.flota-map{position:absolute;inset:0;display:block;width:100%;height:100%}
.flota-legend{position:absolute;left:14px;bottom:12px;display:flex;gap:12px;font-size:11px;color:var(--ui-text-tertiary)}
.flota-legend span{display:inline-flex;align-items:center;gap:5px}
.flota-legend i{width:8px;height:8px;border-radius:50%;display:inline-block}
.flota-orbit{fill:none;stroke:var(--ui-stroke-tertiary);stroke-width:1;stroke-dasharray:2 7;opacity:.7}
.flota-range{fill:none;stroke:var(--ui-stroke-quaternary);stroke-width:1}
.flota-bearing{stroke:var(--ui-stroke-quaternary);stroke-width:1;stroke-dasharray:1 5}
.flota-sweep{transform-box:fill-box;transform-origin:center;animation:flota-spin 14s linear infinite}
.flota-sweep-slice{fill:var(--ui-accent)}
.flota-sweep-edge{stroke:var(--ui-accent);stroke-width:1;opacity:.2}
.flota-swell{fill:none;stroke:var(--ui-accent);stroke-width:1;stroke-linecap:round;opacity:.09}
.flota-link{fill:none;stroke-linecap:round}
.flota-link.down{stroke:var(--ui-text-quaternary);stroke-width:1.4;stroke-dasharray:3 7;opacity:.55}
.flota-link.up{stroke:var(--ui-green);stroke-width:2;opacity:.6}
.flota-link.waking{stroke:var(--ui-yellow);stroke-width:2;opacity:.5}
.flota-flow{fill:none;stroke-linecap:round;stroke-width:2.4;stroke-dasharray:3 15;animation:flota-flow 1.6s linear infinite}
.flota-flow.up{stroke:var(--ui-green);opacity:.85;animation-duration:3.2s}
.flota-flow.waking{stroke:var(--ui-yellow);stroke-dasharray:6 10;animation-duration:.9s}
@keyframes flota-flow{to{stroke-dashoffset:-36}}
.flota-node{cursor:pointer;outline:none}
.flota-pop{transform-box:fill-box;transform-origin:center;animation:flota-in .55s cubic-bezier(.2,.9,.3,1.25) both}
@keyframes flota-in{from{opacity:0;transform:scale(.4)}to{opacity:1;transform:scale(1)}}
.flota-disc{stroke-width:2;transition:fill .3s,stroke .3s}
.flota-node.down .flota-disc{fill:var(--ui-bg-quaternary);stroke:var(--ui-stroke-secondary)}
.flota-node.down .flota-glyph{color:var(--ui-text-tertiary)}
.flota-node.up .flota-disc{fill:color-mix(in srgb,var(--ui-green) 16%,var(--ui-bg-editor));stroke:var(--ui-green);filter:drop-shadow(0 0 10px color-mix(in srgb,var(--ui-green) 45%,transparent))}
.flota-node.up .flota-glyph{color:var(--ui-green)}
.flota-node.waking .flota-disc{fill:color-mix(in srgb,var(--ui-yellow) 16%,var(--ui-bg-editor));stroke:var(--ui-yellow)}
.flota-node.waking .flota-glyph{color:var(--ui-yellow)}
.flota-node:hover .flota-disc,.flota-node:focus-visible .flota-disc{stroke-width:3}
.flota-node.selected .flota-select{opacity:1}
.flota-select{fill:none;stroke:var(--ui-accent);stroke-width:2.5;opacity:0;transition:opacity .2s}
.flota-pulse{fill:none;stroke:var(--ui-yellow);stroke-width:2;transform-box:fill-box;transform-origin:center;animation:flota-pulse 1.6s ease-out infinite}
@keyframes flota-pulse{from{opacity:.8;transform:scale(1)}to{opacity:0;transform:scale(1.55)}}
.flota-busy-ring{fill:none;stroke:var(--ui-accent);stroke-width:2;stroke-dasharray:5 6;transform-box:fill-box;transform-origin:center;animation:flota-spin 6s linear infinite}
@keyframes flota-spin{to{transform:rotate(360deg)}}
.flota-badge circle{fill:var(--ui-accent);stroke:var(--ui-bg-editor);stroke-width:2}
.flota-badge text{fill:var(--ui-bg-editor);font-size:11px;font-weight:700}
.flota-badge.warn circle{fill:var(--ui-yellow)}.flota-badge.err circle{fill:var(--ui-red)}
.flota-name{fill:var(--ui-text-primary);font-size:15px;font-weight:600}
.flota-state{fill:var(--ui-text-tertiary);font-size:12.5px}
.flota-node.up .flota-state{fill:var(--ui-green)}.flota-node.waking .flota-state{fill:var(--ui-yellow)}
.flota-name,.flota-state,.flota-hub-name,.flota-hub-sub{paint-order:stroke;stroke:var(--ui-bg-editor);stroke-width:5px;stroke-linejoin:round}
.flota-hub{cursor:pointer;outline:none}
.flota-hub-disc{fill:color-mix(in srgb,var(--ui-accent) 20%,var(--ui-bg-editor));stroke:var(--ui-accent);stroke-width:2.5;filter:drop-shadow(0 0 18px color-mix(in srgb,var(--ui-accent) 55%,transparent))}
.flota-hub:hover .flota-hub-disc,.flota-hub:focus-visible .flota-hub-disc{stroke-width:3.5}
.flota-hub-ring{fill:none;stroke:var(--ui-accent);stroke-width:1;opacity:.35;stroke-dasharray:2 4}
.flota-hub-glyph{color:var(--ui-accent)}
.flota-hub-helm{transform-box:fill-box;transform-origin:center;animation:flota-helm 9s ease-in-out infinite}
@keyframes flota-helm{0%,100%{transform:rotate(-8deg)}50%{transform:rotate(8deg)}}
.flota-flag rect{fill:var(--ui-accent)}
.flota-flag text{fill:var(--ui-bg-editor);font-size:9px;font-weight:750;letter-spacing:.12em}
.flota-hub-name{fill:var(--ui-text-primary);font-size:16px;font-weight:650}
.flota-crew-bot{color:var(--ui-text-tertiary)}
.flota-bot-head{fill:color-mix(in srgb,currentColor 16%,var(--ui-bg-editor));stroke:currentColor;stroke-width:1.4}
.flota-bot-ant{stroke:currentColor;stroke-width:1.3;stroke-linecap:round}
.flota-bot-tip{fill:currentColor}
.flota-bot-eye{fill:currentColor}
.flota-bot-lid{fill:none;stroke:currentColor;stroke-width:1.3;stroke-linecap:round}
.flota-bot{transform-box:fill-box;transform-origin:center bottom}
.flota-bot.asleep{color:var(--ui-text-tertiary);opacity:.72}
.flota-bot.waking{color:var(--ui-yellow)}
.flota-bot.waking .flota-bot-eyes{transform-box:fill-box;transform-origin:center;animation:flota-blink 1.3s ease-in-out infinite}
@keyframes flota-blink{0%,40%,100%{transform:scaleY(1)}20%{transform:scaleY(.1)}}
.flota-bot.awake{color:var(--ui-green);animation:flota-bob 3.4s ease-in-out infinite}
.flota-bot.working{color:var(--ui-accent);animation:flota-bob 1.1s ease-in-out infinite}
@keyframes flota-bob{0%,100%{transform:translateY(0)}50%{transform:translateY(-1.6px)}}
.flota-bot-ping{fill:var(--ui-accent);transform-box:fill-box;transform-origin:center;animation:flota-ping 1.4s ease-out infinite}
@keyframes flota-ping{from{opacity:.9;transform:scale(1)}to{opacity:0;transform:scale(3.2)}}
.flota-crew-more{fill:var(--ui-text-tertiary);font-size:10.5px;font-weight:650}
.flota-z{fill:var(--ui-text-tertiary);font-size:8.5px;font-weight:700;font-style:italic;opacity:.55;animation:flota-z 3.6s ease-in infinite}
@keyframes flota-z{0%{opacity:0;transform:translate(0,0)}25%{opacity:.75}100%{opacity:0;transform:translate(6px,-13px)}}
.flota-clock circle{fill:var(--ui-bg-elevated);stroke:var(--ui-accent);stroke-width:1.5}
.flota-clock path{fill:none;stroke:var(--ui-accent);stroke-width:1.5;stroke-linecap:round}
.flota-hub-sub{fill:var(--ui-text-tertiary);font-size:12.5px}
.flota-ghost{fill:none;stroke:var(--ui-stroke-secondary);stroke-width:1.5;stroke-dasharray:4 5}
.flota-ghost-plus{stroke:var(--ui-text-quaternary);stroke-width:2;stroke-linecap:round}
.flota-ghost-g{cursor:pointer}.flota-ghost-g:hover .flota-ghost{stroke:var(--ui-accent)}
.flota-tip{position:absolute;z-index:5;pointer-events:none;transform:translate(-50%,-100%);min-width:190px;padding:9px 11px;border-radius:8px;font-size:12px;line-height:1.5;background:var(--ui-bg-elevated);color:var(--ui-text-secondary);box-shadow:0 8px 28px color-mix(in srgb,var(--ui-base) 18%,transparent),inset 0 0 0 1px var(--ui-stroke-tertiary)}
.flota-tip.below{transform:translate(-50%,0)}
.flota-tip.side-right{transform:translate(0,-50%)}.flota-tip.side-left{transform:translate(-100%,-50%)}
.flota-tip b{display:block;color:var(--ui-text-primary);font-size:12.5px;margin-bottom:2px}
.flota-tip span{color:var(--ui-text-tertiary)}
.flota-side{display:flex;flex-direction:column;gap:12px;padding:16px;min-width:0;align-self:start}
.flota-side-head{display:flex;align-items:center;gap:10px}
.flota-side-name{font-size:16px;font-weight:650;overflow:hidden;text-overflow:ellipsis}
.flota-muted{color:var(--ui-text-tertiary);font-size:12px;line-height:1.5}
.flota-facts{display:grid;grid-template-columns:auto 1fr;gap:6px 12px;font-size:12.5px;margin:0}
.flota-facts dt{color:var(--ui-text-tertiary)}.flota-facts dd{margin:0;color:var(--ui-text-primary);overflow-wrap:anywhere}
.flota-actions{display:flex;flex-wrap:wrap;gap:8px}
.flota-cmd{display:flex;align-items:center;gap:6px;padding:6px 6px 6px 10px;border-radius:6px;background:var(--ui-bg-quaternary);font-family:var(--font-mono,ui-monospace,monospace);font-size:11.5px;color:var(--ui-text-primary)}
.flota-cmd code{flex:1;min-width:0;overflow-wrap:anywhere;line-height:1.5}
.flota-note{padding:9px 11px;border-radius:8px;font-size:12px;line-height:1.5;background:color-mix(in srgb,var(--ui-yellow) 10%,transparent);color:var(--ui-text-secondary);box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--ui-yellow) 30%,transparent)}
.flota-note.err{background:color-mix(in srgb,var(--ui-red) 9%,transparent);box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--ui-red) 28%,transparent)}
.flota-note ul{margin:4px 0 0;padding-left:16px}
.flota-steps{margin:0;padding-left:18px;display:flex;flex-direction:column;gap:8px;font-size:12.5px;color:var(--ui-text-secondary)}
.flota-form{display:grid;grid-template-columns:1fr 1fr;gap:10px 12px}
.flota-form label{display:flex;flex-direction:column;gap:4px;font-size:11.5px;color:var(--ui-text-tertiary)}
.flota-form .wide{grid-column:1/-1}
.flota-sched{display:flex;flex-direction:column;gap:10px;padding:16px;min-width:0}
.flota-sched-head{display:flex;align-items:baseline;gap:8px}
.flota-sched-head .flota-muted{margin-left:auto}
.flota-next{display:flex;align-items:center;gap:8px;padding:8px 10px;border-radius:8px;font-size:12.5px;line-height:1.4;color:var(--ui-text-secondary);background:color-mix(in srgb,var(--ui-accent) 9%,transparent);box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--ui-accent) 24%,transparent)}
.flota-next b{color:var(--ui-text-primary);font-weight:620}
.flota-next svg{flex-shrink:0;color:var(--ui-accent)}
.flota-jobs{list-style:none;margin:0;padding:0;display:flex;flex-direction:column}
.flota-job{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:2px 10px;padding:9px 8px;border-radius:7px;border-top:1px solid var(--ui-stroke-quaternary)}
.flota-job+.flota-job.selected,.flota-job.selected+.flota-job,.flota-job.linked:hover+.flota-job{border-top-color:transparent}
.flota-job:first-child{border-top:0}
.flota-job.linked{cursor:pointer}.flota-job.linked:hover,.flota-job.selected{background:var(--ui-bg-quaternary)}
.flota-job.paused{opacity:.62}
.flota-job-time{font-variant-numeric:tabular-nums;font-size:11.5px;color:var(--ui-text-tertiary);white-space:nowrap;text-align:right}
.flota-job-rel{font-variant-numeric:tabular-nums;font-size:12px;font-weight:600;color:var(--ui-accent);white-space:nowrap;text-align:right}
.flota-job.paused .flota-job-rel{color:var(--ui-text-tertiary)}
.flota-job-name{font-size:12.5px;font-weight:600;color:var(--ui-text-primary);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.flota-job-when{font-size:11.5px;color:var(--ui-text-tertiary)}
.flota-job-ship{grid-column:1/-1;display:flex;align-items:center;gap:6px;margin-top:3px;font-size:11.5px;color:var(--ui-text-secondary)}
.flota-ship-chip{display:inline-flex;align-items:center;gap:5px;padding:1px 8px 1px 6px;border-radius:999px;font-size:11px;font-weight:600;background:var(--ui-bg-quaternary);color:var(--ui-text-primary)}
.flota-ship-chip i{width:6px;height:6px;border-radius:50%;display:inline-block;background:var(--ui-text-quaternary)}
.flota-ship-chip.up i{background:var(--ui-green)}.flota-ship-chip.waking i{background:var(--ui-yellow)}
.flota-tag{display:inline-block;margin-left:6px;padding:0 6px;border-radius:4px;font-size:10px;font-weight:650;letter-spacing:.04em;text-transform:uppercase;vertical-align:1px;background:var(--ui-bg-quaternary);color:var(--ui-text-tertiary)}
.flota-chip{display:inline-flex;align-items:center;gap:5px;height:100%;padding:0 6px;font-size:11px;color:var(--ui-text-tertiary);background:none;border:0;cursor:pointer}
.flota-chip:hover{color:var(--ui-text-primary)}
.flota-chip i{width:6px;height:6px;border-radius:50%;background:var(--ui-green);display:inline-block}
.flota-chip.none i{background:var(--ui-text-quaternary)}
/* crews: compact avatar stacks that fan out into a ring on an active node */
.flota-seat{transform:translate(calc(var(--cx) * 1px),calc(var(--cy) * 1px));transition:transform .32s cubic-bezier(.3,1.45,.55,1),opacity .22s ease;transition-delay:calc(var(--i) * 12ms)}
.flota-seat.extra{opacity:0}
.flota-crew-orbit{fill:none;stroke:var(--ui-accent);stroke-width:1;stroke-dasharray:2 5;opacity:0;transform-box:fill-box;transform-origin:center;transform:scale(.6);transition:opacity .25s,transform .32s cubic-bezier(.3,1.45,.55,1)}
.flota-hit{fill:transparent;stroke:none}
.flota-node.active:hover .flota-seat,.flota-node.active:focus-visible .flota-seat,.flota-node.active:focus-within .flota-seat,.flota-hub.active:hover .flota-seat,.flota-hub.active:focus-visible .flota-seat,.flota-hub.active:focus-within .flota-seat,.flota-expanded .flota-seat{transform:translate(calc(var(--rx) * 1px),calc(var(--ry) * 1px)) scale(1.22);opacity:1}
.flota-node.active:hover .flota-crew-orbit,.flota-node.active:focus-visible .flota-crew-orbit,.flota-node.active:focus-within .flota-crew-orbit,.flota-hub.active:hover .flota-crew-orbit,.flota-hub.active:focus-visible .flota-crew-orbit,.flota-hub.active:focus-within .flota-crew-orbit,.flota-expanded .flota-crew-orbit{opacity:.45;transform:scale(1)}
.flota-crew-more{transition:opacity .2s}
.flota-node.active:hover .flota-crew-more,.flota-node.active:focus-visible .flota-crew-more,.flota-node.active:focus-within .flota-crew-more,.flota-hub.active:hover .flota-crew-more,.flota-hub.active:focus-visible .flota-crew-more,.flota-hub.active:focus-within .flota-crew-more,.flota-expanded .flota-crew-more{opacity:0}
/* a ring seat is a door to the bot's chat, or view-only with a padlock */
.flota-seat{outline:none}
.flota-seat.chat{cursor:pointer}.flota-seat.locked{cursor:default}
.flota-seat.chat:hover .flota-av-bg,.flota-seat.chat:focus-visible .flota-av-bg{stroke:var(--ui-accent);stroke-width:2.2}
.flota-seat.chat:hover .flota-av-edge,.flota-seat.chat:focus-visible .flota-av-edge{stroke:var(--ui-accent);stroke-width:2.2}
.flota-seat:focus-visible .flota-av{filter:drop-shadow(0 0 4px color-mix(in srgb,var(--ui-accent) 70%,transparent))}
.flota-lock,.flota-chatb{opacity:0;transition:opacity .18s;pointer-events:none}
.flota-lock circle{fill:var(--ui-bg-elevated);stroke:var(--ui-stroke-secondary);stroke-width:1}
.flota-lock rect{fill:var(--ui-text-secondary)}
.flota-lock-shackle{fill:none;stroke:var(--ui-text-secondary);stroke-width:1.15;stroke-linecap:round}
.flota-chatb circle{fill:var(--ui-accent);stroke:var(--ui-bg-editor);stroke-width:1.2}
.flota-chatb path{fill:var(--ui-bg-editor)}
.flota-node.active:hover .flota-seat.locked .flota-lock,.flota-node.active:focus-within .flota-seat.locked .flota-lock,.flota-hub.active:hover .flota-seat.locked .flota-lock,.flota-expanded .flota-seat.locked .flota-lock{opacity:1}
.flota-seat.chat:hover .flota-chatb,.flota-seat.chat:focus-visible .flota-chatb,.flota-seat.chat.hot .flota-chatb{opacity:1}
.flota-bottip{min-width:180px;max-width:250px}
.flota-bottip-go{color:var(--ui-accent);font-weight:600}
.flota-bottip-lock{color:var(--ui-text-secondary)}
.flota-crew-sec{display:flex;flex-direction:column;gap:6px;min-width:0}
.flota-crew-title{font-size:12.5px;font-weight:650;color:var(--ui-text-primary)}
.flota-crew-rows{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:2px}
.flota-crew-row{all:unset;box-sizing:border-box;width:100%;display:flex;align-items:center;gap:9px;padding:5px 8px 5px 4px;border-radius:7px;font-size:12.5px}
.flota-crew-row.chat{cursor:pointer}
.flota-crew-row.chat:hover,.flota-crew-row.chat:focus-visible{background:var(--ui-bg-quaternary)}
.flota-crew-row.chat:focus-visible{box-shadow:inset 0 0 0 1.5px var(--ui-accent)}
.flota-crew-face{flex-shrink:0;overflow:visible}
.flota-crew-text{flex:1;min-width:0;display:flex;flex-direction:column;line-height:1.3}
.flota-crew-name{font-weight:600;color:var(--ui-text-primary);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.flota-crew-state{font-size:11.5px;color:var(--ui-text-tertiary)}
.flota-crew-state.awake{color:var(--ui-green)}.flota-crew-state.working{color:var(--ui-accent)}.flota-crew-state.waking{color:var(--ui-yellow)}
.flota-crew-note{font-size:11px;color:var(--ui-text-tertiary)}
.flota-crew-go{flex-shrink:0;font-size:11.5px;font-weight:600;color:var(--ui-accent);opacity:.55;transition:opacity .15s}
.flota-crew-row.chat:hover .flota-crew-go,.flota-crew-row.chat:focus-visible .flota-crew-go{opacity:1}
.flota-crew-lock{flex-shrink:0;display:inline-flex;align-items:center;gap:4px;font-size:11px;color:var(--ui-text-tertiary)}
.flota-crew-lock .flota-lock{opacity:1}
.flota-crew-row.locked .flota-crew-face{opacity:.85}
.flota-av{transform-box:fill-box;transform-origin:center}
.flota-av-bg{fill:var(--av);stroke:var(--ui-bg-editor);stroke-width:1.6}
.flota-av-edge{fill:none;stroke:var(--ui-bg-editor);stroke-width:1.6}
.flota-av-letter{fill:var(--ui-bg-editor);font-weight:760;pointer-events:none}
.flota-av-ant{stroke:var(--av);stroke-width:1.3;stroke-linecap:round}
.flota-av-tip{fill:var(--av)}
.flota-av-ring{fill:none;stroke:var(--av);stroke-width:1.2;opacity:.55}
.flota-av.awake{animation:flota-bob 3.4s ease-in-out infinite}
.flota-av.working{animation:flota-bob 1.1s ease-in-out infinite}
.flota-av.working .flota-av-ring{stroke:var(--ui-accent);opacity:.9}
.flota-av-pulse{fill:none;stroke:var(--ui-accent);stroke-width:2;transform-box:fill-box;transform-origin:center;animation:flota-pulse 1.4s ease-out infinite}
.flota-av.asleep{filter:grayscale(1);opacity:.55}
.flota-av.waking{filter:grayscale(.6);opacity:.85}
.flota-av.waking .flota-av-bg{animation:flota-blinkfill 1.3s ease-in-out infinite}
@keyframes flota-blinkfill{50%{opacity:.55}}
.flota-tip-crew{display:flex;flex-wrap:wrap;gap:3px 9px;margin-top:3px}
.flota-tip-bot{display:inline-flex;align-items:center;gap:4px;font-size:11.5px;color:var(--ui-text-secondary)}
.flota-tip-bot i{width:7px;height:7px;border-radius:50%;display:inline-block}
.flota-tip-bot.asleep{color:var(--ui-text-tertiary)}.flota-tip-bot.asleep i{filter:grayscale(1);opacity:.6}
.flota-tip-bot.working{color:var(--ui-accent)}
/* ship view */
.flota-link.signal{stroke:var(--ui-accent);stroke-width:1.6;stroke-dasharray:2 7;opacity:.6}
.flota-flow.signal{stroke:var(--ui-accent);stroke-width:2.4;stroke-dasharray:2 22;opacity:.9;animation-duration:2.8s}
.flota-node.flagship .flota-disc{fill:color-mix(in srgb,var(--ui-accent) 18%,var(--ui-bg-editor));stroke:var(--ui-accent);filter:drop-shadow(0 0 10px color-mix(in srgb,var(--ui-accent) 40%,transparent))}
.flota-node.flagship .flota-glyph{color:var(--ui-accent)}
.flota-node.flagship .flota-state{fill:var(--ui-text-tertiary)}
.flota-self .flota-hub-disc{fill:color-mix(in srgb,var(--ui-green) 16%,var(--ui-bg-editor));stroke:var(--ui-green);filter:drop-shadow(0 0 16px color-mix(in srgb,var(--ui-green) 45%,transparent))}
.flota-self .flota-hub-ring{stroke:var(--ui-green)}
.flota-self .flota-hub-glyph{color:var(--ui-green)}
.flota-self .flota-flag rect{fill:var(--ui-green)}
/* panels and dialogs */
.flota-stack{display:flex;flex-direction:column;gap:12px;min-width:0}
.flota-dialog-body{display:flex;flex-direction:column;gap:12px;min-width:0}
.flota-hint{font-size:11px;color:var(--ui-text-quaternary);line-height:1.35}
.flota-linkbtn{justify-self:start;background:none;border:0;padding:0;font:inherit;font-size:12px;color:var(--ui-accent);cursor:pointer}
.flota-linkbtn:hover{text-decoration:underline}
.flota-mono,.flota-mono input{font-family:var(--font-mono,ui-monospace,monospace)}
.flota-progress{display:flex;align-items:center;gap:10px;padding:10px 12px;border-radius:8px;font-size:12.5px;color:var(--ui-text-secondary);background:color-mix(in srgb,var(--ui-accent) 8%,transparent);box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--ui-accent) 22%,transparent)}
.flota-spinner{width:14px;height:14px;border-radius:50%;flex-shrink:0;border:2px solid color-mix(in srgb,var(--ui-accent) 25%,transparent);border-top-color:var(--ui-accent);animation:flota-spin .8s linear infinite}
.flota-log{list-style:none;margin:0;padding:10px 12px;border-radius:8px;display:flex;flex-direction:column;gap:5px;background:var(--ui-bg-quaternary);font-family:var(--font-mono,ui-monospace,monospace);font-size:11.5px;line-height:1.45}
.flota-log li{display:grid;grid-template-columns:14px 1fr;gap:6px;color:var(--ui-text-primary);overflow-wrap:anywhere}
.flota-log b{font-weight:700}.flota-log .ok b{color:var(--ui-green)}.flota-log .fail b,.flota-log .fail{color:var(--ui-red)}
.flota-ok{padding:9px 11px;border-radius:8px;font-size:12.5px;color:var(--ui-text-primary);background:color-mix(in srgb,var(--ui-green) 10%,transparent);box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--ui-green) 30%,transparent)}
.flota-code{display:flex;align-items:center;gap:8px;padding:12px 10px 12px 14px;border-radius:10px;background:var(--ui-bg-quaternary);box-shadow:inset 0 0 0 1px var(--ui-stroke-tertiary)}
.flota-code code{flex:1;min-width:0;font-family:var(--font-mono,ui-monospace,monospace);font-size:15px;font-weight:600;letter-spacing:.04em;color:var(--ui-text-primary);overflow-wrap:anywhere;line-height:1.4}
.flota-code-head{display:flex;align-items:baseline;justify-content:space-between;font-size:11.5px;color:var(--ui-text-tertiary)}
.flota-countdown{font-variant-numeric:tabular-nums;color:var(--ui-accent)}.flota-countdown.expired{color:var(--ui-red)}
.flota-fleet-list{list-style:none;margin:0;padding:0;display:flex;flex-direction:column}
.flota-fleet{display:flex;align-items:center;gap:10px;padding:8px 4px;border-top:1px solid var(--ui-stroke-quaternary)}
.flota-fleet:first-child{border-top:0}.flota-fleet.selected{background:var(--ui-bg-quaternary);border-radius:7px}
.flota-fleet-icon{flex-shrink:0;color:var(--ui-accent)}.flota-fleet-icon circle:first-child{fill:color-mix(in srgb,var(--ui-accent) 16%,transparent);stroke:var(--ui-accent);stroke-width:1.2}
.flota-fleet-text{flex:1;min-width:0}.flota-fleet-name{font-size:12.5px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.flota-fp{display:block;font-family:var(--font-mono,ui-monospace,monospace);font-size:10.5px;color:var(--ui-text-quaternary);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.flota-crew-list{display:flex;flex-wrap:wrap;gap:6px}
.flota-crew-chip{display:inline-flex;align-items:center;gap:4px;padding:1px 9px 1px 3px;border-radius:999px;font-size:11.5px;background:var(--ui-bg-quaternary);color:var(--ui-text-primary)}
.flota-crew-chip svg{overflow:visible}.flota-crew-chip.asleep{color:var(--ui-text-tertiary)}
/* onboarding */
.flota-choose{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:26px;padding:24px 8px;min-height:0}
.flota-choose-head{text-align:center}.flota-choose-head .flota-title{font-size:26px}
.flota-choices{display:grid;grid-template-columns:repeat(2,minmax(0,330px));gap:18px}
.flota-choices.small{grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}
@media (max-width:760px){.flota-choices{grid-template-columns:minmax(0,420px)}}
.flota-choice{all:unset;box-sizing:border-box;display:flex;flex-direction:column;gap:9px;padding:20px 22px 22px;border-radius:14px;cursor:pointer;background:var(--ui-bg-card);box-shadow:inset 0 0 0 1px var(--ui-stroke-tertiary);transition:transform .2s,box-shadow .2s}
.flota-choice:hover,.flota-choice:focus-visible{transform:translateY(-2px);box-shadow:inset 0 0 0 1.5px var(--ui-accent),0 12px 32px color-mix(in srgb,var(--ui-accent) 16%,transparent)}
.flota-choices.small .flota-choice{padding:14px 16px 16px}
.flota-choice-art{width:100%;height:110px;margin-bottom:4px}
.flota-choices.small .flota-choice-art{height:78px}
.flota-choice-title{font-size:17px;font-weight:650;color:var(--ui-text-primary)}
.flota-choices.small .flota-choice-title{font-size:14.5px}
.flota-choice-text{font-size:12.5px;line-height:1.55;color:var(--ui-text-secondary)}
.flota-choice-art .flota-hub-disc,.flota-choice-art .flota-disc{filter:none}
.flota-choice-cta{margin-top:auto;padding-top:6px;font-size:12.5px;font-weight:600;color:var(--ui-accent)}
.flota-center{flex:1;display:flex;align-items:flex-start;justify-content:center;padding:12px 0 24px;min-height:0}
.flota-wizard{width:100%;max-width:640px;padding:22px 24px;display:flex;flex-direction:column;gap:16px}
.flota-wiz-head{display:flex;flex-direction:column;gap:4px}
.flota-wiz-lead{font-size:14px;font-weight:620;color:var(--ui-text-primary)}
.flota-wiz-foot{display:flex;align-items:center;gap:8px;padding-top:4px}
.flota-stepper{list-style:none;margin:0 0 2px;padding:0;display:grid;grid-template-columns:repeat(4,1fr);gap:8px}
.flota-step{display:flex;align-items:center;gap:7px;padding-top:9px;font-size:11.5px;color:var(--ui-text-tertiary);border-top:2px solid var(--ui-stroke-quaternary)}
.flota-step b{width:19px;height:19px;border-radius:50%;display:grid;place-items:center;flex-shrink:0;font-size:10.5px;background:var(--ui-bg-quaternary);color:var(--ui-text-secondary)}
.flota-step.on{color:var(--ui-text-primary);border-top-color:var(--ui-accent)}.flota-step.on b{background:var(--ui-accent);color:var(--ui-bg-editor)}
.flota-step.done{border-top-color:color-mix(in srgb,var(--ui-green) 60%,transparent)}.flota-step.done b{background:color-mix(in srgb,var(--ui-green) 20%,transparent);color:var(--ui-green)}
.flota-checks{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:10px}
.flota-checks li{display:grid;grid-template-columns:20px minmax(0,1fr);gap:8px;font-size:12.5px}
.flota-checks li>b{width:18px;height:18px;border-radius:50%;display:grid;place-items:center;font-size:11px;background:var(--ui-bg-quaternary);color:var(--ui-text-tertiary)}
.flota-checks .ok>b{background:color-mix(in srgb,var(--ui-green) 18%,transparent);color:var(--ui-green)}
.flota-checks .fail>b{background:color-mix(in srgb,var(--ui-red) 18%,transparent);color:var(--ui-red)}
.flota-checks .warn>b{background:color-mix(in srgb,var(--ui-yellow) 18%,transparent);color:var(--ui-yellow)}
.flota-check-label{color:var(--ui-text-primary);line-height:18px}
.flota-fix{margin-top:5px}
.flota-waiting{align-items:center;text-align:center;padding:8px 0}
.flota-wait-ping{fill:none;stroke:var(--ui-accent);stroke-width:2;transform-box:fill-box;transform-origin:center;animation:flota-pulse 1.8s ease-out infinite}
/* machines vs bots */
.flota-mflag rect{fill:var(--ui-bg-elevated);stroke:var(--ui-stroke-secondary);stroke-width:1}
.flota-mflag text{fill:var(--ui-text-tertiary);font-size:6.6px;font-weight:700;letter-spacing:.12em}
.flota-node.up .flota-mflag rect{stroke:color-mix(in srgb,var(--ui-green) 55%,transparent)}
.flota-hostname{fill:var(--ui-text-tertiary);font-size:11.5px;font-weight:500}
.flota-nobots{fill:var(--ui-text-quaternary);font-size:10.5px;font-style:italic;paint-order:stroke;stroke:var(--ui-bg-editor);stroke-width:4px}
.flota-tip-host{color:var(--ui-text-tertiary);font-weight:500}
.flota-tip-what{font-size:11px;color:var(--ui-text-tertiary);margin-bottom:3px}
/* reaching the flagship, scope and errors */
.flota-scope-chip{display:inline-flex;align-items:center;gap:6px;padding:2px 10px;border-radius:999px;font-size:11.5px;color:var(--ui-text-secondary);background:color-mix(in srgb,var(--ui-accent) 10%,transparent);box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--ui-accent) 28%,transparent);white-space:nowrap}
.flota-scope-chip i{width:7px;height:7px;border-radius:50%;background:var(--ui-accent);display:inline-block}
.flota-reach{width:100%;max-width:640px;padding:22px 24px;display:flex;flex-direction:column;gap:14px}
.flota-reach-head{display:grid;grid-template-columns:150px minmax(0,1fr);gap:18px;align-items:center}
.flota-reach-art{width:150px;height:78px}
.flota-reach-art .flota-hub-disc,.flota-reach-art .flota-disc{filter:none}
.flota-reach-lead{margin-top:6px;font-size:13px;line-height:1.55;color:var(--ui-text-secondary)}
.flota-reach-found{display:flex;align-items:center;gap:14px;flex-wrap:wrap}
.flota-checkline{display:inline-flex;align-items:center;gap:7px;font-size:12px;color:var(--ui-text-secondary);cursor:pointer}
.flota-checkline input{accent-color:var(--ui-accent)}
.flota-reach-ship{display:flex;flex-direction:column;gap:6px;padding-top:12px;border-top:1px solid var(--ui-stroke-quaternary)}
.flota-errdetails{font-size:11.5px;color:var(--ui-text-tertiary)}
.flota-errdetails summary{cursor:pointer;width:max-content}
.flota-errdetails code,.flota-updateerr code{display:block;margin-top:6px;padding:7px 9px;border-radius:6px;background:var(--ui-bg-quaternary);font-family:var(--font-mono,ui-monospace,monospace);font-size:11px;color:var(--ui-text-secondary);overflow-wrap:anywhere;white-space:pre-wrap}
.flota-updateerr{position:relative}
.flota-updateerr summary{cursor:pointer;color:var(--ui-red);list-style:none}
.flota-updateerr summary::after{content:' ▾'}
.flota-updateerr-body{position:absolute;right:0;top:calc(100% + 6px);z-index:20;width:340px;display:flex;flex-direction:column;gap:8px;padding:11px 12px;border-radius:9px;font-size:12px;line-height:1.5;color:var(--ui-text-secondary);background:var(--ui-bg-elevated);box-shadow:0 8px 28px color-mix(in srgb,var(--ui-base) 18%,transparent),inset 0 0 0 1px var(--ui-stroke-tertiary)}
/* header menu */
.flota-menu{position:relative}
.flota-menu-btn{list-style:none;cursor:pointer;padding:4px 10px;border-radius:6px;font-size:12px;color:var(--ui-text-primary);box-shadow:inset 0 0 0 1px var(--ui-stroke-tertiary)}
.flota-menu-btn::-webkit-details-marker{display:none}
.flota-menu-btn:hover,.flota-menu[open] .flota-menu-btn{background:var(--ui-bg-quaternary)}
.flota-menu-list{position:absolute;right:0;top:calc(100% + 6px);z-index:20;min-width:250px;display:flex;flex-direction:column;padding:5px;border-radius:9px;background:var(--ui-bg-elevated);box-shadow:0 8px 28px color-mix(in srgb,var(--ui-base) 18%,transparent),inset 0 0 0 1px var(--ui-stroke-tertiary)}
.flota-menu-list hr{border:0;border-top:1px solid var(--ui-stroke-quaternary);margin:4px 2px}
.flota-menu-item{all:unset;box-sizing:border-box;display:flex;flex-direction:column;gap:1px;padding:7px 9px;border-radius:6px;font-size:12.5px;color:var(--ui-text-primary);cursor:pointer}
.flota-menu-item small{font-size:11px;color:var(--ui-text-tertiary)}
.flota-menu-item:hover:not(:disabled),.flota-menu-item:focus-visible{background:var(--ui-bg-quaternary)}
.flota-menu-item:disabled{cursor:default;opacity:.55}
.flota-menu-item.danger{color:var(--ui-red)}
@media (prefers-reduced-motion:reduce){.flota-page *,.flota-page *::before,.flota-dialog-body *{animation:none!important;transition:none!important}}
`

// --- SVG pieces ------------------------------------------------------------------------------

const svgStroke = { fill: 'none', stroke: 'currentColor', strokeWidth: 1.8, strokeLinecap: 'round', strokeLinejoin: 'round' }

/** A 24x24 machine glyph centred at (0, 0), scaled by `size / 24`. */
function Glyph({ kind, size = 26, className }) {
  const parts = {
    laptop: [jsx('rect', { x: 5, y: 5, width: 14, height: 10, rx: 1.5 }, 'a'), jsx('path', { d: 'M2.5 18.5h19l-1.5-2.5h-16z' }, 'b')],
    server: [jsx('rect', { x: 4, y: 3.5, width: 16, height: 7, rx: 1.6 }, 'a'), jsx('rect', { x: 4, y: 13.5, width: 16, height: 7, rx: 1.6 }, 'b'),
      jsx('path', { d: 'M7.5 7h.01M7.5 17h.01M11 7h5M11 17h5', strokeWidth: 2.2 }, 'c')],
    desktop: [jsx('rect', { x: 3, y: 4, width: 18, height: 12, rx: 2 }, 'a'), jsx('path', { d: 'M12 16v4M8 20h8' }, 'b')],
    hub: [jsx('circle', { cx: 12, cy: 12, r: 3.2 }, 'a'), jsx('circle', { cx: 12, cy: 3.5, r: 1.8 }, 'b'), jsx('circle', { cx: 4.6, cy: 16.5, r: 1.8 }, 'c'),
      jsx('circle', { cx: 19.4, cy: 16.5, r: 1.8 }, 'd'), jsx('path', { d: 'M12 8.8V5.3M9.3 13.6l-3.2 2M14.7 13.6l3.2 2' }, 'e')],
    // A ship's wheel: rim, hub and eight spokes that run out past the rim into handles.
    helm: [jsx('circle', { cx: 12, cy: 12, r: 7 }, 'rim'), jsx('circle', { cx: 12, cy: 12, r: 2.2 }, 'hub'),
      jsx('path', { d: Array.from({ length: 8 }, (_, i) => {
        const a = (i * Math.PI) / 4
        const p = r => `${(12 + r * Math.cos(a)).toFixed(2)} ${(12 + r * Math.sin(a)).toFixed(2)}`
        return `M${p(2.2)}L${p(10.6)}`
      }).join('') }, 'spokes'),
      ...Array.from({ length: 8 }, (_, i) => jsx('circle', { cx: 12 + 10.9 * Math.cos((i * Math.PI) / 4), cy: 12 + 10.9 * Math.sin((i * Math.PI) / 4), r: 0.9, fill: 'currentColor' }, `k${i}`))],
    clock: [jsx('circle', { cx: 12, cy: 12, r: 9 }, 'a'), jsx('path', { d: 'M12 7v5l3.2 2' }, 'b')]
  }
  const s = size / 24
  return jsx('g', { className, transform: `translate(${-size / 2} ${-size / 2}) scale(${s})`, ...svgStroke, children: parts[kind] || parts.desktop })
}

function Badge2({ x, y, text, tone }) {
  return jsxs('g', { className: `flota-badge${tone ? ` ${tone}` : ''}`, transform: `translate(${x} ${y})`, children: [
    jsx('circle', { r: 9.5 }), jsx('text', { textAnchor: 'middle', dy: '0.36em', children: text })
  ] })
}

const shorten = (s, n = 16) => (s.length > n ? `${s.slice(0, n - 1)}…` : s)
const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`

/** One crew member: a small bot head, 14 wide, its bottom edge at y = 3. */
function Bot({ x = 0, y = 0, mood, delay }) {
  const eyes = mood === 'asleep'
    ? jsx('path', { className: 'flota-bot-lid', d: 'M-4.6 -2.9q1.6 1.5 3.2 0M1.4 -2.9q1.6 1.5 3.2 0' })
    : jsxs('g', { className: 'flota-bot-eyes', children: [
      jsx('circle', { className: 'flota-bot-eye', cx: -2.9, cy: -2.6, r: 1.45 }, 'l'), jsx('circle', { className: 'flota-bot-eye', cx: 2.9, cy: -2.6, r: 1.45 }, 'r')
    ] })
  return jsx('g', { transform: `translate(${x} ${y}) scale(1.15)`, children: jsxs('g', {
    className: `flota-bot ${mood}`, 'data-mood': mood, style: delay ? { animationDelay: `${delay}s` } : undefined, children: [
      jsx('path', { className: 'flota-bot-ant', d: 'M0 -8.5V-11' }, 'ant'),
      jsx('circle', { className: 'flota-bot-tip', cy: -12, r: 1.3 }, 'tip'),
      mood === 'working' && jsx('circle', { className: 'flota-bot-ping', cy: -12, r: 1.3 }, 'ping'),
      jsx('rect', { className: 'flota-bot-head', x: -7, y: -8.5, width: 14, height: 11.5, rx: 3.6 }, 'head'),
      eyes
    ] }) })
}

/** One bot of a crew with an avatar: its picture clipped to a circle, or a generated one (its
 *  initial on its colour, with a little antenna so it reads as a bot). Grey while asleep. */
function Avatar({ bot, r = 8.5, delay, title = true }) {
  const mood = bot.mood
  const lit = mood === 'awake' || mood === 'working'
  return jsxs('g', {
    className: `flota-av ${mood}`, 'data-mood': mood, 'data-avatar': bot.avatar ? 'img' : 'gen',
    style: { '--av': botColor(bot), animationDelay: delay ? `${delay}s` : undefined }, children: [
      title && jsx('title', { children: `${bot.name} · ${MOOD_TEXT[mood] || mood}` }, 't'),
      mood === 'working' && jsx('circle', { className: 'flota-av-pulse', r: r + 1 }, 'p'),
      lit && jsx('circle', { className: 'flota-av-ring', r: r + 2.3 }, 'ring'),
      !bot.avatar && jsx('path', { className: 'flota-av-ant', d: `M0 ${-r}V${-r - 3.2}` }, 'ant'),
      !bot.avatar && jsx('circle', { className: 'flota-av-tip', cy: -r - 4.3, r: 1.35 }, 'tip'),
      jsx('circle', { className: 'flota-av-bg', r }, 'bg'),
      bot.avatar
        ? jsx('image', { className: 'flota-av-img', href: bot.avatar, x: -r, y: -r, width: 2 * r, height: 2 * r, preserveAspectRatio: 'xMidYMid slice', clipPath: 'url(#flota-av-clip)' }, 'img')
        : jsx('text', { className: 'flota-av-letter', textAnchor: 'middle', dy: '0.36em', style: { fontSize: `${(r * 1.18).toFixed(1)}px` }, children: botInitial(bot.name) }, 'l'),
      bot.avatar && jsx('circle', { className: 'flota-av-edge', r }, 'edge')
    ] })
}

/** A tiny padlock: this bot is view-only from here. */
function LockBadge({ x, y }) {
  return jsxs('g', { className: 'flota-lock', transform: `translate(${x.toFixed(1)} ${y.toFixed(1)})`, 'aria-hidden': true, children: [
    jsx('circle', { r: 5.4 }, 'c'), jsx('path', { className: 'flota-lock-shackle', d: 'M-1.6 -0.4v-1.3a1.6 1.6 0 0 1 3.2 0v1.3' }, 's'),
    jsx('rect', { x: -2.6, y: -0.6, width: 5.2, height: 3.9, rx: 0.9 }, 'b')
  ] })
}

/** A tiny speech bubble: click to open this bot's chat. */
function ChatBadge({ x, y }) {
  return jsxs('g', { className: 'flota-chatb', transform: `translate(${x.toFixed(1)} ${y.toFixed(1)})`, 'aria-hidden': true, children: [
    jsx('circle', { r: 5.4 }, 'c'), jsx('path', { d: 'M-2.6 -2h5.2a.8 .8 0 0 1 .8 .8v2.4a.8 .8 0 0 1 -.8 .8h-2.6l-1.6 1.4v-1.4h-1a.8 .8 0 0 1 -.8 -.8v-2.4a.8 .8 0 0 1 .8 -.8z' }, 'b')
  ] })
}

/** A crew seat's accessible name. */
export function seatLabel(b, a) {
  const mood = MOOD_TEXT[b.mood] || b.mood
  return a.ok ? `Open chat with ${b.name} (${mood})` : `${b.name} (${mood}), view only. ${a.hint}`
}

const AvatarClip = () => jsx('clipPath', { id: 'flota-av-clip', clipPathUnits: 'objectBoundingBox', children: jsx('circle', { cx: 0.5, cy: 0.5, r: 0.5 }) })

/** A node's crew. At rest: a compact overlapping stack (CREW_CAP + '+N'), grey with drifting z's
 *  while asleep. On hover or focus of an active node the seats fan out into a ring (pure CSS). */
function Crew({ c, seats, expandable, access = null, onBot = () => {}, onBotHover = () => {} }) {
  const members = c.all.slice(0, seats.shown)
  const rowY = seats.compact.length ? seats.compact[0].y : 0
  const hiddenNames = c.all.slice(c.members.length).map(b => `${b.name} · ${MOOD_TEXT[b.mood] || b.mood}`).join('\n')
  return jsxs('g', {
    className: `flota-crew ${c.mood}${expandable ? ' expandable' : ''}`, 'data-crew': c.total, 'data-shown': seats.vis,
    'data-expand': expandable ? 'ring' : 'none', children: [
      expandable && jsx('circle', { className: 'flota-crew-orbit', r: seats.R }, 'orbit'),
      ...members.map((b, i) => {
        const p = seats.compact[i]
        const q = seats.ring[i]
        // Only a fanned-out ring's bots are doors to their chats; a compact stack just shows who is aboard.
        const a = expandable && c.avatars && access ? access(b) : null
        // A locked bot's click falls through to its ship, whose details say how to connect it.
        const open = e => { if (a.ok) { e.stopPropagation(); onBot(b, a) } }
        return jsxs('g', {
          className: `flota-seat${i >= seats.vis ? ' extra' : ''}${a ? (a.ok ? ' chat' : ' locked') : ''}`, 'data-ring': expandable ? `${q.x.toFixed(1)},${q.y.toFixed(1)}` : undefined,
          style: { '--cx': p.x.toFixed(1), '--cy': p.y.toFixed(1), '--rx': (expandable ? q.x : p.x).toFixed(1), '--ry': (expandable ? q.y : p.y).toFixed(1), '--i': i },
          ...(a ? {
            role: 'button', tabIndex: 0, 'data-bot': b.id, 'data-access': a.ok ? 'chat' : 'locked', 'aria-label': seatLabel(b, a),
            'aria-disabled': a.ok ? undefined : 'true', onClick: open,
            onKeyDown: e => { if (a.ok && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); open(e) } },
            onMouseEnter: e => onBotHover(b, a, e.currentTarget), onMouseLeave: () => onBotHover(null),
            onFocus: e => { e.stopPropagation(); onBotHover(b, a, e.currentTarget) }, onBlur: e => { e.stopPropagation(); onBotHover(null) }
          } : {}),
          children: [
            c.avatars ? jsx(Avatar, { bot: b, delay: (i * 0.37).toFixed(2), title: !a }, 'av') : jsx(Bot, { y: 4, mood: b.mood, delay: (i * 0.37).toFixed(2) }, 'av'),
            a && !a.ok && jsx(LockBadge, { x: 6.6, y: 6.2 }, 'lock'),
            a && a.ok && jsx(ChatBadge, { x: 6.6, y: -6.4 }, 'chat')
          ]
        }, `${b.role}-${b.id || b.name}-${i}`)
      }),
      c.extra > 0 && jsxs('text', { className: 'flota-crew-more', x: seats.moreX - (c.avatars ? 0 : -4), y: rowY + 4, children: [`+${c.extra}`, jsx('title', { children: hiddenNames }, 't')] }, 'more'),
      ...(c.mood === 'asleep' ? [0, 1, 2].map(i => jsx('text', {
        className: 'flota-z', x: seats.moreX + (c.extra ? 16 : -2) + i * 3, y: rowY - 8 - i * 4, style: { animationDelay: `${i * 1.2}s`, fontSize: 7.5 + i * 1.5 }, children: 'z'
      }, `z${i}`)) : [])
    ] })
}

function ClockBadge({ x, y }) {
  return jsxs('g', { className: 'flota-clock', transform: `translate(${x} ${y})`, children: [
    jsx('circle', { r: 8.5 }), jsx('path', { d: 'M0 -4.2V0l2.8 1.8' })
  ] })
}

function MachineNode({ m, pos, r, cx, cy, selected, onSelect, onHover, wake, net = null, onBot = () => {}, onBotHover = () => {}, expanded = false }) {
  const state = nodeState(m)
  const above = pos.y < cy - 20 // crew and labels go on the outside so links never cross them
  const ly = above ? -r - 51 : r + 49 // name baseline; the status line sits 17 below it
  const crewY = above ? -r - 12 : r + 23
  const dx = pos.x - cx
  const anchor = Math.abs(dx) > 300 ? (dx > 0 ? 'start' : 'end') : 'middle'
  const lx = anchor === 'middle' ? 0 : (dx > 0 ? -r : r)
  const busy = m.busy > 0 // a subagent waiting for a wake counts too
  const c = crew(m)
  const active = state === 'up' && c.total > 0 // only a ship under way fans its crew out
  const seats = crewSeats({ count: c.total, r, rowY: crewY - 4, anchor, edge: r, labelAbove: above, step: c.avatars ? 13 : 18 })
  const push = active ? seats.R + 16 - r : 0 // the tooltip clears the expanded ring
  const label = `${m.name}: ${hostLabel(m) ? `machine ${hostLabel(m)}, ` : ''}${statusLabel(m)}, ${c.avatars && !c.total ? NO_BOTS : `crew of ${c.total}`} ${c.working ? `(${c.working} working)` : c.mood}${wake ? `, next scheduled wake ${relTime(wake.row.next)}` : ''}`
  const children = [
    active && jsx('circle', { className: 'flota-hit', r: seats.R + 12 }, 'hit'),
    jsx('circle', { className: 'flota-select', r: r + 7 }, 'sel'),
    state === 'waking' && jsx('circle', { className: 'flota-pulse', r: r + 2 }, 'pulse'),
    busy && jsx('circle', { className: 'flota-busy-ring', r: r + 5 }, 'busy'),
    jsx('circle', { className: 'flota-disc', r }, 'disc'),
    jsx(Glyph, { kind: machineKind(m), size: r * 0.95, className: 'flota-glyph' }, 'glyph'),
    // On the inner side of the disc: the crew and the labels sit on the outer side.
    jsxs('g', { className: 'flota-mflag', transform: `translate(0 ${above ? r : -r})`, 'data-machine-badge': '', children: [
      jsx('title', { children: MACHINE_TIP }, 't'),
      jsx('rect', { x: -20, y: -5.5, width: 40, height: 11, rx: 5.5 }, 'r'), jsx('text', { textAnchor: 'middle', dy: '0.36em', children: 'MACHINE' }, 'x')
    ] }, 'mflag'),
    busy && jsx(Badge2, { x: r * 0.74, y: -r * 0.74, text: String(m.busy) }, 'count'),
    !m.profile_exists && jsx(Badge2, { x: -r * 0.74, y: -r * 0.74, text: '!', tone: 'warn' }, 'setup'),
    m.wake_error && state === 'down' && jsx(Badge2, { x: r * 0.74, y: -r * 0.74, text: '!', tone: 'err' }, 'err'),
    wake && jsx(ClockBadge, { x: -r * 0.74, y: r * 0.74 }, 'clock'),
    jsx(Crew, { c, seats, expandable: active, access: b => chatAccess(b, m, net), onBot: (b, a) => onBot(b, a, m), onBotHover: (b, a, el) => onBotHover(b, a, el, m) }, 'crew'),
    c.avatars && c.total === 0 && jsx('text', { className: 'flota-nobots', x: anchor === 'middle' ? 0 : lx, y: crewY, textAnchor: anchor, children: NO_BOTS }, 'nobots'),
    jsxs('text', { className: 'flota-name', x: lx, y: ly, textAnchor: anchor, children: [shorten(m.name),
      hostLabel(m) && jsx('tspan', { className: 'flota-hostname', children: ` · ${shorten(hostLabel(m), 18)}` }, 'h')] }, 'name'),
    jsx('text', { className: 'flota-state', x: lx, y: ly + 17, textAnchor: anchor, children: statusLabel(m) }, 'state')
  ]
  return jsx('g', {
    className: `flota-node ${state}${active ? ' active' : ''}${selected ? ' selected' : ''}${expanded && active ? ' flota-expanded' : ''}`, transform: `translate(${pos.x.toFixed(1)} ${pos.y.toFixed(1)})`,
    role: 'button', tabIndex: 0, 'aria-label': label, 'data-machine': m.name, 'data-state': state,
    onClick: () => onSelect(m.name),
    onKeyDown: e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onSelect(m.name) } },
    onMouseEnter: e => onHover(m.name, e.currentTarget, above, push), onMouseLeave: () => onHover(null), onFocus: e => onHover(m.name, e.currentTarget, above, push), onBlur: () => onHover(null),
    children: jsx('g', { className: 'flota-pop', children })
  }, m.name)
}

/** SVG path from the hub edge to the node edge: straight, or a curve through `to.via`. */
export function linkPath(from, to, r0, r1) {
  const f = n => n.toFixed(1)
  const unit = (a, b) => { const l = Math.hypot(b.x - a.x, b.y - a.y) || 1; return { x: (b.x - a.x) / l, y: (b.y - a.y) / l } }
  // Quadratic control point that makes the curve pass through `via` at its midpoint.
  const c = to.via ? { x: 2 * to.via.x - (from.x + to.x) / 2, y: 2 * to.via.y - (from.y + to.y) / 2 } : null
  const u0 = unit(from, c || to)
  const u1 = unit(c || from, to)
  const a = { x: from.x + u0.x * r0, y: from.y + u0.y * r0 }
  const b = { x: to.x - u1.x * r1, y: to.y - u1.y * r1 }
  return c ? `M${f(a.x)} ${f(a.y)}Q${f(c.x)} ${f(c.y)} ${f(b.x)} ${f(b.y)}` : `M${f(a.x)} ${f(a.y)}L${f(b.x)} ${f(b.y)}`
}

function Link({ from, to, r0, r1, state }) {
  const d = linkPath(from, to, r0, r1)
  return jsxs('g', { children: [
    jsx('path', { className: `flota-link ${state}`, d }),
    state !== 'down' && jsx('path', { className: `flota-flow ${state}`, d })
  ] })
}

function Tooltip({ m, at, wake }) {
  const c = crew(m)
  const crewText = c.avatars
    ? (c.total ? `${plural(c.total, 'bot')}${c.working ? ` · ${c.working} working` : ` · ${c.mood}`}` : NO_BOTS)
    : `${plural(c.total, 'agent')}${c.working ? ` · ${c.working} working` : ` · ${c.mood}`}`
  return jsxs('div', {
    className: `flota-tip${at.side ? ` side-${at.side}` : at.below ? ' below' : ''}`, role: 'tooltip', style: { left: at.x, top: at.y },
    children: [
      jsxs('b', { children: [m.name, hostLabel(m) && jsx('span', { className: 'flota-tip-host', children: ` · ${hostLabel(m)}` }, 'h'), jsx('span', { className: 'flota-tag', children: 'Machine' }, 'tag')] }),
      jsx('div', { className: 'flota-tip-what', children: MACHINE_TIP }),
      jsxs('div', { children: [jsx('span', { children: 'Host ' }), `${m.user ? `${m.user}@` : ''}${m.host}${m.port && m.port !== 22 ? `:${m.port}` : ''}`] }),
      jsxs('div', { children: [jsx('span', { children: 'OS ' }), osName(m.os)] }),
      jsxs('div', { children: [jsx('span', { children: 'Wake ' }), wakeText(m)] }),
      jsxs('div', { children: [jsx('span', { children: 'Sleep ' }), sleepText(m)] }),
      jsxs('div', { children: [jsx('span', { children: c.avatars ? 'Bots ' : 'Crew ' }), crewText] }),
      c.avatars && jsx('div', { className: 'flota-tip-crew', children: c.all.slice(0, 12).map(b => jsxs('span', { className: `flota-tip-bot ${b.mood}`, children: [jsx('i', { style: { background: botColor(b) } }), b.name] }, b.id)) }),
      c.avatars && c.all.length > 12 && jsx('div', { children: jsx('span', { children: `+${c.all.length - 12} more` }) }),
      wake && jsxs('div', { children: [jsx('span', { children: 'Next job ' }), `${wake.row.name} · ${relTime(wake.row.next)}`] })
    ]
  })
}

/** Hovering a bot in a fanned-out ring: who it is, and whether its chat is one click away. */
export function BotTip({ t }) {
  const mood = MOOD_TEXT[t.bot.mood] || t.bot.mood
  const place = t.side ? ` side-${t.side}` : t.below ? ' below' : ''
  return jsxs('div', { className: `flota-tip flota-bottip${t.access.ok ? ' ok' : ' locked'}${place}`, role: 'tooltip', 'data-bot-tip': t.bot.id, style: { left: t.x, top: t.y }, children: [
    jsx('b', { children: t.bot.name }, 'n'),
    jsx('div', { children: jsx('span', { children: `${mood}${t.m ? ` · on ${t.m.name}` : ''}` }) }, 's'),
    t.access.ok
      ? jsx('div', { className: 'flota-bottip-go', children: `${t.access.hint} →` }, 'go')
      : jsx('div', { className: 'flota-bottip-lock', children: t.access.hint }, 'lock'),
    t.access.ok && sleepNote(t.bot, t.m) && jsx('div', { children: jsx('span', { children: sleepNote(t.bot, t.m) }) }, 'note')
  ] })
}

/** Radar backdrop: range rings and bearings around the flagship, a slow sonar sweep, faint swell. */
function Radar({ L }) {
  const rings = [0.3, 0.56, 0.82].map(k => Math.round(Math.min(L.view.w, L.view.h) * k / 1.15))
  const R = rings[rings.length - 1]
  const slices = Array.from({ length: 7 }, (_, i) => {
    const a0 = (-i - 1) * 0.07
    const a1 = -i * 0.07
    const p = a => `${(R * Math.cos(a)).toFixed(1)} ${(R * Math.sin(a)).toFixed(1)}`
    return jsx('path', { className: 'flota-sweep-slice', d: `M0 0L${p(a0)}A${R.toFixed(0)} ${R.toFixed(0)} 0 0 1 ${p(a1)}Z`, style: { fillOpacity: 0.05 * (1 - i / 7) ** 1.5 } }, `s${i}`)
  })
  const swell = (y, w, k) => {
    const n = Math.ceil(w / 60)
    let d = `M${(L.cx - w / 2).toFixed(0)} ${y}`
    for (let i = 0; i < n; i++) d += `q15 ${-4 * k} 30 0t30 0`
    return d
  }
  const bottom = L.view.y + L.view.h
  return jsxs('g', { className: 'flota-radar', 'aria-hidden': true, children: [
    jsx('path', { className: 'flota-bearing', d: `M${L.view.x} ${L.cy}H${L.view.x + L.view.w}M${L.cx} ${L.view.y}V${bottom}` }, 'b'),
    ...rings.map((r, i) => jsx('circle', { className: 'flota-range', cx: L.cx, cy: L.cy, r }, `r${i}`)),
    jsx('path', { className: 'flota-swell', d: swell(bottom - 34, L.view.w * 0.5, 1) }, 'w1'),
    jsx('path', { className: 'flota-swell', d: swell(bottom - 22, L.view.w * 0.36, 0.7), style: { opacity: 0.06 } }, 'w2'),
    jsx('g', { transform: `translate(${L.cx} ${L.cy})`, children: jsxs('g', { className: 'flota-sweep', children: [
      jsx('circle', { r: R, fill: 'none' }, 'box'), // makes the group's box centred on the flagship
      ...slices,
      jsx('path', { className: 'flota-sweep-edge', d: `M0 0L${R.toFixed(0)} 0` }, 'edge')
    ] }) }, 'sweep')
  ] })
}

/** The flagship's (or this ship's) own crew: every bot on this Hermes host. Compact above the
 *  disc, fanning out around it on hover while the host is active. */
function HubCrew({ list, active, linkAngles = [], hubR = 50, net = null, onBot = () => {}, onBotHover = () => {} }) {
  const bots = (Array.isArray(list) ? list : []).filter(b => b && !b.ship_hand)
  if (!bots.length) return { node: null, active: false, R: hubR, label: '' }
  const c = crew({ crew: bots, up: true, name: 'hub' })
  const at = hubCrewAnchor(linkAngles, hubR)
  // Off to one side the stack grows away from the vertical, so it never sits on a link.
  const side = at.x > 10 ? 'start' : at.x < -10 ? 'end' : 'middle'
  const seats = crewSeats({ count: c.total, r: hubR, rowY: at.y, rowX: at.x, anchor: side, edge: 0, hub: true, step: 13 })
  const expandable = active && c.total > 0
  const label = `, crew of ${c.total}${c.working ? ` (${c.working} working)` : ''}`
  const crewNode = jsx(Crew, { c, seats, expandable, access: b => chatAccess(b, null, net), onBot: (b, a) => onBot(b, a, null), onBotHover: (b, a, el) => onBotHover(b, a, el, null) }, 'hubcrew')
  return { node: crewNode, active: expandable, R: seats.R, label }
}

/** The network map: the Hermes host in the centre, machines around it. Pure render of `machines`. */
/** Where a ring seat's tooltip goes, in px inside the card: on the far side of the seat from its
 *  node's disc, so it never covers the node or the rest of the ring. `seat`, `disc`, `card` are
 *  client rects. Returns { x, y, side?, below? } (the .flota-tip placement classes). */
export function tipPlacement(seat, disc, card) {
  const sx = seat.left + seat.width / 2
  const sy = seat.top + seat.height / 2
  const dx = sx - (disc ? disc.left + disc.width / 2 : sx)
  const dy = sy - (disc ? disc.top + disc.height / 2 : sy + 1)
  const x = sx - card.left
  const y = sy - card.top
  const W = 250 // a bot tooltip's widest
  const roomRight = card.width - x > W + seat.width
  const roomLeft = x > W + seat.width
  if (Math.abs(dx) > Math.abs(dy) * 0.8 && (dx > 0 ? roomRight : roomLeft)) {
    return dx > 0 ? { x: x + seat.width / 2 + 8, y, side: 'right' } : { x: x - seat.width / 2 - 8, y, side: 'left' }
  }
  const cx = Math.min(Math.max(x, W / 2 + 6), card.width - W / 2 - 6) // keep it inside the card
  const below = dy > 0 ? card.height - y > 120 : y < 110
  return below ? { x: cx, y: y + seat.height / 2 + 8, below: true } : { x: cx, y: y - seat.height / 2 - 8 }
}

/** Hover handler for a ring seat: puts its tooltip just outside the ring. */
function botHoverAt(wrap, setBotTip) {
  return (bot, access, el, m = null) => {
    if (!bot || !el || !el.getBoundingClientRect || !wrap.current) return setBotTip(null)
    const node = el.closest && el.closest('.flota-node, .flota-hub')
    const disc = node && node.querySelector('.flota-disc, .flota-hub-disc')
    setBotTip({ bot, access, m, ...tipPlacement(el.getBoundingClientRect(), disc && disc.getBoundingClientRect(), wrap.current.getBoundingClientRect()) })
  }
}

export function FleetMap({ hub, machines, selected, onSelect = () => {}, onAdd, offline = false, schedule = [], hubCrew = null, hubActive = true,
  net = null, onBot = (b, a) => openBotChat(b, a), expanded = null }) {
  const list = machines || []
  const wrap = useRef(null)
  const [hover, setHover] = useState(null) // { name, x, y } in px inside the card
  const [botTip, setBotTip] = useState(null) // { bot, access, m, x, y }
  const onBotHover = botHoverAt(wrap, setBotTip)
  const onHover = (name, el, below, push = 0) => {
    const disc = el && el.querySelector && el.querySelector('.flota-disc')
    if (!name || !disc || !wrap.current) return setHover(null)
    const a = disc.getBoundingClientRect()
    const b = wrap.current.getBoundingClientRect()
    const box = wrap.current.querySelector('svg')
    const sb = box ? box.getBoundingClientRect() : b
    const k = Math.min(sb.width / L.view.w, sb.height / L.view.h) || 1 // svg units -> px (viewBox 'meet')
    if (push > 0) {
      // The crew fans out around the node: the tooltip goes beside the ring, toward the middle.
      const right = a.left + a.width / 2 < b.left + b.width / 2
      const reach = (L.r + push) * k
      return setHover({ name, side: right ? 'right' : 'left', x: a.left - b.left + a.width / 2 + (right ? reach : -reach), y: a.top - b.top + a.height / 2 })
    }
    // Below a node whose label sits above it, above otherwise: the tooltip never hides the name.
    setHover({ name, below, x: a.left - b.left + a.width / 2, y: below ? a.bottom - b.top + 8 : a.top - b.top - 8 })
  }
  const L = layoutRing(list.length)
  const centre = { x: L.cx, y: L.cy }
  const hubName = (hub && hub.name) || 'this host'
  // An empty fleet shows three dashed slots where machines will appear; each one opens the form.
  const ghosts = list.length || offline ? [] : layoutRing(3).nodes
  const hovered = hover && list.find(m => m.name === hover.name)
  const now = Date.now()
  const wakeOf = name => nextWake((schedule || []).filter(r => r.machine === name), now)
  const fleetWake = nextWake(schedule, now)
  const linkAngles = L.nodes.map(n => Math.atan2((n.via || n).y - L.cy, (n.via || n).x - L.cx))
  const hub$ = HubCrew({ list: hubCrew, active: hubActive && !offline, linkAngles, hubR: L.hubR, net, onBot, onBotHover })
  const hubSub = offline ? 'waiting for the Flota backend' : !list.length ? 'no ships yet'
    : fleetWake ? `${plural(list.length, 'ship')} · next wake ${relTime(fleetWake.row.next, now)}` : `${plural(list.length, 'ship')} in the fleet`
  const svg = jsxs('svg', {
    className: 'flota-map', viewBox: `${L.view.x} ${L.view.y} ${L.view.w} ${L.view.h}`, preserveAspectRatio: 'xMidYMid meet', role: 'img',
    'aria-label': `Fleet map: ${plural(list.length, 'ship')} around the flagship ${hubName}`,
    children: [
      jsx('defs', { children: [jsxs('radialGradient', { id: 'flota-hub-glow', children: [
        jsx('stop', { offset: '0%', style: { stopColor: 'var(--ui-accent)', stopOpacity: 0.22 } }),
        jsx('stop', { offset: '100%', style: { stopColor: 'var(--ui-accent)', stopOpacity: 0 } })
      ] }, 'g'), jsx(AvatarClip, {}, 'clip')] }),
      jsx(Radar, { L }, 'radar'),
      jsx('circle', { cx: L.cx, cy: L.cy, r: 170, fill: 'url(#flota-hub-glow)' }),
      ...L.rings.map((ring, i) => jsx('ellipse', { className: 'flota-orbit', cx: L.cx, cy: L.cy, rx: ring.rx, ry: ring.ry }, `orbit${i}`)),
      ...ghosts.map((g, i) => jsx('path', { className: 'flota-link down', d: linkPath(centre, g, L.hubR + 6, 32) }, `gl${i}`)),
      ...list.map((m, i) => jsx(Link, { from: centre, to: L.nodes[i], r0: L.hubR + 6, r1: L.r + 4, state: nodeState(m) }, `l-${m.name}`)),
      jsxs('g', {
        className: `flota-hub${hub$.active ? ' active' : ''}`, transform: `translate(${L.cx} ${L.cy})`, role: 'button', tabIndex: 0, 'data-flagship': hubName,
        'aria-label': `Flagship: Hermes on ${hubName}${hub$.label}. Show the fleet overview and schedule`,
        onClick: () => onSelect(null), onKeyDown: e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onSelect(null) } },
        children: [
          hub$.active && jsx('circle', { className: 'flota-hit', r: hub$.R + 13 }, 'hit'),
          jsx('circle', { className: 'flota-hub-ring', r: L.hubR + 10 }),
          jsx('circle', { className: 'flota-hub-disc', r: L.hubR }),
          jsx('g', { className: 'flota-hub-helm', children: jsx(Glyph, { kind: 'helm', size: 50, className: 'flota-hub-glyph' }) }),
          jsxs('g', { className: 'flota-flag', transform: `translate(0 ${L.hubR})`, children: [
            jsx('rect', { x: -32, y: -8, width: 64, height: 16, rx: 8 }), jsx('text', { textAnchor: 'middle', dy: '0.36em', children: 'FLAGSHIP' })
          ] }),
          jsx('text', { className: 'flota-hub-name', y: L.hubR + 32, textAnchor: 'middle', children: `Hermes · ${shorten(hubName, 22)}` }),
          jsx('text', { className: 'flota-hub-sub', y: L.hubR + 50, textAnchor: 'middle', children: hubSub }),
          hub$.node
        ]
      }),
      ...ghosts.map((g, i) => jsxs('g', {
        className: 'flota-ghost-g', transform: `translate(${g.x} ${g.y})`, onClick: onAdd, role: 'button', 'aria-label': 'Add machine',
        children: [jsx('circle', { className: 'flota-ghost', r: 28 }), jsx('path', { className: 'flota-ghost-plus', d: 'M-7 0h14M0 -7v14' }),
          i === 0 && jsx('text', { className: 'flota-state', y: -44, textAnchor: 'middle', children: 'Add machine' })]
      }, `g${i}`)),
      ...list.map((m, i) => jsx(MachineNode, { m, pos: L.nodes[i], r: L.r, cx: L.cx, cy: L.cy, selected: m.name === selected, onSelect, onHover, wake: wakeOf(m.name), net, onBot, onBotHover, expanded: [].concat(expanded || []).includes(m.name) }, m.name))
    ]
  })
  return jsxs('div', { className: 'flota-card flota-mapwrap', ref: wrap, children: [
    svg,
    botTip ? jsx(BotTip, { t: botTip }) : hovered && jsx(Tooltip, { m: hovered, at: hover, wake: wakeOf(hovered.name) }),
    list.length > 0 && jsxs('div', { className: 'flota-legend', 'aria-hidden': true, children: [
      jsxs('span', { children: [jsx('i', { style: { background: 'var(--ui-green)' } }), 'Under way'] }),
      jsxs('span', { children: [jsx('i', { style: { background: 'var(--ui-yellow)' } }), 'Raising anchor'] }),
      jsxs('span', { children: [jsx('i', { style: { background: 'var(--ui-text-quaternary)' } }), 'Docked, crew asleep'] }),
      jsxs('span', { children: [jsx('i', { style: { background: 'var(--ui-accent)' } }), 'Crew working'] })
    ] })
  ] })
}

/** A joined fleet's date: '2026-11-10T…' or epoch -> 'joined Nov 10'. */
export function joinedText(t) {
  const ms = toMs(t)
  return ms ? `joined ${new Date(ms).toLocaleDateString([], { month: 'short', day: 'numeric' })}` : 'joined'
}

/** The ship's view: this machine in the centre as a ship (with its own crew) and a dotted signal
 *  line out to every flagship whose fleet it joined. */
export function ShipMap({ hub, fleets, selected, onSelect = () => {}, crew: hubCrew = null, net = null, onBot = (b, a) => openBotChat(b, a) }) {
  const list = fleets || []
  const wrap = useRef(null)
  const [botTip, setBotTip] = useState(null)
  const onBotHover = botHoverAt(wrap, setBotTip)
  const L = layoutRing(list.length)
  const centre = { x: L.cx, y: L.cy }
  const me = (hub && hub.name) || 'this machine'
  const linkAngles = L.nodes.map(n => Math.atan2((n.via || n).y - L.cy, (n.via || n).x - L.cx))
  const hub$ = HubCrew({ list: hubCrew, active: true, linkAngles, hubR: L.hubR, net, onBot, onBotHover })
  const svg = jsxs('svg', {
    className: 'flota-map', viewBox: `${L.view.x} ${L.view.y} ${L.view.w} ${L.view.h}`, preserveAspectRatio: 'xMidYMid meet', role: 'img',
    'aria-label': `Ship view: ${me} belongs to ${plural(list.length, 'fleet')}`,
    children: [
      jsx('defs', { children: [jsxs('radialGradient', { id: 'flota-self-glow', children: [
        jsx('stop', { offset: '0%', style: { stopColor: 'var(--ui-green)', stopOpacity: 0.18 } }),
        jsx('stop', { offset: '100%', style: { stopColor: 'var(--ui-green)', stopOpacity: 0 } })
      ] }, 'g'), jsx(AvatarClip, {}, 'clip')] }),
      jsx(Radar, { L }, 'radar'),
      jsx('circle', { cx: L.cx, cy: L.cy, r: 160, fill: 'url(#flota-self-glow)' }, 'glow'),
      ...L.rings.map((ring, i) => jsx('ellipse', { className: 'flota-orbit', cx: L.cx, cy: L.cy, rx: ring.rx, ry: ring.ry }, `orbit${i}`)),
      ...list.map((f, i) => {
        const d = linkPath(L.nodes[i], centre, L.r + 6, L.hubR + 6)
        return jsxs('g', { children: [jsx('path', { className: 'flota-link signal', d }), jsx('path', { className: 'flota-flow signal', d })] }, `l-${f.flagship}`)
      }),
      jsxs('g', {
        className: `flota-hub flota-self${hub$.active ? ' active' : ''}`, transform: `translate(${L.cx} ${L.cy})`, role: 'button', tabIndex: 0, 'data-ship-self': me,
        'aria-label': `This machine, ${me}: a ship in ${plural(list.length, 'fleet')}${hub$.label}`,
        onClick: () => onSelect(null), onKeyDown: e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onSelect(null) } },
        children: [
          hub$.active && jsx('circle', { className: 'flota-hit', r: hub$.R + 13 }, 'hit'),
          jsx('circle', { className: 'flota-hub-ring', r: L.hubR + 10 }, 'ring'),
          jsx('circle', { className: 'flota-hub-disc', r: L.hubR }, 'disc'),
          jsx(Glyph, { kind: machineKind({ os: hub && hub.os, name: me }), size: 44, className: 'flota-hub-glyph' }, 'glyph'),
          jsxs('g', { className: 'flota-flag', transform: `translate(0 ${L.hubR})`, children: [
            jsx('rect', { x: -24, y: -8, width: 48, height: 16, rx: 8 }), jsx('text', { textAnchor: 'middle', dy: '0.36em', children: 'SHIP' })
          ] }, 'flag'),
          jsx('text', { className: 'flota-hub-name', y: L.hubR + 32, textAnchor: 'middle', children: `This machine · ${shorten(me, 20)}` }, 'name'),
          jsx('text', { className: 'flota-hub-sub', y: L.hubR + 50, textAnchor: 'middle', children: list.length === 1 ? `a ship of ${shorten(String(list[0].flagship || '?'), 18)}` : `a ship in ${plural(list.length, 'fleet')}` }, 'sub'),
          hub$.node
        ]
      }, 'self'),
      ...list.map((f, i) => {
        const pos = L.nodes[i]
        const above = pos.y < L.cy - 20
        const ly = above ? -L.r - 22 : L.r + 26
        const dx = pos.x - L.cx
        const anchor = Math.abs(dx) > 300 ? (dx > 0 ? 'start' : 'end') : 'middle'
        const lx = anchor === 'middle' ? 0 : (dx > 0 ? -L.r : L.r)
        const name = String(f.flagship || '?')
        return jsx('g', {
          className: `flota-node flagship${name === selected ? ' selected' : ''}`, transform: `translate(${pos.x.toFixed(1)} ${pos.y.toFixed(1)})`,
          role: 'button', tabIndex: 0, 'data-flagship-of': name, 'aria-label': `Flagship ${name}, ${joinedText(f.joined_at)}`,
          onClick: () => onSelect(name), onKeyDown: e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onSelect(name) } },
          children: jsxs('g', { className: 'flota-pop', children: [
            jsx('circle', { className: 'flota-select', r: L.r + 7 }, 'sel'),
            jsx('circle', { className: 'flota-disc', r: L.r }, 'disc'),
            jsx('g', { className: 'flota-hub-helm', children: jsx(Glyph, { kind: 'helm', size: L.r * 1.05, className: 'flota-glyph' }) }, 'helm'),
            jsx('text', { className: 'flota-name', x: lx, y: above ? ly - 17 : ly, textAnchor: anchor, children: `Flagship · ${shorten(name, 18)}` }, 'n'),
            jsx('text', { className: 'flota-state', x: lx, y: above ? ly : ly + 17, textAnchor: anchor, children: joinedText(f.joined_at) }, 's')
          ] })
        }, name)
      })
    ]
  })
  return jsxs('div', { className: 'flota-card flota-mapwrap', ref: wrap, children: [
    svg,
    botTip && jsx(BotTip, { t: botTip }),
    jsxs('div', { className: 'flota-legend', 'aria-hidden': true, children: [
      jsxs('span', { children: [jsx('i', { style: { background: 'var(--ui-green)' } }), 'This machine'] }),
      jsxs('span', { children: [jsx('i', { style: { background: 'var(--ui-accent)' } }), 'Flagships that can wake it'] })
    ] })
  ] })
}

// --- side panel ------------------------------------------------------------------------------

function Command({ text }) {
  return jsxs('div', { className: 'flota-cmd', children: [
    jsx('code', { children: text }),
    jsx(CopyButton, { text, appearance: 'icon', label: 'Copy command' })
  ] })
}

function Fact({ label, children }) {
  return [jsx('dt', { children: label }, `t-${label}`), jsx('dd', { children }, `d-${label}`)]
}

/** Every bot of a crew, each a door to its chat or marked view-only, with why. */
export function CrewSection({ bots, m = null, net = null, onBot = (b, a) => openBotChat(b, a), title = 'Crew' }) {
  const list = bots || []
  if (!list.length) return null
  const rows = list.map(b => ({ b, a: chatAccess(b, m, net) }))
  const locked = rows.filter(r => !r.a.ok && !r.a.pending).length
  return jsxs('div', { className: 'flota-crew-sec', 'aria-label': title, children: [
    jsx('svg', { width: 0, height: 0, 'aria-hidden': true, style: { position: 'absolute' }, children: jsx('defs', { children: jsx(AvatarClip, {}) }) }, 'clip'),
    jsxs('div', { className: 'flota-sched-head', children: [
      jsx('div', { className: 'flota-crew-title', children: title }, 't'),
      jsx('span', { className: 'flota-muted', children: plural(list.length, 'bot') }, 'n')
    ] }, 'h'),
    jsx('ul', { className: 'flota-crew-rows', children: rows.map(({ b, a }) => {
      const mood = MOOD_TEXT[b.mood] || b.mood
      const note = a.ok ? sleepNote(b, m) : ''
      const face = jsx('svg', { width: 26, height: 28, viewBox: '-13 -15 26 28', 'aria-hidden': true, className: 'flota-crew-face', children: jsx(Avatar, { bot: b, r: 9, title: false }) }, 'f')
      const text = jsxs('span', { className: 'flota-crew-text', children: [
        jsx('span', { className: 'flota-crew-name', children: b.name }, 'n'),
        jsx('span', { className: `flota-crew-state ${b.mood}`, children: `${mood}${a.ok && a.via && a.via !== 'here' ? ` · via ${a.via}` : ''}` }, 's'),
        note && jsx('span', { className: 'flota-crew-note', children: note }, 'note')
      ] }, 't')
      const row = a.ok
        ? jsxs('button', { type: 'button', className: 'flota-crew-row chat', 'data-bot': b.id, 'data-access': 'chat', 'aria-label': seatLabel(b, a),
          onClick: () => onBot(b, a, m), children: [face, text, jsx('span', { className: 'flota-crew-go', children: 'Open chat' }, 'go')] })
        : jsxs('div', { className: 'flota-crew-row locked', 'data-bot': b.id, 'data-access': a.pending ? 'pending' : 'locked', 'aria-disabled': 'true', title: a.hint, children: [
          face, text, jsxs('span', { className: 'flota-crew-lock', children: [
            jsx('svg', { width: 12, height: 12, viewBox: '-6 -6 12 12', 'aria-hidden': true, children: jsx(LockBadge, { x: 0, y: 0 }) }, 'i'),
            a.pending ? 'Checking…' : 'View only — not connected'
          ] }, 'l')
        ] })
      return jsx('li', { children: row }, b.id)
    }) }, 'rows'),
    locked > 0 && jsxs('div', { className: 'flota-hint', children: [
      rows.find(r => !r.a.ok && !r.a.pending).a.hint, '. ',
      jsx('button', { type: 'button', className: 'flota-linkbtn', onClick: () => host.navigate(SETTINGS_GATEWAYS), children: 'Open Settings → Gateways' }, 'go')
    ] }, 'hint')
  ] })
}

/** What removing a ship does, on both sides. */
export function removeText(m) {
  return `Flota stops waking ${m.name} and sending subagents there; the machine itself is not touched. ` +
    `Its SSH access profile ${m.profile} is kept (\`hermes profile delete ${m.profile}\` removes it). ` +
    `To revoke this flagship's key on ${m.name} too, open Flota there and choose Leave fleet.`
}

export function Details({ m, status, api, onRemoved, net = null, onBot }) {
  const [pending, setPending] = useState(null)
  const [error, setError] = useState(null)
  const [confirm, setConfirm] = useState(null) // { kind: 'force' | 'remove', action?, message }
  const state = nodeState(m)
  // Per machine since the API sends can_power_off; older APIs only had the panel switch.
  const canOff = typeof m.can_power_off === 'boolean' ? m.can_power_off : !!status.allow_power_off && m.wake !== 'none'
  const offReason = canOff ? null : (m.power_off_reason || (status.allow_power_off
    ? `${m.name} has no wake method, so Flota would not be able to turn it back on; it can only be suspended.`
    : 'Power off is disabled. Turn on “Allow power off” in Plugins › Flota.'))

  const run = async (label, fn, { force } = {}) => {
    setPending(label)
    setError(null)
    try {
      const result = await fn(force)
      if (label !== 'remove') host.notify({ kind: 'success', message: label === 'wake' ? `Waking ${m.name}…` : (result && result.result) || `${m.name}: done` })
      queryClient.invalidateQueries({ queryKey: KEY })
      return result
    } catch (err) {
      const e = parseError(err)
      if (e.status === 409 && (label === 'sleep' || label === 'off') && !force) {
        setConfirm({ kind: 'force', action: label, message: e.message })
      } else {
        setError(e.message)
      }
      return null
    } finally {
      setPending(null)
    }
  }
  const actions = {
    wake: () => run('wake', () => api.wake(m.name)),
    sleep: force => run('sleep', f => api.sleep(m.name, f), { force }),
    off: force => run('off', f => api.off(m.name, f), { force })
  }
  const busyText = !status.busy_known ? 'unknown (gateway not running)' : m.busy ? `${m.busy} subagent${m.busy === 1 ? '' : 's'} running` : 'idle'
  const c = crew(m)
  const agents = c.avatars ? c.all.map(b => `${b.name} (${MOOD_TEXT[b.mood] || b.mood})`) : Array.isArray(m.agents) && m.agents.length ? m.agents : [m.profile]

  return jsxs('div', { className: 'flota-side flota-card', children: [
    jsxs('div', { className: 'flota-side-head', children: [
      jsx('svg', { width: 34, height: 34, viewBox: '-17 -17 34 34', className: `flota-node ${state}`, style: { overflow: 'visible', flexShrink: 0 }, 'aria-hidden': true, children: [
        jsx('circle', { className: 'flota-disc', r: 15 }, 'c'), jsx(Glyph, { kind: machineKind(m), size: 17, className: 'flota-glyph' }, 'g')
      ] }),
      jsxs('div', { style: { minWidth: 0 }, children: [
        jsxs('div', { className: 'flota-side-name', title: MACHINE_TIP, children: [m.name, jsx('span', { className: 'flota-tag', children: 'Machine' }, 't')] }),
        hostLabel(m) && jsx('div', { className: 'flota-muted', children: `hostname ${hostLabel(m)}` }),
        jsx('span', { className: `flota-pill ${state}`, children: [jsx('i', {}, 'i'), statusLabel(m)] })
      ] })
    ] }),
    jsx('div', { className: 'flota-muted', children: `${MACHINE_TIP}. Bots live on machines; ${m.name} is the computer, its bots are its crew.` }),
    m.description && jsx('div', { className: 'flota-muted', children: m.description }),
    jsx('dl', { className: 'flota-facts', children: [
      Fact({ label: 'Host', children: `${m.host}${m.port && m.port !== 22 ? `:${m.port}` : ''}` }),
      Fact({ label: 'User', children: m.user || '—' }),
      Fact({ label: 'OS', children: osName(m.os) }),
      Fact({ label: 'Wake', children: wakeText(m) }),
      Fact({ label: 'Sleep', children: sleepText(m) }),
      Fact({ label: 'SSH access', children: `${m.profile}${m.profile_exists ? '' : ' (not created yet)'} · Flota’s door, not a bot` }),
      Fact({ label: c.avatars ? 'Bots' : 'Crew', children: c.avatars ? (c.total ? `${plural(c.total, 'bot')}${c.working ? ` · ${c.working} working` : ` · ${c.mood}`}` : NO_BOTS) : `${agents.join(', ')} · ${c.mood}` }),
      Fact({ label: 'Subagents', children: busyText })
    ].flat() }),
    c.avatars && jsx(CrewSection, { bots: c.all, m, net, onBot }, 'crew'),
    m.wake_error && jsx('div', { className: 'flota-note err', children: `Last wake failed: ${m.wake_error}` }),
    !m.profile_exists && m.setup_command && jsxs('div', { className: 'flota-note', children: [
      jsx('div', { style: { marginBottom: 6 }, children: 'Finish setup: run this on the Hermes host to create the SSH profile.' }),
      jsx(Command, { text: m.setup_command })
    ] }),
    jsxs('div', { className: 'flota-actions', children: [
      jsx(Button, { size: 'sm', variant: state === 'down' ? 'default' : 'secondary', disabled: !!pending || state !== 'down' || m.wake === 'none',
        title: m.wake === 'none' ? 'This machine has no wake method' : undefined, onClick: actions.wake, children: pending === 'wake' ? 'Waking…' : 'Wake' }),
      jsx(Button, { size: 'sm', variant: 'secondary', disabled: !!pending || state !== 'up', onClick: () => actions.sleep(false), children: pending === 'sleep' ? 'Sleeping…' : 'Sleep' }),
      jsx(Button, { size: 'sm', variant: 'outline', disabled: !!pending || !canOff || state !== 'up',
        title: offReason || undefined, onClick: () => actions.off(false), children: 'Power off' }),
      jsx(Button, { size: 'sm', variant: 'text', disabled: !!pending, onClick: () => setConfirm({ kind: 'remove' }), children: 'Remove from fleet' })
    ] }),
    offReason && jsx('div', { className: 'flota-muted', children: offReason }),
    error && jsx('div', { className: 'flota-note err', role: 'alert', children: error }),
    confirm && confirm.kind === 'force' && jsx(ConfirmDialog, {
      open: true, onClose: () => setConfirm(null), title: `Busy — ${confirm.action === 'off' ? 'power off' : 'sleep'} anyway?`,
      description: confirm.message, confirmLabel: confirm.action === 'off' ? 'Power off anyway' : 'Sleep anyway', destructive: true,
      dismissOnConfirm: true, onConfirm: () => actions[confirm.action](true)
    }),
    confirm && confirm.kind === 'remove' && jsx(ConfirmDialog, {
      open: true, onClose: () => setConfirm(null), title: `Remove ${m.name} from the fleet?`,
      description: removeText(m),
      confirmLabel: 'Remove from fleet', destructive: true,
      onConfirm: async () => {
        try { await api.remove(m.name) } catch (err) { throw new Error(parseError(err).message) }
        host.notify({ kind: 'success', message: `${m.name} removed` })
        queryClient.invalidateQueries({ queryKey: KEY })
        onRemoved()
      }
    })
  ] })
}

function Overview({ status, onAdd, net = null, onBot }) {
  const list = status.machines
  const up = list.filter(m => nodeState(m) === 'up').length
  return jsxs('div', { className: 'flota-side flota-card', children: [
    jsx('div', { className: 'flota-side-name', children: 'Your fleet' }),
    jsx('div', { className: 'flota-muted', children: `${up} of ${plural(list.length, 'ship')} under way. Subagents you delegate to a machine run there; Flota wakes it first and puts it back to sleep when the last one finishes.` }),
    jsx('div', { className: 'flota-muted', children: 'Hover a ship under way to meet its crew and click a bot to open its chat; select a ship for details and actions.' }),
    jsx(CrewSection, { bots: botsOf(status.hub_crew || [], 'up'), net, onBot, title: `Flagship crew · ${(status.hub && status.hub.name) || 'this host'}` }, 'crew'),
    jsxs('div', { className: 'flota-actions', children: [
      jsx(Button, { size: 'sm', variant: 'secondary', onClick: () => onAdd('address'), children: 'Add by address' }, 'a'),
      jsx(Button, { size: 'sm', variant: 'outline', onClick: () => onAdd('invite'), children: 'Invite a ship' }, 'i')
    ] })
  ] })
}

function EmptyCard({ onAdd }) {
  return jsxs('div', { className: 'flota-side flota-card', children: [
    jsx('div', { className: 'flota-side-name', children: 'Add your first ship' }),
    jsxs('ol', { className: 'flota-steps', children: [
      jsxs('li', { children: [jsx('b', { children: 'Add by address' }), ': a name, its IP or Tailscale address and the SSH user. Flota connects, detects the OS, network and MAC, and saves it.'] }),
      jsxs('li', { children: [jsx('b', { children: 'Or invite a ship' }), ': give another Hermes with Flota a code, then paste back the ship code it shows.'] }),
      jsx('li', { children: 'Delegate. Flota wakes the ship, runs the subagent there, and puts it back to sleep after the last one.' })
    ] }),
    jsxs('div', { className: 'flota-actions', children: [
      jsx(Button, { size: 'sm', onClick: () => onAdd('address'), children: 'Add by address' }, 'a'),
      jsx(Button, { size: 'sm', variant: 'secondary', onClick: () => onAdd('invite'), children: 'Invite a ship' }, 'i')
    ] }),
    jsxs('div', { className: 'flota-muted', children: ['Prefer the terminal?', jsx('div', { style: { marginTop: 6 }, children: jsx(Command, { text: SETUP_HINT }) })] })
  ] })
}

/** ✓ / ✗ lines from a setup, join or add-ship call. */
export function StepLog({ result, lines }) {
  const rows = lines || setupLog(result)
  if (!rows.length) return null
  return jsx('ul', { className: 'flota-log', 'aria-label': 'Steps', children: rows.map((r, i) => jsxs('li', { className: r.ok ? 'ok' : 'fail', 'data-ok': r.ok ? '1' : '0', children: [
    jsx('b', { 'aria-hidden': true, children: r.ok ? '✓' : '✗' }), jsx('span', { children: r.text })
  ] }, `${i}-${r.text}`)) })
}

/** A code to hand over: big monospace with a copy button. */
function CodeBox({ code, label = 'Copy code' }) {
  return jsxs('div', { className: 'flota-code', 'data-code': code, children: [
    jsx('code', { children: code }), jsx(CopyButton, { text: code, appearance: 'icon', label })
  ] })
}

function Countdown({ until }) {
  const [, tick] = useState(0)
  useEffect(() => {
    const t = setInterval(() => tick(x => x + 1), 1000)
    return () => clearInterval(t)
  }, [])
  if (!until) return null
  const text = countdown(until)
  return jsx('span', { className: `flota-countdown${text === 'expired' ? ' expired' : ''}`, children: text })
}

/** Fleets this machine belongs to (it is a ship in them), each with Leave. */
export function FleetsCard({ fleets, api, selected, onJoin, compact = false, unsupported = false }) {
  const [leaving, setLeaving] = useState(null)
  const list = fleets || []
  return jsxs('div', { className: 'flota-side flota-card flota-fleets', 'aria-label': 'Fleets this machine belongs to', children: [
    jsxs('div', { className: 'flota-sched-head', children: [
      jsx('div', { className: 'flota-side-name', children: compact ? 'Also a ship in' : 'Fleets this machine belongs to' }),
      jsx('span', { className: 'flota-muted', children: list.length ? plural(list.length, 'fleet') : '' })
    ] }),
    unsupported ? jsx('div', { className: 'flota-muted', children: UPDATE_NOTE })
      : !list.length ? jsx('div', { className: 'flota-muted', children: 'None yet. Join a fleet to let another Hermes wake this machine and run agents on it.' })
      : jsx('ul', { className: 'flota-fleet-list', children: list.map(f => jsxs('li', { className: `flota-fleet${f.flagship === selected ? ' selected' : ''}`, 'data-fleet': f.flagship, children: [
        jsx('svg', { width: 26, height: 26, viewBox: '-13 -13 26 26', 'aria-hidden': true, className: 'flota-fleet-icon', children: [
          jsx('circle', { r: 12 }, 'c'), jsx(Glyph, { kind: 'helm', size: 15 }, 'g')
        ] }),
        jsxs('div', { className: 'flota-fleet-text', children: [
          jsx('div', { className: 'flota-fleet-name', children: `Flagship · ${f.flagship}` }),
          jsx('div', { className: 'flota-muted', children: joinedText(f.joined_at) }),
          f.pubkey_fingerprint && jsx('code', { className: 'flota-fp', title: `Flagship key ${f.pubkey_fingerprint}`, children: shorten(String(f.pubkey_fingerprint), 30) })
        ] }),
        jsx(Button, { size: 'sm', variant: 'text', onClick: () => setLeaving(f.flagship), children: 'Leave' })
      ] }, f.flagship)) }),
    onJoin && !unsupported && jsx('div', { className: 'flota-actions', children: jsx(Button, { size: 'sm', variant: 'secondary', onClick: onJoin, children: list.length ? 'Join another fleet' : 'Join a fleet' }) }),
    leaving && jsx(ConfirmDialog, {
      open: true, onClose: () => setLeaving(null), title: `Leave ${leaving}'s fleet?`,
      description: `Its key is removed from this machine, so ${leaving} can no longer wake it or run agents here. You can join again with a new invite.`,
      confirmLabel: 'Leave fleet', destructive: true,
      onConfirm: async () => {
        try { await api.leave(leaving) } catch (err) { throw new Error(parseError(err).message) }
        host.notify({ kind: 'success', message: `Left ${leaving}'s fleet` })
        queryClient.invalidateQueries({ queryKey: FLEETS_KEY })
        setLeaving(null)
      }
    })
  ] })
}

/** This machine as a ship: its crew, and what its flagships can do with it. */
function SelfCard({ hub, crewList, fleets, net = null, onBot }) {
  const bots = botsOf(crewList || [], 'up')
  return jsxs('div', { className: 'flota-side flota-card', children: [
    jsx('div', { className: 'flota-side-name', children: `This machine · ${(hub && hub.name) || 'this host'}` }),
    jsx('div', { className: 'flota-muted', children: (fleets || []).length === 1
      ? `A ship in ${fleets[0].flagship}’s fleet. ${fleets[0].flagship} reaches it over SSH with its own key: it wakes this machine when a subagent needs it and runs agents here.`
      : `A ship in ${plural((fleets || []).length, 'fleet')}. Its flagships reach it over SSH with their own keys: they wake it when a subagent needs it and run agents here.` }),
    jsx(CrewSection, { bots, net, onBot }, 'crew')
  ] })
}

// --- schedule panel --------------------------------------------------------------------------

function ShipChip({ name, machines }) {
  const m = (machines || []).find(x => x.name === name)
  return jsxs('span', { className: `flota-ship-chip ${m ? nodeState(m) : 'down'}`, children: [jsx('i', {}), name] })
}

/** The flagship's scheduled jobs, soonest first, with the ship each one will wake. Read-only. */
export function SchedulePanel({ data, error, rows, machines, hubName, selected, onSelect = () => {}, now = Date.now() }) {
  const list = rows || []
  const next = nextWake(list, now)
  let body
  if (!data && !error) {
    body = jsx('div', { className: 'flota-muted', children: 'Reading the schedule…' })
  } else if (error || (data && data.available === false)) {
    body = jsx('div', { className: 'flota-muted', children: `The schedule is not readable from here${(data && data.error) || error ? ` (${(data && data.error) || error})` : ''}. Flota still wakes a machine whenever a job delegates to it.` })
  } else if (!list.length) {
    body = jsx('div', { className: 'flota-muted flota-sched-empty', children: EMPTY_SCHEDULE })
  } else {
    body = jsx('ul', { className: 'flota-jobs', children: list.map(r => {
      const m = r.machine && (machines || []).find(x => x.name === r.machine)
      const verb = m && nodeState(m) === 'up' ? 'runs on' : 'wakes'
      return jsxs('li', {
        className: `flota-job${r.paused ? ' paused' : ''}${r.machine ? ' linked' : ''}${r.machine && r.machine === selected ? ' selected' : ''}`,
        'data-job': r.id, 'data-machine-target': r.machine || undefined,
        onClick: r.machine ? () => onSelect(r.machine) : undefined,
        title: r.machine ? `Show ${r.machine} on the map` : undefined,
        children: [
          jsxs('div', { className: 'flota-job-name', children: [r.name, r.paused && jsx('span', { className: 'flota-tag', children: 'paused' })] }),
          jsx('div', { className: 'flota-job-rel', children: r.next != null ? relTime(r.next, now) : r.paused ? '' : 'no next run' }),
          jsx('div', { className: 'flota-job-when', children: r.human }),
          jsx('div', { className: 'flota-job-time', children: r.next != null ? localWhen(r.next, now) : '' }),
          r.machine && jsxs('div', { className: 'flota-job-ship', children: [
            verb, jsx(ShipChip, { name: r.machine, machines }),
            r.machines.length > 1 && jsx('span', { className: 'flota-muted', children: `+${r.machines.length - 1} more` })
          ] })
        ]
      }, r.id)
    }) })
  }
  return jsxs('div', { className: 'flota-card flota-sched', 'aria-label': 'Schedule', children: [
    jsxs('div', { className: 'flota-sched-head', children: [
      jsx('div', { className: 'flota-side-name', children: 'Schedule' }),
      jsx('span', { className: 'flota-muted', children: `on the flagship · ${shorten(hubName || 'this host', 18)}` })
    ] }),
    next && jsxs('div', { className: 'flota-next', role: 'status', children: [
      jsx('svg', { width: 16, height: 16, viewBox: '-8 -8 16 16', 'aria-hidden': true, children: jsx(Glyph, { kind: 'clock', size: 15 }) }),
      jsxs('span', { children: ['Next wake: ', jsx('b', { children: next.machine }), ` ${relTime(next.row.next, now)}`, jsx('span', { className: 'flota-muted', children: ` (job: ${next.row.name})` })] })
    ] }),
    body,
    list.length > 0 && jsx('div', { className: 'flota-muted', children: 'Flota wakes a ship when the job delegates to it, then puts it back to sleep.' })
  ] })
}

// --- add ships: by address (one-click setup) or by invite ------------------------------------

const BLANK = { name: '', host: '', user: '', key: '', description: '', mac: '', broadcast: '', wake: 'wol', sleep: 'suspend', sleep_after: '10m' }
const SETUP_STEPS = ['Connecting over SSH…', 'Detecting the OS and network…', 'Looking for the MAC address for Wake-on-LAN…', 'Creating the SSH profile…', 'Saving the ship…']

/** The POST /machines body for a form state, with setup: true so the backend connects, detects the
 *  OS / interface / MAC, creates the SSH profile and saves it in one go. WoL without a MAC is sent
 *  as 'none': setup switches Wake-on-LAN on once it finds the MAC. */
export function machineBody(form) {
  const body = { name: form.name.trim().toLowerCase() }
  for (const key of ['host', 'user', 'key', 'description', 'mac', 'broadcast', 'sleep', 'sleep_after']) {
    const v = String(form[key] || '').trim()
    if (v) body[key] = v
  }
  body.wake = form.wake === 'wol' && body.mac ? 'wol' : 'none'
  if (body.sleep === 'none') delete body.sleep_after
  body.setup = true
  return body
}

function Field({ label, hint, wide, children }) {
  return jsxs('label', { className: wide ? 'wide' : undefined, children: [label, children, hint && jsx('span', { className: 'flota-hint', children: hint })] })
}

/** Cycles through what setup is doing while the request runs. */
function Progress({ steps, label }) {
  const [i, setI] = useState(0)
  useEffect(() => {
    const t = setInterval(() => setI(x => Math.min(x + 1, steps.length - 1)), 1400)
    return () => clearInterval(t)
  }, [])
  return jsxs('div', { className: 'flota-progress', role: 'status', 'aria-live': 'polite', children: [
    jsx('span', { className: 'flota-spinner', 'aria-hidden': true }),
    jsx('span', { children: label || steps[i] })
  ] })
}

/** Add by address: the form, the running setup, then its step log. */
export function AddressForm({ api, onDone, onBack, backLabel = 'Back', initial = {} }) {
  const [form, setForm] = useState(initial.form || BLANK)
  const [more, setMore] = useState(false)
  const [error, setError] = useState(null)
  const [saving, setSaving] = useState(false)
  const [done, setDone] = useState(initial.done || null)
  const set = key => e => setForm(f => ({ ...f, [key]: e && e.target ? e.target.value : e }))
  const input = (key, placeholder, extra) => jsx(Input, { value: form[key], onChange: set(key), placeholder, spellCheck: false, ...extra })
  const ready = form.name.trim() && form.host.trim() && form.user.trim()

  const submit = async e => {
    if (e) e.preventDefault()
    if (!ready || saving) return
    setSaving(true)
    setError(null)
    setDone(null)
    try {
      const result = await api.save(machineBody(form))
      queryClient.invalidateQueries({ queryKey: KEY })
      setDone(result || { ok: true })
    } catch (err) {
      setError(parseError(err))
    } finally {
      setSaving(false)
    }
  }

  const failed = done && done.ok === false
  const legacy = done && !Array.isArray(done.lines) // an older backend: saved, setup still to run by hand
  const machine = done && (typeof done.machine === 'string' ? done.machine : done.machine && done.machine.name) || form.name.trim().toLowerCase()
  let body
  if (saving) {
    body = jsxs('div', { className: 'flota-stack', children: [jsx(Progress, { steps: SETUP_STEPS }), jsx('div', { className: 'flota-muted', children: `Setting up ${form.name.trim() || 'the ship'} at ${form.host.trim()}. This takes a few seconds.` })] })
  } else if (done && !failed) {
    body = jsxs('div', { className: 'flota-stack', children: [
      jsx(StepLog, { result: done }),
      legacy && done.setup_command
        ? jsxs('div', { className: 'flota-stack', children: [jsx('div', { className: 'flota-muted', children: `${machine} is on the map. One step left: run this on the Hermes host to create its SSH profile and detect its MAC address.` }), jsx(Command, { text: done.setup_command })] })
        : jsx('div', { className: 'flota-ok', children: `${machine} joined the fleet. It is on the map now.` })
    ] })
  } else {
    body = jsxs('form', { className: 'flota-form', onSubmit: submit, id: 'flota-add-form', children: [
      jsx(Field, { label: 'Name', hint: 'How Hermes calls it: escritorio, nas…', children: input('name', 'escritorio', { autoFocus: true, required: true }) }),
      jsx(Field, { label: 'SSH user', hint: 'Your account on that machine', children: input('user', 'you', { required: true }) }),
      jsx(Field, { label: 'Host', wide: true, hint: 'IP or Tailscale address of the machine; it must accept SSH with this Hermes’ key', children: input('host', '192.0.2.20 or escritorio.tailnet.ts.net', { required: true }) }),
      jsx(Field, { label: 'SSH key (optional)', hint: 'Defaults to this Hermes’ key', children: input('key', 'path to a private key') }),
      jsx(Field, { label: 'Description (optional)', children: input('description', 'Gaming desktop with the RTX card') }),
      !more && jsx('button', { type: 'button', className: 'flota-linkbtn wide', onClick: () => setMore(true), children: 'Wake and sleep options…' }),
      more && jsx(Field, { label: 'Wake', children: jsx(SegmentedControl, { value: form.wake, onChange: set('wake'), options: [{ id: 'wol', label: 'Wake-on-LAN' }, { id: 'none', label: 'Never' }] }) }),
      more && jsx(Field, { label: 'Sleep after use', children: jsx(SegmentedControl, { value: form.sleep, onChange: set('sleep'), sizing: 'content', options: [{ id: 'suspend', label: 'Suspend' }, { id: 'hibernate', label: 'Hibernate' }, { id: 'none', label: 'Stay on' }] }) }),
      more && form.wake === 'wol' && jsx(Field, { label: 'MAC address', children: input('mac', 'auto-detected') }),
      more && form.wake === 'wol' && jsx(Field, { label: 'Broadcast', children: input('broadcast', 'auto-detected') }),
      more && form.sleep !== 'none' && jsx(Field, { label: 'Sleep after idle', children: input('sleep_after', '10m') }),
      jsx('div', { className: 'flota-muted wide', children: 'Flota connects, detects the OS, network interface and MAC, turns Wake-on-LAN on when it can, creates the SSH profile and saves the ship.' }),
      failed && jsxs('div', { className: 'flota-note err wide', role: 'alert', children: [jsx('div', { style: { marginBottom: 6 }, children: 'Setup did not finish. Fix this and try again:' }), jsx(StepLog, { result: done })] }),
      error && jsxs('div', { className: 'flota-note err wide', role: 'alert', children: [
        error.message, error.problems.length > 0 && jsx('ul', { children: error.problems.map(p => jsx('li', { children: p }, p)) })
      ] })
    ] })
  }
  const footer = done && !failed
    ? [jsx(Button, { variant: 'ghost', onClick: () => { setDone(null); setForm(BLANK) }, children: 'Add another' }, 'again'), jsx(Button, { onClick: onDone, children: 'Done' }, 'd')]
    : [onBack && jsx(Button, { variant: 'ghost', onClick: onBack, disabled: saving, children: backLabel }, 'b'),
      jsx(Button, { type: 'submit', form: 'flota-add-form', disabled: saving || !ready, children: saving ? 'Setting up…' : failed ? 'Try again' : 'Add ship' }, 's')]
  return [
    jsxs(DialogHeader, { children: [jsx(DialogTitle, { children: done && !failed ? 'Ship added' : 'Add a ship by address' }), jsx(DialogDescription, { children: 'Any computer you can reach over SSH. Flota wakes it when a subagent needs it.' })] }, 'h'),
    jsx('div', { className: 'flota-dialog-body', children: body }, 'b'),
    jsx(DialogFooter, { children: footer }, 'f')
  ]
}

/** Flagship side of pairing: an invite code for the other machine, then its ship code back. */
export function InvitePanel({ api, onDone, onBack, backLabel = 'Back', initial = {} }) {
  const [invite, setInvite] = useState(initial.invite || null) // { code, expires_at, flagship } | { error }
  const [shipCode, setShipCode] = useState(initial.shipCode || '')
  const [adding, setAdding] = useState(false)
  const [result, setResult] = useState(initial.result || null)
  const [error, setError] = useState(null)
  const load = async () => {
    setInvite(null)
    try { setInvite(await api.invite()) } catch (err) { setInvite({ error: classifyError(err) }) }
  }
  useEffect(() => { if (!initial.invite) load() }, [])
  const add = async e => {
    if (e) e.preventDefault()
    if (!shipCode.trim() || adding) return
    setAdding(true)
    setError(null)
    try {
      const r = await api.addShip(shipCode.trim())
      setResult(r || { ok: true })
      if (!r || r.ok !== false) queryClient.invalidateQueries({ queryKey: KEY })
    } catch (err) {
      setError(classifyError(err))
    } finally {
      setAdding(false)
    }
  }
  const ok = result && result.ok !== false
  const unsupported = invite && invite.error && isOlderBackend(invite.error)
  let body
  if (unsupported) {
    body = jsxs('div', { className: 'flota-note', children: [`${UPDATE_NOTE} Meanwhile, add the machine by address.`, jsx(ErrorDetails, { error: invite.error }, 'd')] })
  } else if (ok) {
    const name = typeof result.machine === 'string' ? result.machine : (result.machine && result.machine.name) || 'The ship'
    body = jsxs('div', { className: 'flota-stack', children: [jsx(StepLog, { result }), jsx('div', { className: 'flota-ok', children: `${name} joined the fleet. It is on the map now.` })] })
  } else {
    const until = invite && toMs(invite.expires_at)
    body = jsxs('div', { className: 'flota-stack', children: [
      !invite ? jsx(Progress, { steps: ['Creating an invite…'] })
        : invite.error ? jsx(ErrorNote, { error: invite.error, what: 'create an invite' })
        : jsxs('div', { className: 'flota-stack', style: { gap: 6 }, children: [
          jsxs('div', { className: 'flota-code-head', children: [jsx('span', { children: 'Invite code' }), jsx(Countdown, { until })] }),
          jsx(CodeBox, { code: String(invite.code), label: 'Copy invite code' })
        ] }),
      jsxs('ol', { className: 'flota-steps', children: [
        jsxs('li', { children: ['On the other machine open ', jsx('b', { children: 'Hermes → Flota → Join a fleet' }), '.'] }),
        jsx('li', { children: 'Paste this code.' }),
        jsx('li', { children: 'Paste the ship code it gives you here.' })
      ] }),
      jsx('form', { id: 'flota-ship-form', onSubmit: add, className: 'flota-form', children: [
        jsx(Field, { label: 'Ship code', wide: true, children: jsx(Input, { value: shipCode, onChange: e => setShipCode(e && e.target ? e.target.value : e), placeholder: 'paste the ship code', spellCheck: false, className: 'flota-mono' }) }, 'f')
      ] }),
      adding && jsx(Progress, { steps: ['Checking the ship code…', 'Connecting to the ship over SSH…', 'Detecting its network…', 'Saving the ship…'] }),
      result && result.ok === false && jsxs('div', { className: 'flota-note err', role: 'alert', children: [jsx('div', { style: { marginBottom: 6 }, children: 'The ship could not be added:' }), jsx(StepLog, { result })] }),
      error && jsx(ErrorNote, { error, what: 'add the ship' })
    ] })
  }
  const footer = ok
    ? [jsx(Button, { onClick: onDone, children: 'Done' }, 'd')]
    : [onBack && jsx(Button, { variant: 'ghost', onClick: onBack, children: backLabel }, 'b'),
      !unsupported && invite && !invite.error && jsx(Button, { variant: 'ghost', onClick: load, children: 'New code' }, 'n'),
      !unsupported && jsx(Button, { type: 'submit', form: 'flota-ship-form', disabled: adding || !shipCode.trim(), children: adding ? 'Adding…' : 'Add ship' }, 'a')]
  return [
    jsxs(DialogHeader, { children: [jsx(DialogTitle, { children: ok ? 'Ship added' : 'Invite a ship' }), jsx(DialogDescription, { children: 'Pair another Hermes with Flota in two codes.' })] }, 'h'),
    jsx('div', { className: 'flota-dialog-body', children: body }, 'b'),
    jsx(DialogFooter, { children: footer }, 'f')
  ]
}

/** Two big choices. Used by the Add ships dialog and the first-run chooser. */
function ChoiceCard({ kind, title, text, onClick, cta }) {
  return jsxs('button', { type: 'button', className: `flota-choice ${kind}`, onClick, 'data-choice': kind, children: [
    jsx('svg', { className: 'flota-choice-art', viewBox: '-90 -46 180 92', 'aria-hidden': true, children: ChoiceArt({ kind }) }),
    jsx('div', { className: 'flota-choice-title', children: title }),
    jsx('div', { className: 'flota-choice-text', children: text }),
    cta && jsx('span', { className: 'flota-choice-cta', children: cta })
  ] })
}

function ChoiceArt({ kind }) {
  const ring = [jsx('circle', { className: 'flota-range', r: 40 }, 'r1'), jsx('circle', { className: 'flota-range', r: 24 }, 'r2')]
  if (kind === 'flagship' || kind === 'address' || kind === 'invite') {
    const ships = [[-62, -18], [60, -22], [-40, 30], [48, 28]]
    return [...ring,
      ...ships.map(([x, y], i) => jsx('path', { className: `flota-link ${i < 2 ? 'up' : 'down'}`, d: `M0 0L${x} ${y}` }, `l${i}`)),
      ...ships.map(([x, y], i) => jsx('g', { transform: `translate(${x} ${y})`, className: `flota-node ${i < 2 ? 'up' : 'down'}`, children: [jsx('circle', { className: 'flota-disc', r: 9 }, 'c'), jsx(Glyph, { kind: i % 2 ? 'laptop' : 'desktop', size: 10, className: 'flota-glyph' }, 'g')] }, `s${i}`)),
      jsx('circle', { className: 'flota-hub-disc', r: 17 }, 'hub'), jsx(Glyph, { kind: kind === 'invite' ? 'hub' : 'helm', size: 20, className: 'flota-hub-glyph' }, 'helm')]
  }
  return [...ring,
    jsx('path', { className: 'flota-link signal', d: 'M-14 6L52 -18' }, 'l'),
    jsx('path', { className: 'flota-flow signal', d: 'M-14 6L52 -18' }, 'f'),
    jsx('g', { transform: 'translate(58 -20)', children: [jsx('circle', { className: 'flota-hub-disc', r: 13 }, 'c'), jsx(Glyph, { kind: 'helm', size: 15, className: 'flota-hub-glyph' }, 'g')] }, 'fs'),
    jsx('g', { transform: 'translate(-24 10)', className: 'flota-node up', children: [jsx('circle', { className: 'flota-disc', r: 17 }, 'c'), jsx(Glyph, { kind: 'desktop', size: 18, className: 'flota-glyph' }, 'g')] }, 'me')]
}

/** 'Add ships': pick how, then the address form or the invite. */
export function AddShipsDialog({ open, mode: start = 'choose', onClose, api }) {
  const [mode, setMode] = useState(start)
  useEffect(() => { if (open) setMode(start) }, [open, start])
  const back = start === 'choose' ? () => setMode('choose') : onClose
  const backLabel = start === 'choose' ? 'Back' : 'Cancel'
  let content
  if (mode === 'address') content = jsx(AddressForm, { api, onDone: onClose, onBack: back, backLabel }, 'address')
  else if (mode === 'invite') content = jsx(InvitePanel, { api, onDone: onClose, onBack: back, backLabel }, 'invite')
  else content = [
    jsxs(DialogHeader, { children: [jsx(DialogTitle, { children: 'Add ships' }), jsx(DialogDescription, { children: 'Every ship is a machine Flota wakes when a subagent needs it.' })] }, 'h'),
    jsxs('div', { className: 'flota-choices small', children: [
      jsx(ChoiceCard, { kind: 'address', title: 'Add by address', text: 'You know its IP or Tailscale name and it accepts SSH from this Hermes. One step: Flota sets everything up.', onClick: () => setMode('address') }, 'a'),
      jsx(ChoiceCard, { kind: 'invite', title: 'Invite a ship', text: 'The other machine has Hermes with Flota. Swap two codes and it joins, keys included.', onClick: () => setMode('invite') }, 'i')
    ] }, 'c'),
    jsx(DialogFooter, { children: jsx(Button, { variant: 'ghost', onClick: onClose, children: 'Cancel' }) }, 'f')
  ]
  return jsx(Dialog, { open, onOpenChange: v => { if (!v) onClose() }, children: open && jsx(DialogContent, { style: { maxWidth: 600 }, children: content }) })
}

// --- join a fleet (ship side) ----------------------------------------------------------------

const WIZARD_STEPS = ['Check', 'Invite', 'Join', 'Flagship']

/** The guided join: what you need, paste the invite, confirm and get the ship code, wait. */
export function JoinWizard({ api, hub, onDone, onCancel, onJoined, initial = {}, card = false, extra = null }) {
  const [step, setStep] = useState(initial.step || 1)
  const [check, setCheck] = useState(initial.check !== undefined ? initial.check : null)
  const [code, setCode] = useState(initial.code || '')
  const [joining, setJoining] = useState(false)
  const [result, setResult] = useState(initial.result || null)
  const [error, setError] = useState(initial.error || null)
  const os = hub && hub.os
  const runCheck = async () => {
    setCheck(null)
    try { setCheck(await api.joinCheck()) } catch (err) { setCheck({ error: parseError(err) }) }
  }
  useEffect(() => { if (step === 1 && initial.check === undefined) runCheck() }, [])
  const peek = decodeInvite(code)
  const flagship = (result && result.flagship) || (peek && peek.flagship) || null
  const join = async () => {
    setJoining(true)
    setError(null)
    try {
      const r = await api.join(code.trim())
      setResult(r || {})
      if (onJoined) onJoined()
    } catch (err) {
      setError(classifyError(err))
    } finally {
      setJoining(false)
    }
  }

  let body
  let footer
  if (step === 1) {
    const rows = check && !check.error ? joinChecks(check.unsupported ? null : check, os) : check && check.error ? joinChecks(null, os) : []
    const live = check && !check.error && !check.unsupported && Array.isArray(check.checks)
    const blocking = rows.filter(r => r.ok === false && !r.optional).length
    body = jsxs('div', { className: 'flota-stack', children: [
      jsx('div', { className: 'flota-wiz-lead', children: 'What you need on this machine' }),
      !check ? jsx(Progress, { steps: ['Checking SSH, the network and Wake-on-LAN…'] })
        : jsx('ul', { className: 'flota-checks', children: rows.map(r => jsxs('li', { className: r.ok === true ? 'ok' : r.ok === false ? (r.optional ? 'warn' : 'fail') : 'unknown', 'data-check': r.id, children: [
          jsx('b', { 'aria-hidden': true, children: r.ok === true ? '✓' : r.ok === false ? (r.optional ? '!' : '✗') : '•' }),
          jsxs('div', { children: [
            jsx('div', { className: 'flota-check-label', children: r.label }),
            r.detail && jsx('div', { className: 'flota-muted', children: r.detail }),
            r.ok !== true && r.fix && jsx('div', { className: 'flota-fix', children: jsx(Command, { text: r.fix }) })
          ] })
        ] }, r.id)) }),
      live && wakeOptions(check).length > 0 && jsxs('div', { className: 'flota-stack flota-wake-opts', style: { gap: 8 }, children: [
        jsx('div', { className: 'flota-wiz-lead', children: 'How the flagship can wake this machine' }, 'h'),
        jsx('ul', { className: 'flota-checks', children: wakeOptions(check).map(o => jsxs('li', { className: o.ok ? 'ok' : 'unknown', 'data-wake': o.id, children: [
          jsx('b', { 'aria-hidden': true, children: o.ok ? '✓' : '•' }), jsx('div', { className: 'flota-check-label', children: o.label })
        ] }, o.id)) }, 'l')
      ] }),
      check && !live && jsx('div', { className: 'flota-muted', children: 'This Flota cannot run these checks itself yet, so make sure of them by hand.' }),
      live && blocking > 0 && jsx('div', { className: 'flota-muted', children: `Fix the ${plural(blocking, 'item')} marked ✗, then check again. You can continue anyway if you know it works.` })
    ] })
    footer = [onCancel && jsx(Button, { variant: 'ghost', onClick: onCancel, children: 'Cancel' }, 'c'),
      jsx(Button, { variant: 'ghost', onClick: runCheck, disabled: !check, children: 'Check again' }, 'r'),
      jsx(Button, { onClick: () => setStep(2), disabled: !check, children: 'Next' }, 'n')]
  } else if (step === 2) {
    body = jsxs('div', { className: 'flota-stack', children: [
      jsx('div', { className: 'flota-wiz-lead', children: 'Paste the invite code' }),
      jsxs('div', { className: 'flota-muted', children: ['On the flagship: ', jsx('b', { children: 'Flota → Add ships → Invite a ship' }), '. It shows a code that expires in a few minutes.'] }),
      jsx(Input, { value: code, onChange: e => setCode(e && e.target ? e.target.value : e), placeholder: 'paste the invite code', spellCheck: false, autoFocus: true, className: 'flota-mono' }),
      peek && peek.flagship && jsx('div', { className: 'flota-muted', children: `Invite from ${peek.flagship}.` })
    ] })
    footer = [jsx(Button, { variant: 'ghost', onClick: () => setStep(1), children: 'Back' }, 'b'), jsx(Button, { onClick: () => setStep(3), disabled: !code.trim(), children: 'Next' }, 'n')]
  } else if (step === 3) {
    body = result
      ? jsxs('div', { className: 'flota-stack', children: [
        jsx(StepLog, { result }),
        jsx('div', { className: 'flota-wiz-lead', children: 'Your ship code' }),
        jsx(CodeBox, { code: String(result.ship_code || ''), label: 'Copy ship code' }),
        jsxs('div', { className: 'flota-muted', children: ['Paste it on the flagship, in ', jsx('b', { children: 'Invite a ship → Ship code' }), ', and press Add ship.'] })
      ] })
      : jsxs('div', { className: 'flota-stack', children: [
        jsx('div', { className: 'flota-wiz-lead', children: `Join ${flagship ? `${flagship}'s` : 'the'} fleet?` }),
        jsx('div', { className: 'flota-note', children: `This lets ${flagship || 'the flagship'} reach this machine over SSH with its key, so it can wake it and run agents here. You can leave the fleet anytime.` }),
        joining && jsx(Progress, { steps: ['Checking the invite…', 'Authorising the flagship’s key…', 'Preparing your ship code…'] }),
        error && (error.status === 409 && /already/i.test(error.message)
          ? jsxs('div', { className: 'flota-note err', role: 'alert', 'data-error': 'already-in-fleet', children: [
            `This machine is already a ship in ${(error.fleet && (error.fleet.flagship || error.fleet)) || 'another'}’s fleet. A machine belongs to one fleet at a time: leave that fleet first (Flota → Fleet → Leave fleet), then join this one.`,
            jsx(ErrorDetails, { error }, 'd')] })
          : jsx(ErrorNote, { error, what: 'join the fleet' }))
      ] })
    footer = result
      ? [jsx(Button, { onClick: () => setStep(4), children: 'I pasted it' }, 'n')]
      : [jsx(Button, { variant: 'ghost', onClick: () => setStep(2), disabled: joining, children: 'Back' }, 'b'), jsx(Button, { onClick: join, disabled: joining, children: joining ? 'Joining…' : 'Join fleet' }, 'j')]
  } else {
    body = jsxs('div', { className: 'flota-stack flota-waiting', children: [
      jsx('svg', { viewBox: '-40 -40 80 80', width: 80, height: 80, 'aria-hidden': true, children: [
        jsx('circle', { className: 'flota-range', r: 34 }, 'a'), jsx('circle', { className: 'flota-range', r: 20 }, 'b'),
        jsx('circle', { className: 'flota-wait-ping', r: 14 }, 'p'),
        jsx('circle', { className: 'flota-hub-disc', r: 12 }, 'c'), jsx(Glyph, { kind: 'helm', size: 14, className: 'flota-hub-glyph' }, 'g')
      ] }),
      jsx('div', { className: 'flota-wiz-lead', children: `Waiting for ${flagship || 'the flagship'}` }),
      jsx('div', { className: 'flota-muted', children: 'Once it adds this ship, it can wake this machine and run agents here. Your fleet view is ready now.' })
    ] })
    footer = [jsx(Button, { onClick: () => { queryClient.invalidateQueries({ queryKey: FLEETS_KEY }); onDone && onDone() }, children: 'Show my fleet' }, 'd')]
  }
  const stepper = jsx('ol', { className: 'flota-stepper', children: WIZARD_STEPS.map((label, i) => jsxs('li', {
    className: `flota-step${i + 1 === step ? ' on' : i + 1 < step ? ' done' : ''}`, 'data-step': i + 1, 'aria-current': i + 1 === step ? 'step' : undefined,
    children: [jsx('b', { children: i + 1 < step ? '✓' : String(i + 1) }), jsx('span', { children: label })]
  }, label)) })
  return [stepper, jsx('div', { className: 'flota-dialog-body', 'data-wizard-step': step, children: body }, 'body'), card
    ? jsxs('div', { className: 'flota-wiz-foot', children: [!result && extra, jsx('div', { className: 'flota-actions', style: { marginLeft: 'auto' }, children: footer }, 'a')] }, 'foot')
    : jsx(DialogFooter, { children: footer }, 'foot')]
}

/** The join wizard as the page's main card (a fresh ship) or inside a dialog (a flagship joining too). */
function JoinCard({ api, hub, onDone, onJoined, onSwitch }) {
  return jsxs('div', { className: 'flota-card flota-wizard', children: [
    jsxs('div', { className: 'flota-wiz-head', children: [jsx('div', { className: 'flota-side-name', children: 'Join a fleet' }), jsx('div', { className: 'flota-muted', children: 'This machine becomes a ship: another Hermes (the flagship) can wake it and run agents here.' })] }, 'h'),
    jsx(JoinWizard, { api, hub, onDone, onJoined, card: true, extra: onSwitch && jsx(Button, { variant: 'text', size: 'sm', onClick: onSwitch, children: 'Create a fleet instead' }, 's') }, 'w')
  ] })
}

// --- errors, told plainly --------------------------------------------------------------------

/** The full error text, folded away: what the backend really said. */
export function ErrorDetails({ error, label = 'Details' }) {
  const raw = error && (error.raw || error.message)
  if (!raw) return null
  return jsxs('details', { className: 'flota-errdetails', children: [jsx('summary', { children: label }, 's'), jsx('code', { children: String(raw) }, 'c')] })
}

/** One sentence for an error, by kind, never a bare '(Plugin not found)'. */
export function errorText(error, what = 'do this', scope = null) {
  const kind = errKind(error || {}, error && error.raw)
  const where = scope ? ` (profile ${scope.profile} on ${scope.label})` : ''
  if (kind === 'plugin-off') return `Flota is not enabled on the Hermes profile this request reached${where}. Desktop sends plugin requests to the profile you last chatted with; open the flagship’s own profile (usually default) and try again.`
  if (kind === 'route-missing') return UPDATE_NOTE
  if (kind === 'unreachable') return `The Flota backend did not answer${where}. Is its gateway running?`
  if (kind === 'auth') return `This Hermes refused the request${where}: sign in to it again in Settings → Gateways.`
  return `Could not ${what}: ${(error && error.message) || 'unknown error'}`
}

export function ErrorNote({ error, what, scope }) {
  let sc = scope
  if (sc === undefined) { try { sc = currentScope(host) } catch { sc = null } }
  return jsxs('div', { className: 'flota-note err', role: 'alert', 'data-error': errKind(error || {}, error && error.raw), children: [
    jsx('div', { children: errorText(error, what, errKind(error || {}, error && error.raw) === 'plugin-off' ? sc : null) }, 't'),
    jsx(ErrorDetails, { error }, 'd')
  ] })
}

/** The header's 'update failed' note, with the real reason one click away. */
export function UpdateError({ error, scope, onFix }) {
  const kind = errKind(error, error.raw)
  const summary = kind === 'plugin-off' ? `Flota not on profile ${scope ? scope.profile : '?'} · retrying` : kind === 'unreachable' ? 'backend not answering · retrying' : 'update failed · retrying'
  return jsxs('details', { className: 'flota-updateerr', 'data-error': kind, children: [
    jsx('summary', { children: summary }, 's'),
    jsxs('div', { className: 'flota-updateerr-body', children: [
      jsx('div', { children: errorText(error, 'refresh', scope) }, 't'),
      kind === 'plugin-off' && scope && scope.profile !== 'default' && onFix && jsx(Button, { size: 'sm', variant: 'secondary', onClick: onFix, children: `Use ${scope.label} · default` }, 'f'),
      jsx('code', { children: String(error.raw || error.message) }, 'c')
    ] }, 'b')
  ] })
}

// --- reaching the flagship -------------------------------------------------------------------

/** Shown instead of an error when this connection has no Flota: find the flagship on another
 *  connection and open it there, and say what this machine can do as a ship. */
export function ReachCard({ error, scope, probe, onReach, onRetry, remember = true, setRemember = () => {}, reaching = false }) {
  const kind = errKind(error || {}, error && error.raw)
  const found = probe && probe.found
  const off = kind === 'plugin-off' || kind === 'route-missing'
  let head
  let lead
  let actions = []
  if (found && found.sameConnection) {
    head = `Flota runs on another profile of ${found.label}`
    lead = `The app sends plugin requests to the profile you last chatted with (${scope.profile}), and Flota is not enabled there. It runs on ${found.route.profile}.`
    actions = [jsx(Button, { size: 'sm', onClick: () => onReach(found), disabled: reaching, children: reaching ? 'Opening…' : `Use ${found.label} · ${found.route.profile}` }, 'go')]
  } else if (found) {
    head = scope.local ? 'This PC’s Hermes is a ship' : `Flota is not enabled on ${scope.label}`
    const ships = found.machines.length ? ` It leads ${plural(found.machines.length, 'ship')}: ${found.machines.slice(0, 5).join(', ')}${found.machines.length > 5 ? '…' : ''}.` : ''
    lead = `Your fleet is managed by the flagship on ${found.label}.${ships} Open it there to see the map, wake machines and add ships.`
    actions = [jsx(Button, { size: 'sm', onClick: () => onReach(found), disabled: reaching, children: reaching ? 'Opening…' : `Open the fleet on ${found.label}` }, 'go')]
  } else {
    head = off ? 'Flota is not enabled on this Hermes' : 'Flota backend not reachable'
    lead = off ? BACKEND_OFF : errorText(error, 'reach Flota')
  }
  const looking = !probe
  return jsxs('div', { className: 'flota-card flota-reach', 'data-reach': found ? (found.sameConnection ? 'profile' : 'found') : looking ? 'looking' : 'none', children: [
    jsxs('div', { className: 'flota-reach-head', children: [
      jsx('svg', { className: 'flota-reach-art', viewBox: '-90 -46 180 92', 'aria-hidden': true, children: ChoiceArt({ kind: 'ship' }) }, 'a'),
      jsxs('div', { children: [jsx('div', { className: 'flota-side-name', children: head }, 'h'), jsx('div', { className: 'flota-reach-lead', children: lead }, 'l')] }, 't')
    ] }, 'head'),
    looking && jsx(Progress, { steps: ['Looking for your flagship on this app’s other connections…'] }, 'p'),
    found && jsxs('div', { className: 'flota-reach-found', children: [
      jsxs('span', { className: 'flota-scope-chip', 'data-flagship-route': `${found.route.connectionId}::${found.route.profile}`, children: [jsx('i', {}, 'i'), `Flagship on ${found.label}`] }, 'c'),
      !found.sameConnection && jsxs('label', { className: 'flota-checkline', children: [
        jsx('input', { type: 'checkbox', checked: !!remember, onChange: e => setRemember(!!(e && e.target ? e.target.checked : e)) }, 'x'),
        'Open the flagship automatically next time'
      ] }, 'r')
    ] }, 'found'),
    probe && !found && jsx('div', { className: 'flota-muted', children: probe.supported === false
      ? 'This Hermes Desktop cannot look at its other connections from a plugin. If your flagship is another Hermes, switch to its connection in the app and open Flota there.'
      : probe.tried ? `None of this app’s other connections (${probe.tried} checked) runs Flota. Add the flagship in Settings → Gateways, then come back.`
        : 'This app has no other connection to look for a flagship on. Add the flagship in Settings → Gateways, then come back.' }, 'none'),
    jsxs('div', { className: 'flota-actions', children: [...actions,
      !found && jsx(Button, { size: 'sm', variant: 'secondary', onClick: () => host.navigate(SETTINGS_GATEWAYS), children: 'Open Settings → Gateways' }, 'gw'),
      jsx(Button, { size: 'sm', variant: 'ghost', onClick: onRetry, children: 'Try again' }, 'retry')] }, 'acts'),
    off && jsxs('div', { className: 'flota-reach-ship', children: [
      jsx('div', { className: 'flota-crew-title', children: scope.local ? 'This PC as a ship' : `${scope.label} as a ship` }, 't'),
      jsx('div', { className: 'flota-muted', children: `To let ${found && !found.sameConnection ? `the flagship on ${found.label}` : 'a flagship'} wake ${scope.local ? 'this PC' : 'that machine'} and run agents on it, enable Flota on its Hermes (Plugins → Flota), restart its gateway, then open Flota here and choose Join a fleet. Or choose Create a fleet to make it a flagship itself.` }, 'm'),
      jsx('div', { className: 'flota-actions', children: jsx(Button, { size: 'sm', variant: 'outline', onClick: () => host.navigate(PLUGINS_PAGE), children: 'Open Plugins' }) }, 'a')
    ] }, 'ship'),
    jsx(ErrorDetails, { error, label: 'What this Hermes answered' }, 'd')
  ] })
}

// --- role lifecycle --------------------------------------------------------------------------

/** A small header menu (a native <details>, so it needs no state). */
export function Menu({ label, items, id }) {
  const close = e => { const d = e && e.currentTarget && e.currentTarget.closest && e.currentTarget.closest('details'); if (d) d.removeAttribute('open') }
  return jsxs('details', { className: 'flota-menu', 'data-menu': id, children: [
    jsx('summary', { className: 'flota-menu-btn', children: label }, 's'),
    jsx('div', { className: 'flota-menu-list', role: 'menu', children: items.filter(Boolean).map((it, i) => it.sep
      ? jsx('hr', {}, `sep${i}`)
      : jsxs('button', { type: 'button', role: 'menuitem', className: `flota-menu-item${it.danger ? ' danger' : ''}`, disabled: !!it.disabled, title: it.title,
        'data-action': it.id, onClick: e => { close(e); if (!it.disabled) it.run() }, children: [
          jsx('span', { children: it.label }, 'l'), it.note && jsx('small', { children: it.note }, 'n')] }, it.id)) }, 'l')
  ] })
}

/** The confirm text for a role change. kind: 'leave' | 'flagship' | 'ship' | 'ship-clear'. */
export function roleActionText(kind, { fleet = null, machines = [] } = {}) {
  const f = fleet && fleet.flagship
  const n = (machines || []).length
  if (kind === 'leave') return { title: `Leave ${f}’s fleet?`, confirmLabel: 'Leave fleet', destructive: true,
    description: `${f}’s key is removed from this machine, so it can no longer wake it or run agents here. Then you choose again: join another fleet, or create your own.` }
  if (kind === 'flagship') return { title: 'Become a flagship instead?', confirmLabel: f ? 'Leave and become a flagship' : 'Become a flagship', destructive: !!f,
    description: `${f ? `This machine leaves ${f}’s fleet first (its key is removed here). ` : ''}Then it becomes the flagship of its own fleet: it should stay on, and it wakes the machines you add.` }
  if (kind === 'ship-clear') return { title: `Remove ${plural(n, 'ship')} and become a ship?`, confirmLabel: `Remove ${plural(n, 'ship')} and continue`, destructive: true,
    description: `A machine is either a flagship or a ship. ${(machines || []).map(m => m.name).join(', ')} will be removed from this fleet (each can choose Leave fleet on its side to revoke this flagship’s key). Then this machine can join another fleet.` }
  return { title: 'Join a fleet as a ship instead?', confirmLabel: 'Become a ship', destructive: false,
    description: 'This machine stops being a flagship and becomes a ship: a short checklist and an invite code from the other fleet’s flagship.' }
}

/** Friendly words for a 409 from POST /role. */
export function roleRefusal(e) {
  if (e && e.needs === 'remove_ships') return 'This flagship still has ships. Remove them first (Remove from fleet on each), then try again.'
  if (e && e.needs === 'leave_fleet') return 'This machine is still a ship in a fleet. Leave that fleet first, then try again.'
  return (e && e.message) || 'The role could not be changed.'
}

/** Run a role change against the API. Returns the role this machine has afterwards. */
export async function runRoleAction(kind, api, { fleet = null, machines = [] } = {}) {
  try {
    if (kind === 'leave') {
      const r = await api.leave(fleet.flagship)
      return (r && 'role' in r) ? r.role : null
    }
    if (kind === 'flagship') {
      if (fleet && fleet.flagship) await api.leave(fleet.flagship)
      await api.setRole('flagship')
      return 'flagship'
    }
    if (kind === 'ship-clear') {
      for (const m of machines || []) await api.remove(m.name)
      await api.setRole('ship')
      return 'ship'
    }
    if ((machines || []).length) throw Object.assign(new Error(roleRefusal({ needs: 'remove_ships' })), { needs: 'remove_ships' })
    await api.setRole('ship')
    return 'ship'
  } catch (err) {
    if (err && err.needs) throw err
    const e = classifyError(err)
    throw Object.assign(new Error(e.status === 409 ? roleRefusal(e) : errorText(e, 'change the role')), { needs: e.needs })
  }
}

/** A ship's membership: its flagship, and what it can do next. */
export function MembershipCard({ fleet, onLeave, onBecomeFlagship }) {
  const f = fleet && fleet.flagship
  return jsxs('div', { className: 'flota-side flota-card flota-membership', 'aria-label': 'Fleet membership', children: [
    jsxs('div', { className: 'flota-sched-head', children: [jsx('div', { className: 'flota-side-name', children: 'Your fleet' }, 'n'), jsx('span', { className: 'flota-muted', children: 'one fleet at a time' }, 'm')] }, 'h'),
    f ? jsxs('div', { className: 'flota-fleet', 'data-fleet': f, children: [
      jsx('svg', { width: 26, height: 26, viewBox: '-13 -13 26 26', 'aria-hidden': true, className: 'flota-fleet-icon', children: [jsx('circle', { r: 12 }, 'c'), jsx(Glyph, { kind: 'helm', size: 15 }, 'g')] }, 'i'),
      jsxs('div', { className: 'flota-fleet-text', children: [
        jsx('div', { className: 'flota-fleet-name', children: `Flagship · ${f}` }, 'n'),
        jsx('div', { className: 'flota-muted', children: joinedText(fleet.joined_at) }, 'j'),
        fleet.pubkey_fingerprint && jsx('code', { className: 'flota-fp', title: `Flagship key ${fleet.pubkey_fingerprint}`, children: shorten(String(fleet.pubkey_fingerprint), 30) }, 'fp')
      ] }, 't')
    ] }, 'f') : jsx('div', { className: 'flota-muted', children: 'Not in a fleet yet.' }, 'none'),
    jsxs('div', { className: 'flota-actions', children: [
      f && jsx(Button, { size: 'sm', variant: 'secondary', onClick: onLeave, children: 'Leave fleet' }, 'l'),
      jsx(Button, { size: 'sm', variant: 'outline', disabled: !!f, title: f ? `Leave ${f}’s fleet first` : undefined, children: 'Join another fleet' }, 'j'),
      jsx(Button, { size: 'sm', variant: 'text', onClick: onBecomeFlagship, children: 'Become a flagship instead' }, 'b')
    ] }, 'a'),
    f && jsx('div', { className: 'flota-hint', children: `To join another fleet, leave ${f}’s first: a machine belongs to one fleet at a time.` }, 'hint')
  ] })
}

/** First run: is this Hermes the flagship of a fleet, or a ship joining one? */
export function RoleChooser({ hubName, onChoose }) {
  return jsxs('div', { className: 'flota-choose', 'data-onboarding': 'role', children: [
    jsxs('div', { className: 'flota-choose-head', children: [
      jsx('div', { className: 'flota-title', children: 'Set sail with Flota' }),
      jsx('div', { className: 'flota-sub', children: `How does ${hubName || 'this Hermes'} take part? You can change this later.` })
    ] }),
    jsxs('div', { className: 'flota-choices', children: [
      jsx(ChoiceCard, { kind: 'flagship', title: 'Create a fleet', cta: 'Become the flagship →',
        text: 'This machine stays on and manages the others. It becomes the flagship: add the machines it can reach, by address or with an invite code, and it wakes them whenever a subagent needs one.', onClick: () => onChoose('flagship') }, 'f'),
      jsx(ChoiceCard, { kind: 'ship', title: 'Join a fleet', cta: 'Become a ship →',
        text: 'This machine becomes a ship of another Hermes, the flagship. A short checklist and an invite code, and the flagship can wake this machine and run agents here. You can leave anytime.', onClick: () => onChoose('ship') }, 's')
    ] })
  ] })
}

// --- page ------------------------------------------------------------------------------------

function Ago({ at }) {
  const [, tick] = useState(0)
  useEffect(() => {
    const t = setInterval(() => tick(x => x + 1), 1000)
    return () => clearInterval(t)
  }, [])
  if (!at) return null
  const s = Math.max(0, Math.round((Date.now() - at) / 1000))
  return jsx('span', { title: new Date(at).toLocaleTimeString(), children: s < 3 ? 'updated just now' : `updated ${s < 90 ? `${s}s` : `${Math.round(s / 60)} min`} ago` })
}

function Summary({ machines }) {
  const count = s => machines.filter(m => nodeState(m) === s).length
  const running = machines.reduce((sum, m) => sum + (m.busy > 0 ? m.busy : 0), 0)
  return jsxs('div', { className: 'flota-summary', children: [
    jsxs('span', { className: 'flota-pill up', children: [jsx('i', {}), `${count('up')} under way`] }),
    count('waking') > 0 && jsxs('span', { className: 'flota-pill waking', children: [jsx('i', {}), `${count('waking')} raising anchor`] }),
    count('down') > 0 && jsxs('span', { className: 'flota-pill down', children: [jsx('i', {}), `${count('down')} docked`] }),
    running > 0 && jsx('span', { className: 'flota-pill', children: `${running} subagent${running === 1 ? '' : 's'} running` })
  ] })
}

const readStored = () => { try { return (typeof localStorage !== 'undefined' && localStorage.getItem(ROLE_STORE)) || null } catch { return null } }
const writeStored = role => { try { if (typeof localStorage !== 'undefined') localStorage.setItem(ROLE_STORE, role) } catch { /* private mode */ } }

const SUBTITLE = {
  flagship: 'Your fleet: machines woken on demand and put back to sleep',
  ship: 'This machine is a ship: its flagships wake it and run agents here',
  join: 'Join a fleet: let another Hermes wake this machine and run agents here',
  choose: 'Machines woken on demand and put back to sleep',
  offline: 'Your fleet of machines, woken on demand: Flota finds its flagship for you'
}

/** A bot was clicked: say so when its machine is asleep, then open its chat. */
export async function openChat(bot, access, m, sdk = host) {
  if (!access || !access.ok) return { ok: false, how: 'locked' }
  const note = sleepNote(bot, m)
  if (note && typeof sdk.notify === 'function') sdk.notify({ kind: 'info', message: note })
  const result = await openBotChat(bot, access, sdk)
  if (!result.ok && typeof sdk.notify === 'function') sdk.notify({ kind: 'error', message: 'This Hermes cannot open a chat from a plugin yet. Update Hermes Desktop.' })
  return result
}

export function FlotaPage({ api, sdk = host }) {
  const query = useQuery({ queryKey: KEY, queryFn: api.status, refetchInterval: POLL_MS, refetchIntervalInBackground: false, retry: 1 })
  const sched = useQuery({ queryKey: SCHEDULE_KEY, queryFn: api.schedule, refetchInterval: SCHEDULE_POLL_MS, refetchIntervalInBackground: false, retry: 0 })
  const fleetsQ = useQuery({ queryKey: FLEETS_KEY, queryFn: api.fleets, refetchInterval: SCHEDULE_POLL_MS, refetchIntervalInBackground: false, retry: 0 })
  const [selected, setSelected] = useState(null)
  const [adding, setAdding] = useState(null) // null | 'choose' | 'address' | 'invite'
  const [picked, setPicked] = useState(null)
  const [pinJoin, setPinJoin] = useState(false)
  const [roleAct, setRoleAct] = useState(null) // null | 'leave' | 'flagship' | 'ship' | 'ship-clear'
  const [remember, setRemember] = useState(true)
  const [reaching, setReaching] = useState(false)
  const autoTried = useRef(false)
  const netQ = useQuery({ queryKey: NET_KEY, queryFn: () => loadNet(sdk), refetchInterval: NET_POLL_MS, refetchIntervalInBackground: false, retry: 0 })
  const net = netQ.data || { links: [], routes: [], activeId: null, ready: !!netQ.error }
  const scope = currentScope(sdk, net)
  const status = query.data
  const machines = (status && status.machines) || []
  const current = machines.find(m => m.name === selected)
  const err = query.error ? classifyError(query.error) : null
  const fleetsData = fleetsQ.data
  const fleetsUnsupported = !!(fleetsData && fleetsData.unsupported)
  const fleets = fleetsData ? (Array.isArray(fleetsData.fleets) ? fleetsData.fleets : []) : fleetsQ.error ? [] : null
  const fleet = status ? fleetOf(status, fleets) : null
  const role = effectiveRole(status, picked, readStored())
  const mode = err && !status ? 'offline' : viewMode({ status, fleets, role, pinJoin })
  // No Flota on this connection: look for the flagship on the app's other connections.
  const lost = mode === 'offline' && err.kind !== 'auth'
  const probeQ = useQuery({ queryKey: [ID, 'probe', scope.connectionId, scope.profile], queryFn: () => findFlagship(sdk, net, scope),
    enabled: lost && net.ready !== false, retry: 0, staleTime: 60000, refetchInterval: false })
  const probe = probeQ.data || (probeQ.error ? { supported: true, tried: 0, found: null } : null)
  const rows = scheduleRows(sched.data, machines.map(m => m.name))
  const hub = status && status.hub
  const hubName = (hub && hub.name) || 'this host'
  const hubActive = !status ? false : hub && typeof hub.gateway_up === 'boolean' ? hub.gateway_up : status.busy_known !== false
  const openAdd = (m = 'choose') => setAdding(m)
  const onBot = (b, a, m) => openChat(b, a, m, sdk)
  const refreshAll = () => { for (const k of [KEY, FLEETS_KEY, SCHEDULE_KEY, NET_KEY, [ID, 'probe']]) queryClient.invalidateQueries({ queryKey: k }) }
  const notify = n => { try { if (typeof sdk.notify === 'function') sdk.notify(n) } catch { /* toast is optional */ } }

  const reach = async (found, { auto = false } = {}) => {
    setReaching(true)
    try {
      if (!auto && remember && !found.sameConnection) writeReach(found.route)
      await reachRoute(found.route, sdk)
      notify({ kind: 'info', message: `Flota: opened the flagship on ${found.label}` })
    } catch (e) {
      notify({ kind: 'error', message: e.message })
    } finally {
      setReaching(false)
      refreshAll()
    }
  }
  useEffect(() => {
    const found = probe && probe.found
    if (!lost || !found || found.sameConnection || autoTried.current) return
    if (sameRoute(found.route, readReach())) { autoTried.current = true; reach(found, { auto: true }) }
  }, [lost, probe && probe.found && `${probe.found.route.connectionId}::${probe.found.route.profile}`])

  const choose = async next => {
    setPicked(next)
    writeStored(next)
    setSelected(null)
    try { await api.setRole(next) } catch { /* kept locally */ }
    queryClient.invalidateQueries({ queryKey: KEY })
  }
  const doRole = async kind => {
    const after = await runRoleAction(kind, api, { fleet, machines })
    setPicked(after || null)
    writeStored(after || '')
    setSelected(null)
    setPinJoin(false)
    notify({ kind: 'success', message: kind === 'leave' ? `Left ${fleet && fleet.flagship}’s fleet` : after === 'flagship' ? 'This machine is a flagship now' : 'This machine is ready to join a fleet' })
    refreshAll()
  }
  const askShip = () => setRoleAct(machines.length ? 'ship-clear' : 'ship')

  let side = null
  let main = null
  if (mode === 'offline') {
    main = jsx('div', { className: 'flota-center', children: jsx(ReachCard, { error: err, scope, probe: lost ? probe : { supported: true, tried: 0, found: null },
      onReach: reach, onRetry: refreshAll, remember, setRemember, reaching }) })
  } else if (mode === 'loading') {
    side = jsx('div', { className: 'flota-side flota-card flota-muted', children: 'Checking your machines…' })
  } else if (mode === 'choose') {
    main = jsx(RoleChooser, { hubName, onChoose: choose })
  } else if (mode === 'join') {
    main = jsx('div', { className: 'flota-center', children: jsx(JoinCard, {
      api, hub, onJoined: () => setPinJoin(true), onDone: () => { setPinJoin(false); refreshAll() },
      onSwitch: () => { setPinJoin(false); choose('flagship') }
    }) })
  } else if (mode === 'ship') {
    const shown = status.fleet && status.fleet.flagship ? [status.fleet] : (fleets || [])
    const f = shown.find(x => x.flagship === selected)
    const legacyMany = !reportsRole(status) && shown.length > 1
    main = jsxs('div', { className: 'flota-body', children: [
      jsx(ShipMap, { hub, fleets: shown, crew: status.hub_crew, net, onBot, selected: f ? f.flagship : null, onSelect: name => setSelected(s => (name == null || s === name ? null : name)) }),
      jsxs('div', { className: 'flota-col', children: [
        jsx(SelfCard, { hub, crewList: status.hub_crew, fleets: shown, net, onBot }, 'self'),
        legacyMany
          ? jsx(FleetsCard, { fleets: shown, api, selected, unsupported: fleetsUnsupported }, 'fleets')
          : jsx(MembershipCard, { fleet, onLeave: () => setRoleAct('leave'), onBecomeFlagship: () => setRoleAct('flagship') }, 'member')
      ] })
    ] })
  }
  if (mode === 'flagship' || mode === 'loading') {
    if (mode === 'flagship') {
      if (!machines.length) side = jsx(EmptyCard, { onAdd: openAdd })
      else if (current) side = jsx(Details, { m: current, status, api, net, onBot, onRemoved: () => setSelected(null) }, current.name)
      else side = jsx(Overview, { status, onAdd: openAdd, net, onBot })
    }
    const select = name => setSelected(s => (name == null || s === name ? null : name))
    const schedule = status && jsx(SchedulePanel, {
      data: sched.data, error: sched.error && !sched.data ? parseError(sched.error).message : null, rows, machines, hubName,
      selected, onSelect: name => setSelected(name)
    })
    main = jsxs('div', { className: 'flota-body', children: [
      jsx(FleetMap, { hub, machines, selected, onSelect: select, onAdd: () => openAdd('choose'), offline: !status, schedule: rows, hubCrew: status && status.hub_crew, hubActive, net, onBot }),
      jsxs('div', { className: 'flota-col', children: [side, schedule, mode === 'flagship' && fleets && fleets.length > 0 && jsx(FleetsCard, { fleets, api, compact: true }, 'fleets')] })
    ] })
  }

  const fname = fleet && fleet.flagship
  const actions = mode === 'flagship'
    ? [jsx(Menu, { id: 'fleet', label: 'Fleet ▾', items: [
        { id: 'address', label: 'Add a ship by address', run: () => openAdd('address') },
        { id: 'invite', label: 'Invite a ship', run: () => openAdd('invite') },
        { sep: true },
        { id: 'become-ship', label: 'Join a fleet as a ship instead', run: askShip,
          note: machines.length ? `remove its ${plural(machines.length, 'ship')} first` : 'this machine stops leading a fleet' }
      ] }, 'menu'),
      jsx(Button, { size: 'sm', onClick: () => openAdd('choose'), children: 'Add ships' }, 'a')]
    : mode === 'ship'
      ? [jsx(Menu, { id: 'fleet', label: 'Fleet ▾', items: [
          fname && { id: 'leave', label: 'Leave fleet', danger: true, run: () => setRoleAct('leave'), note: `${fname} can no longer wake this machine` },
          { id: 'join-another', label: 'Join another fleet', disabled: !!fname, run: () => setPinJoin(true), title: fname ? `Leave ${fname}’s fleet first` : undefined,
            note: fname ? `leave ${fname}’s fleet first` : undefined },
          { id: 'become-flagship', label: 'Become a flagship instead', run: () => setRoleAct('flagship'), note: fname ? `leaves ${fname}’s fleet first` : undefined }
        ] }, 'menu')]
      : mode === 'loading' ? [jsx(Button, { size: 'sm', disabled: true, children: 'Add ships' }, 'a')] : []
  const confirm = roleAct && roleActionText(roleAct, { fleet, machines })
  const scopeChip = status && !scope.local && jsxs('span', { className: 'flota-scope-chip', title: `Flota reaches this Hermes through the ${scope.label} connection (profile ${scope.profile})`,
    children: [jsx('i', {}, 'i'), `${mode === 'ship' ? 'Ship' : 'Flagship'}: ${hubName} (via ${scope.label} connection)`] }, 'scope')

  return jsxs('div', { className: `flota-page mode-${mode}`, 'data-mode': mode, children: [
    mode !== 'choose' && jsxs('div', { className: 'flota-head', children: [
      jsxs('div', { children: [
        jsx('div', { className: 'flota-title', children: 'Flota' }),
        jsx('div', { className: 'flota-sub', children: mode === 'ship' && fname ? `This machine is a ship in ${fname}’s fleet: ${fname} wakes it and runs agents here` : SUBTITLE[mode] || SUBTITLE.flagship })
      ] }),
      mode === 'flagship' && machines.length > 0 && jsx(Summary, { machines }),
      mode === 'ship' && jsx('div', { className: 'flota-summary', children: jsxs('span', { className: 'flota-pill up', children: [jsx('i', {}), fname ? `ship of ${fname}` : `ship in ${plural((fleets || []).length, 'fleet')}`] }) }),
      jsxs('div', { className: 'flota-head-right', children: [
        scopeChip,
        err && status ? jsx(UpdateError, { error: err, scope, onFix: () => reach({ route: { connectionId: scope.connectionId, profile: 'default' }, label: scope.label, sameConnection: true }) }) : mode !== 'offline' && jsx(Ago, { at: query.dataUpdatedAt }),
        ...actions
      ] })
    ] }),
    status && status.problems && status.problems.length > 0 && jsxs('div', { className: 'flota-note', children: [
      'Some machines in the config were skipped:', jsx('ul', { children: status.problems.map(p => jsx('li', { children: p }, p)) })
    ] }),
    main,
    jsx(AddShipsDialog, { open: !!adding, mode: adding || 'choose', onClose: () => setAdding(null), api }),
    confirm && jsx(ConfirmDialog, { open: true, onClose: () => setRoleAct(null), title: confirm.title, description: confirm.description,
      confirmLabel: confirm.confirmLabel, destructive: confirm.destructive, 'data-role-action': roleAct,
      onConfirm: async () => { await doRole(roleAct); setRoleAct(null) } })
  ] })
}

function StatusChip({ api }) {
  const { data } = useQuery({ queryKey: KEY, queryFn: api.status, refetchInterval: 60000, retry: false })
  const list = (data && data.machines) || []
  if (!list.length) return null
  const up = list.filter(m => nodeState(m) === 'up').length
  return jsxs('button', {
    type: 'button', className: `flota-chip${up ? '' : ' none'}`, title: 'Open the Flota fleet map', onClick: () => host.navigate(PATH),
    children: [jsx('i', {}), `Flota ${up}/${list.length} up`]
  })
}

/** The REST calls, all relative to /api/plugins/flota. Endpoints an older backend lacks answer
 *  calmly instead of throwing: fleets -> { fleets: [], unsupported }, join check -> { unsupported },
 *  role -> { ok: false, unsupported }. */
export function makeApi(rest) {
  const name = n => encodeURIComponent(n)
  // Only a 404 of the route itself (the plugin answering) means an older backend; a
  // 'Plugin not found' (Flota off on this profile or Hermes) is a real error and is thrown.
  const orMissing = (fn, fallback) => async (...args) => {
    try { return await fn(...args) } catch (err) {
      if (isOlderBackend(classifyError(err))) return fallback
      throw err
    }
  }
  return {
    status: () => rest('/status'),
    // An older Flota backend has no /schedule: read the jobs through the gateway's own cron door.
    schedule: async () => {
      try {
        return await rest('/schedule')
      } catch (err) {
        if (!isOlderBackend(classifyError(err)) || typeof host.request !== 'function') throw err
        const res = await host.request('cron.manage', { action: 'list', include_disabled: true })
        return { jobs: (res && res.jobs) || [], available: true, error: null, via: 'cron.manage' }
      }
    },
    wake: n => rest(`/wake/${name(n)}`, { method: 'POST' }),
    sleep: (n, force) => rest(`/sleep/${name(n)}${force ? '?force=1' : ''}`, { method: 'POST', timeoutMs: 60000 }),
    off: (n, force) => rest(`/off/${name(n)}${force ? '?force=1' : ''}`, { method: 'POST', timeoutMs: 60000 }),
    save: body => rest('/machines', { method: 'POST', body, timeoutMs: 120000 }),
    remove: n => rest(`/machines/${name(n)}`, { method: 'DELETE' }),
    invite: () => rest('/invite'),
    addShip: code => rest('/ships', { method: 'POST', body: { ship_code: code }, timeoutMs: 120000 }),
    join: code => rest('/join', { method: 'POST', body: { code }, timeoutMs: 60000 }),
    joinCheck: orMissing(() => rest('/join/check', { timeoutMs: 30000 }), { unsupported: true }),
    fleets: orMissing(() => rest('/fleets'), { fleets: [], unsupported: true }),
    leave: f => rest(`/fleets/${name(f)}`, { method: 'DELETE' }),
    setRole: orMissing((role, force) => rest('/role', { method: 'POST', body: force ? { role, force: true } : { role } }), { ok: false, unsupported: true })
  }
}

export default {
  id: ID,
  name: 'Flota',
  description: 'Your fleet on a radar: the machines your subagents run on, their crews, and when scheduled jobs wake them. Create a fleet or join one.',
  register(ctx) {
    const style = document.createElement('style')
    style.dataset.plugin = ID
    style.textContent = CSS
    document.head.append(style)
    ctx.onDispose(() => style.remove())
    const api = makeApi((path, opts) => ctx.rest(path, opts))
    ctx.registerMany([
      { id: 'page', area: ROUTES_AREA, data: { path: PATH }, render: () => jsx(FlotaPage, { api }) },
      { id: 'nav', area: SIDEBAR_NAV_AREA, data: { path: PATH, label: 'Flota', codicon: 'server-environment' } },
      { id: 'open', area: PALETTE_AREA, data: { id: 'flota.open', label: 'Flota: open fleet map', keywords: ['flota', 'fleet', 'machines', 'wake', 'sleep', 'schedule', 'cron', 'join', 'invite', 'ship'], run: () => host.navigate(PATH) } },
      { id: 'chip', area: STATUSBAR_AREAS.right, order: 140, render: () => jsx(StatusChip, { api }) }
    ])
  }
}
