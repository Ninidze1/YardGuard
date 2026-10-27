/* YardGuard phone access. Plain JavaScript, no libraries, no external requests.
   Pages: #/cameras, #/camera/<ch>, #/incidents, #/incident/<id>, #/areas, #/area/<id>, #/pair. */
'use strict';

const ICONS = {
  camera: '<path d="m16 13 5.223 3.482a.5.5 0 0 0 .777-.416V7.87a.5.5 0 0 0-.752-.432L16 10.5"/><rect x="2" y="6" width="14" height="12" rx="2"/>',
  shield: '<path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/>',
  areas: '<path d="M3 7V5a2 2 0 0 1 2-2h2"/><path d="M17 3h2a2 2 0 0 1 2 2v2"/><path d="M21 17v2a2 2 0 0 1-2 2h-2"/><path d="M7 21H5a2 2 0 0 1-2-2v-2"/><rect x="7" y="7" width="10" height="10" rx="1"/>',
  back: '<path d="m15 18-6-6 6-6"/>',
  more: '<circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/><circle cx="5" cy="12" r="1"/>',
  close: '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
  play: '<polygon points="6 3 20 12 6 21 6 3"/>',
  image: '<rect width="18" height="18" x="3" y="3" rx="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-3.086-3.086a2 2 0 0 0-2.828 0L6 21"/>',
  nocam: '<path d="M10.66 6H14a2 2 0 0 1 2 2v2.5l5.248-3.062A.5.5 0 0 1 22 7.87v8.196"/><path d="M16 16a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h2"/><path d="m2 2 20 20"/>',
  offline: '<path d="M12 20h.01"/><path d="M8.5 16.429a5 5 0 0 1 7 0"/><path d="M5 12.859a10 10 0 0 1 5.17-2.69"/><path d="M19 12.859a10 10 0 0 0-2.007-1.523"/><path d="M2 8.82a15 15 0 0 1 4.177-2.643"/><path d="M22 8.82a15 15 0 0 0-11.288-3.764"/><path d="m2 2 20 20"/>',
  car: '<path d="M19 17h2c.6 0 1-.4 1-1v-3c0-.9-.7-1.7-1.5-1.9C18.7 10.6 16 10 16 10s-1.3-1.4-2.2-2.3c-.5-.4-1.1-.7-1.8-.7H5c-.6 0-1.1.4-1.4.9l-1.4 2.9A3.7 3.7 0 0 0 2 12v4c0 .6.4 1 1 1h2"/><circle cx="7" cy="17" r="2"/><path d="M9 17h6"/><circle cx="17" cy="17" r="2"/>',
  hash: '<line x1="4" x2="20" y1="9" y2="9"/><line x1="4" x2="20" y1="15" y2="15"/><line x1="10" x2="8" y1="3" y2="21"/><line x1="16" x2="14" y1="3" y2="21"/>',
  check: '<circle cx="12" cy="12" r="10"/><path d="m9 12 2 2 4-4"/>',
  clock: '<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>',
  phone: '<rect width="14" height="20" x="5" y="2" rx="2" ry="2"/><path d="M12 18h.01"/>',
  route: '<circle cx="6" cy="19" r="3"/><path d="M9 19h8.5a3.5 3.5 0 0 0 0-7h-11a3.5 3.5 0 0 1 0-7H15"/><circle cx="18" cy="5" r="3"/>',
  user: '<path d="M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>',
  help: '<circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><path d="M12 17h.01"/>',
  parking: '<rect width="18" height="18" x="3" y="3" rx="2"/><path d="M9 17V7h4a3 3 0 0 1 0 6H9"/>',
  box: '<path d="M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16Z"/><path d="m3.3 7 8.7 5 8.7-5"/><path d="M12 22V12"/>',
  power: '<path d="M12 2v10"/><path d="M18.4 6.6a9 9 0 1 1-12.77.04"/>',
};
const TASK_ICON = { count: 'hash', log_plates: 'car', parking_spot: 'parking', no_parking: 'parking', loitering: 'clock',
  presence: 'user', watch_zone: 'user', line_cross: 'route', left_object: 'box', state_watch: 'areas', ask_ai: 'help' };
const KIND_TEXT = { enter: 'Came in', exit: 'Went out', cross_in: 'In', cross_out: 'Out', count: 'Count',
  occupied: 'Occupied', freed: 'Free again', blocked: 'Blocking', loitering: 'Lingering', presence: 'Seen',
  plate: 'Number plate', appeared: 'Left behind', removed: 'Taken away', changed: 'Changed', answer_yes: 'Yes',
  answer_no: 'No' };
const SEVERITY = { critical: 'Critical', warning: 'Warning', info: 'Info' };
const VERDICT = { confirmed: ['AI confirmed', 'danger'], pending: ['AI is checking', 'warn'],
  rejected: ['AI found nothing', ''], unverified: ['Not checked by AI', ''] };
const STATUS = { open: 'Open', handled: 'Handled', false_alarm: 'False alarm' };
const LIVE_MAX_MS = 10 * 60 * 1000;
const BLANK = 'data:image/gif;base64,R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw==';

const S = { status: null, seq: null, skew: 0, route: '', pairCode: '', poll: null, lives: new Set(),
  leave: [], incFilter: store('yg_filter') || 'open', polling: false, offline: false, timer: null };

/* -- small helpers ------------------------------------------------------------------- */
function store(key, value) {
  try {
    if (value === undefined) return localStorage.getItem(key);
    localStorage.setItem(key, String(value));
  } catch (e) { /* private mode */ }
  return null;
}
function icon(name) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('class', 'i');
  svg.setAttribute('aria-hidden', 'true');
  svg.innerHTML = ICONS[name] || '';
  return svg;
}
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'text') el.textContent = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const c of kids.flat(3)) if (c != null && c !== false) el.append(c instanceof Node ? c : String(c));
  return el;
}
const $ = (id) => document.getElementById(id);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const now = () => Date.now() / 1000 + S.skew;
function clock(ts) { return new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }); }
function when(ts) {
  const d = new Date(ts * 1000), t = new Date();
  const day = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diff = Math.round((day(t) - day(d)) / 86400000);
  if (diff === 0) return clock(ts);
  if (diff === 1) return 'Yesterday ' + clock(ts);
  if (diff < 7) return d.toLocaleDateString([], { weekday: 'short' }) + ' ' + clock(ts);
  return d.toLocaleDateString([], { day: 'numeric', month: 'short' }) + ' ' + clock(ts);
}
function ago(ts) {
  const s = now() - ts;
  if (s < 60) return 'just now';
  if (s < 3600) return Math.floor(s / 60) + ' min ago';
  return when(ts);
}
function plural(n, word) { return n + ' ' + word + (n === 1 ? '' : 's'); }
function toast(text) {
  const el = h('div', { class: 'toast', role: 'status', text });
  document.body.append(el);
  setTimeout(() => el.remove(), 3200);
}
function onLeave(fn) { S.leave.push(fn); }

/* -- server ---------------------------------------------------------------------------- */
class ApiError extends Error {
  constructor(message, code, status) { super(message); this.code = code; this.status = status; }
}
async function api(path, opts = {}) {
  let r;
  try {
    r = await fetch(path, Object.assign({ credentials: 'same-origin', cache: 'no-store' }, opts));
  } catch (e) {
    if (e.name === 'AbortError') throw e;
    setOffline(true);
    throw new ApiError("Can't reach YardGuard. Check that the PC is on and this phone is online.", 'offline', 0);
  }
  setOffline(false);
  let data = {};
  try { data = await r.json(); } catch (e) { /* empty body */ }
  if (r.status === 401) { showPair(); throw new ApiError('Pair this phone again.', 'pair', 401); }
  if (!r.ok) throw new ApiError(data.message || 'Something went wrong. Try again.', data.error || 'http', r.status);
  return data;
}
async function post(path, body) {
  const send = () => api(path, { method: 'POST', body: JSON.stringify(body || {}),
    headers: { 'Content-Type': 'application/json', 'X-YG-CSRF': (S.status && S.status.csrf) || '' } });
  try {
    return await send();
  } catch (e) {
    if (e.code !== 'csrf') throw e;
    await loadStatus();   // YardGuard restarted: fetch a fresh token once
    return send();
  }
}
function media(kind, id, what, w) { return `/media/${kind}/${encodeURIComponent(id)}/${what}` + (w ? `?w=${w}` : ''); }
function setOffline(on) {
  if (S.offline === on) return;
  S.offline = on;
  const b = $('banner');
  b.replaceChildren();
  if (on) b.append(icon('offline'), h('span', { text: "Can't reach YardGuard. Check that the PC is on and this phone is online." }));
  b.className = 'banner';
  b.hidden = !on;
}

async function loadStatus() {
  const st = await api('/api/status');
  S.status = st;
  S.skew = st.server_time - Date.now() / 1000;
  paintStatus();
  return st;
}
function paintStatus() {
  const st = S.status;
  if (!st) return;
  const sub = $('sub');
  if (!S.subOverride) {
    const on = st.monitoring.running;
    sub.replaceChildren(h('i', { class: 'dot ' + (on ? 'ok' : '') }),
      h('span', { text: (on ? 'Watching' : 'Monitoring is off') + (st.site ? ' · ' + st.site : '') }));
  }
  const seen = parseFloat(store('yg_seen') || '0');
  $('badge').hidden = !(st.newest_ts > seen) || S.route.startsWith('#/incidents');
  document.dispatchEvent(new CustomEvent('yg-status'));
}

/* -- long poll for changes --------------------------------------------------------------- */
async function changesLoop() {
  if (S.polling) return;
  S.polling = true;
  let backoff = 1000;
  while (!document.hidden && S.status) {
    S.poll = new AbortController();
    try {
      const since = S.seq == null ? Number.MAX_SAFE_INTEGER : S.seq;
      const d = await api(`/api/changes?since=${since}&wait=25`, { signal: S.poll.signal });
      const first = S.seq == null;
      S.seq = d.seq;
      backoff = 1000;
      if (d.reset && !first) refreshView();
      else if (d.items && d.items.length) handleChanges(d.items);
    } catch (e) {
      if (e.name === 'AbortError' || e.code === 'pair') break;
      await sleep(backoff);
      backoff = Math.min(backoff * 2, 30000);
    }
  }
  S.polling = false;
}
let changeTimer = null, pending = { incident: new Set(), area: false, status: false };
function handleChanges(items) {
  for (const it of items) {
    if (it.type === 'incident') pending.incident.add(it.id || '*');
    else if (it.type === 'area_event') pending.area = true;
    else pending.status = true;
  }
  clearTimeout(changeTimer);
  changeTimer = setTimeout(() => {
    const p = pending;
    pending = { incident: new Set(), area: false, status: false };
    loadStatus().catch(() => {});
    const r = S.route;
    if (p.incident.size && r === '#/incidents') softReload();
    else if (p.incident.size && r.startsWith('#/incident/')) {
      const id = r.split('/')[2];
      if (p.incident.has(id) || p.incident.has('*')) softReload();
    } else if (p.area && (r === '#/areas' || r.startsWith('#/area/'))) softReload();
  }, 600);
}

/* -- router ------------------------------------------------------------------------------ */
const ROUTES = [
  [/^#\/cameras$/, viewCameras, 'cameras'], [/^#\/camera\/(\d+)$/, viewCamera, 'cameras'],
  [/^#\/incidents$/, viewIncidents, 'incidents'], [/^#\/incident\/([\w-]+)$/, viewIncident, 'incidents'],
  [/^#\/areas$/, viewAreas, 'areas'], [/^#\/area\/([\w-]+)$/, viewArea, 'areas'],
];
function leaveView() {
  for (const fn of S.leave.splice(0)) { try { fn(); } catch (e) { /* ignore */ } }
  for (const l of S.lives) l.stop();
  S.lives.clear();
}
function route(soft) {
  let hash = location.hash || '#/incidents';
  if (hash.startsWith('#/pair')) { showPair(); return; }
  const hit = ROUTES.find(([re]) => re.test(hash)) || ROUTES[2];
  if (!ROUTES.some(([re]) => re.test(hash))) hash = '#/incidents';
  const [re, fn, tab] = hit;
  const m = hash.match(re) || [];
  if (!soft) leaveView();
  S.route = hash;
  S.subOverride = false;
  document.querySelectorAll('.tabs a').forEach((a) => a.classList.toggle('on', a.dataset.tab === tab));
  const deep = /^#\/(camera|incident|area)\//.test(hash);
  $('back').hidden = !deep;
  if (!soft) window.scrollTo(0, 0);
  paintStatus();
  Promise.resolve(fn(m[1], soft)).catch((e) => showError(e));
}
function refreshView() { route(true); }
function softReload() { route(true); }
function setTitle(text, sub) {
  $('title').textContent = text;
  $('title').classList.toggle('long', text.length > 20);
  document.title = text === 'YardGuard' ? text : text + ' · YardGuard';
  if (sub != null) { S.subOverride = true; $('sub').replaceChildren(h('span', { text: sub })); }
}
function fill(el, ...kids) { el.replaceChildren(...kids.flat(3).filter((k) => k != null && k !== false)); }
function render(...nodes) { fill($('view'), ...nodes); }
function showError(e) {
  if (e && (e.code === 'pair' || e.name === 'AbortError')) return;
  render(h('div', { class: 'empty' }, icon(e && e.code === 'offline' ? 'offline' : 'help'),
    h('h2', { text: e && e.code === 'offline' ? 'YardGuard is not reachable' : 'Something went wrong' }),
    h('div', { text: (e && e.message) || 'Try again.' }),
    h('div', { class: 'more' }, h('button', { class: 'btn', onclick: () => route() , text: 'Try again' }))));
}
function loading() { render(h('div', { class: 'spinner', 'aria-label': 'Loading' })); }

/* -- live pictures ------------------------------------------------------------------------ */
function liveFrame(ch, opts = {}) {
  const img = h('img', { alt: '' });
  const msg = h('div', { class: 'msg' }, h('span', { text: 'Connecting…' }));
  const badge = h('div', { class: 'live', text: 'LIVE', hidden: true });
  const el = h('div', { class: 'frame' }, img, msg, badge);
  let mode = opts.poll || navigator.standalone === true ? 'poll' : 'mjpeg';
  let timer = null, endTimer = null, running = false, gen = 0;
  const say = (text, ic) => { msg.replaceChildren(...(ic ? [icon(ic)] : []), h('span', { text })); msg.hidden = false; };
  const shown = () => { msg.hidden = true; badge.hidden = false; };
  const pollOnce = (g) => {
    if (!running || g !== gen) return;
    const next = new Image();
    next.onload = () => {
      if (!running || g !== gen) return;
      img.src = next.src; shown();
      timer = setTimeout(() => pollOnce(g), opts.every || 1000);
    };
    next.onerror = () => {
      if (!running || g !== gen) return;
      say('No picture right now', 'nocam'); badge.hidden = true;
      timer = setTimeout(() => pollOnce(g), 4000);
    };
    next.src = `/live/${ch}.jpg?t=${Date.now()}`;
  };
  const live = {
    el,
    start() {
      if (running) return;
      running = true; gen += 1;
      live.resume = false;
      S.lives.add(live);
      say('Connecting…');
      if (mode === 'mjpeg') {
        img.onload = shown;
        img.onerror = () => { if (running && mode === 'mjpeg') { mode = 'poll'; img.onerror = null; pollOnce(gen); } };
        img.src = `/live/${ch}.mjpg?t=${Date.now()}`;
      } else pollOnce(gen);
      clearTimeout(endTimer);
      endTimer = setTimeout(() => { live.stop(); say('Live view paused. Tap to keep watching.', 'play'); live.paused = true; },
        LIVE_MAX_MS);
      live.paused = false;
    },
    stop() {
      running = false; gen += 1;
      clearTimeout(timer); clearTimeout(endTimer);
      img.onload = null; img.onerror = null;
      img.src = BLANK;   // closes the stream connection
      badge.hidden = true;
    },
    pause() {   // page hidden: stop now, start again when it is visible
      if (running) { live.stop(); live.resume = true; }
    },
    paused: false,
    resume: false,
  };
  el.addEventListener('click', (e) => { if (live.paused) { e.stopPropagation(); live.start(); } });
  return live;
}
function camState(c) {
  const cls = { watching: 'ok', idle: 'ok', starting: 'warn', offline: 'bad' }[c.state] || '';
  return h('span', { class: 'chip' }, h('i', { class: 'dot ' + cls }), c.state_text);
}
function camSees(c) {
  const parts = [];
  if (c.people) parts.push(plural(c.people, 'person').replace(/persons$/, 'people'));
  if (c.vehicles) parts.push(plural(c.vehicles, 'vehicle'));
  return parts.join(' · ');
}

/* -- cameras --------------------------------------------------------------------------------- */
async function viewCameras(_arg, soft) {
  setTitle('Cameras');
  if (soft && S.camTiles) { updateTiles(); return; }
  if (!S.status) await loadStatus();
  const cams = S.status.cameras;
  if (!cams.length) {
    render(h('div', { class: 'empty' }, icon('nocam'), h('h2', { text: 'No cameras yet' }),
      h('div', { text: 'Add your cameras in YardGuard on the PC.' })));
    return;
  }
  const poll = cams.length > 4;
  S.camTiles = new Map();
  const grid = h('div', { class: 'grid' });
  for (const c of cams) {
    const live = liveFrame(c.channel, { poll, every: 2000 });
    const meta = h('div', { class: 'tile-meta' });
    const tile = h('div', { class: 'card tile', role: 'link', tabindex: '0',
      'aria-label': 'Open ' + c.name, onclick: () => { if (!live.paused) location.hash = '#/camera/' + c.channel; } },
    live.el, meta);
    grid.append(tile);
    S.camTiles.set(c.channel, meta);
    live.start();
  }
  const top = [];
  if (!S.status.monitoring.running) top.push(h('div', { class: 'banner info' }, icon('power'),
    h('span', { text: 'Monitoring is off. You can watch the cameras, but YardGuard is not checking them.' })));
  render(...top, grid);
  updateTiles();
  const onStatus = () => updateTiles();
  document.addEventListener('yg-status', onStatus);
  onLeave(() => { document.removeEventListener('yg-status', onStatus); S.camTiles = null; });
}
function updateTiles() {
  if (!S.camTiles || !S.status) return;
  for (const c of S.status.cameras) {
    const meta = S.camTiles.get(c.channel);
    if (!meta) continue;
    const sees = camSees(c);
    fill(meta, h('span', { class: 'name', text: c.name }), sees ? h('span', { class: 'small muted', text: sees }) : null,
      camState(c));
  }
}
async function viewCamera(ch, soft) {
  if (!S.status) await loadStatus();
  const c = S.status.cameras.find((x) => String(x.channel) === String(ch));
  if (!c) { location.replace('#/cameras'); return; }
  setTitle(c.name, c.state_text);
  if (soft && S.camMeta) return;   // the yg-status listener keeps the line current
  const live = liveFrame(c.channel);
  S.camMeta = h('div', { class: 'tile-meta' });
  const paintMeta = () => {
    const cur = S.status.cameras.find((x) => x.channel === c.channel) || c;
    fill(S.camMeta, h('span', { class: 'name', text: cur.name }),
      camSees(cur) ? h('span', { class: 'small muted', text: camSees(cur) }) : null, camState(cur));
  };
  paintMeta();
  // below the live picture: this camera's latest incidents and its areas
  const incBox = h('div', { class: 'list' }, h('div', { class: 'small muted', text: 'Loading…' }));
  const areaBox = h('div');
  render(h('div', { class: 'cam-full' }, live.el, S.camMeta),
    h('div', { class: 'section', text: 'Latest incidents' }), incBox, areaBox);
  live.start();
  document.addEventListener('yg-status', paintMeta);
  onLeave(() => { document.removeEventListener('yg-status', paintMeta); S.camMeta = null; });
  const mine = (it) => (it.channels || []).map(String).includes(String(c.channel));
  api('/api/incidents?status=all&limit=100').then((d) => {
    const items = d.items.filter(mine).slice(0, 5);
    fill(incBox, items.length ? items.map(incidentRow)
      : h('div', { class: 'small muted', text: 'No incidents from this camera lately.' }));
  }).catch(() => fill(incBox, h('div', { class: 'small muted', text: 'Incidents could not be loaded.' })));
  api('/api/areas').then((d) => {
    const list = d.items.filter((a) => String(a.channel) === String(c.channel));
    if (!list.length) return;
    fill(areaBox, h('div', { class: 'section', text: 'Areas' }), h('div', { class: 'card' }, list.map(areaRow)));
  }).catch(() => {});
}

/* -- incidents -------------------------------------------------------------------------------- */
function verdictChip(v) {
  const [text, cls] = VERDICT[v] || VERDICT.unverified;
  return h('span', { class: 'chip ' + cls, text });
}
function incidentRow(it) {
  const thumb = h('div', { class: 'thumb' });
  if (it.cover && (it.has_crop || it.has_snapshot)) {
    thumb.append(h('img', { alt: '', loading: 'lazy', src: media('alert', it.cover, it.has_crop ? 'crop' : 'snapshot', 240) }));
  } else thumb.append(icon('image'));
  const chips = [verdictChip(it.verdict)];
  if (it.status !== 'open') chips.unshift(h('span', { class: 'chip success', text: STATUS[it.status] || it.status }));
  if (it.alert_count > 1) chips.push(h('span', { class: 'chip', text: plural(it.alert_count, 'alert') }));
  return h('div', { class: 'card inc' + (it.status !== 'open' ? ' done' : ''), role: 'link', tabindex: '0',
    onclick: () => { location.hash = '#/incident/' + it.id; } },
  thumb,
  h('div', { class: 'body' },
    h('div', { class: 't' }, h('i', { class: 'sev ' + it.severity, title: SEVERITY[it.severity] || '' }),
      h('span', { text: it.title })),
    h('div', { class: 's', text: [it.cameras.join(', '), ago(it.start_ts)].filter(Boolean).join(' · ') }),
    h('div', { class: 'chips' }, chips)),
  it.unread ? h('i', { class: 'unread-mark', title: 'New' }) : null);
}
async function viewIncidents(_arg, soft) {
  setTitle('Incidents');
  const filter = S.incFilter === 'all' ? 'all' : 'open';
  const keep = soft && S.incCount ? Math.min(100, S.incCount) : 30;
  if (!soft) loading();
  const data = await api(`/api/incidents?status=${filter}&limit=${keep}`);
  const items = data.items.slice();
  const list = h('div', { class: 'list' });
  const moreBox = h('div', { class: 'more' });
  const add = (rows) => rows.forEach((it) => list.append(incidentRow(it)));
  add(items);
  S.incCount = items.length;
  const paintMore = (more) => {
    moreBox.replaceChildren();
    if (!more) return;
    const btn = h('button', { class: 'btn', text: 'Show older' });
    btn.onclick = async () => {
      btn.disabled = true;
      try {
        const last = items[items.length - 1];
        const d = await api(`/api/incidents?status=${filter}&limit=30&before=${last.start_ts}`);
        items.push(...d.items); add(d.items); S.incCount = items.length; paintMore(d.more);
      } catch (e) { btn.disabled = false; toast(e.message); }
    };
    moreBox.append(btn);
  };
  paintMore(data.more);
  const seg = h('div', { class: 'seg', role: 'tablist' },
    ['open', 'all'].map((f) => h('button', { class: f === filter ? 'on' : '', role: 'tab', 'aria-selected': String(f === filter),
      text: f === 'open' ? 'Open' : 'All', onclick: () => { S.incFilter = f; store('yg_filter', f); S.incCount = 0; route(); } })));
  const c = (S.status && S.status.counts) || {};
  const summary = h('span', { class: 'small muted', text: c.open != null ? `Last 24 h: ${plural(c.open + (c.handled || 0) + (c.false_alarm || 0), 'incident')}` : '' });
  const body = items.length ? [list, moreBox] : [h('div', { class: 'empty' }, icon('check'),
    h('h2', { text: filter === 'open' ? 'Nothing open' : 'No incidents yet' }),
    h('div', { text: filter === 'open' ? 'All quiet. New incidents show up here.' : 'When YardGuard sees something, it shows up here.' }))];
  render(h('div', { class: 'toolbar' }, seg, summary), ...body);
  const newest = items.reduce((m, it) => Math.max(m, it.start_ts), (S.status && S.status.newest_ts) || 0);
  store('yg_seen', newest);
  $('badge').hidden = true;
}

async function viewIncident(id, soft) {
  if (soft) {   // never interrupt a clip that is playing
    const v = document.querySelector('#view video');
    if (v && !v.paused) return;
  } else { setTitle('Incident'); loading(); }
  const inc = await api('/api/incidents/' + encodeURIComponent(id));
  setTitle(inc.title, [inc.cameras.join(', '), when(inc.start_ts)].filter(Boolean).join(' · '));
  const alerts = inc.alerts.length ? inc.alerts : [];
  let index = soft && S.incIndex && S.incIndex.id === id ? Math.min(S.incIndex.i, alerts.length - 1) : alerts.length - 1;
  index = Math.max(0, index);
  let view = store('yg_pic') === 'snapshot' ? 'snapshot' : 'crop';
  const pager = h('div', { class: 'pager', 'aria-label': 'Pictures' });
  const pick = (a) => (view === 'crop' && a.has_crop) || !a.has_snapshot ? 'crop' : 'snapshot';
  const slides = alerts.map((a) => {
    const img = (a.has_crop || a.has_snapshot) ? h('img', { alt: a.title, loading: 'lazy', src: media('alert', a.id, pick(a), 1280) }) : null;
    return h('div', { class: 'slide' }, img || h('div', { class: 'frame' }, h('div', { class: 'msg' }, icon('image'), h('span', { text: 'No picture' }))));
  });
  pager.append(...slides);
  const dots = h('div', { class: 'dots' }, alerts.length > 1 ? alerts.map(() => h('i')) : []);
  const counter = h('span', { class: 'small muted' });
  const seg = h('div', { class: 'seg' }, [['crop', 'Close-up'], ['snapshot', 'Whole picture']].map(([k, t]) =>
    h('button', { class: k === view ? 'on' : '', text: t, onclick: (e) => {
      view = k; store('yg_pic', k);
      seg.querySelectorAll('button').forEach((b) => b.classList.toggle('on', b === e.currentTarget));
      alerts.forEach((a, i) => { const im = slides[i].querySelector('img'); if (im) im.src = media('alert', a.id, pick(a), 1280); });
    } })));
  const cap = h('div', { class: 'card alert-cap' });
  const clipBox = h('div', { class: 'clip' });
  let clipFor = '';
  const paint = () => {
    const a = alerts[index];
    S.incIndex = { id, i: index };
    [...dots.children].forEach((d, i) => d.classList.toggle('on', i === index));
    counter.textContent = alerts.length > 1 ? `${index + 1} of ${alerts.length}` : '';
    if (!a) { cap.hidden = true; return; }
    fill(cap, h('div', { class: 't', text: a.title }),
      h('div', { class: 'small muted', text: [a.camera, when(a.ts), SEVERITY[a.severity]].filter(Boolean).join(' · ') }),
      a.detail ? h('div', { class: 'small', text: a.detail }) : null,
      h('div', { class: 'findings' }, verdictChip(a.verdict), a.brain_findings.map((f) => h('span', { class: 'chip accent', text: f }))));
    if (clipFor !== a.id) {
      clipFor = a.id;
      clipBox.replaceChildren();
      if (a.has_clip) {
        const v = h('video', { controls: true, playsinline: true, preload: 'metadata', src: media('alert', a.id, 'clip') });
        v.addEventListener('loadedmetadata', () => { if (a.clip_offset_s > 0) v.currentTime = Math.max(0, a.clip_offset_s - 1); }, { once: true });
        clipBox.append(h('div', { class: 'section', text: 'Video clip' }), v);
      } else if (a.clip_pending) {
        clipBox.append(h('div', { class: 'small muted', text: 'The video clip is still being saved. It appears here in a minute.' }));
      }
    }
  };
  pager.addEventListener('scroll', () => {
    const i = Math.round(pager.scrollLeft / Math.max(1, pager.clientWidth));
    if (i !== index && i >= 0 && i < alerts.length) { index = i; paint(); }
  }, { passive: true });
  const facts = h('div', { class: 'card facts' }, [
    ['Level', SEVERITY[inc.severity] || inc.severity], ['AI check', (VERDICT[inc.verdict] || VERDICT.unverified)[0]],
    ['Status', STATUS[inc.status] || inc.status], ['Cameras', inc.cameras.join(', ') || '-'],
    ['When', inc.end_ts - inc.start_ts > 60 ? `${when(inc.start_ts)} – ${clock(inc.end_ts)}` : when(inc.start_ts)],
    ['Alerts', String(inc.alert_count)],
  ].map(([k, v]) => h('div', { class: 'fact' }, h('span', { class: 'k', text: k }), h('span', { class: 'v', text: v }))));
  const actions = h('div', { class: 'actions' });
  if (S.status && S.status.allow_actions) {
    const mark = async (status, btn) => {
      btn.disabled = true;
      try {
        await post(`/api/incidents/${encodeURIComponent(id)}/mark`, { status });
        toast(status === 'open' ? 'Reopened' : status === 'handled' ? 'Marked as handled' : 'Marked as a false alarm');
        softReload();
      } catch (e) { btn.disabled = false; toast(e.message); }
    };
    const btns = inc.status === 'open'
      ? [h('button', { class: 'btn primary', text: 'Mark handled', onclick: (e) => mark('handled', e.currentTarget) }),
        h('button', { class: 'btn', text: 'False alarm', onclick: (e) => mark('false_alarm', e.currentTarget) })]
      : [h('button', { class: 'btn', text: 'Reopen', onclick: (e) => mark('open', e.currentTarget) })];
    actions.append(h('div', { class: 'row-btns' }, btns));
  }
  const bar = h('div', { class: 'pager-bar' }, alerts.some((a) => a.has_crop) && alerts.some((a) => a.has_snapshot) ? seg : h('span'), dots, counter);
  render(h('div', { class: 'detail' }, h('div', { class: 'detail-media' }, pager, bar, clipBox),
    h('div', { class: 'detail-side' }, cap, actions, facts)));
  paint();
  requestAnimationFrame(() => { pager.scrollLeft = index * pager.clientWidth; });
}

/* -- areas ------------------------------------------------------------------------------------- */
async function viewAreas(_arg, soft) {
  setTitle('Areas');
  if (!soft) loading();
  const data = await api('/api/areas');
  if (!data.items.length) {
    render(h('div', { class: 'empty' }, icon('areas'), h('h2', { text: 'No areas yet' }),
      h('div', { text: 'Draw areas on the cameras in YardGuard on the PC (Areas page). Their log shows up here.' })));
    return;
  }
  const groups = new Map();
  for (const a of data.items) {
    if (!groups.has(a.camera)) groups.set(a.camera, []);
    groups.get(a.camera).push(a);
  }
  const out = [];
  for (const [cam, list] of groups) {
    out.push(h('div', { class: 'section', text: cam }));
    out.push(h('div', { class: 'card' }, list.map(areaRow)));
  }
  render(...out);
}
function areaRow(a) {
  return h('div', { class: 'area', role: 'link', tabindex: '0', onclick: () => { location.hash = '#/area/' + a.id; } },
    h('div', { class: 'ico' }, icon(TASK_ICON[a.task] || 'areas')),
    h('div', { class: 'body' }, h('div', { class: 'n', text: a.name }),
      h('div', { class: 's', text: a.task_title + (a.last_ts ? ' · last ' + ago(a.last_ts) : '') })),
    h('div', { class: 'h', text: a.headline }));
}
function eventRow(ev) {
  const thumb = h('div', { class: 'thumb' });
  if (ev.has_crop || ev.has_snapshot) thumb.append(h('img', { alt: '', loading: 'lazy', src: media('area', ev.id, ev.has_crop ? 'crop' : 'snapshot', 160) }));
  else thumb.append(icon(TASK_ICON[ev.task] || 'areas'));
  const title = ev.kind === 'plate' && ev.value
    ? h('div', { class: 't' }, h('span', { class: 'plate', text: ev.value }),
      ev.direction ? h('span', { class: 'small muted', text: ev.direction === 'in' ? '  Came in' : '  Went out' }) : null)
    : h('div', { class: 't', text: ev.text });
  return h('div', { class: 'card ev', role: 'button', tabindex: '0', onclick: () => openEvent(ev) },
    thumb, h('div', { class: 'body' }, title, h('div', { class: 's', text: `${ev.kind_text} · ${when(ev.ts)}` })));
}
async function viewArea(id, soft) {
  if (!soft) loading();
  const [areas, data] = await Promise.all([api('/api/areas'), api(`/api/area_events?area=${encodeURIComponent(id)}&limit=50`)]);
  const a = areas.items.find((x) => x.id === id);
  setTitle(a ? a.name : 'Area', a ? `${a.camera} · ${a.task_title}` : '');
  const stats = h('div', { class: 'stats' });
  if (a) {
    stats.append(h('span', { class: 'chip accent', text: a.headline }));
    const counts = Object.entries(a.counts || {});
    if (counts.length > 1 || a.task !== 'log_plates') for (const [k, n] of counts) stats.append(h('span', { class: 'chip', text: `${KIND_TEXT[k] || k}: ${n}` }));
  }
  const list = h('div', { class: 'list' }, data.items.map(eventRow));
  const moreBox = h('div', { class: 'more' });
  let items = data.items.slice();
  const paintMore = (more) => {
    moreBox.replaceChildren();
    if (!more) return;
    const btn = h('button', { class: 'btn', text: 'Show older' });
    btn.onclick = async () => {
      btn.disabled = true;
      try {
        const d = await api(`/api/area_events?area=${encodeURIComponent(id)}&limit=50&before=${items[items.length - 1].ts}`);
        items = items.concat(d.items); d.items.forEach((ev) => list.append(eventRow(ev))); paintMore(d.more);
      } catch (e) { btn.disabled = false; toast(e.message); }
    };
    moreBox.append(btn);
  };
  paintMore(data.more);
  render(h('div', { class: 'section', text: 'Today' }), stats, h('div', { class: 'section', text: 'Log' }),
    items.length ? list : h('div', { class: 'empty' }, h('div', { text: 'Nothing logged yet.' })), moreBox);
}
function openEvent(ev) {
  if (!ev.has_snapshot && !ev.has_crop && !ev.has_clip) return;
  const stage = h('div', { class: 'stage' });
  if (ev.has_clip) {
    const v = h('video', { controls: true, playsinline: true, autoplay: true, muted: true, preload: 'metadata', src: media('area', ev.id, 'clip') });
    v.addEventListener('loadedmetadata', () => { if (ev.clip_offset_s > 0) v.currentTime = Math.max(0, ev.clip_offset_s - 1); }, { once: true });
    stage.append(v);
  } else stage.append(h('img', { alt: ev.text, src: media('area', ev.id, ev.has_snapshot ? 'snapshot' : 'crop') }));
  lightbox(ev.kind === 'plate' && ev.value ? `${ev.value} · ${when(ev.ts)}` : `${ev.text} · ${when(ev.ts)}`, stage);
}
function lightbox(title, stage) {
  const close = () => { box.remove(); document.removeEventListener('keydown', key); };
  const key = (e) => { if (e.key === 'Escape') close(); };
  const box = h('div', { class: 'lightbox', role: 'dialog', 'aria-label': title },
    h('div', { class: 'bar' }, h('span', { class: 't', text: title }),
      h('button', { class: 'icon-btn', 'aria-label': 'Close', onclick: close }, icon('close'))), stage);
  document.addEventListener('keydown', key);
  document.body.append(box);
  onLeave(close);
}

/* -- menu sheet ---------------------------------------------------------------------------------- */
function openMenu() {
  const st = S.status;
  if (!st) return;
  const close = () => scrim.remove();
  const rows = [];
  rows.push(h('div', { class: 'rowline' }, icon('phone'), h('div', { class: 'body' },
    h('div', { text: (st.device && st.device.name) || 'This phone' }), h('div', { class: 'small muted', text: 'This phone is paired with YardGuard' + (st.site ? ` (${st.site})` : '') }))));
  const running = st.monitoring.running;
  const sw = h('button', { class: 'switch' + (running ? ' on' : ''), role: 'switch', 'aria-checked': String(running),
    'aria-label': 'Monitoring', disabled: !st.allow_actions });
  sw.onclick = async () => {
    const on = !S.status.monitoring.running;
    if (!on && !confirm('Turn monitoring off? Nothing will be watched until you turn it on again.')) return;
    sw.disabled = true;
    try {
      const r = await post('/api/monitoring', { on });
      toast(r.message || (on ? 'Monitoring is starting' : 'Monitoring is turning off'));
      setTimeout(() => loadStatus().catch(() => {}), 1500);
      close();
    } catch (e) { sw.disabled = false; toast(e.message); }
  };
  rows.push(h('div', { class: 'rowline' }, icon('power'), h('div', { class: 'body' },
    h('div', { text: running ? 'Monitoring is on' : 'Monitoring is off' }),
    h('div', { class: 'small muted', text: st.allow_actions ? st.monitoring.message : 'To switch it from here, allow control from phones in YardGuard on the PC.' })), sw));
  if (!(navigator.standalone || matchMedia('(display-mode: standalone)').matches)) {
    const ios = /iPhone|iPad|iPod/.test(navigator.userAgent);
    rows.push(h('div', { class: 'rowline' }, icon('areas'), h('div', { class: 'body' }, h('div', { text: 'Add to Home Screen' }),
      h('div', { class: 'small muted', text: ios ? 'Tap the Share button, then "Add to Home Screen".' : 'Open the browser menu and tap "Add to Home screen" or "Install app".' }))));
  }
  const unpair = h('button', { class: 'btn danger block', text: 'Unpair this phone' });
  unpair.onclick = async () => {
    if (!confirm('Unpair this phone? You will need a new code from the PC to use it again.')) return;
    try { await post('/api/logout', {}); } catch (e) { /* already gone */ }
    close(); S.status = null; showPair();
  };
  const sheet = h('div', { class: 'sheet', role: 'dialog', 'aria-label': 'YardGuard' }, h('div', { class: 'grab' }),
    h('h3', { text: 'YardGuard' }), rows, h('div', { class: 'more' }, unpair),
    h('div', { class: 'small muted', text: 'Version ' + st.version }));
  const scrim = h('div', { class: 'scrim', onclick: (e) => { if (e.target === scrim) close(); } }, sheet);
  document.body.append(scrim);
  onLeave(close);
}

/* -- pairing ------------------------------------------------------------------------------------- */
function guessName() {
  const ua = navigator.userAgent;
  if (/iPhone/.test(ua)) return 'iPhone';
  if (/iPad/.test(ua)) return 'iPad';
  if (/Android/.test(ua)) return /Mobile/.test(ua) ? 'Android phone' : 'Android tablet';
  return 'Browser';
}
function showPair(message) {
  leaveView();
  if (S.poll) S.poll.abort();
  S.status = null; S.seq = null;
  $('app').hidden = true;
  const box = $('pairing');
  box.hidden = false;
  const code = h('input', { class: 'code', name: 'code', autocomplete: 'one-time-code', autocapitalize: 'characters',
    spellcheck: 'false', maxlength: '9', placeholder: 'XXXX-XXXX', required: true, value: S.pairCode || '' });
  const name = h('input', { name: 'name', maxlength: '40', value: store('yg_name') || guessName(), required: true });
  const err = h('div', { class: 'err', role: 'alert', text: message || '' });
  const btn = h('button', { class: 'btn primary block', type: 'submit', text: 'Pair this phone' });
  const form = h('form', { onsubmit: async (e) => {
    e.preventDefault();
    btn.disabled = true; err.textContent = '';
    try {
      await api('/api/pair', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code: code.value, name: name.value }) });
      store('yg_name', name.value);
      S.pairCode = '';
      start();
    } catch (ex) { err.textContent = ex.message; btn.disabled = false; }
  } }, h('label', {}, 'Code from the PC', code), h('label', {}, 'Name of this phone', name), err, btn);
  box.replaceChildren(h('div', { class: 'pair' },
    h('img', { class: 'logo', src: '/icon-192.png', alt: '' }),
    h('h1', { text: 'Pair this phone' }),
    h('p', { text: 'Enter the code YardGuard shows on the PC. You only do this once.' }), form,
    h('div', { class: 'help' }, h('strong', { text: 'Where is the code?' }),
      h('ol', {}, h('li', { text: 'On the PC, open YardGuard > Integrations.' }),
        h('li', { text: 'In "Phone access", press "Add phone".' }),
        h('li', { text: 'Scan the QR code, or type the code shown there.' })))));
  (S.pairCode ? name : code).focus();
  if (location.hash !== '#/pair') history.replaceState(null, '', '#/pair');
}

/* -- start --------------------------------------------------------------------------------------- */
async function start() {
  $('pairing').hidden = true;
  try {
    await loadStatus();
  } catch (e) {
    if (e.code === 'pair') return;
    $('app').hidden = false;
    showError(e);
    setTimeout(start, 5000);
    return;
  }
  $('app').hidden = false;
  if (!location.hash || location.hash.startsWith('#/pair') || location.hash.startsWith('#pair')) {
    history.replaceState(null, '', '#/incidents');
  }
  route();
  changesLoop();
  clearInterval(S.timer);
  S.timer = setInterval(() => { if (!document.hidden) loadStatus().catch(() => {}); }, 15000);
}
function init() {
  // a pairing link carries the code in the fragment (#pair=K7P29QXM): take it and hide it from the address bar
  const m = location.hash.match(/^#\/?pair[=/]([A-Za-z0-9-]{4,12})/);
  if (m) { S.pairCode = m[1].toUpperCase(); history.replaceState(null, '', '#/pair'); }
  document.querySelectorAll('[data-icon]').forEach((el) => el.replaceWith(icon(el.dataset.icon)));
  $('back').append(icon('back'));
  $('menu').append(icon('more'));
  $('back').onclick = () => {
    const r = S.route;
    if (history.length > 1 && S.navigated) history.back();
    else location.hash = r.startsWith('#/camera/') ? '#/cameras' : r.startsWith('#/area/') ? '#/areas' : '#/incidents';
  };
  $('menu').onclick = openMenu;
  window.addEventListener('hashchange', () => { S.navigated = true; if (S.status) route(); });
  window.addEventListener('scroll', () => $('top').classList.toggle('scrolled', window.scrollY > 4), { passive: true });
  document.addEventListener('visibilitychange', () => {
    if (!S.status) return;
    if (document.hidden) {
      for (const l of S.lives) l.pause();
      if (S.poll) S.poll.abort();
    } else {
      for (const l of S.lives) if (l.resume) l.start();
      loadStatus().then(() => { refreshView(); changesLoop(); }).catch(() => {});
    }
  });
  if (S.pairCode) showPair(); else start();
}
document.addEventListener('DOMContentLoaded', init);
