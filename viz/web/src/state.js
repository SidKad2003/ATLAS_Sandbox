// Pure world model. Applying the same actions in the same order always gives the same world,
// which is how replay rebuilds state for any point on the timeline.
import { describe } from './describe.js';
import { HOME_BY_TYPE, PLACES } from './layout.js';

// Agent types that act through others and do not get a sprite of their own.
const NO_SPRITE = new Set(['conductor', 'approval-service', 'mcp-server']);
const LOG_LIMIT = 600;

export function emptyWorld() {
  return {
    agents: {}, // id -> { id, type, name, place, order, activity, icon, basket, orders, resting }
    faults: {}, // place -> label
    counters: { orders: 0, chats: 0, handoffs: 0, approvals: 0, incidents: 0, faults: 0, guardrails: 0, failures: 0 },
    pendingApprovals: 0,
    simTime: '',
    tick: 0,
    log: [], // { seq, tick, agentId, who, cat, icon, text, place, important, index }
    seen: new Set(),
    runId: null,
    runLabel: '',
    nextSeq: 0,
  };
}

function ensureAgent(world, agent) {
  if (!agent || !agent.id || NO_SPRITE.has(agent.type)) return null;
  let a = world.agents[agent.id];
  if (!a) {
    a = {
      id: agent.id, type: agent.type || 'unknown', name: agent.name || agent.id,
      place: HOME_BY_TYPE[agent.type] || 'home', order: Object.keys(world.agents).length,
      activity: agent.type === 'shopper' ? 'At home' : 'Waiting', icon: '', basket: 0, orders: 0, resting: false,
    };
    world.agents[agent.id] = a;
  }
  if (agent.name && agent.name !== agent.id) a.name = agent.name;
  return a;
}

export const nameResolver = (world) => (id) => (world.agents[id] && world.agents[id].name) || id || 'someone';

function whoFor(world, action) {
  const ag = action.agent || {};
  if (ag.type === 'mcp-server') {
    const d = action.detail || {};
    const by = d.requested_by || d.opened_by;
    return by ? `${nameResolver(world)(by)} (via ${ag.id})` : ag.id;
  }
  if (ag.type === 'conductor') return 'Conductor';
  if (ag.type === 'approval-service') {
    const by = (action.detail || {}).requested_by;
    return by ? nameResolver(world)(by) : 'Approval desk';
  }
  return (world.agents[ag.id] && world.agents[ag.id].name) || ag.name || ag.id || '?';
}

// Returns the visual effect for the scene to animate, or null.
export function applyAction(world, action, index = -1) {
  if (!action) return null;
  if (action.event_id) {
    if (world.seen.has(action.event_id)) return null;
    world.seen.add(action.event_id);
  }
  if (action.run_id) world.runId = action.run_id;
  if (typeof action.sim_tick === 'number') world.tick = Math.max(world.tick, action.sim_tick);
  if (action.sim_time) world.simTime = action.sim_time;

  const k = action.kind;
  const d = action.detail || {};
  const c = world.counters;
  if (k === 'order.placed') c.orders += 1;
  if (k === 'chat.message') c.chats += 1;
  if (k === 'handoff.sent') c.handoffs += 1;
  if (k === 'approval.requested') { c.approvals += 1; world.pendingApprovals += 1; }
  if (k === 'approval.decided') world.pendingApprovals = Math.max(0, world.pendingApprovals - 1);
  if (k === 'incident.opened') c.incidents += 1;
  if (k === 'fault.injected') c.faults += 1;
  if (k === 'guardrail.flag') c.guardrails += 1;
  if (k === 'tool.result' && d.ok === false) c.failures += 1;
  if (k === 'run.started') { world.runLabel = d.label || ''; world.faults = {}; }

  const a = ensureAgent(world, action.agent);
  const desc = describe(action, nameResolver(world));
  const place = action.to && PLACES[action.to] ? action.to : (a ? a.place : null);

  if (a) {
    if (action.to && PLACES[action.to]) a.place = action.to;
    if (desc && desc.cat !== 'thought' && desc.cat !== 'result' && desc.cat !== 'memory') {
      a.activity = desc.text.charAt(0).toUpperCase() + desc.text.slice(1);
      a.icon = desc.icon;
    }
    if (k === 'tool.call' && d.tool === 'add_to_cart') a.basket += Number((d.args || {}).quantity || 1);
    if (k === 'order.placed') { a.orders += 1; a.basket = 0; }
    if (k === 'thought' && /heading home|done shopping|give up|nothing in my budget/i.test(String(d.text || ''))) a.resting = true;
    if (k === 'thought' && /comes back to the shop/i.test(String(d.text || ''))) { a.resting = false; a.basket = 0; }
  }

  if (desc) {
    world.log.push({
      seq: world.nextSeq++, tick: action.sim_tick ?? 0, agentId: a ? a.id : (action.agent || {}).id,
      who: whoFor(world, action), cat: desc.cat, icon: desc.icon, text: desc.text, important: desc.important,
      place, index, kind: k,
    });
    if (world.log.length > LOG_LIMIT) world.log.splice(0, world.log.length - LOG_LIMIT);
  }

  if (action.action === 'fault' && action.to) world.faults[action.to] = action.text || 'fault';
  if (action.action === 'fault_clear' && action.to) delete world.faults[action.to];

  return { type: action.action, kind: k, agentId: a ? a.id : null, place, text: action.text, desc, detail: d,
           requester: d.requested_by || d.opened_by || null };
}

// Rebuild the world from the start up to (and including) actions[uptoIndex].
export function buildWorldTo(actions, uptoIndex) {
  const world = emptyWorld();
  for (let i = 0; i <= uptoIndex && i < actions.length; i++) applyAction(world, actions[i], i);
  return world;
}

// Index of the last action at or before a tick.
export function lastIndexForTick(actions, tick) {
  let idx = -1;
  for (let i = 0; i < actions.length; i++) {
    if ((actions[i].sim_tick ?? 0) > tick) break;
    idx = i;
  }
  return idx;
}
