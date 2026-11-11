"""The Hermes Desktop half: ``desktop/plugin.js``.

The app loads the file uncompiled, so it may only import ``@hermes/plugin-sdk``, ``react`` and
``react/jsx-runtime``. The file is then run under node with tiny stand-ins for those three modules
(a ``jsx()`` that calls function components and builds plain objects), ``register(ctx)`` is called
with a fake context, and the fleet map is rendered to markup for several fleet sizes.
"""
from __future__ import annotations

import html
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1] / "desktop" / "plugin.js"
ALLOWED = {"@hermes/plugin-sdk", "react", "react/jsx-runtime"}
IMPORT = re.compile(r"""^\s*import\s+(?:([\s\S]*?)\s+from\s+)?['"]([^'"]+)['"]""", re.M)
DYNAMIC = re.compile(r"""\bimport\s*\(""")


def _imports(source: str) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for clause, spec in IMPORT.findall(source):
        names = re.findall(r"[A-Za-z_$][\w$]*", clause.split("{", 1)[1].split("}")[0]) if "{" in clause else []
        found.setdefault(spec, []).extend(names)
    return found


def test_only_the_three_allowed_imports():
    source = PLUGIN.read_text()
    assert set(_imports(source)) <= ALLOWED
    assert not DYNAMIC.search(source), "dynamic import() is refused by the desktop loader"
    assert "require(" not in source


def test_no_jsx_syntax_and_a_default_export_with_the_folder_id():
    source = PLUGIN.read_text()
    assert not re.search(r"return\s*\(?\s*<[A-Za-z]", source), "JSX does not parse in a disk plugin"
    assert re.search(r"^export default \{\s*\n\s*id: ID,", source, re.M)
    assert re.search(r"^const ID = 'flota'$", source, re.M)
    assert PLUGIN.parent.name == "desktop" and PLUGIN.parents[1].joinpath("plugin.yaml").exists()


def test_no_hardcoded_colours():
    css = re.search(r"export const CSS = `([\s\S]*?)`", PLUGIN.read_text())[1]
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(", css), "colours must come from theme variables"
    assert "prefers-reduced-motion" in css


# --- run it under node ---------------------------------------------------------------------------

STUB_REACT = """
export function useState(v) { return [typeof v === 'function' ? v() : v, () => {}] }
export function useRef(v = null) { return { current: v } }
export function useEffect() {}
export function useMemo(f) { return f() }
export function useCallback(f) { return f }
export default { useState, useRef, useEffect, useMemo, useCallback }
"""

STUB_JSX = """
export function jsx(type, props, key) {
  if (typeof type === 'function') return type(props || {})
  return { type, props: props || {}, key }
}
export const jsxs = jsx
export const Fragment = 'fragment'
"""

RUNNER = r"""
const calls = []
globalThis.__calls = calls
globalThis.document = { createElement: tag => ({ tag, dataset: {}, remove() { calls.push(['style-removed']) } }),
                        head: { append: el => calls.push(['style', el.tag, el.textContent.length]) } }
const mod = await import('./plugin.mjs')
const plugin = mod.default

// Markup, enough to count nodes and classes.
const esc = s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;')
function render(node) {
  if (node == null || node === false || node === true) return ''
  if (Array.isArray(node)) return node.map(render).join('')
  if (typeof node !== 'object') return esc(node)
  const { children, ...rest } = node.props
  const attrs = Object.entries(rest).filter(([k, v]) => typeof v !== 'function' && typeof v !== 'object' && v !== undefined && v !== false)
    .map(([k, v]) => ` ${k === 'className' ? 'class' : k}="${esc(v)}"`).join('')
  return `<${node.type}${attrs}>${render(children)}</${node.type}>`
}

const disposers = []
const contributions = []
const ctx = {
  source: 'plugin:flota',
  register: c => { contributions.push(c); return () => {} },
  registerMany: cs => { contributions.push(...cs); return () => {} },
  onDispose: fn => disposers.push(fn),
  rest: (path, opts) => { calls.push(['rest', path, opts || null]); return Promise.resolve({}) },
}
plugin.register(ctx)

const base = { user: 'me', port: 22, key: '', os: 'linux', profile_exists: true, setup_command: null, wake: 'wol',
  mac: 'aa:bb:cc:dd:ee:ff', broadcast: '', via: '', sleep: 'suspend', custom_sleep: false, sleep_after: 600,
  fallback: '', wake_error: null, waking: false, busy: 0, up: false, description: '' }
const fleet = n => Array.from({ length: n }, (_, i) => ({ ...base, name: `m${i}`, host: `10.0.0.${i + 2}`,
  profile: `flota-m${i}`, up: i % 3 === 0, waking: i % 3 === 2, busy: i % 3 === 2 ? 2 : 0 }))
const maps = {}
for (const n of [0, 1, 3, 8, 14]) {
  maps[n] = render(mod.FleetMap({ hub: { name: 'estudio', os: 'linux' }, machines: fleet(n), selected: n ? 'm0' : null }))
}
const layouts = {}
for (const n of [1, 2, 3, 5, 8, 12, 13, 14, 20]) layouts[n] = mod.layoutRing(n)

const page = {}
const status = (machines, extra = {}) => ({ hub: { name: 'estudio' }, machines, problems: ["machine 'x': bad"], allow_power_off: false, busy_known: true, ...extra })
globalThis.__query = { data: status([]), dataUpdatedAt: Date.now() }
page.empty = render(mod.FlotaPage({ api: mod.makeApi(ctx.rest) }))
globalThis.__query = { data: status([], { role: 'flagship' }), dataUpdatedAt: Date.now() }
page.flagshipEmpty = render(mod.FlotaPage({ api: mod.makeApi(ctx.rest) }))
globalThis.__query = { error: new Error("Error invoking remote method 'hermes:api': Error: 404: {\"detail\":\"Not Found\"}") }
page.offline = render(mod.FlotaPage({ api: mod.makeApi(ctx.rest) }))
globalThis.__query = { data: status(fleet(3)), dataUpdatedAt: Date.now() }
page.three = render(mod.FlotaPage({ api: mod.makeApi(ctx.rest) }))

const api = mod.makeApi(ctx.rest)
calls.length = 0
await api.status(); await api.wake('torre'); await api.sleep('torre', true); await api.off('torre')
await api.save({ name: 'x' }); await api.remove('a b'); await api.schedule()
await api.invite(); await api.addShip('SHIP-1'); await api.join('INV-1'); await api.joinCheck(); await api.fleets()
await api.leave('estudio main'); await api.setRole('ship')
const restCalls = calls.filter(c => c[0] === 'rest').map(c => [c[1], c[2] && c[2].method || 'GET'])
const restBodies = Object.fromEntries(calls.filter(c => c[0] === 'rest' && c[2] && c[2].body).map(c => [c[1], c[2].body]))
// An older backend: the new endpoints 404 and the UI degrades instead of throwing.
const notFound = () => Promise.reject(new Error('Error invoking remote method: Error: 404: {"detail":"Not Found"}'))
const oldBackend = mod.makeApi(notFound)
const fallback = { fleets: await oldBackend.fleets(), joinCheck: await oldBackend.joinCheck(), role: await oldBackend.setRole('ship') }
try { await oldBackend.invite(); fallback.invite = 'resolved' } catch (e) { fallback.invite = mod.parseError(e).status }
// An older backend without /schedule: the jobs come through the gateway's cron.manage door.
const oldApi = mod.makeApi(path => path === '/schedule' ? Promise.reject(new Error('404: {"detail":"Not Found"}')) : Promise.resolve({}))
const rpcSchedule = await oldApi.schedule()

// --- crew ---
const crewOf = m => { const c = mod.crew({ ...base, name: 't', host: 'h', profile: 'flota-t', ...m }); return { ...c, members: c.members.map(x => `${x.role}:${x.mood}`) } }
const crews = {
  asleep: crewOf({ up: false }), waking: crewOf({ waking: true }), idle: crewOf({ up: true, agents: ['flota-t', 'gpu'] }),
  busy: crewOf({ up: true, busy: 2 }), crowd: crewOf({ up: true, busy: 6, agents: ['flota-t', 'b'] }),
}
const ship = m => render(mod.FleetMap({ hub: { name: 'estudio' }, machines: [{ ...base, name: 't', host: 'h', profile: 'flota-t', ...m }] }))
const ships = { asleep: ship({ up: false }), waking: ship({ waking: true }), idle: ship({ up: true }), busy: ship({ up: true, busy: 2 }),
  crowd: ship({ up: true, busy: 6 }) }

// --- avatars, compact clusters, expanding rings ---
const IMG = 'data:image/png;base64,iVBORw0KGgo='
const bots = [{ id: 'a', name: 'aide', avatar: IMG, color: null, state: 'awake' }, { id: 'c', name: 'coder', avatar: null, color: null, state: 'working' },
  { id: 's', name: 'scribe', avatar: null, color: 'oklch(0.7 0.1 30)', state: 'asleep' }]
const eight = ['hermes', 'aide', 'coder', 'research', 'scribe', 'ops', 'tutor', 'chef'].map((n, i) => ({ id: n, name: n, avatar: null, color: null, state: i === 5 ? 'asleep' : 'awake' }))
const av = {
  up: ship({ up: true, crew: bots }),
  asleep: ship({ up: false, crew: bots.map(b => ({ ...b, state: 'asleep' })) }),
  waking: ship({ waking: true, crew: [bots[2]] }),
  hub8: render(mod.FleetMap({ hub: { name: 'estudio' }, machines: fleet(3), hubCrew: eight, hubActive: true })),
  hub8off: render(mod.FleetMap({ hub: { name: 'estudio' }, machines: fleet(3), hubCrew: eight, hubActive: false })),
  crew: (() => { const c = mod.crew({ ...base, name: 't', up: true, crew: eight }); return { total: c.total, members: c.members.length, extra: c.extra, avatars: c.avatars } })(),
  colors: [mod.botColor({ name: 'coder' }), mod.botColor({ name: 'coder' }), mod.botColor({ name: 'scribe' }), mod.botColor({ name: 'x', color: 'red' })],
  initials: [mod.botInitial('flota-torre'), mod.botInitial('éclair'), mod.botInitial('__'), mod.botInitial('9lives')],
  bots: mod.botsOf([{ name: 'a', state: 'AWAKE' }, { name: 'b', state: 'weird' }, { id: 'c', state: 'asleep', avatar: 'javascript:alert(1)' }, null], 'waking'),
}
// Ring geometry for every layout: seats on the ring, clear of the label, inside the view.
const geometry = []
for (const n of [1, 3, 5, 8, 12, 14]) {
  const L = mod.layoutRing(n)
  for (const p of L.nodes) {
    const above = p.y < L.cy - 20
    const seats = mod.crewSeats({ count: 9, r: L.r, rowY: above ? -L.r - 16 : L.r + 19, labelAbove: above })
    geometry.push({ n, above, R: seats.R, r: L.r, x: p.x, y: p.y, view: L.view, ring: seats.ring, compact: seats.compact, vis: seats.vis, shown: seats.shown })
  }
}
const hubSeats = mod.crewSeats({ count: 8, r: 50, rowY: -72, hub: true })
const anchors = [mod.hubCrewAnchor([-Math.PI / 2]), mod.hubCrewAnchor([0, Math.PI]), mod.hubCrewAnchor([])]

// --- roles, onboarding, pairing ---
const roles = {
  modes: [
    mod.viewMode({ status: null, fleets: [], role: null }),
    mod.viewMode({ status: { machines: [] }, fleets: [], role: null }),
    mod.viewMode({ status: { machines: [] }, fleets: null, role: null }),
    mod.viewMode({ status: { machines: [] }, fleets: [], role: 'flagship' }),
    mod.viewMode({ status: { machines: [] }, fleets: [], role: 'ship' }),
    mod.viewMode({ status: { machines: [] }, fleets: [{ flagship: 'estudio' }], role: 'ship' }),
    mod.viewMode({ status: { machines: [] }, fleets: [{ flagship: 'estudio' }], role: null }),
    mod.viewMode({ status: { machines: [{ name: 'x' }] }, fleets: [{ flagship: 'e' }], role: 'ship' }),
    mod.viewMode({ status: { machines: [] }, fleets: [{ flagship: 'estudio' }], role: 'ship', pinJoin: true }),
  ],
  effective: [mod.effectiveRole({ role: 'ship' }, null, 'flagship'), mod.effectiveRole({ role: 'ship' }, 'flagship', null),
    mod.effectiveRole({}, null, 'ship'), mod.effectiveRole({ role: null }, null, null), mod.effectiveRole({ role: 'bogus' }, null, null)],
}
const setupResult = { ok: true, lines: ['detected: linux · enp8s0 · mac aa:bb:cc:dd:ee:ff', '✓ Saved machine torre', 'failed: could not enable WoL'], problems: ['ssh: permission denied'], machine: 'torre' }
const flows = {
  log: mod.setupLog(setupResult),
  addDone: render(mod.AddressForm({ api, initial: { done: setupResult } })),
  addFailed: render(mod.AddressForm({ api, initial: { done: { ok: false, lines: ['Connecting to 10.0.0.9'], problems: ['ssh: connection refused'] } } })),
  addLegacy: render(mod.AddressForm({ api, initial: { done: { ok: true, machine: 'torre', setup_command: 'hermes flota setup torre --host h' } } })),
  addForm: render(mod.AddressForm({ api })),
  invite: render(mod.InvitePanel({ api, initial: { invite: { code: 'FLOTA-ABCD-1234', expires_at: new Date(Date.now() + 600e3).toISOString(), flagship: 'estudio' } } })),
  invite404: render(mod.InvitePanel({ api, initial: { invite: { error: { status: 404, message: 'Not Found', problems: [] } } } })),
  inviteDone: render(mod.InvitePanel({ api, initial: { invite: { code: 'X' }, result: { ok: true, lines: ['Saved machine portatil'], machine: 'portatil' } } })),
  choose: render(mod.AddShipsDialog({ open: true, mode: 'choose', onClose() {}, api })),
  wiz1: render(mod.JoinWizard({ api, hub: { os: 'linux' }, initial: { step: 1, check: { checks: [{ id: 'ssh', label: 'SSH server running', ok: true }, { id: 'port', label: 'Port 22 reachable', ok: false, fix: 'as root: ufw allow 22/tcp' }, { id: 'wol', label: 'Wake-on-LAN', ok: false, optional: true, fix: 'as root: ethtool -s enp8s0 wol g' }] } } })),
  wiz1old: render(mod.JoinWizard({ api, hub: { os: 'macos' }, initial: { step: 1, check: { unsupported: true } } })),
  wiz2: render(mod.JoinWizard({ api, initial: { step: 2, check: {} } })),
  wiz3: render(mod.JoinWizard({ api, initial: { step: 3, check: {}, code: 'flota1.' + Buffer.from(JSON.stringify({ flagship: 'estudio' })).toString('base64') } })),
  wiz3done: render(mod.JoinWizard({ api, initial: { step: 3, check: {}, code: 'X', result: { ship_code: 'SHIP-XYZ-42', lines: ['Authorised estudio'] } } })),
  wiz4: render(mod.JoinWizard({ api, initial: { step: 4, check: {}, code: 'X', result: { ship_code: 'S', flagship: 'estudio' } } })),
  chooser: render(mod.RoleChooser({ hubName: 'estudio', onChoose() {} })),
  fleets: render(mod.FleetsCard({ fleets: [{ flagship: 'estudio', pubkey_fingerprint: 'SHA256:abc', joined_at: '2026-11-08T10:00:00Z' }, { flagship: 'oficina', joined_at: 1762770000 }], api, onJoin() {} })),
  fleetsOld: render(mod.FleetsCard({ fleets: [], api, unsupported: true })),
  shipMap: render(mod.ShipMap({ hub: { name: 'studio', os: 'linux' }, fleets: [{ flagship: 'estudio' }, { flagship: 'oficina' }], crew: bots })),
  decoded: [mod.decodeInvite('flota1.' + Buffer.from(JSON.stringify({ flagship: 'estudio' })).toString('base64')), mod.decodeInvite('nonsense'), mod.decodeInvite('')],
  countdown: [mod.countdown(1000 + 581e3, 1000), mod.countdown(0, 1000), mod.countdown(1000 + 7500e3, 1000)],
  toMs: [mod.toMs(1762770000), mod.toMs(1762770000000), mod.toMs('2026-11-10T00:00:00Z'), mod.toMs(null)],
  checksMac: mod.joinChecks(null, 'darwin').map(c => [c.id, c.ok, c.fix]),
  checksWin: mod.joinChecks({ os: 'windows' }).map(c => c.fix),
}
const fleetRow = { flagship: 'estudio', pubkey_fingerprint: 'SHA256:abc', joined_at: '2026-11-08T10:00:00Z' }
globalThis.__queries = { status: { data: status([], { role: 'ship', hub_crew: bots, hub: { name: 'studio' } }), dataUpdatedAt: Date.now() }, fleets: { data: { fleets: [fleetRow] } } }
page.ship = render(mod.FlotaPage({ api }))
globalThis.__queries = { status: { data: status([], { role: 'ship' }), dataUpdatedAt: Date.now() }, fleets: { data: { fleets: [] } } }
page.join = render(mod.FlotaPage({ api }))
globalThis.__queries = { status: { data: status([], { role: null }), dataUpdatedAt: Date.now() }, fleets: { data: { fleets: [], unsupported: true } } }
page.choose = render(mod.FlotaPage({ api }))
globalThis.__queries = { status: { data: status(fleet(2), { hub_crew: eight }), dataUpdatedAt: Date.now() }, fleets: { data: { fleets: [fleetRow] } } }
page.flagshipAlsoShip = render(mod.FlotaPage({ api }))
globalThis.__queries = null

// --- schedule ---
const NOW = Date.parse('2026-11-10T12:00:00Z')
const H = 3600e3
const iso = t => new Date(t).toISOString()
const fakeJobs = { jobs: [
  { id: 'later', name: 'Render video', prompt_preview: 'render on the mini-pc', schedule: '30 3 * * 1', next_run_at: iso(NOW + 50 * H), enabled: true },
  { id: 'paused', name: 'Disk check on torre', schedule: 'every 6h', next_run_at: null, enabled: false, state: 'paused' },
  { id: 'soon', name: 'Digest', prompt_preview: 'summarise my inbox', schedule: '0 8 * * 1-5', next_run_at: iso(NOW + 2 * H + 10 * 60e3), enabled: true },
  { id: 'backup', job_id: 'x', name: 'Nightly backup', prompt_preview: 'back up TORRE', schedule: '0 2 * * *', next_run_at: iso(NOW + 3 * H), enabled: true },
  { id: 'server', name: 'Explicit', schedule: 'every 1d', next_run_at: iso(NOW + 4 * H), enabled: true, machine: 'mini', machines: ['mini'] },
] }
const names = ['torre', 'mini']
const rows = mod.scheduleRows(fakeJobs, names)
const sched = {
  order: rows.map(r => r.id), machines: Object.fromEntries(rows.map(r => [r.id, r.machine])),
  next: mod.nextWake(rows, NOW) && { machine: mod.nextWake(rows, NOW).machine, job: mod.nextWake(rows, NOW).row.name },
  rel: [mod.relTime(NOW + 2 * H + 10 * 60e3, NOW), mod.relTime(NOW + 3 * H, NOW), mod.relTime(NOW + 50 * H, NOW), mod.relTime(NOW + 5 * 60e3, NOW),
    mod.relTime(NOW + 10e3, NOW), mod.relTime(NOW - 10 * 60e3, NOW)],
  human: ['0 2 * * *', '30 3 * * 1', '0 8 * * 1-5', 'every 6h', 'every 1d', '*/15 * * * *', '0 */4 * * *', '0 9 1 * *', '0 9 * * 1,3', 'weird', ''].map(mod.humanSchedule),
  mentions: [mod.jobMachines({ name: 'torretas', prompt: 'on mini' }, names), mod.jobMachines({ prompt: 'flota-torre then mini-pc' }, names),
    mod.jobMachines({ name: 'x', skills: ['torre'] }, names)],
  panel: render(mod.SchedulePanel({ data: fakeJobs, rows, machines: [{ ...base, name: 'torre', up: false }, { ...base, name: 'mini', up: true }], hubName: 'estudio', selected: 'torre', now: NOW })),
  empty: render(mod.SchedulePanel({ data: { jobs: [], available: true }, rows: [], machines: [], hubName: 'estudio', now: NOW })),
  unavailable: render(mod.SchedulePanel({ data: { jobs: [], available: false, error: 'cron jobs unavailable: no cron' }, rows: [], machines: [], hubName: 'estudio', now: NOW })),
  map: render(mod.FleetMap({ hub: { name: 'estudio' }, machines: [{ ...base, name: 'torre', host: 'h', profile: 'flota-torre' }, { ...base, name: 'mini', host: 'm', profile: 'flota-mini' }], schedule: mod.scheduleRows({ jobs: [{ id: 'j', name: 'Nightly backup', machine: 'torre', machines: ['torre'], next_run_at: iso(Date.now() + 3 * H), schedule: '0 2 * * *' }] }, names) })),
}
globalThis.__queries = { status: { data: status(fleet(3)), dataUpdatedAt: Date.now() }, schedule: { data: fakeJobs } }
page.withSchedule = render(mod.FlotaPage({ api: mod.makeApi(ctx.rest) }))
globalThis.__queries = null

// --- chats: which bots this app can reach, and what a click does ---
const sdkCalls = []
const fakeSdk = (over = {}) => ({
  request: (m, p) => { sdkCalls.push(['request', m, p]); return Promise.resolve({ profiles: [{ name: 'coder', canonical_session: { id: 'c0', resolved_id: 'c1' } }, { name: 'scribe', last_session: { id: 's9' } }] }) },
  requestProfile: (route, m, p, t, o) => { sdkCalls.push(['requestProfile', route, m, o]); return Promise.resolve({ profiles: [{ name: 'ops', last_session: { id: 'r7' } }] }) },
  openSession: (id, o) => { sdkCalls.push(['openSession', id, o]); return Promise.resolve() },
  newChat: p => { sdkCalls.push(['newChat', p]) },
  notify: n => { sdkCalls.push(['notify', n.kind, n.message]) },
  connections: () => Promise.resolve([{ id: 'local', kind: 'local', label: 'estudio' }, { id: 'c-torre', kind: 'ssh', label: 'Torre box', host: 'me@torre.example.ts.net', tokenSet: false },
    { id: 'c-web', kind: 'remote', label: 'Web', url: 'https://10.0.0.9:9119/' }]),
  agents: () => Promise.resolve({ agents: [], sources: [{ connectionId: 'c-lab', label: 'lab', kind: 'remote', reachable: true }] }),
  profileRoutes: () => Promise.resolve([{ profile: 'coder', targetProfile: 'coder', connectionId: 'local', mode: 'local' },
    { profile: 'default', targetProfile: 'ops', connectionId: 'c-torre', mode: 'remote' }]),
  activeConnectionId: () => null, ...over })
const net = await mod.loadNet(fakeSdk())
const netOld = await mod.loadNet({}) // a Desktop without any of these doors
const netWeb = await mod.loadNet({ connections: () => Promise.reject(new Error('no registry')), agents: () => Promise.resolve({ sources: [{ connectionId: 'local', label: 'estudio:9119', kind: 'local' }] }),
  profileRoutes: () => Promise.resolve([{ profile: 'coder', targetProfile: 'coder', connectionId: 'local', mode: 'local' }]) })
const torre = { ...base, name: 'torre', host: '10.0.0.3', hostname: 'torre', profile: 'flota-torre', up: true }
const mini = { ...base, name: 'mini', host: '10.0.0.4', hostname: 'mini-pc', profile: 'flota-mini', up: true }
const rb = (id, machine, hostname, state = 'awake') => ({ id, name: id, avatar: null, color: null, state, remote: true, machine, hostname })
const one = b => mod.botsOf([b])[0]
const access = {
  hub: mod.chatAccess(one({ id: 'coder', name: 'Coder' }), null, net),
  hubOld: mod.chatAccess(one({ id: 'coder', name: 'Coder' }), null, netOld),
  local: mod.chatAccess(one({ id: 'flota-torre', name: 'flota-torre' }), torre, net),
  torre: mod.chatAccess(one(rb('ops', 'torre', 'torre')), torre, net),
  torreNoRoute: mod.chatAccess(one(rb('chef', 'torre', 'torre')), torre, net),
  mini: mod.chatAccess(one(rb('tutor', 'mini', 'mini-pc')), mini, net),
  pending: mod.chatAccess(one(rb('tutor', 'mini', 'mini-pc')), mini, { ready: false }),
  byIp: mod.chatAccess(one(rb('x', 'web', '')), { name: 'web', host: '10.0.0.9' }, net),
  byLabel: mod.chatAccess(one(rb('y', 'lab', '')), { name: 'lab', host: '10.9.9.9' }, net),
  notLocal: mod.chatAccess(one(rb('z', 'estudio', 'estudio')), { name: 'estudio', host: '10.0.0.1', hostname: 'estudio' }, net),
  oldDesktop: mod.chatAccess(one(rb('ops', 'torre', 'torre')), torre, netOld),
  web: mod.chatAccess(one(rb('ops', 'torre', 'torre')), torre, netWeb),
  webHub: mod.chatAccess(one({ id: 'coder' }), null, netWeb),
}
const hostKeys = [mod.hostKeys(['ssh://Me@Torre.Local:22/x']), mod.hostKeys(['me@10.0.0.3:2222', 'localhost']), mod.hostKeys(['https://[fd7a::1]:9119/'])]
const callsFor = async fn => { sdkCalls.length = 0; const r = await fn(); return { r, calls: JSON.parse(JSON.stringify(sdkCalls)) } }
const hang = fakeSdk({ requestProfile: () => new Promise(() => {}) })
const opens = {
  hubOld: await callsFor(() => mod.openBotChat(one({ id: 'coder' }), access.hubOld, fakeSdk())),
  torre: await callsFor(() => mod.openBotChat(one(rb('ops', 'torre', 'torre')), access.torre, fakeSdk())),
  chef: await callsFor(() => mod.openBotChat(one(rb('chef', 'torre', 'torre')), access.torreNoRoute, fakeSdk())),
  locked: await callsFor(() => mod.openBotChat(one(rb('tutor', 'mini', 'mini-pc')), access.mini, fakeSdk())),
  slow: await callsFor(() => mod.openBotChat(one(rb('ops', 'torre', 'torre')), access.torre, hang, { timeoutMs: 20 })),
  asleep: await callsFor(() => mod.openChat(one(rb('ops', 'torre', 'torre', 'asleep')), access.torre, { ...torre, up: false }, fakeSdk())),
  unsupported: await callsFor(() => mod.openChat(one({ id: 'coder' }), access.hubOld, null, { notify: fakeSdk().notify })),
}
// The map with a reachable ship (torre) and one this app has no connection to (mini).
const ten = Array.from({ length: 10 }, (_, i) => rb(`bot${i}`, 'mini', 'mini-pc', i === 3 ? 'working' : 'awake'))
const chatShips = [{ ...torre, crew: [{ id: 'flota-torre', name: 'flota-torre', state: 'awake' }, rb('ops', 'torre', 'torre'), rb('chef', 'torre', 'torre', 'working')] },
  { ...mini, crew: [rb('tutor', 'mini', 'mini-pc'), rb('scribe', 'mini', 'mini-pc')] }]
const botClicks = []
const chatMap = mod.FleetMap({ hub: { name: 'estudio' }, machines: chatShips, hubCrew: eight.slice(0, 3), net, onBot: (b, a, m) => botClicks.push([b.id, a.ok, m && m.name, a.route && a.route.connectionId]) })
const walk = (n, out = []) => { if (Array.isArray(n)) n.forEach(x => walk(x, out)); else if (n && typeof n === 'object' && n.props) { out.push(n); walk(n.props.children, out) } return out }
const seats = walk(chatMap).filter(n => n.props['data-bot'] && n.props.role === 'button')
const ev = { stopPropagation() { ev.stopped = (ev.stopped || 0) + 1 }, preventDefault() {} }
for (const sId of ['ops', 'tutor', 'flota-torre']) seats.find(n => n.props['data-bot'] === sId).props.onClick(ev)
seats.find(n => n.props['data-bot'] === 'chef').props.onKeyDown({ ...ev, key: 'Enter' })
seats.find(n => n.props['data-bot'] === 'scribe').props.onKeyDown({ ...ev, key: 'Enter' })
seats.find(n => n.props['data-bot'] === 'hermes').props.onClick(ev)
const panelClicks = []
const crewPanel = mod.CrewSection({ bots: mod.botsOf(chatShips[0].crew, 'up'), m: chatShips[0], net, onBot: (b, a, m) => panelClicks.push([b.id, m && m.name, a.route && a.route.connectionId]) })
walk(crewPanel).filter(n => n.type === 'button' && n.props['data-bot']).forEach(n => n.props.onClick())
const crewPanelMini = mod.CrewSection({ bots: mod.botsOf(chatShips[1].crew, 'up'), m: chatShips[1], net, onBot: (b, a, m) => panelClicks.push([b.id, m && m.name]) })
walk(crewPanelMini).filter(n => n.type === 'button' && n.props['data-bot']).forEach(n => n.props.onClick())
const chats = {
  access, hostKeys, opens, botClicks, panelClicks, stopped: ev.stopped,
  net: { links: net.links, routes: net.routes.length, activeId: net.activeId }, netOld, netWeb: { links: netWeb.links, routes: netWeb.routes.length },
  map: render(chatMap),
  mapExpanded: render(mod.FleetMap({ hub: { name: 'estudio' }, machines: chatShips, net, expanded: ['mini'] })),
  details: render(mod.Details({ m: chatShips[0], status: status([chatShips[0]]), api, net, onRemoved() {} })),
  detailsDocked: render(mod.Details({ m: { ...chatShips[0], up: false, crew: chatShips[0].crew.map(b => ({ ...b, state: 'asleep' })) }, status: status([]), api, net, onRemoved() {} })),
  detailsMini: render(mod.Details({ m: chatShips[1], status: status([]), api, net, onRemoved() {} })),
  panel: render(crewPanel), panelMini: render(crewPanelMini),
  ten: render(mod.FleetMap({ hub: { name: 'estudio' }, machines: [{ ...mini, crew: ten }], net })),
  tenCrew: (() => { const c = mod.crew({ ...mini, crew: ten }); return { total: c.total, members: c.members.length, extra: c.extra } })(),
  ring10: [mod.crewSeats({ count: 10, r: 33, rowY: 56 }), mod.crewSeats({ count: 10, r: 28, rowY: 47, labelAbove: true }), mod.crewSeats({ count: 10, r: 50, rowY: -72, hub: true }),
    mod.crewSeats({ count: 12, r: 33, rowY: 56 }), mod.crewSeats({ count: 3, r: 33, rowY: 56 })],
  tips: [
    mod.tipPlacement({ left: 140, top: 90, width: 20, height: 20 }, { left: 80, top: 80, width: 40, height: 40 }, { left: 0, top: 0, width: 1000, height: 600 }), // seat right of its node
    mod.tipPlacement({ left: 940, top: 90, width: 20, height: 20 }, { left: 880, top: 80, width: 40, height: 40 }, { left: 0, top: 0, width: 1000, height: 600 }), // no room right: below
    mod.tipPlacement({ left: 490, top: 400, width: 20, height: 20 }, { left: 480, top: 300, width: 40, height: 40 }, { left: 0, top: 0, width: 1000, height: 600 }), // under its node
  ],
  ring10View: (() => {
    const out = []
    for (const n of [1, 3, 5, 8, 12, 14]) {
      const L = mod.layoutRing(n)
      for (const p of L.nodes) {
        const above = p.y < L.cy - 20
        out.push({ x: p.x, y: p.y, view: L.view, ring: mod.crewSeats({ count: 10, r: L.r, rowY: above ? -L.r - 16 : L.r + 19, labelAbove: above }).ring })
      }
    }
    return out
  })(),
}
globalThis.__queries = { status: { data: status(chatShips, { hub_crew: eight.slice(0, 2) }), dataUpdatedAt: Date.now() }, net: { data: net } }
page.chats = render(mod.FlotaPage({ api }))
globalThis.__queries = null


// --- reaching the flagship from any connection ---
const pluginOff = new Error("Error invoking remote method 'hermes:api': Error: 404: {\"detail\":\"Plugin not found\"}")
const routeOff = new Error('Error invoking remote method: Error: 404: {"detail":"Not Found"}')
const reach = {}
reach.kinds = [pluginOff, routeOff, new Error('Hermes desktop bridge unavailable'), new Error('502: bad gateway'), new Error('401: {"detail":"Unauthorized"}'),
  new Error('500: boom'), new Error('409: {"detail":{"error":"already in a fleet","fleet":{"flagship":"servidor"}}}')].map(e => mod.classifyError(e).kind)
reach.older = [mod.isOlderBackend(mod.classifyError(pluginOff)), mod.isOlderBackend(mod.classifyError(routeOff)), mod.isOlderBackend({ status: 404, message: 'Not Found' }),
  mod.isOlderBackend({ status: 404, message: 'Plugin not found' })]
reach.conflict = mod.classifyError(new Error('409: {"detail":{"error":"already in a fleet","fleet":{"flagship":"servidor"}}}'))
const routesAll = [{ profile: 'default', targetProfile: 'default', connectionId: 'local', mode: 'local' }, { profile: 'aide', targetProfile: 'aide', connectionId: 'local', mode: 'local' },
  { profile: 'default', targetProfile: 'default', connectionId: 'server', mode: 'remote' }, { profile: 'flota-studio', targetProfile: 'flota-studio', connectionId: 'server', mode: 'remote' },
  { profile: 'default', targetProfile: 'default', connectionId: 'server', mode: 'remote' }]
const netReach = { links: [{ id: 'local', kind: 'local', label: 'desk-pc', keys: [] }, { id: 'server', kind: 'remote', label: 'server', keys: ['servidor'] }], routes: routesAll, activeId: null, ready: true }
const probeCalls = []
const probeSdk = { activeConnectionId: () => null, state: { profile: { get: () => 'default' } }, notify: n => probeCalls.push(['notify', n.kind, n.message]),
  requestProfile: (route, method, params) => { probeCalls.push([route.connectionId, route.profile, method, params]); return route.connectionId === 'server'
    ? Promise.resolve({ type: 'plugin', output: 'Usage: /flota [status | dry-run <machine> | wake <machine>]\nMachines: studio' })
    : Promise.reject(new Error('4018 not a quick/plugin/bundle/skill command: flota')) },
  ensureAgent: (c, prof) => { probeCalls.push(['ensureAgent', c, prof]); return Promise.resolve() } }
reach.scopeLocal = mod.currentScope(probeSdk, netReach)
reach.cands = mod.probeCandidates(routesAll, reach.scopeLocal).map(r => `${r.connectionId}::${r.profile}`)
reach.found = await mod.findFlagship(probeSdk, netReach, reach.scopeLocal)
reach.probeCalls = probeCalls.slice()
const botSdk = { ...probeSdk, activeConnectionId: () => 'server', state: { profile: { get: () => 'flota-studio' } } }
reach.scopeBot = mod.currentScope(botSdk, netReach)
reach.foundBot = await mod.findFlagship(botSdk, netReach, reach.scopeBot)
reach.none = await mod.findFlagship({ ...probeSdk, requestProfile: () => Promise.reject(new Error('x')) }, netReach, reach.scopeLocal)
reach.unsupported = await mod.findFlagship({}, netReach, reach.scopeLocal)
reach.usage = [mod.usageMachines('Usage: /flota x\nMachines: a, b'), mod.usageMachines('Usage\nMachines: none configured'), mod.usageMachines('')]
probeCalls.length = 0
await mod.reachRoute({ connectionId: 'server', profile: 'default' }, probeSdk)
await mod.reachRoute({ connectionId: 'local', profile: 'default' }, probeSdk)
reach.ensure = probeCalls.slice()
try { await mod.reachRoute({ connectionId: 'server', profile: 'default' }, {}); reach.noEnsure = 'ok' } catch (e) { reach.noEnsure = e.message }
const offErr = mod.classifyError(pluginOff)
reach.cardFound = render(mod.ReachCard({ error: offErr, scope: reach.scopeLocal, probe: reach.found, onReach() {}, onRetry() {} }))
reach.cardBot = render(mod.ReachCard({ error: offErr, scope: reach.scopeBot, probe: reach.foundBot, onReach() {}, onRetry() {} }))
reach.cardNone = render(mod.ReachCard({ error: offErr, scope: reach.scopeLocal, probe: reach.none, onReach() {}, onRetry() {} }))
reach.cardOld = render(mod.ReachCard({ error: offErr, scope: reach.scopeLocal, probe: reach.unsupported, onReach() {}, onRetry() {} }))
reach.cardLooking = render(mod.ReachCard({ error: offErr, scope: reach.scopeLocal, probe: null, onReach() {}, onRetry() {} }))
reach.cardDown = render(mod.ReachCard({ error: mod.classifyError(new Error('Hermes desktop bridge unavailable')), scope: reach.scopeLocal, probe: reach.none, onReach() {}, onRetry() {} }))
globalThis.__query = null
globalThis.__queries = { status: { error: pluginOff }, probe: { data: reach.found }, net: { data: netReach } }
const reachPage = mod.FlotaPage({ api, sdk: probeSdk })
page.reach = render(reachPage)
probeCalls.length = 0
walk(reachPage).find(n => n.type === 'Button' && n.props.children === 'Open the fleet on server').props.onClick()
await new Promise(r => setTimeout(r, 20))
reach.clicked = probeCalls.slice()
globalThis.__queries = { status: { data: status(fleet(2), { hub: { name: 'servidor' }, role: 'flagship', fleet: null, ships_count: 2 }), error: pluginOff, dataUpdatedAt: Date.now() }, net: { data: netReach } }
page.staleErr = render(mod.FlotaPage({ api, sdk: botSdk }))
globalThis.__queries = { status: { data: status(fleet(2), { hub: { name: 'servidor' }, role: 'flagship', fleet: null, ships_count: 2 }), dataUpdatedAt: Date.now() }, net: { data: netReach } }
const viaServer = { ...probeSdk, activeConnectionId: () => 'server' }
const flagPage = mod.FlotaPage({ api, sdk: viaServer })
page.viaServer = render(flagPage)
page.local = render(mod.FlotaPage({ api, sdk: probeSdk }))
globalThis.__queries = null
reach.invitePluginOff = render(mod.InvitePanel({ api, initial: { invite: { error: offErr } } }))
const offApi = mod.makeApi(() => Promise.reject(pluginOff))
reach.offApi = {}
for (const k of ['fleets', 'joinCheck', 'setRole']) { try { await offApi[k]('ship'); reach.offApi[k] = 'resolved' } catch (e) { reach.offApi[k] = mod.classifyError(e).kind } }
const upd = mod.UpdateError({ error: offErr, scope: reach.scopeBot, onFix() {} })
reach.updateErr = render(upd)

// --- role lifecycle ---
const life = {}
const S = extra => ({ machines: [], role: null, fleet: null, ships_count: 0, ...extra })
life.modes = [
  mod.viewMode({ status: S(), fleets: [], role: mod.effectiveRole(S(), null, 'ship') }),
  mod.viewMode({ status: S({ role: 'ship' }), fleets: [], role: 'ship' }),
  mod.viewMode({ status: S({ role: 'ship', fleet: { flagship: 'servidor' } }), fleets: null, role: 'ship' }),
  mod.viewMode({ status: S({ role: 'flagship' }), fleets: [], role: 'flagship' }),
  mod.viewMode({ status: S({ fleet: { flagship: 'servidor' } }), fleets: [], role: null }),
  mod.viewMode({ status: S({ machines: [{ name: 'x' }] }), fleets: [], role: null }),
]
life.effective = [mod.effectiveRole(S(), null, 'ship'), mod.effectiveRole(S({ role: 'ship' }), null, 'flagship'), mod.effectiveRole(S(), 'flagship', null)]
const lifeCalls = []
let reply = () => ({})
const lifeApi = mod.makeApi((path, opts) => { lifeCalls.push([path, (opts && opts.method) || 'GET', (opts && opts.body) || null]); const r = reply(path, opts); return r instanceof Error ? Promise.reject(r) : Promise.resolve(r) })
const runLife = async (kind, ctx) => { lifeCalls.length = 0; try { const role = await mod.runRoleAction(kind, lifeApi, ctx); return { role, calls: lifeCalls.slice() } } catch (e) { return { error: e.message, needs: e.needs || null, calls: lifeCalls.slice() } } }
const srv = { flagship: 'servidor', joined_at: '2026-11-08T10:00:00Z' }
reply = path => path.startsWith('/fleets/') ? { ok: true, role: null } : { ok: true }
life.leave = await runLife('leave', { fleet: srv })
life.toFlagship = await runLife('flagship', { fleet: srv })
life.toFlagshipFresh = await runLife('flagship', { fleet: null })
life.toShipWithShips = await runLife('ship', { machines: [{ name: 'a' }, { name: 'b' }] })
life.toShipClear = await runLife('ship-clear', { machines: [{ name: 'a' }, { name: 'b c' }] })
life.toShip = await runLife('ship', { machines: [] })
reply = path => path === '/role' ? new Error('Error invoking remote method: Error: 404: {"detail":"Not Found"}') : { ok: true }
life.toShipClearOld = await runLife('ship-clear', { machines: [{ name: 'a' }, { name: 'b c' }] })
reply = path => path.startsWith('/fleets/') ? { ok: true, role: null } : { ok: true }
reply = path => path === '/role' ? new Error('409: {"detail":{"error":"this flagship has ships","needs":"remove_ships"}}') : { ok: true }
life.refusedShips = await runLife('ship', { machines: [] })
reply = path => path === '/role' ? new Error('409: {"error":"still in a fleet","needs":"leave_fleet"}') : { ok: true }
life.refusedFleet = await runLife('flagship', { fleet: null })
// The backend's real 409 shape: a plain-text detail beside error / needs / fleet.
reply = path => path === '/role' ? new Error('Error invoking remote method: Error: 409: {"error":"this machine is a ship of servidor","detail":"this machine is a ship of servidor","needs":"leave_fleet","fleet":{"flagship":"servidor"}}') : { ok: true }
life.refusedReal = await runLife('flagship', { fleet: null })
life.realJoin409 = mod.classifyError(new Error('409: {"error":"already in a fleet","fleet":{"flagship":"servidor"},"detail":"already in servidor\'s fleet"}'))
life.texts = Object.fromEntries(['leave', 'flagship', 'ship', 'ship-clear'].map(k => [k, mod.roleActionText(k, { fleet: srv, machines: [{ name: 'studio' }, { name: 'nas' }] })]))
life.textFresh = mod.roleActionText('flagship', {})
life.member = render(mod.MembershipCard({ fleet: srv, onLeave() {}, onBecomeFlagship() {} }))
const shipHand = { id: 'flota-studio', name: 'flota-studio', state: 'awake', ship_hand: true }
globalThis.__queries = { status: { data: status([], { role: 'ship', fleet: srv, ships_count: 0, hub_crew: [...bots, shipHand], hub: { name: 'studio', os: 'linux' } }), dataUpdatedAt: Date.now() },
  fleets: { data: { fleets: [srv] } } }
page.shipNew = render(mod.FlotaPage({ api }))
globalThis.__queries = { status: { data: status([], { role: null, fleet: null, ships_count: 0 }), dataUpdatedAt: Date.now() }, fleets: { data: { fleets: [] } } }
page.afterLeave = render(mod.FlotaPage({ api }))
globalThis.__queries = { status: { data: status([], { role: 'ship', fleet: null, ships_count: 0 }), dataUpdatedAt: Date.now() }, fleets: { data: { fleets: [] } } }
page.rejoin = render(mod.FlotaPage({ api }))
globalThis.__queries = { status: { data: status(fleet(2), { role: 'flagship', fleet: null, ships_count: 2 }), dataUpdatedAt: Date.now() }, fleets: { data: { fleets: [] } } }
page.flagshipNew = render(mod.FlotaPage({ api }))
globalThis.__queries = { status: { data: status([], { role: 'flagship', fleet: null, ships_count: 0 }), dataUpdatedAt: Date.now() }, fleets: { data: { fleets: [] } } }
page.flagshipNoShips = render(mod.FlotaPage({ api }))
globalThis.__queries = null
life.already = render(mod.JoinWizard({ api, initial: { step: 3, check: {}, code: 'X', error: reach.conflict } }))
life.joinPluginOff = render(mod.JoinWizard({ api, initial: { step: 3, check: {}, code: 'X', error: offErr } }))
life.wake = render(mod.JoinWizard({ api, hub: { os: 'windows' }, initial: { step: 1, check: { os: 'windows', checks: [{ id: 'sshd', label: 'OpenSSH Server service running', ok: false, fix: 'Start-Service sshd' }],
  wake_options: [{ id: 'wol', ok: true, label: 'Wake-on-LAN (Intel I225-V)' }, { id: 'timer', ok: false }, { id: 'none', ok: true }] } } }))
life.wakeOpts = mod.wakeOptions({ wake_options: [{ id: 'wol', ok: true }, { id: 'timer' }, null] })

// --- machines are not bots ---
const desk = { ...base, name: 'studio', host: '203.0.113.7', hostname: 'desk-pc', profile: 'flota-studio', up: true, crew: [shipHand] }
const deskBots = { ...desk, name: 'torre2', hostname: 'torre2', crew: [shipHand, rb('ops', 'torre2', 'torre2')] }
const machinesView = {
  map: render(mod.FleetMap({ hub: { name: 'servidor' }, machines: [desk, deskBots], net })),
  crew: (() => { const c = mod.crew(desk); return { total: c.total, avatars: c.avatars } })(),
  crewBots: mod.crew(deskBots).all.map(b => b.id),
  details: render(mod.Details({ m: desk, status: status([desk]), api, net, onRemoved() {} })),
  labels: [mod.hostLabel(desk), mod.hostLabel(deskBots), mod.hostLabel({ name: 'x' })],
  remove: mod.removeText(desk),
}

// --- hardening ---
const hard = {}
const hc = []
const hApi = mod.makeApi((path, opts) => { hc.push([path, (opts && opts.method) || 'GET', (opts && opts.body) || null]); return path === '/invite' && opts && opts.method === 'POST' ? Promise.reject(new Error('405: {"detail":"Method Not Allowed"}')) : Promise.resolve({}) })
await hApi.addShip('SHIP-1', 'nas'); hard.named = [hc[0][0], hc[0][2]]
await hApi.join('INV-1', 'servidor'); hard.confirmed = [hc[1][0], hc[1][2]]
hc.length = 0; await hApi.invite(); hard.inviteOld = hc.map(c => [c[0], c[1]])
const formBase = { name: 'nas', host: '10.0.0.9', user: 'u', key: '', description: '', mac: '', broadcast: '', wake: 'wol', sleep: 'suspend', sleep_after: '10m' }
hard.bodyAuto = mod.machineBody({ ...formBase, port: '2222' })
hard.bodyMac = mod.machineBody({ ...formBase, mac: 'aa:bb:cc:dd:ee:ff', port: '22' })
hard.bodyNever = mod.machineBody({ ...formBase, wake: 'none' })
hard.wake = [mod.wakeOutcome({ wake: 'wol', mac: 'aa:bb:cc:dd:ee:ff' }, 'wol'), mod.wakeOutcome({ wake: 'none', mac: '' }, 'wol'), mod.wakeOutcome({ wake: 'none' }, 'none'), mod.wakeOutcome({ ok: true }, 'wol')]
hard.addDone = render(mod.AddressForm({ api, initial: { form: formBase, done: { ok: true, lines: ['✓ saved'], machine: 'nas', wake: 'none', mac: '' } } }))
hard.invitePanel = render(mod.InvitePanel({ api, initial: { invite: { code: 'X', expires_at: null, flagship: 'f' } } }))
hc.length = 0; await hApi.addShip('SHIP-1', 'nas', true); await hApi.addShip('SHIP-1', undefined, true); hard.replaceBodies = hc.map(c => c[2])
const moved = "this ship code says it is 'torre', but it logs in as u@10.0.0.9 and 'torre' is saved as u@10.0.0.5. Nothing was changed. If 'torre' really moved there, run it again with --replace (hermes flota add-ship <code> --replace); otherwise do not add this code."
const panelWith = detail => render(mod.InvitePanel({ api, initial: { invite: { code: 'X', expires_at: null, flagship: 'f' }, error: mod.classifyError(new Error('400: ' + JSON.stringify({ detail }))) } }))
hard.replaceReason = [mod.replaceReason(mod.classifyError(new Error('400: ' + JSON.stringify({ detail: moved })))), mod.replaceReason(mod.classifyError(new Error('400: {"detail":"already used"}'))), mod.replaceReason(null)]
hard.replacePanel = panelWith(moved); hard.usedPanel = panelWith('already used')
const ref = { current: null }
const e404 = new Error('404: {"detail":"Plugin not found"}')
hard.sticky = [mod.stickyError(ref, { error: e404 }), mod.stickyError(ref, {}), mod.stickyError(ref, { data: {} }), mod.stickyError(ref, {})].map(e => e ? String(mod.parseError(e).status) : null)
const store = {}
globalThis.localStorage = { getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v) }, removeItem: k => { delete store[k] } }
const scL = { connectionId: 'local', profile: 'default', local: true }, scS = { connectionId: 'server', profile: 'default', local: false }
hard.roleKeys = [mod.roleKey(scL), mod.roleKey(scS)]
store['flota.role'] = 'ship'
hard.roles = [mod.readStored(scL), mod.readStored(scS)]
mod.writeStored('ship', scS); mod.writeStored('', scL)
hard.roles.push(mod.readStored(scS), mod.readStored(scL))
hard.help = [mod.isFlotaHelp('Usage: /flota [status]\nMachines: a'), mod.isFlotaHelp('Unknown command /flota'), mod.isFlotaHelp('try /flota help\nMachines: x'), mod.isFlotaHelp('')]
const net2 = { links: [...netReach.links, { id: 'office', kind: 'remote', label: 'office', keys: [] }], routes: [...routesAll, { profile: 'default', targetProfile: 'default', connectionId: 'office', mode: 'remote' }], activeId: null, ready: true }
const sdk2 = { ...probeSdk, requestProfile: route => route.connectionId === 'local' ? Promise.resolve({ output: 'Unknown command: /flota' })
  : Promise.resolve({ output: `Usage: /flota [status]\nMachines: ${route.connectionId === 'server' ? 'studio' : 'none configured'}` }) }
hard.two = await mod.findFlagship(sdk2, net2, reach.scopeLocal)
hard.cardTwo = render(mod.ReachCard({ error: offErr, scope: reach.scopeLocal, probe: hard.two, onReach() {}, onRetry() {} }))
// The page: choosing one remembers it only when asked to; auto-open can be cleared.
const clickOpen = async (remember) => {
  globalThis.__queries = { status: { error: pluginOff }, probe: { data: hard.two }, net: { data: net2 } }
  const pg = mod.FlotaPage({ api, sdk: sdk2 })
  walk(pg).find(n => n.type === 'Button' && n.props.children === 'Open the fleet on office').props.onClick()
  await new Promise(r => setTimeout(r, 20))
}
await clickOpen(false); hard.rememberOff = mod.readReach()
store['flota.flagshipRoute'] = JSON.stringify({ connectionId: 'office', profile: 'default' })
await clickOpen(true); hard.rememberOn = mod.readReach()
hard.lastPreferred = (await mod.findFlagship(sdk2, net2, reach.scopeLocal)).found.label
mod.clearReach(); hard.cleared = mod.readReach()
// A refetch with no data and no error (react-query resets it) keeps the offline card only through the ref;
// with the stub's fresh refs the page itself must at least stay offline while the error is present.
globalThis.__queries = { status: { error: pluginOff }, probe: { data: reach.found }, net: { data: netReach } }
hard.offlineStill = mod.FlotaPage({ api, sdk: probeSdk }).props['data-mode']
globalThis.__queries = null
delete globalThis.localStorage
disposers.forEach(fn => fn())
console.log(JSON.stringify({
  hard, reach, life, machinesView,
  chats,
  id: plugin.id,
  contributions: contributions.map(c => ({ id: c.id, area: c.area, data: c.data && { ...c.data, run: typeof c.data.run }, render: typeof c.render })),
  maps, layouts, page, restCalls, restBodies, fallback, av, geometry, hubSeats, anchors, roles, flows, rpcSchedule, rpcCalls: globalThis.__rpc, crews, ships, sched, styleRemoved: calls.some(c => c[0] === 'style-removed'),
  parsed: mod.parseError(new Error('Error invoking remote method \'hermes:api\': Error: 422: {"detail":{"message":"Nothing was saved.","problems":["machine \'x\': bad mac"]}}')),
  busy: mod.parseError(new Error('409: {"detail":"torre is busy with 2 subagent(s)"}')),
  body: mod.machineBody({ name: ' Torre ', host: '10.0.0.2', user: '', key: '', description: '', mac: '', broadcast: '', wake: 'wol', sleep: 'none', sleep_after: '10m' }),
  kinds: [mod.machineKind({ description: 'ThinkPad laptop' }), mod.machineKind({ name: 'nas' }), mod.machineKind({ description: 'gaming tower' }), mod.machineKind({ os: 'macos' })],
  powerOff: [
    render(mod.Details({ m: { ...base, name: 'torre', host: 'h', profile: 'p', up: true, can_power_off: true, power_off_reason: null }, status: status([]), api, onRemoved() {} })),
    render(mod.Details({ m: { ...base, name: 'mini', host: 'h', profile: 'p', up: true, wake: 'none', can_power_off: false, power_off_reason: 'mini has no wake method, so Flota would not be able to turn it back on; it can only be suspended.' }, status: status([], { allow_power_off: true }), api, onRemoved() {} })),
  ],
  durations: [mod.formatDuration(600), mod.formatDuration(5400), mod.formatDuration(0), mod.formatDuration(45)],
}))
"""


def _sdk_stub(names: list[str]) -> str:
    lines = ["export const STATUSBAR_AREAS = { left: 'statusbar.left', right: 'statusbar.right' }",
             "export const host = { notify() {}, navigate() {}, request(method, params) { "
             "(globalThis.__rpc = globalThis.__rpc || []).push([method, params]); "
             "return Promise.resolve({ jobs: [{ job_id: 'r1', name: 'From RPC' }] }) } }",
             "export const queryClient = { invalidateQueries() {} }",
             "export function useQuery(q) { const k = q && q.queryKey && q.queryKey[1]; "
             "return (globalThis.__queries && globalThis.__queries[k]) || globalThis.__query || {} }"]
    special = {"STATUSBAR_AREAS", "host", "queryClient", "useQuery"}
    for name in sorted(set(names) - special):
        if name.endswith("_AREA"):
            lines.append(f"export const {name} = '{name}'")
        else:  # a UI-kit component: render as an element named after it
            lines.append(f"export function {name}(props) {{ return {{ type: '{name}', props: props || {{}} }} }}")
    return "\n".join(lines) + "\n"


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    tmp = tmp_path_factory.mktemp("desktop")
    source = PLUGIN.read_text()
    imports = _imports(source)
    (tmp / "sdk.mjs").write_text(_sdk_stub(imports.get("@hermes/plugin-sdk", [])))
    (tmp / "react.mjs").write_text(STUB_REACT)
    (tmp / "jsx-runtime.mjs").write_text(STUB_JSX)
    for spec, stub in (("@hermes/plugin-sdk", "./sdk.mjs"), ("react/jsx-runtime", "./jsx-runtime.mjs"), ("react", "./react.mjs")):
        source = source.replace(f"from '{spec}'", f"from '{stub}'")
    (tmp / "plugin.mjs").write_text(source)
    (tmp / "run.mjs").write_text(RUNNER)
    proc = subprocess.run([node, "run.mjs"], cwd=tmp, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_register_adds_the_page_nav_row_palette_command_and_chip(run):
    assert run["id"] == "flota"
    by_id = {c["id"]: c for c in run["contributions"]}
    assert by_id["page"]["area"] == "ROUTES_AREA" and by_id["page"]["data"]["path"] == "/flota"
    assert by_id["page"]["render"] == "function"
    assert by_id["nav"]["area"] == "SIDEBAR_NAV_AREA"
    assert by_id["nav"]["data"] == {"path": "/flota", "label": "Flota", "codicon": "server-environment", "run": "undefined"}
    assert by_id["open"]["area"] == "PALETTE_AREA" and by_id["open"]["data"]["label"] == "Flota: open fleet map"
    assert by_id["open"]["data"]["run"] == "function"
    assert by_id["chip"]["area"] == "statusbar.right"
    assert run["styleRemoved"], "the injected <style> is removed on dispose"


@pytest.mark.parametrize("n", [0, 1, 3, 8, 14])
def test_map_draws_one_node_per_machine_around_the_hub(run, n):
    html = run["maps"][str(n)]
    assert html.count('data-machine="') == n
    assert "Hermes · estudio" in html and ">FLAGSHIP<" in html and 'data-flagship="estudio"' in html
    assert html.count('class="flota-sweep"') == 1  # the radar backdrop
    states = re.findall(r'data-state="(\w+)"', html)
    assert states.count("up") == len(range(0, n, 3))
    assert states.count("waking") == len(range(2, n, 3))
    assert states.count("down") == n - states.count("up") - states.count("waking")
    assert html.count('class="flota-link up"') == states.count("up")
    assert html.count('class="flota-link down"') == states.count("down") + (3 if n == 0 else 0)  # ghost slots
    assert html.count('class="flota-pulse"') == states.count("waking")
    assert html.count('class="flota-busy-ring"') == states.count("waking")  # the fixture's busy ones
    if n:
        assert html.count("flota-node up active selected") == 1  # a ship under way with a crew can fan it out
    else:
        assert html.count('aria-label="Add machine"') == 3


@pytest.mark.parametrize("n", [1, 2, 3, 5, 8, 12, 13, 14, 20])
def test_layout_keeps_nodes_apart_and_inside_the_view(run, n):
    lay = run["layouts"][str(n)]
    nodes, r, view = lay["nodes"], lay["r"], lay["view"]
    assert len(nodes) == n
    assert len(lay["rings"]) == (2 if n > 12 else 1)
    for i, a in enumerate(nodes):
        assert view["x"] + r < a["x"] < view["x"] + view["w"] - r
        assert view["y"] + r < a["y"] < view["y"] + view["h"] - r
        assert ((a["x"] - lay["cx"]) ** 2 + (a["y"] - lay["cy"]) ** 2) ** 0.5 > lay["hubR"] + r + 40
        for b in nodes[i + 1:]:
            assert ((a["x"] - b["x"]) ** 2 + (a["y"] - b["y"]) ** 2) ** 0.5 > 2 * r + 30
    # The hub stays in the middle of the view.
    assert abs(view["x"] + view["w"] / 2 - lay["cx"]) <= 1 and abs(view["y"] + view["h"] / 2 - lay["cy"]) <= 1


def test_page_states(run):
    empty, offline, three = (html.unescape(run["page"][k]) for k in ("empty", "offline", "three"))
    flagship = html.unescape(run["page"]["flagshipEmpty"])
    # No machines, no fleets, no role: the role chooser comes first.
    assert 'data-mode="choose"' in empty and 'data-choice="flagship"' in empty and 'data-choice="ship"' in empty
    assert "Add your first ship" in flagship and "hermes flota setup <name> --host <ip> --user <user>" in flagship
    assert ">Add ships<" in flagship and ">Invite a ship<" in flagship and ">Add by address<" in flagship
    assert "machine 'x': bad" in empty  # config problems are listed
    assert "Enable the Flota plugin on this Hermes (Plugins) and restart its gateway." in offline
    assert 'data-machine="' not in offline and 'aria-label="Add machine"' not in offline
    assert three.count('data-machine="') == 3 and "Your fleet" in three
    assert "Your fleet: machines woken on demand and put back to sleep" in three
    assert "Schedule" in three and "on the flagship · estudio" in three


def test_page_shows_the_schedule_beside_the_map(run):
    page = html.unescape(run["page"]["withSchedule"])
    assert 'class="flota-col"' in page and 'class="flota-card flota-sched"' in page
    assert page.count('data-job="') == 5


def test_rest_paths(run):
    assert run["restCalls"] == [["/status", "GET"], ["/wake/torre", "POST"], ["/sleep/torre?force=1", "POST"],
                                ["/off/torre", "POST"], ["/machines", "POST"], ["/machines/a%20b", "DELETE"],
                                ["/schedule", "GET"], ["/invite", "POST"], ["/ships", "POST"], ["/join", "POST"],
                                ["/join/check", "GET"], ["/fleets", "GET"], ["/fleets/estudio%20main", "DELETE"], ["/role", "POST"]]
    # Every POST carries a JSON body, so the backend's "POST must be application/json" rule holds.
    assert run["restBodies"] == {"/wake/torre": {}, "/sleep/torre?force=1": {"force": True}, "/off/torre": {}, "/invite": {},
                                   "/machines": {"name": "x"}, "/ships": {"ship_code": "SHIP-1"}, "/join": {"code": "INV-1"},
                                   "/role": {"role": "ship"}}
    h = run["hard"]
    assert h["named"] == ["/ships", {"ship_code": "SHIP-1", "name": "nas"}]  # the ship's name travels with its code
    assert h["confirmed"] == ["/join", {"code": "INV-1", "confirm": "servidor"}]
    assert h["inviteOld"] == [["/invite", "POST"], ["/invite", "GET"]]  # a backend from before POST /invite


def test_add_by_address_lets_setup_detect_wake(run):
    h = run["hard"]
    assert "wake" not in h["bodyAuto"] and h["bodyAuto"]["port"] == 2222  # WoL without a MAC: setup autodetects
    assert h["bodyMac"]["wake"] == "wol" and h["bodyNever"]["wake"] == "none" and "port" not in h["bodyNever"]
    ok, missing, never, old = h["wake"]
    assert ok == {"ok": True, "text": "Wake: Wake-on-LAN is ready (MAC aa:bb:cc:dd:ee:ff)."}
    assert missing["ok"] is False and "could not find this ship’s MAC address" in missing["text"]
    assert never["ok"] is True and old is None
    done = html.unescape(h["addDone"])
    assert 'data-wake-result="none"' in done and "cannot wake it yet" in done
    assert 'data-field="ship-name"' in h["invitePanel"] and "Name (optional)" in h["invitePanel"]


def test_offline_error_does_not_blink_and_role_is_per_connection(run):
    h = run["hard"]
    assert h["sticky"] == ["404", "404", None, None]  # error kept through the errorless refetch, cleared by data
    assert h["offlineStill"] == "offline"
    assert h["roleKeys"] == ["flota.role::local::default", "flota.role::server::default"]
    assert h["roles"] == ["ship", None, "ship", None]  # legacy key only for this PC's default profile


def test_only_real_flota_help_counts_and_two_flagships_are_listed(run):
    h = run["hard"]
    assert h["help"] == [True, False, False, False]
    two = h["two"]
    assert [x["label"] for x in two["all"]] == ["server", "office"] and two["found"]["label"] == "server"
    card = html.unescape(h["cardTwo"])
    assert card.count('data-flagship-route="') == 2 and ">Open the fleet on server<" in card and ">Open the fleet on office<" in card
    assert "2 of this app’s connections run Flota" in card and "Open the one I choose automatically next time" in card
    assert "checked" not in html.unescape(run["reach"]["cardFound"])  # auto-open is off until asked for
    assert h["rememberOff"] is None and h["rememberOn"] == {"connectionId": "office", "profile": "default"}
    assert h["lastPreferred"] == "office" and h["cleared"] is None


def test_new_endpoints_degrade_on_an_older_backend(run):
    f = run["fallback"]
    assert f["fleets"] == {"fleets": [], "unsupported": True}
    assert f["joinCheck"] == {"unsupported": True} and f["role"] == {"ok": False, "unsupported": True}
    assert f["invite"] == 404  # the invite panel turns this into a calm note
    note = "This Flota backend is older and cannot do this yet."
    assert note in html.unescape(run["flows"]["invite404"]) and "add the machine by address" in html.unescape(run["flows"]["invite404"])
    assert note in html.unescape(run["flows"]["fleetsOld"])
    old = html.unescape(run["flows"]["wiz1old"])  # no /join/check: the same checklist, unchecked, with this OS' fixes
    assert old.count('class="unknown"') == 4 and "Remote Login" in old and "cannot run these checks itself yet" in old


def test_schedule_falls_back_to_the_gateway_cron_door(run):
    assert run["rpcCalls"] == [["cron.manage", {"action": "list", "include_disabled": True}]]
    assert run["rpcSchedule"]["jobs"] == [{"job_id": "r1", "name": "From RPC"}] and run["rpcSchedule"]["via"] == "cron.manage"


# --- crew ----------------------------------------------------------------------------------------

def test_crew_sleeps_while_docked_and_works_while_subagents_run(run):
    c = run["crews"]
    assert c["asleep"]["mood"] == "asleep" and c["asleep"]["members"] == ["agent:asleep"]  # at least its own profile
    assert c["waking"]["members"] == ["agent:waking"]
    assert c["idle"]["members"] == ["agent:awake", "agent:awake"] and c["idle"]["working"] == 0
    assert c["busy"]["members"] == ["agent:awake", "subagent:working", "subagent:working"] and c["busy"]["working"] == 2
    # Four in the compact stack, the ship's own agent first, the rest as +N.
    assert c["crowd"]["members"] == ["agent:awake"] + ["subagent:working"] * 3
    assert c["crowd"]["total"] == 8 and c["crowd"]["extra"] == 4


def test_ships_draw_their_crew_in_each_state(run):
    s = run["ships"]
    assert s["asleep"].count('data-mood="asleep"') == 1 and s["asleep"].count('class="flota-z"') == 3
    assert "Docked · asleep" in s["asleep"] and "crew of 1 asleep" in s["asleep"]
    assert s["waking"].count('data-mood="waking"') == 1 and 'class="flota-z"' not in s["waking"]
    assert "Raising anchor…" in s["waking"]
    assert s["idle"].count('data-mood="awake"') == 1 and 'class="flota-z"' not in s["idle"]
    assert 'flota-bot-lid' not in s["idle"] and 'flota-bot-eye' in s["idle"]
    assert s["busy"].count('data-mood="working"') == 2 and s["busy"].count('class="flota-bot-ping"') == 2
    assert "Under way · 2 working" in s["busy"] and 'class="flota-busy-ring"' in s["busy"]
    # Every member gets a seat (up to the ring's twelve); beyond the compact four they wait hidden.
    assert s["crowd"].count('data-mood="') == 7 and s["crowd"].count("flota-seat extra") == 3 and ">+3<" in s["crowd"]


# --- schedule ------------------------------------------------------------------------------------

def test_schedule_rows_are_sorted_with_the_ship_each_job_wakes(run):
    s = run["sched"]
    assert s["order"] == ["soon", "backup", "server", "later", "paused"]
    assert s["machines"] == {"soon": None, "backup": "torre", "server": "mini", "later": "mini", "paused": "torre"}
    assert s["next"] == {"machine": "torre", "job": "Nightly backup"}
    assert s["rel"] == ["in 2 h 10 m", "in 3 h", "in 2 d 2 h", "in 5 m", "due now", "overdue"]
    assert s["human"] == ["Every day at 02:00", "Mondays at 03:30", "Weekdays at 08:00", "Every 6 h", "Every day",
                          "Every 15 min", "Every 4 h", "Day 1 of each month at 09:00", "Mondays and Wednesdays at 09:00",
                          "weird", "Unscheduled"]
    assert s["mentions"] == [["mini"], ["torre", "mini"], ["torre"]]


def test_schedule_panel(run):
    panel = html.unescape(run["sched"]["panel"])
    assert "Schedule" in panel and "on the flagship · estudio" in panel
    assert "Next wake: " in panel and ">torre<" in panel and "(job: Nightly backup)" in panel
    jobs = re.findall(r'data-job="([\w-]+)"', panel)
    assert jobs == ["soon", "backup", "server", "later", "paused"]
    assert panel.count('data-machine-target="torre"') == 2 and panel.count('data-machine-target="mini"') == 2
    assert "wakes" in panel and "runs on" in panel  # torre is docked, mini already under way
    assert panel.count("flota-job paused") == 1 and ">paused<" in panel
    assert panel.count("flota-job linked selected") == 1  # the selected ship's job stands out
    assert "Every day at 02:00" in panel and "in 2 h 10 m" in panel
    empty = html.unescape(run["sched"]["empty"])
    assert ('No scheduled jobs. Ask Hermes to schedule one (e.g. "every night at 2 run the backup on studio") '
            "and Flota will wake that machine for it.") in empty
    assert "Next wake" not in empty and 'data-job="' not in empty
    assert "not readable from here (cron jobs unavailable: no cron)" in html.unescape(run["sched"]["unavailable"])


def test_map_marks_ships_with_a_scheduled_wake(run):
    m = html.unescape(run["sched"]["map"])
    assert m.count('class="flota-clock"') == 1
    assert re.search(r'aria-label="torre: [^"]*next scheduled wake in (2 h 59 m|3 h)"', m)
    assert "next wake in" in m  # the flagship's subtitle


def test_helpers(run):
    assert run["parsed"] == {"status": 422, "message": "Nothing was saved.", "problems": ["machine 'x': bad mac"]}
    assert run["busy"]["status"] == 409 and run["busy"]["message"].startswith("torre is busy")
    # WoL without a MAC is saved as 'none' (setup detects the MAC and turns WoL on); no sleep_after with sleep none.
    assert run["body"] == {"name": "torre", "host": "10.0.0.2", "sleep": "none", "setup": True}
    assert run["kinds"] == ["laptop", "server", "desktop", "laptop"]
    assert run["durations"] == ["10 min", "1 h 30 min", "", "45 s"]


# --- crews with avatars --------------------------------------------------------------------------

def test_crew_avatars_use_the_picture_or_a_generated_one(run):
    up = run["av"]["up"]
    assert up.count('data-avatar="img"') == 1 and up.count('clipPath="url(#flota-av-clip)"') == 1
    assert 'href="data:image/png;base64,' in up and 'id="flota-av-clip"' in up
    assert up.count('data-avatar="gen"') == 2 and 'class="flota-av-letter"' in up and ">C<" in up and ">S<" in up
    assert up.count('class="flota-av-ant"') == 2  # generated avatars carry an antenna: they are bots
    # In a fanned-out ring each bot is a button named for what a click does (its own tooltip, no <title>).
    assert 'aria-label="Open chat with coder (working)"' in up and 'aria-label="Open chat with scribe (asleep)"' in up
    assert "<title>scribe · asleep</title>" in run["av"]["asleep"]  # a docked ship's compact stack keeps plain titles
    assert up.count('class="flota-av-pulse"') == 1  # the working one pulses
    assert up.count("flota-av asleep") == 1 and up.count("flota-av awake") == 1 and up.count("flota-av working") == 1
    assert run["av"]["colors"][0] == run["av"]["colors"][1] != run["av"]["colors"][2] and run["av"]["colors"][3] == "red"
    assert run["av"]["initials"] == ["F", "É", "?", "9"]
    bots = run["av"]["bots"]
    assert [b["mood"] for b in bots] == ["awake", "waking", "waking"] and bots[2]["avatar"] is None  # no javascript: URLs


def test_docked_ship_crew_is_grey_and_never_expands(run):
    asleep = run["av"]["asleep"]
    assert asleep.count("flota-av asleep") == 3 and 'data-expand="none"' in asleep
    assert "data-ring=" not in asleep and "flota-crew-orbit" not in asleep and "flota-hit" not in asleep
    assert asleep.count('class="flota-z"') == 3 and " active" not in asleep
    waking = run["av"]["waking"]
    assert "flota-av waking" in waking and 'data-expand="none"' in waking and "data-ring=" not in waking


def test_active_ship_crew_is_compact_at_rest_and_fans_out_into_a_ring(run):
    up = run["av"]["up"]
    assert 'data-expand="ring"' in up and up.count("data-ring=") == 3 and 'class="flota-crew-orbit"' in up
    assert "flota-node up active" in up and 'class="flota-hit"' in up  # hover anywhere on the ring keeps it open
    assert 'data-shown="3"' in up and "flota-seat extra" not in up


def test_flagship_crew_of_eight(run):
    hub8, off = run["av"]["hub8"], run["av"]["hub8off"]
    crew = run["av"]["crew"]
    assert crew == {"total": 8, "members": 4, "extra": 4, "avatars": True}
    assert re.search(r'class="flota-hub active"[^>]*data-flagship="estudio"', hub8)
    assert "crew of 8" in hub8 and ">+4<" in hub8 and 'data-crew="8"' in hub8
    hub_part = hub8.split('data-flagship="estudio"', 1)[1].split('data-machine="', 1)[0]
    assert hub_part.count("data-ring=") == 8 and hub_part.count("flota-seat extra") == 4
    assert hub_part.count("flota-av asleep") == 1
    # Gateway down: the flagship keeps its compact stack.
    off_part = off.split('data-flagship="estudio"', 1)[1].split('data-machine="', 1)[0]
    assert 'class="flota-hub"' in off and "data-ring=" not in off_part and 'data-expand="none"' in off_part
    # The ring leaves the bottom (flag and name) free; every seat sits on the ring.
    hs = run["hubSeats"]
    assert len(hs["ring"]) == 8 and all(abs((p["x"] ** 2 + p["y"] ** 2) ** 0.5 - hs["R"]) < 0.01 for p in hs["ring"])
    assert all(not (p["y"] > 0 and abs(p["x"]) < 40) for p in hs["ring"])
    # The compact stack goes to the first bearing no link uses.
    a0, a1, a2 = run["anchors"]
    assert abs(a0["a"] + 1.5708) > 0.4 and abs(a1["a"] + 1.5708) < 1e-4 and a2 == a1


def test_crew_ring_stays_clear_of_labels_and_inside_the_view(run):
    for g in run["geometry"]:
        R, view = g["R"], g["view"]
        assert g["vis"] == 4 and g["shown"] == 9 and len(g["ring"]) == 9
        assert R + 12 < g["r"] + 49 - 12  # under the name line's cap height
        for p in g["ring"]:
            assert abs((p["x"] ** 2 + p["y"] ** 2) ** 0.5 - R) < 0.01
            # Not under the label: the arc leaves a gap on the label's side.
            assert (p["y"] > -0.82 * R) if g["above"] else (p["y"] < 0.82 * R)
            assert view["x"] < g["x"] + p["x"] - 12 and g["x"] + p["x"] + 12 < view["x"] + view["w"]
            assert view["y"] < g["y"] + p["y"] - 12 and g["y"] + p["y"] + 12 < view["y"] + view["h"]
        xs = [p["x"] for p in g["compact"]]
        assert len(set(xs[:4])) == 4 and set(xs[4:]) <= {xs[3]}  # extras stack behind the fourth


# --- roles and onboarding ------------------------------------------------------------------------

def test_view_mode_and_role(run):
    assert run["roles"]["modes"] == ["loading", "choose", "loading", "flagship", "join", "ship", "ship", "flagship", "join"]
    assert run["roles"]["effective"] == ["ship", "flagship", "ship", None, None]


def test_role_chooser_and_page_modes(run):
    chooser = html.unescape(run["flows"]["chooser"])
    assert "Set sail with Flota" in chooser and "Create a fleet" in chooser and "Join a fleet" in chooser
    assert "This machine stays on and manages the others." in chooser and "This machine becomes a ship" in chooser
    page = run["page"]
    assert 'data-mode="choose"' in page["choose"] and 'data-onboarding="role"' in page["choose"]
    assert 'data-mode="join"' in page["join"] and "What you need on this machine" in page["join"]
    assert "Create a fleet instead" in page["join"]
    ship = html.unescape(page["ship"])
    assert 'data-mode="ship"' in ship and ">Become a flagship instead<" in ship and ">Join another fleet<" in ship
    assert "Your fleet" in ship and "Flagship · estudio" in ship and 'data-fleet="estudio"' in ship and ">Leave fleet<" in ship
    also = html.unescape(page["flagshipAlsoShip"])  # an older backend's flagship that also joined a fleet
    assert 'data-mode="flagship"' in also and ">Add ships<" in also and "Join a fleet as a ship instead" in also and "Also a ship in" in also


def test_join_wizard_steps(run):
    f = {k: html.unescape(v) if isinstance(v, str) else v for k, v in run["flows"].items()}
    w1 = f["wiz1"]
    assert 'aria-current="step"' in w1 and 'data-step="1"' in w1 and "What you need on this machine" in w1
    assert 'data-check="ssh"' in w1 and w1.count('class="ok"') == 1 and w1.count('class="fail"') == 1 and w1.count('class="warn"') == 1
    assert "as root: ufw allow 22/tcp" in w1 and "Fix the 1 item marked ✗" in w1
    assert "Paste the invite code" in f["wiz2"] and "Flota → Add ships → Invite a ship" in f["wiz2"]
    assert ("This lets estudio reach this machine over SSH with its key, so it can wake it and run agents here. "
            "You can leave the fleet anytime.") in f["wiz3"] and ">Join fleet<" in f["wiz3"]
    assert 'data-code="SHIP-XYZ-42"' in f["wiz3done"] and "Copy ship code" in run["flows"]["wiz3done"] and "Authorised estudio" in f["wiz3done"]
    assert "Waiting for estudio" in f["wiz4"] and ">Show my fleet<" in f["wiz4"]
    mac = run["flows"]["checksMac"]
    assert [c[0] for c in mac] == ["ssh", "port", "network", "wol"] and all(c[1] is None for c in mac) and "Remote Login" in mac[0][2]
    assert "OpenSSH Server" in run["flows"]["checksWin"][0] and "New-NetFirewallRule" in run["flows"]["checksWin"][1]


# --- add ships -----------------------------------------------------------------------------------

def test_add_by_address_runs_setup_and_shows_its_steps(run):
    f = {k: html.unescape(v) for k, v in run["flows"].items() if isinstance(v, str)}
    log = run["flows"]["log"]
    assert [r["ok"] for r in log] == [True, True, False, False] and log[1]["text"] == "Saved machine torre"
    done = f["addDone"]
    assert "Ship added" in done and done.count('data-ok="1"') == 2 and done.count('data-ok="0"') == 2
    assert "detected: linux · enp8s0 · mac aa:bb:cc:dd:ee:ff" in done and "torre joined the fleet" in done
    assert "hermes flota setup" not in done  # one click: no command left to run
    assert "Setup did not finish" in f["addFailed"] and "ssh: connection refused" in f["addFailed"] and ">Try again<" in f["addFailed"]
    assert "hermes flota setup torre --host h" in f["addLegacy"]  # an older backend still says what is left
    form = f["addForm"]
    assert "IP or Tailscale address of the machine; it must accept SSH with this Hermes’ key" in form
    assert "Defaults to this Hermes’ key" in form and "SSH key (optional)" in form
    assert form.count('required="true"') == 3  # name, user, host
    assert "Add ships" in f["choose"] and 'data-choice="address"' in f["choose"] and 'data-choice="invite"' in f["choose"]


def test_invite_a_ship(run):
    f = {k: html.unescape(v) for k, v in run["flows"].items() if isinstance(v, str)}
    inv = f["invite"]
    assert 'data-code="FLOTA-ABCD-1234"' in inv and "Copy invite code" in inv and "flota-countdown" in inv
    assert re.search(r"expires in (10:00|9:5\d)", inv)
    assert "On the other machine open Hermes → Flota → Join a fleet." in re.sub(r"<[^>]+>", "", inv)
    assert "Paste this code." in inv and "Paste the ship code it gives you here." in inv and ">Ship code" in inv and ">Add ship<" in inv
    assert "portatil joined the fleet" in f["inviteDone"] and 'data-ok="1"' in f["inviteDone"]
    assert run["flows"]["countdown"] == ["expires in 9:41", "expired", "expires in 2 h 5 m"]
    assert run["flows"]["toMs"][:2] == [1762770000000, 1762770000000] and run["flows"]["toMs"][3] is None
    assert run["flows"]["decoded"][0]["flagship"] == "estudio" and run["flows"]["decoded"][1:] == [None, None]


def test_ship_view_and_fleets(run):
    m = html.unescape(run["flows"]["shipMap"])
    assert m.count('data-flagship-of="') == 2 and "Flagship · estudio" in m and "Flagship · oficina" in m
    assert m.count('class="flota-link signal"') == 2 and m.count('class="flota-flow signal"') == 2
    assert "This machine · studio" in m and ">SHIP<" in m and "a ship in 2 fleets" in m
    assert 'data-expand="ring"' in m  # this machine is on: its own crew fans out too
    fl = html.unescape(run["flows"]["fleets"])
    assert fl.count('data-fleet="') == 2 and fl.count(">Leave<") == 2 and "SHA256:abc" in fl and ">Join another fleet<" in fl


# --- clicking a bot opens its chat; view-only without a connection -------------------------------

def test_flagship_bots_are_reachable_through_the_current_backend(run):
    a = run["chats"]["access"]
    assert a["hub"]["ok"] and a["hub"]["route"] == {"profile": "coder", "targetProfile": "coder", "connectionId": "local", "mode": "local"}
    assert a["hubOld"] == {"ok": True, "route": None, "profile": "coder", "via": "here", "hint": "Open chat"}  # no route API: plain profile
    assert a["local"]["ok"] and a["local"]["route"] is None  # a ship's flagship-side profile lives here too
    assert a["webHub"]["ok"] and a["webHub"]["route"]["connectionId"] == "local"


def test_ship_bots_need_a_registered_connection_to_their_machine(run):
    a = run["chats"]["access"]
    # torre: an SSH connection to torre.example.ts.net, with a route that serves 'ops' as its default profile.
    assert a["torre"]["ok"] and a["torre"]["route"] == {"profile": "default", "targetProfile": "ops", "connectionId": "c-torre", "mode": "remote"}
    assert a["torre"]["via"] == "Torre box"
    # Same machine, a profile no route lists yet: the connection is enough.
    assert a["torreNoRoute"]["route"] == {"connectionId": "c-torre", "mode": "remote", "profile": "chef", "targetProfile": "chef"}
    assert a["byIp"]["ok"] and a["byIp"]["route"]["connectionId"] == "c-web"  # matched by the URL's address
    assert a["byLabel"]["ok"] and a["byLabel"]["route"]["connectionId"] == "c-lab"  # a roster source's label
    # No connection to mini (or none at all on an older Desktop / a Webapp without saved connections): view only.
    for key in ("mini", "oldDesktop", "web"):
        assert a[key]["ok"] is False and a[key]["route"] is None
    assert a["mini"]["hint"] == "Connect mini in Hermes Desktop (Settings → Gateways) to chat with tutor"
    assert a["notLocal"]["ok"] is False  # this app's own local source is never taken for a ship
    assert a["pending"]["ok"] is False and a["pending"]["pending"] is True
    assert run["chats"]["hostKeys"] == [["torre.local", "torre"], ["10.0.0.3"], ["fd7a::1"]]
    net = run["chats"]["net"]
    assert [link["id"] for link in net["links"]] == ["local", "c-torre", "c-web", "c-lab"] and net["routes"] == 2
    assert run["chats"]["netOld"] == {"links": [], "routes": [], "activeId": None, "ready": True}
    assert all("token" not in json.dumps(link).lower() for link in net["links"])  # only ids, kinds, labels, host keys


def test_opening_a_chat_uses_the_sdk_doors(run):
    o = run["chats"]["opens"]
    # Here: profiles.list on the active gateway -> its Bot Chat (the live tip) -> host.openSession.
    assert o["hubOld"]["calls"] == [["request", "profiles.list", {}], ["openSession", "c1", {"profile": "coder", "intent": "in-place"}]]
    route = {"profile": "default", "targetProfile": "ops", "connectionId": "c-torre", "mode": "remote"}
    # Another machine: the same through host.requestProfile on its route, dialled as a user action.
    assert o["torre"]["calls"] == [["requestProfile", route, "profiles.list", {"spawnPriority": "foreground"}],
                                   ["openSession", "r7", {"route": route, "profile": "default", "intent": "in-place"}]]
    # No session yet, or a backend that does not answer in time: a fresh chat with that bot.
    assert o["chef"]["r"] == {"ok": True, "how": "new"} and o["chef"]["calls"][-1][0] == "newChat"
    assert o["chef"]["calls"][-1][1]["connectionId"] == "c-torre" and o["chef"]["calls"][-1][1]["targetProfile"] == "chef"
    assert o["slow"]["r"]["how"] == "new" and o["slow"]["calls"] == [["newChat", route]]
    assert o["locked"] == {"r": {"ok": False, "how": "locked"}, "calls": []}
    # Asleep machine: still opens, with a note that Flota wakes it for a task.
    assert o["asleep"]["calls"][0] == ["notify", "info", "torre is asleep — Flota will wake it when it gets a task"]
    assert o["asleep"]["r"]["how"] == "session"
    assert o["unsupported"]["r"] == {"ok": False, "how": "unsupported"}
    assert o["unsupported"]["calls"] == [["notify", "error", "This Hermes cannot open a chat from a plugin yet. Update Hermes Desktop."]]


def test_ring_bots_are_buttons_and_locked_ones_show_a_padlock(run):
    c = run["chats"]
    m = html.unescape(c["map"])
    assert m.count('data-access="chat"') == 6  # 3 flagship bots + torre's 3
    assert m.count('data-access="locked"') == 2  # mini's two
    assert m.count('class="flota-lock"') == 2 and m.count('class="flota-chatb"') == 6
    assert m.count("flota-seat chat") == 6 and m.count("flota-seat locked") == 2
    assert 'aria-label="tutor (awake), view only. Connect mini in Hermes Desktop (Settings → Gateways) to chat with tutor"' in m
    assert m.count('aria-disabled="true"') == 2
    # Click / Enter: the right bot, its machine and route; a locked one never opens, and its click reaches the ship.
    assert c["botClicks"] == [["ops", True, "torre", "c-torre"], ["flota-torre", True, "torre", None],
                              ["chef", True, "torre", "c-torre"], ["hermes", True, None, None]]
    assert c["stopped"] == 4
    assert "flota-expanded" in c["mapExpanded"] and c["mapExpanded"].count("flota-expanded") == 1
    css = re.search(r"export const CSS = `([\s\S]*?)`", PLUGIN.read_text())[1]
    assert ".flota-seat.chat{cursor:pointer}" in css and ".flota-seat.locked{cursor:default}" in css
    assert re.search(r"\.flota-node\.active:hover \.flota-seat\.locked \.flota-lock[^{]*\{opacity:1\}", css)
    assert ".flota-node.active:focus-within .flota-seat," in css  # tabbing into the ring keeps it open


def test_details_list_every_bot_with_open_chat_or_view_only(run):
    c = run["chats"]
    d = html.unescape(c["details"])
    assert 'aria-label="Crew"' in d and "3 bots" in d
    assert d.count('class="flota-crew-row chat"') == 3 and d.count(">Open chat<") == 3 and "awake · via Torre box" in d
    mini = html.unescape(c["detailsMini"])
    assert mini.count('class="flota-crew-row locked"') == 2 and mini.count("View only — not connected") == 2
    assert "Connect mini in Hermes Desktop (Settings → Gateways) to chat with tutor" in mini and ">Open Settings → Gateways<" in mini
    docked = html.unescape(c["detailsDocked"])
    assert docked.count("torre is asleep — Flota will wake it when it gets a task") == 3 and docked.count(">Open chat<") == 3
    assert c["panelClicks"] == [["flota-torre", "torre", None], ["ops", "torre", "c-torre"], ["chef", "torre", "c-torre"]]  # mini's rows are not buttons
    page = html.unescape(run["page"]["chats"])
    assert "Flagship crew · estudio" in page and page.count('data-access="locked"') == 2


def test_bot_tooltips_go_outside_the_ring_and_stay_in_the_card(run):
    right, edge, below = run["chats"]["tips"]
    assert right == {"x": 168, "y": 100, "side": "right"}
    assert edge["below"] is True and edge["x"] <= 1000 - 125 - 6
    assert below == {"x": 500, "y": 428, "below": True}


def test_a_crew_of_ten_fans_out_without_overlap(run):
    c = run["chats"]
    assert c["tenCrew"] == {"total": 10, "members": 4, "extra": 6}
    ten = html.unescape(c["ten"])
    assert ten.count("data-ring=") == 10 and ">+6<" in ten and ten.count("flota-seat extra") == 6
    assert ten.count('data-access="locked"') == 10  # nobody on mini is reachable from here
    for seats in c["ring10"]:
        ring, R = seats["ring"], seats["R"]
        assert all(abs((p["x"] ** 2 + p["y"] ** 2) ** 0.5 - R) < 0.01 for p in ring)
        gaps = [((a["x"] - b["x"]) ** 2 + (a["y"] - b["y"]) ** 2) ** 0.5 for i, a in enumerate(ring) for b in ring[i + 1:]]
        assert not gaps or min(gaps) >= 26  # an expanded avatar is ~21 wide, plus its badge
    assert c["ring10"][3]["R"] > c["ring10"][0]["R"]  # twelve bots: a wider ring
    assert c["ring10"][4]["R"] == c["ring10"][0]["R"] == 57  # small crews keep the snug ring
    for g in c["ring10View"]:
        for p in g["ring"]:
            assert g["view"]["x"] < g["x"] + p["x"] - 12 < g["x"] + p["x"] + 12 < g["view"]["x"] + g["view"]["w"]
            assert g["view"]["y"] < g["y"] + p["y"] - 12 < g["y"] + p["y"] + 12 < g["view"]["y"] + g["view"]["h"]


def _power_off_button(markup: str) -> str:
    return next(b for b in re.findall(r"<Button[^>]*>[^<]*</Button>", markup) if b.endswith(">Power off</Button>"))


def test_power_off_button_follows_each_machines_can_power_off(run):
    wakeable, bare = (html.unescape(m) for m in run["powerOff"])
    button = _power_off_button(wakeable)
    assert 'disabled="true"' not in button and "title=" not in button
    button = _power_off_button(bare)
    assert 'disabled="true"' in button
    assert 'title="mini has no wake method, so Flota would not be able to turn it back on; it can only be suspended."' in button
    assert "it can only be suspended" in bare.split(button, 1)[1]  # the reason is also spelled out under the buttons


# --- reaching the flagship from any connection ---------------------------------------------------

def test_errors_are_classified_and_only_a_route_404_is_an_older_backend(run):
    r = run["reach"]
    assert r["kinds"] == ["plugin-off", "route-missing", "unreachable", "unreachable", "auth", "server", "conflict"]
    assert r["older"] == [False, True, True, False]
    assert r["conflict"]["message"] == "already in a fleet" and r["conflict"]["fleet"] == {"flagship": "servidor"}
    # 'Plugin not found' (Flota off on the profile the app scoped to) is thrown, never taken for an older backend.
    assert r["offApi"] == {"fleets": "plugin-off", "joinCheck": "plugin-off", "setRole": "plugin-off"}
    inv = html.unescape(r["invitePluginOff"])
    assert "backend is older" not in inv and "Flota is not enabled on the Hermes profile this request reached" in inv
    assert '<details class="flota-errdetails">' in inv and "Plugin not found" in inv  # the real text, one click away
    assert "This Flota backend is older" in html.unescape(run["flows"]["invite404"])  # a genuine route 404 still says so


def test_the_flagship_is_found_on_another_connection(run):
    r = run["reach"]
    assert r["scopeLocal"] == {"connectionId": "local", "profile": "default", "label": "this PC", "local": True}
    assert r["cands"] == ["server::default"]  # only default profiles, never the scope that failed, no duplicates
    assert r["probeCalls"] == [["server", "default", "command.dispatch", {"name": "flota", "arg": "help"}]]
    f = r["found"]
    assert f["supported"] is True and f["tried"] == 1
    assert f["found"]["label"] == "server" and f["found"]["machines"] == ["studio"] and f["found"]["sameConnection"] is False
    assert f["found"]["route"]["connectionId"] == "server"
    # Scoped to a bot profile on the flagship's connection: its default profile answers.
    assert r["scopeBot"]["profile"] == "flota-studio" and r["scopeBot"]["label"] == "server"
    assert r["foundBot"]["found"]["sameConnection"] is True and r["foundBot"]["found"]["route"]["profile"] == "default"
    assert r["none"] == {"supported": True, "tried": 1, "found": None, "all": []}
    assert r["unsupported"] == {"supported": False, "tried": 0, "found": None}
    assert r["usage"] == [["a", "b"], [], []]
    # Opening it makes that connection the active one (host.ensureAgent); local goes through the profile path.
    assert r["ensure"] == [["ensureAgent", "server", "default"], ["ensureAgent", None, "default"]]
    assert "cannot switch connections" in r["noEnsure"]


def test_reach_card_replaces_the_error(run):
    r = {k: html.unescape(v) for k, v in run["reach"].items() if isinstance(v, str)}
    found = r["cardFound"]
    assert 'data-reach="found"' in found and "This PC’s Hermes is a ship" in found
    assert "Your fleet is managed by the flagship on server. It leads 1 ship: studio." in found
    assert ">Open the fleet on server<" in found and "Open the flagship automatically next time" in found
    assert 'data-flagship-route="server::default"' in found
    assert "This PC as a ship" in found and ">Open Plugins<" in found and "choose Join a fleet" in found
    assert "(Plugin not found)" not in found and "What this Hermes answered" in found  # raw text only in the toggle
    bot = r["cardBot"]
    assert 'data-reach="profile"' in bot and "Flota runs on another profile of server" in bot
    assert "profile you last chatted with (flota-studio)" in bot and ">Use server · default<" in bot
    none = r["cardNone"]
    assert 'data-reach="none"' in none and "Flota is not enabled on this Hermes" in none
    assert "Enable the Flota plugin on this Hermes (Plugins) and restart its gateway." in none
    assert "None of this app’s other connections (1 checked) runs Flota" in none and ">Open Settings → Gateways<" in none
    assert "cannot look at its other connections from a plugin" in r["cardOld"]
    assert 'data-reach="looking"' in r["cardLooking"] and "Looking for your flagship" in r["cardLooking"]
    down = r["cardDown"]
    assert "Flota backend not reachable" in down and "did not answer" in down and "This PC as a ship" not in down


def test_page_offers_the_flagship_and_switches_to_it(run):
    page = html.unescape(run["page"]["reach"])
    assert 'data-mode="offline"' in page and "This PC’s Hermes is a ship" in page and ">Open the fleet on server<" in page
    assert 'data-machine="' not in page
    assert ["ensureAgent", "server", "default"] in run["reach"]["clicked"]
    assert ["notify", "info", "Flota: opened the flagship on server"] in run["reach"]["clicked"]
    via = html.unescape(run["page"]["viaServer"])
    assert "Flagship: servidor (via server connection)" in via and 'class="flota-scope-chip"' in via
    assert "via server connection" not in html.unescape(run["page"]["local"])  # this PC's own backend needs no chip


def test_stale_page_explains_a_wrong_profile_instead_of_update_failed(run):
    stale = html.unescape(run["page"]["staleErr"])
    assert 'class="flota-updateerr"' in stale and 'data-error="plugin-off"' in stale
    assert "Flota not on profile flota-studio · retrying" in stale and ">Use server · default<" in stale
    assert "update failed" not in stale and "Plugin not found" in stale  # the real error in the toggle
    upd = html.unescape(run["reach"]["updateErr"])
    assert "Desktop sends plugin requests to the profile you last chatted with" in upd


# --- role lifecycle ------------------------------------------------------------------------------

def test_view_mode_follows_the_backends_role(run):
    life = run["life"]
    assert life["modes"] == ["choose", "join", "ship", "flagship", "ship", "flagship"]
    assert life["effective"] == [None, "ship", "flagship"]  # a stale local 'ship' never overrides the backend


def test_role_actions_call_the_api_in_order(run):
    life = run["life"]
    assert life["leave"] == {"role": None, "calls": [["/fleets/servidor", "DELETE", None]]}
    assert life["toFlagship"]["role"] == "flagship"
    assert life["toFlagship"]["calls"] == [["/fleets/servidor", "DELETE", None], ["/role", "POST", {"role": "flagship"}]]
    assert life["toFlagshipFresh"]["calls"] == [["/role", "POST", {"role": "flagship"}]]
    assert life["toShipWithShips"]["needs"] == "remove_ships" and life["toShipWithShips"]["calls"] == []
    assert "Remove them first" in life["toShipWithShips"]["error"]
    assert life["toShipClear"]["calls"] == [["/role", "POST", {"role": "ship", "force": True}]]  # one atomic step
    assert life["toShipClearOld"]["calls"] == [["/role", "POST", {"role": "ship", "force": True}], ["/machines/a", "DELETE", None], ["/machines/b%20c", "DELETE", None]]
    assert life["toShip"] == {"role": "ship", "calls": [["/role", "POST", {"role": "ship"}]]}
    assert life["refusedShips"]["needs"] == "remove_ships" and "still has ships" in life["refusedShips"]["error"]
    assert life["refusedFleet"]["needs"] == "leave_fleet" and "Leave that fleet first" in life["refusedFleet"]["error"]
    assert life["refusedReal"]["needs"] == "leave_fleet" and "Leave that fleet first" in life["refusedReal"]["error"]
    j = life["realJoin409"]
    assert j["kind"] == "conflict" and j["fleet"] == {"flagship": "servidor"} and "already" in j["message"]


def test_role_confirmations_say_what_happens(run):
    t = run["life"]["texts"]
    assert t["leave"]["title"] == "Leave servidor’s fleet?" and t["leave"]["destructive"] is True
    assert "join another fleet, or create your own" in t["leave"]["description"]
    assert "leaves servidor’s fleet first" in t["flagship"]["description"] and t["flagship"]["confirmLabel"] == "Leave and become a flagship"
    assert run["life"]["textFresh"]["confirmLabel"] == "Become a flagship"
    assert t["ship-clear"]["title"] == "Remove 2 ships and become a ship?" and "studio, nas will be removed" in t["ship-clear"]["description"]
    assert "Leave fleet on its side to revoke" in t["ship-clear"]["description"]
    assert t["ship"]["title"] == "Join a fleet as a ship instead?"


def test_ship_side_leave_rejoin_and_become_flagship(run):
    member = html.unescape(run["life"]["member"])
    assert "Flagship · servidor" in member and ">Leave fleet<" in member and ">Become a flagship instead<" in member
    join = re.search(r'<Button[^>]*>Join another fleet</Button>', member)[0]
    assert 'disabled="true"' in join and "Leave servidor’s fleet first" in join
    assert "a machine belongs to one fleet at a time" in member
    ship = html.unescape(run["page"]["shipNew"])
    assert 'data-mode="ship"' in ship and "This machine is a ship in servidor’s fleet" in ship and "ship of servidor" in ship
    assert 'data-menu="fleet"' in ship and 'data-action="leave"' in ship and 'data-action="become-flagship"' in ship
    assert re.search(r'data-action="join-another"[^>]*>', ship) and "leave servidor’s fleet first" in ship
    assert re.search(r'disabled="true"[^>]*data-action="join-another"', ship)
    assert 'data-flagship-of="servidor"' in ship and "This machine · studio" in ship
    assert "flota-studio" not in ship  # Flota's own SSH access is not a bot
    # After leaving: role null -> the chooser; picking Join -> the wizard again.
    assert 'data-mode="choose"' in run["page"]["afterLeave"] and 'data-onboarding="role"' in run["page"]["afterLeave"]
    assert 'data-mode="join"' in run["page"]["rejoin"] and "What you need on this machine" in run["page"]["rejoin"]


def test_flagship_side_menu_and_becoming_a_ship(run):
    flag = html.unescape(run["page"]["flagshipNew"])
    assert 'data-mode="flagship"' in flag and ">Add ships<" in flag and 'data-menu="fleet"' in flag
    assert 'data-action="address"' in flag and 'data-action="invite"' in flag and 'data-action="become-ship"' in flag
    assert "Join a fleet as a ship instead" in flag and "remove its 2 ships first" in flag
    empty = html.unescape(run["page"]["flagshipNoShips"])
    assert "this machine stops leading a fleet" in empty and "Add your first ship" in empty


def test_join_wizard_explains_already_in_a_fleet_and_wake_options(run):
    already = html.unescape(run["life"]["already"])
    assert 'data-error="already-in-fleet"' in already and "already a ship in servidor’s fleet" in already
    assert "leave that fleet first" in already
    off = html.unescape(run["life"]["joinPluginOff"])
    assert "backend is older" not in off and 'data-error="plugin-off"' in off
    wake = html.unescape(run["life"]["wake"])
    assert 'data-check="sshd"' in wake and "Start-Service sshd" in wake  # the OS-specific checks come from the backend
    assert "How the flagship can wake this machine" in wake and wake.count('data-wake="') == 3
    assert "Wake-on-LAN (Intel I225-V)" in wake and "A wake timer on this machine" in wake
    assert [o["ok"] for o in run["life"]["wakeOpts"]] == [True, False] and run["life"]["wakeOpts"][1]["label"] == "A wake timer on this machine"


# --- machines are not bots -----------------------------------------------------------------------

def test_ships_read_as_machines_with_their_hostname(run):
    v = run["machinesView"]
    m = html.unescape(v["map"])
    assert '<tspan class="flota-hostname"> · desk-pc</tspan>' in m
    assert m.count('class="flota-mflag"') == 2 and ">MACHINE<" in m and "<title>Machine registered in this fleet</title>" in m
    assert v["labels"] == ["desk-pc", "", ""]
    # ship_hand crew entries are Flota's SSH access: never shown as bots.
    assert v["crew"] == {"total": 0, "avatars": True} and v["crewBots"] == ["ops"]
    assert 'data-bot="flota-studio"' not in m and "no bots on this machine" in m
    assert 'aria-label="studio: machine desk-pc, Under way, no bots on this machine' in m
    d = html.unescape(v["details"])
    assert '>Machine</span>' in d and "hostname desk-pc" in d and "no bots on this machine" in d
    assert "Flota’s door, not a bot" in d and ">Remove from fleet<" in d
    assert "open Flota there and choose Leave fleet" in v["remove"]


def test_a_refused_re_point_offers_a_confirmed_replace(run):
    h = run["hard"]
    assert h["replaceBodies"] == [{"ship_code": "SHIP-1", "name": "nas", "replace": True},
                                  {"ship_code": "SHIP-1", "replace": True}]
    reason = h["replaceReason"]
    assert reason[0].endswith("Nothing was changed.") and "u@10.0.0.9" in reason[0] and "--replace" not in reason[0]
    assert reason[1:] == [None, None]
    panel = html.unescape(h["replacePanel"])
    assert 'data-action="replace-ship"' in panel and "This machine really moved - replace it" in panel
    assert "saved as u@10.0.0.5" in panel and "hermes flota add-ship" not in panel
    assert "replace-ship" not in h["usedPanel"] and "already used" in h["usedPanel"]
