// sim-viz entry point: live mode (joins a run in progress) and replay mode (scrubber, staggered playback).
import { CATEGORIES } from './describe.js';
import { HEIGHT, PLACES, ROLE_LABELS, WIDTH, agentColour, cssColour } from './layout.js';
import { TownScene } from './scene.js';
import { applyAction, buildWorldTo, emptyWorld, lastIndexForTick } from './state.js';

const Phaser = window.Phaser;
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const ui = {
  mode: $('mode'), runSelect: $('run-select'), refresh: $('refresh-runs'), play: $('play'), speed: $('speed'),
  scrubber: $('scrubber'), tickLabel: $('tick-label'), simTime: $('sim-time'), status: $('status'), runLabel: $('run-label'),
  log: $('log'), counters: $('counters'), replayControls: $('replay-controls'), agents: $('agents'), filters: $('filters'),
  focus: $('focus'), focusBody: $('focus-body'), focusClose: $('focus-close'), agentFilter: $('agent-filter'),
  idleHint: $('idle-hint'), stepBack: $('step-back'), stepFwd: $('step-fwd'),
};

const app = {
  scene: null,
  mode: 'live',
  world: emptyWorld(),
  ws: null,
  wsRetry: 0,
  live: { actions: [] },
  replay: { actions: [], index: -1, maxTick: 0, playing: false, timer: null, perTick: {} },
  speed: 1,
  filters: new Set(Object.entries(CATEGORIES).filter(([, v]) => v.default).map(([k]) => k)),
  agentFilter: null,
  selected: null,
  renderQueued: false,
};

// ---------------- panels ----------------
function formatSimTime(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso || 'Not started';
  const m = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  const p = (n) => String(n).padStart(2, '0');
  return `${p(d.getDate())} ${m[d.getMonth()]} ${d.getFullYear()} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function setStatus(text, ok = true) {
  ui.status.textContent = text;
  ui.status.className = ok ? 'status ok' : 'status warn';
}

function queueRender() {
  if (app.renderQueued) return;
  app.renderQueued = true;
  requestAnimationFrame(() => { app.renderQueued = false; renderPanels(); });
}

function renderPanels() {
  const w = app.world;
  ui.simTime.textContent = w.simTime ? formatSimTime(w.simTime) : 'Not started';
  ui.tickLabel.textContent = app.mode === 'replay' ? `Tick ${w.tick} of ${app.replay.maxTick}` : `Tick ${w.tick}`;
  ui.runLabel.textContent = w.runLabel || (w.runId ? `Run ${w.runId.slice(0, 8)}` : '');
  const c = w.counters;
  ui.counters.innerHTML = [
    ['✅', 'Orders', c.orders], ['💬', 'Chat messages', c.chats], ['🤝', 'Handoffs', c.handoffs],
    ['🧾', 'Approvals', c.approvals], ['🚨', 'Incidents', c.incidents], ['⚠', 'Faults', c.faults],
    ['🛡', 'Guardrails', c.guardrails], ['❌', 'Failed calls', c.failures], ['⏳', 'Waiting for you', w.pendingApprovals],
  ].map(([i, k, v]) => `<div class="counter"><span>${i} ${k}</span><strong>${v}</strong></div>`).join('');
  renderAgents();
  renderLog();
}

function renderAgents() {
  const agents = Object.values(app.world.agents).sort((a, b) => (a.type === 'shopper') - (b.type === 'shopper') || a.order - b.order);
  ui.agents.innerHTML = agents.map((a) => {
    const place = PLACES[a.place] ? PLACES[a.place].label : a.place;
    const extras = a.type === 'shopper' ? `<span class="chips">${a.basket ? `🛒 ${a.basket}` : ''} ${a.orders ? `✅ ${a.orders}` : ''} ${a.resting ? '💤' : ''}</span>` : '';
    const sel = app.agentFilter === a.id ? ' selected' : '';
    return `<button class="agent${sel}" data-agent="${esc(a.id)}" title="Show only ${esc(a.name)} in the log">
      <span class="dot" style="background:${cssColour(agentColour(a))}"></span>
      <span class="who"><strong>${esc(a.name)}</strong> <small>${esc(ROLE_LABELS[a.type] || a.type)} · ${esc(place)}</small>
      <span class="doing">${esc(a.icon)} ${esc(a.activity)}</span></span>${extras}</button>`;
  }).join('') || '<p class="muted">Agents appear when a run starts.</p>';
  ui.agentFilter.hidden = !app.agentFilter;
  if (app.agentFilter) ui.agentFilter.querySelector('span').textContent = (app.world.agents[app.agentFilter] || {}).name || app.agentFilter;
}

function renderLog() {
  const rows = [];
  const log = app.world.log;
  for (let i = log.length - 1; i >= 0 && rows.length < 250; i--) {
    const e = log[i];
    if (!app.filters.has(e.cat)) continue;
    if (app.agentFilter && e.agentId !== app.agentFilter && !(e.who || '').includes((app.world.agents[app.agentFilter] || {}).name || '\u0000')) continue;
    const agent = app.world.agents[e.agentId];
    const colour = agent ? cssColour(agentColour(agent)) : '#7f8c8d';
    const catColour = (CATEGORIES[e.cat] || {}).colour || '#7f8c8d';
    const sel = app.selected === e.seq ? ' selected' : '';
    rows.push(`<li class="entry${sel}" data-seq="${e.seq}" style="--cat:${catColour}">
      <span class="t">t${e.tick}</span><span class="ic">${esc(e.icon)}</span>
      <span class="txt"><b style="color:${colour}">${esc(e.who)}</b> ${esc(e.text)}</span></li>`);
  }
  ui.log.innerHTML = rows.join('') || '<li class="muted">Nothing to show with the current filters.</li>';
}

function renderFilters() {
  ui.filters.innerHTML = Object.entries(CATEGORIES).map(([k, v]) =>
    `<label class="chip" style="--cat:${v.colour}"><input type="checkbox" data-cat="${k}" ${app.filters.has(k) ? 'checked' : ''}> ${esc(v.label)}</label>`).join('');
}

function showFocus(entry, action) {
  const agent = app.world.agents[entry.agentId];
  const place = entry.place && PLACES[entry.place] ? PLACES[entry.place].label : '';
  const detail = action ? { kind: action.kind, sim_time: action.sim_time, ...(action.detail || {}) } : {};
  ui.focusBody.innerHTML = `
    <p class="focus-line"><span class="big">${esc(entry.icon)}</span> <b>${esc(entry.who)}</b> ${esc(entry.text)}</p>
    <p class="muted">Tick ${entry.tick}${action && action.sim_time ? ` · ${esc(formatSimTime(action.sim_time))}` : ''}${place ? ` · ${esc(place)}` : ''}${agent ? ` · ${esc(ROLE_LABELS[agent.type] || agent.type)}` : ''}</p>
    <pre>${esc(JSON.stringify(detail, null, 2))}</pre>`;
  ui.focus.hidden = false;
}

// ---------------- applying actions ----------------
function apply(action, index, animate) {
  const effect = applyAction(app.world, action, index);
  if (!app.scene || !effect) return effect;
  app.scene.syncWorld(app.world, animate);
  if (animate) app.scene.animate(effect);
  return effect;
}

function resetScene() {
  if (app.scene) { app.scene.clear(); app.scene.syncWorld(app.world, false); }
}

// ---------------- live mode ----------------
function connectLive() {
  if (app.ws) return;
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(`${proto}://${location.host}/ws/live`);
  app.ws = ws;
  setStatus('Live: connecting');
  ws.onopen = () => { app.wsRetry = 0; setStatus('Live: connected'); };
  ws.onmessage = (msg) => {
    if (app.mode !== 'live') return;
    let data;
    try { data = JSON.parse(msg.data); } catch { return; }
    if (data.type === 'backlog') {
      app.world = emptyWorld();
      app.live.actions = data.actions || [];
      app.live.actions.forEach((a, i) => applyAction(app.world, a, i));
      resetScene();
      ui.idleHint.hidden = true;
      setStatus(`Live: joined "${data.run.label || data.run.run_id.slice(0, 8)}" at tick ${app.world.tick}`);
      queueRender();
      return;
    }
    if (data.type === 'idle') {
      ui.idleHint.hidden = false;
      setStatus('Live: connected, waiting for a run to start');
      return;
    }
    if (data.kind === 'run.started' && data.run_id !== app.world.runId) {
      app.world = emptyWorld();
      app.live.actions = [];
      resetScene();
      ui.idleHint.hidden = true;
      setStatus('Live: new run started');
    }
    app.live.actions.push(data);
    apply(data, app.live.actions.length - 1, true);
    if (data.kind === 'run.finished') setStatus('Live: run finished, waiting for the next one');
    queueRender();
  };
  ws.onclose = () => {
    app.ws = null;
    if (app.mode !== 'live') return;
    app.wsRetry = Math.min(app.wsRetry + 1, 5);
    const delay = 500 * 2 ** app.wsRetry;
    setStatus(`Live: disconnected, retrying in ${Math.round(delay / 1000)} s`, false);
    setTimeout(connectLive, delay);
  };
  ws.onerror = () => ws.close();
}

function disconnectLive() {
  if (app.ws) { const ws = app.ws; app.ws = null; ws.onclose = null; ws.close(); }
}

// ---------------- replay mode ----------------
async function loadRuns() {
  try {
    const data = await (await fetch('/api/runs')).json();
    const current = ui.runSelect.value;
    ui.runSelect.innerHTML = '';
    for (const run of data.runs || []) {
      const opt = document.createElement('option');
      opt.value = run.run_id;
      const when = formatSimTime(run.first_seen).slice(0, 17);
      const s = run.summary || {};
      const label = run.label || `Seed ${run.seed ?? '?'} run`;
      opt.textContent = `${label} · ${run.status || ''} · ${s.orders ?? '?'} orders · ${when}`;
      ui.runSelect.appendChild(opt);
    }
    if (!data.runs || data.runs.length === 0) ui.runSelect.innerHTML = '<option value="">No runs recorded yet</option>';
    if (current && [...ui.runSelect.options].some((o) => o.value === current)) ui.runSelect.value = current;
    if (data.error) setStatus(`Replay: ${data.error}`, false);
  } catch {
    setStatus('Replay: could not reach the bridge', false);
  }
}

async function loadRun(runId) {
  stopPlayback();
  if (!runId) return;
  setStatus('Replay: loading run');
  const data = await (await fetch(`/api/runs/${encodeURIComponent(runId)}/events`)).json();
  const r = app.replay;
  r.actions = (data.actions || []).filter((a) => a.kind !== 'clock.tick');
  r.maxTick = r.actions.reduce((m, a) => Math.max(m, a.sim_tick ?? 0), 0);
  r.perTick = {};
  for (const a of r.actions) r.perTick[a.sim_tick ?? 0] = (r.perTick[a.sim_tick ?? 0] || 0) + 1;
  ui.scrubber.max = String(r.maxTick);
  seekIndex(-1);
  setStatus(`Replay: ${r.actions.length} events loaded. Press Play, or click any log line.`);
}

function seekIndex(index) {
  app.replay.index = index;
  app.world = buildWorldTo(app.replay.actions, index);
  if (index < 0) app.world.tick = 0;
  ui.scrubber.value = String(app.world.tick);
  resetScene();
  queueRender();
}

function tickMs() { return 1600 / app.speed; }

function playNext() {
  const r = app.replay;
  const i = r.index + 1;
  if (i >= r.actions.length) { stopPlayback(); setStatus('Replay: end of run'); return; }
  const a = r.actions[i];
  r.index = i;
  apply(a, i, true);
  ui.scrubber.value = String(app.world.tick);
  queueRender();
  const next = r.actions[i + 1];
  if (!next) { r.timer = setTimeout(playNext, tickMs()); return; }
  const cur = a.sim_tick ?? 0;
  const nt = next.sim_tick ?? 0;
  const delay = nt === cur
    ? Math.min(450 / app.speed, (tickMs() * 0.85) / Math.max(1, r.perTick[cur] || 1))
    : Math.min(tickMs() * Math.max(1, nt - cur), tickMs() * 2.5);
  r.timer = setTimeout(playNext, delay);
}

function startPlayback() {
  const r = app.replay;
  if (r.playing || r.actions.length === 0) return;
  if (r.index >= r.actions.length - 1) seekIndex(-1);
  r.playing = true;
  ui.play.textContent = 'Pause';
  playNext();
}

function stopPlayback() {
  const r = app.replay;
  r.playing = false;
  ui.play.textContent = 'Play';
  if (r.timer) clearTimeout(r.timer);
  r.timer = null;
}

// Jump to one event: rebuild the world just before it, then replay it with its cue.
function focusEvent(entry) {
  app.selected = entry.seq;
  if (app.mode === 'replay' && entry.index >= 0) {
    stopPlayback();
    const actions = app.replay.actions;
    app.replay.index = entry.index;
    app.world = buildWorldTo(actions, entry.index - 1);
    resetScene();
    apply(actions[entry.index], entry.index, true);
    app.selected = app.world.log.length ? app.world.log[app.world.log.length - 1].seq : null;
    ui.scrubber.value = String(app.world.tick);
    showFocus(entry, actions[entry.index]);
  } else {
    showFocus(entry, app.live.actions[entry.index]);
  }
  if (app.scene) {
    if (entry.agentId) app.scene.highlight(entry.agentId);
    if (entry.place) app.scene.flashPlace(entry.place);
  }
  queueRender();
}

// ---------------- mode switching ----------------
function setMode(mode) {
  app.mode = mode;
  stopPlayback();
  app.world = emptyWorld();
  app.selected = null;
  ui.focus.hidden = true;
  resetScene();
  ui.replayControls.hidden = mode !== 'replay';
  ui.idleHint.hidden = true;
  if (mode === 'live') {
    connectLive();
  } else {
    disconnectLive();
    loadRuns().then(() => loadRun(ui.runSelect.value));
  }
  queueRender();
}

// ---------------- wiring ----------------
ui.mode.addEventListener('change', () => setMode(ui.mode.value));
ui.refresh.addEventListener('click', () => loadRuns());
ui.runSelect.addEventListener('change', () => loadRun(ui.runSelect.value));
ui.play.addEventListener('click', () => (app.replay.playing ? stopPlayback() : startPlayback()));
ui.stepFwd.addEventListener('click', () => { stopPlayback(); if (app.replay.index < app.replay.actions.length - 1) { app.replay.index += 1; apply(app.replay.actions[app.replay.index], app.replay.index, true); ui.scrubber.value = String(app.world.tick); queueRender(); } });
ui.stepBack.addEventListener('click', () => { stopPlayback(); seekIndex(Math.max(-1, app.replay.index - 1)); });
ui.scrubber.addEventListener('input', () => { stopPlayback(); seekIndex(lastIndexForTick(app.replay.actions, parseInt(ui.scrubber.value, 10) || 0)); });
ui.speed.addEventListener('change', () => {
  app.speed = parseFloat(ui.speed.value) || 1;
  if (app.scene) app.scene.speed = app.speed;
});
ui.filters.addEventListener('change', (e) => {
  const cat = e.target.dataset.cat;
  if (!cat) return;
  if (e.target.checked) app.filters.add(cat); else app.filters.delete(cat);
  queueRender();
});
ui.log.addEventListener('click', (e) => {
  const li = e.target.closest('li.entry');
  if (!li) return;
  const entry = app.world.log.find((x) => x.seq === Number(li.dataset.seq));
  if (entry) focusEvent(entry);
});
function selectAgent(id) {
  app.agentFilter = app.agentFilter === id ? null : id;
  if (app.scene) app.scene.highlight(app.agentFilter);
  queueRender();
}
ui.agents.addEventListener('click', (e) => {
  const b = e.target.closest('[data-agent]');
  if (b) selectAgent(b.dataset.agent);
});
ui.agentFilter.querySelector('button').addEventListener('click', () => selectAgent(app.agentFilter));
ui.focusClose.addEventListener('click', () => { ui.focus.hidden = true; app.selected = null; if (app.scene) app.scene.highlight(null); queueRender(); });

renderFilters();
const scene = new TownScene();
scene.onAgentClick = (id) => selectAgent(id);
scene.onReady = (s) => {
  app.scene = s;
  s.syncWorld(app.world, false);
  setMode('live');
};

new Phaser.Game({
  type: Phaser.AUTO,
  parent: 'stage',
  width: WIDTH,
  height: HEIGHT,
  backgroundColor: '#dfe9d8',
  scale: { mode: Phaser.Scale.FIT, autoCenter: Phaser.Scale.CENTER_HORIZONTALLY },
  scene: [scene],
});
renderPanels();
