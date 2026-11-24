// Turns a sprite action into a plain-English description: category, icon, sentence and a short map label.
// Pure functions only, so replay and live describe events identically.

// The upstream catalogue is fixed (OpenTelemetry Demo 3.1.0), so ids can be shown as names.
export const PRODUCTS = {
  OLJCESPC7Z: 'National Park Foundation Explorascope',
  '66VCHSJNUP': 'Starsense Explorer Refractor Telescope',
  '1YMWWN1N4O': 'Eclipsmart Travel Refractor Telescope',
  L9ECAV7KIM: 'Lens Cleaning Kit',
  '2ZYFJ3GM2N': 'Roof Binoculars',
  '0PUK6V6EV0': 'Solar System Color Imager',
  LS4PSXUNUM: 'Red Flashlight',
  '9SIQT8TOJO': 'Optical Tube Assembly',
  '6E92ZMYYFZ': 'Solar Filter',
  HQTGWGPNH4: 'The Comet Book',
};

export const CATEGORIES = {
  shopping: { label: 'Browsing', colour: '#0984e3', default: true },
  basket: { label: 'Basket', colour: '#00a8a8', default: true },
  order: { label: 'Orders', colour: '#00a046', default: true },
  chat: { label: 'Chats', colour: '#6c5ce7', default: true },
  fraud: { label: 'Fraud', colour: '#8e44ad', default: true },
  approval: { label: 'Approvals', colour: '#e17055', default: true },
  ops: { label: 'Faults and ops', colour: '#d63031', default: true },
  safety: { label: 'Guardrails', colour: '#c0392b', default: true },
  memory: { label: 'Memory and plans', colour: '#7f8c8d', default: false },
  service: { label: 'Shop services', colour: '#636e72', default: false },
  thought: { label: 'Thoughts', colour: '#95a5a6', default: false },
  result: { label: 'Tool results', colour: '#95a5a6', default: false },
  system: { label: 'Run', colour: '#2d3436', default: true },
};

const product = (id) => PRODUCTS[id] || id || 'a product';
const money = (amount, currency) => (amount == null ? '' : `${Number(amount).toFixed(2)} ${currency || 'USD'}`);
const clip = (s, n = 110) => {
  const t = String(s ?? '').replace(/\s+/g, ' ').trim();
  return t.length > n ? `${t.slice(0, n - 1)}…` : t;
};

const TOOL_TEXT = {
  search_products: (a) => ['shopping', '🔍', `searches for "${a.query ?? ''}"`, 'SEARCH'],
  get_product: (a) => ['shopping', '👀', `looks at ${product(a.product_id)}`, 'VIEW'],
  get_recommendations: () => ['shopping', '🎯', 'checks recommendations', 'RECS'],
  get_ads: () => ['shopping', '📣', 'glances at the offers', 'ADS'],
  add_to_cart: (a) => ['basket', '🛒', `adds ${a.quantity && a.quantity > 1 ? `${a.quantity} x ` : ''}${product(a.product_id)} to the basket`, '+ BASKET'],
  view_cart: () => ['basket', '🧺', 'checks the basket', 'BASKET'],
  get_shipping_quote: () => ['basket', '📦', 'asks for a delivery quote', 'DELIVERY?'],
  convert_currency: () => ['basket', '💱', 'converts a price', 'FX'],
  checkout: () => ['order', '💳', 'goes to checkout and pays', 'CHECKOUT'],
  // assistant (store-ops-mcp)
  search_catalogue: (a) => ['chat', '📚', `looks up "${a.query ?? ''}" in the catalogue`, 'LOOKUP'],
  get_reviews: (a) => ['chat', '⭐', `reads reviews of ${product(a.product_id)}`, 'REVIEWS'],
  get_order_status: () => ['chat', '📋', 'checks an order status', 'ORDER?'],
  cart_get: () => ['chat', '🧺', "checks the customer's basket", 'BASKET'],
  cart_add: () => ['chat', '🛒', "adds to the customer's basket", '+ BASKET'],
  // fraud
  get_order: () => ['fraud', '📄', 'pulls the order record', 'ORDER'],
  get_order_history: () => ['fraud', '🗂', 'checks order history for the address', 'HISTORY'],
  get_payment_signals: () => ['fraud', '💳', 'checks payment signals', 'SIGNALS'],
  get_fraud_verdict: () => ['fraud', '⚖', 'scores the order for fraud', 'SCORE'],
  hold_order: () => ['fraud', '⏸', 'places a hold on the order', 'HOLD'],
  release_order: () => ['fraud', '▶', 'releases the order', 'RELEASE'],
  escalate_to_human: (a) => ['approval', '🧾', `asks a human to approve ${String(a.action || 'a hold').replace('_', ' ')}`, 'ASK HUMAN'],
  // sre
  read_flags: () => ['ops', '🚩', 'reads the feature flags', 'FLAGS'],
  query_metrics: () => ['ops', '📈', 'checks error-rate metrics', 'METRICS'],
  query_traces: (a) => ['ops', '🧵', `pulls error traces for ${a.service ?? 'a service'}`, 'TRACES'],
  search_logs: (a) => ['ops', '📜', `searches logs for ${a.service ?? 'a service'}`, 'LOGS'],
  open_incident: () => ['ops', '🚨', 'opens an incident', 'INCIDENT'],
  propose_flag_change: (a) => ['approval', '🧾', `proposes turning ${a.flag ?? 'a flag'} ${a.variant ?? 'off'} (needs approval)`, 'PROPOSE'],
};

// Returns { cat, icon, text, pill, important } where text excludes the agent name.
export function describe(action, nameOf = (id) => id) {
  const d = action.detail || {};
  const args = d.args || {};
  const k = action.kind;
  const tool = d.tool;
  const r = (cat, icon, text, pill = null) => ({ cat, icon, text, pill, important: CATEGORIES[cat]?.default ?? true });

  switch (k) {
    case 'run.started': return r('system', '▶', `Run started${d.label ? `: ${d.label}` : ''} (seed ${d.seed ?? '?'})`);
    case 'run.finished': return r('system', '⏹', `Run finished: ${d.orders ?? '?'} orders, ${d.chats ?? '?'} chats, ${d.incidents ?? '?'} incidents`);
    case 'clock.tick': return null;
    case 'plan.created': return r('memory', '🗒', `plans the day: ${clip((d.plan || []).join('; '), 90)}`);
    case 'memory.reflect': return r('memory', '🌙', `reflects overnight and forms ${(d.beliefs || []).length || 'new'} beliefs`, 'REFLECT');
    case 'memory.write': return r('memory', '🧠', `remembers: ${clip(d.content || d.kind || '', 90)}`);
    case 'thought': {
      const t = String(d.text || action.text || '');
      if (/comes back to the shop/i.test(t)) return r('shopping', '🚶', 'comes back for another visit', 'BACK');
      if (/done shopping|give up|heading home|nothing in my budget/i.test(t)) return r('shopping', '🏠', `heads home (${clip(t, 60)})`, 'HOME');
      if (/arrives at the shop/i.test(t)) return r('shopping', '🚪', 'arrives at the shop', 'ARRIVES');
      if (/alert received/i.test(t)) return r('ops', '🔔', clip(t), 'ALERT');
      return r('thought', '💭', `thinks: "${clip(t, 90)}"`);
    }
    case 'tool.call': {
      const f = TOOL_TEXT[tool];
      if (f) { const [cat, icon, text, pill] = f(args); return r(cat, icon, text, pill); }
      return r('result', '🔧', `calls ${tool}`);
    }
    case 'tool.result': {
      if (d.ok === false) return { ...r('order', '❌', `${tool} failed: ${clip(d.summary || '', 80)}`, 'FAILED'), important: true };
      return r('result', '↩', `${tool} returned ${clip(d.summary || 'ok', 80)}`);
    }
    case 'order.placed': return r('order', '✅', `places an order${d.total != null ? ` for ${money(d.total, d.currency)}` : ''}`, 'ORDER ✓');
    case 'chat.message': {
      const said = `"${clip(d.text || action.text, 120)}"`;
      return d.role === 'user'
        ? r('chat', '💬', `asks the Shopping Assistant: ${said}`, 'ASKS')
        : r('chat', '💬', `replies to ${nameOf(d.to)}: ${said}`, 'REPLIES');
    }
    case 'handoff.sent': return r('fraud', '🤝', `hands a case to Fraud Review: ${clip(d.reason, 90)}`, 'HANDOFF');
    case 'handoff.received': return r('fraud', '📥', `picks up the case: ${clip(d.reason, 90)}`, 'CASE');
    case 'approval.requested': return r('approval', '🧾', `needs a human to approve ${String(d.kind || '').replace('_', ' ')} for ${clip(d.subject, 40)}`, 'APPROVAL?');
    case 'approval.decided': return r('approval', d.decision === 'approved' ? '✔' : '✖', `${d.decision} ${String(d.kind || '').replace('_', ' ')} for ${clip(d.subject, 40)}`, d.decision === 'approved' ? 'APPROVED' : 'REJECTED');
    case 'fault.injected': return r('ops', '⚠', `injects fault ${d.flag} (${d.variant ?? 'on'})`, 'FAULT');
    case 'fault.cleared': return r('ops', '🔧', `fault ${d.flag} cleared${d.by ? ` (${d.by})` : ''}`, 'FIXED');
    case 'incident.opened': return r('ops', '🚨', `incident opened: ${clip(d.title, 90)}`, 'INCIDENT');
    case 'order.held': return r('fraud', '⏸', `order held: ${clip(d.reason, 80)}`, 'HELD');
    case 'order.released': return r('fraud', '▶', `order released: ${clip(d.reason, 80)}`, 'RELEASED');
    case 'guardrail.flag': return { ...r('safety', '🛡', `guardrail ${d.stage === 'output' ? 'blocked a reply' : 'flagged input'}: ${clip(d.reason, 80)}`, 'BLOCKED'), important: true };
    case 'error': return r('order', '❗', `error: ${clip(d.error || d.detail, 90)}`, 'ERROR');
    case 'span': return r('service', '⚙', `${d.service ?? 'service'}: ${d.operation ?? action.text ?? ''}`);
    default: return r('result', '•', clip(action.text || k, 90));
  }
}
