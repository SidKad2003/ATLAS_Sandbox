// Store town layout (logical 1200 x 760 canvas). All geometry is drawn procedurally.
export const WIDTH = 1200;
export const HEIGHT = 760;

export const PALETTE = {
  grass: 0xdfe9d8,
  path: 0xcfc6b4,
  storeFloor: 0xf6f1e7,
  storeWall: 0x5b6573,
  aisle: 0xe7dcc6,
  aisleShelf: 0xb99b6b,
  desk: 0xd5e3f0,
  back: 0xe2e2dc,
  office: 0xe9e1f0,
  home: 0xf3dcc8,
  roof: 0xb86f52,
  label: '#2f3640',
  labelLight: '#ffffff',
  fault: 0xd63031,
};

// Each place: rectangle, fill colour, label, zone.
export const PLACES = {
  home: { x: 30, y: 60, w: 190, h: 650, fill: PALETTE.home, label: 'Homes', zone: 'town' },
  'aisle:telescopes': { x: 270, y: 110, w: 110, h: 200, fill: PALETTE.aisle, label: 'Telescopes', zone: 'floor' },
  'aisle:binoculars': { x: 390, y: 110, w: 110, h: 200, fill: PALETTE.aisle, label: 'Binoculars', zone: 'floor' },
  'aisle:accessories': { x: 510, y: 110, w: 110, h: 200, fill: PALETTE.aisle, label: 'Accessories', zone: 'floor' },
  'aisle:books': { x: 630, y: 110, w: 110, h: 200, fill: PALETTE.aisle, label: 'Books', zone: 'floor' },
  'aisle:other': { x: 750, y: 110, w: 110, h: 200, fill: PALETTE.aisle, label: 'Travel and gadgets', zone: 'floor' },
  'help-desk': { x: 280, y: 350, w: 170, h: 90, fill: PALETTE.desk, label: 'Help desk', zone: 'store' },
  checkout: { x: 680, y: 350, w: 180, h: 90, fill: PALETTE.desk, label: 'Checkout', zone: 'store' },
  entrance: { x: 500, y: 455, w: 140, h: 60, fill: PALETTE.path, label: 'Entrance', zone: 'store' },
  warehouse: { x: 920, y: 60, w: 250, h: 230, fill: PALETTE.back, label: 'Warehouse', zone: 'back' },
  'shipping-dock': { x: 920, y: 310, w: 250, h: 150, fill: PALETTE.back, label: 'Shipping dock', zone: 'back' },
  'fraud-office': { x: 260, y: 570, w: 190, h: 140, fill: PALETTE.office, label: 'Fraud office', zone: 'offices' },
  'ops-room': { x: 480, y: 570, w: 190, h: 140, fill: PALETTE.office, label: 'Ops room', zone: 'offices' },
  'approval-desk': { x: 700, y: 570, w: 190, h: 140, fill: PALETTE.office, label: 'Approval desk', zone: 'offices' },
};

// The store building outline that groups the shop floor, desks and entrance.
export const STORE = { x: 255, y: 60, w: 620, h: 455 };

export const HOME_BY_TYPE = {
  shopper: 'home',
  assistant: 'help-desk',
  fraud: 'fraud-office',
  sre: 'ops-room',
  conductor: 'ops-room',
  'approval-service': 'approval-desk',
  human: 'approval-desk',
  service: 'warehouse',
  'mcp-server': 'fraud-office',
};

export const TYPE_COLOURS = {
  assistant: 0x0984e3,
  fraud: 0x6c5ce7,
  sre: 0x00897b,
  conductor: 0x2d3436,
  service: 0x636e72,
  'approval-service': 0xe17055,
  human: 0xe17055,
  'mcp-server': 0x8e44ad,
};

export const SHOPPER_COLOURS = [0xe84393, 0xf0a500, 0x00b894, 0xff7675, 0x3d8bfd, 0xa29bfe, 0xfab1a0, 0x55efc4, 0xffeaa7, 0x81ecec];

export const ROLE_LABELS = {
  shopper: 'Shopper', assistant: 'Shopping Assistant', fraud: 'Fraud Review', sre: 'SRE / Ops', conductor: 'Conductor',
  'approval-service': 'Approval desk', human: 'Human approver', service: 'Shop services', 'mcp-server': 'MCP server',
};

// One colour per agent, shared by the map and the side panel.
export function agentColour(agent) {
  if (agent.type === 'shopper') {
    const idx = parseInt(String(agent.id).replace(/\D/g, ''), 10);
    const n = Number.isFinite(idx) ? idx - 1 : 0;
    return SHOPPER_COLOURS[((n % SHOPPER_COLOURS.length) + SHOPPER_COLOURS.length) % SHOPPER_COLOURS.length];
  }
  return TYPE_COLOURS[agent.type] ?? 0x2d3436;
}

export const cssColour = (n) => `#${n.toString(16).padStart(6, '0')}`;

// Deterministic slot positions so several agents at one place do not overlap.
export function slotPosition(placeId, slot) {
  const p = PLACES[placeId] || PLACES.home;
  const cols = Math.max(1, Math.floor((p.w - 20) / 34));
  const col = slot % cols;
  const row = Math.floor(slot / cols);
  return {
    x: p.x + 24 + col * 34,
    y: p.y + 44 + (row * 34) % Math.max(34, p.h - 50),
  };
}
