// Phaser scene: draws the store town procedurally and renders the world model with explicit action cues.
import { HEIGHT, PALETTE, PLACES, STORE, WIDTH, agentColour, slotPosition } from './layout.js';
import { CATEGORIES } from './describe.js';

const Phaser = window.Phaser;
const FONT = 'system-ui, "Segoe UI", "Segoe UI Emoji", "Apple Color Emoji", sans-serif';
const LABEL_STYLE = { fontFamily: FONT, fontSize: '14px', color: PALETTE.label, fontStyle: 'bold' };
const TAG_STYLE = { fontFamily: FONT, fontSize: '11px', color: '#2f3640', backgroundColor: 'rgba(255,255,255,0.9)', padding: { x: 3, y: 1 } };
const BUBBLE_STYLE = { fontFamily: FONT, fontSize: '12px', color: '#1e272e', backgroundColor: '#ffffff', padding: { x: 7, y: 5 }, wordWrap: { width: 210 } };
const hex = (css) => parseInt(String(css).replace('#', ''), 16);

export class TownScene extends Phaser.Scene {
  constructor() {
    super('town');
    this.sprites = {};
    this.faultOverlays = {};
    this.badges = {};
    this.speed = 1;
    this.onAgentClick = null;
  }

  create() {
    this.drawMap();
    if (this.onReady) this.onReady(this);
  }

  // ---------------- map ----------------
  drawMap() {
    const g = this.add.graphics();
    g.fillStyle(PALETTE.grass, 1).fillRect(0, 0, WIDTH, HEIGHT);
    g.fillStyle(PALETTE.path, 1);
    g.fillRect(220, 520, 980, 34);
    g.fillRect(555, 515, 30, 60);
    g.fillRect(875, 380, 45, 20);
    g.fillStyle(PALETTE.storeFloor, 1).fillRoundedRect(STORE.x, STORE.y, STORE.w, STORE.h, 10);
    g.lineStyle(4, PALETTE.storeWall, 1).strokeRoundedRect(STORE.x, STORE.y, STORE.w, STORE.h, 10);
    this.add.text(STORE.x + 12, STORE.y + 10, 'Astronomy Shop', { ...LABEL_STYLE, fontSize: '16px' });
    g.lineStyle(3, PALETTE.storeWall, 0.6).strokeRoundedRect(910, 50, 270, 420, 10);
    this.add.text(922, 470, 'Back of house', { ...LABEL_STYLE, fontSize: '12px', color: '#636e72' });
    g.lineStyle(3, PALETTE.storeWall, 0.6).strokeRoundedRect(250, 560, 650, 160, 10);
    this.add.text(262, 724, 'Offices (agents and humans)', { ...LABEL_STYLE, fontSize: '12px', color: '#636e72' });

    this.placeFlash = {};
    for (const [id, p] of Object.entries(PLACES)) {
      g.fillStyle(p.fill, 1).fillRoundedRect(p.x, p.y, p.w, p.h, 8);
      g.lineStyle(1, 0x95a5a6, 1).strokeRoundedRect(p.x, p.y, p.w, p.h, 8);
      if (id.startsWith('aisle:')) {
        g.fillStyle(PALETTE.aisleShelf, 1);
        g.fillRect(p.x + 8, p.y + p.h - 26, p.w - 16, 10);
        g.fillRect(p.x + 8, p.y + p.h - 46, p.w - 16, 10);
      }
      if (id === 'home') this.drawHouses(g, p);
      if (id === 'warehouse') this.drawCrates(g, p);
      this.add.text(p.x + 8, p.y + 8, p.label, LABEL_STYLE);

      const flash = this.add.graphics().setDepth(4).setAlpha(0);
      flash.lineStyle(5, 0xf0a500, 1).strokeRoundedRect(p.x - 3, p.y - 3, p.w + 6, p.h + 6, 10);
      this.placeFlash[id] = flash;

      const badge = this.add.text(p.x + p.w - 6, p.y + 6, '', { ...TAG_STYLE, fontStyle: 'bold' }).setOrigin(1, 0).setDepth(6).setVisible(false);
      this.badges[id] = badge;

      const overlay = this.add.container(p.x, p.y).setVisible(false).setDepth(5);
      const og = this.add.graphics();
      og.fillStyle(PALETTE.fault, 0.22).fillRoundedRect(0, 0, p.w, p.h, 8);
      og.lineStyle(3, PALETTE.fault, 1).strokeRoundedRect(0, 0, p.w, p.h, 8);
      const tag = this.add.text(8, p.h - 22, '⚠ Broken', { ...TAG_STYLE, color: '#ffffff', backgroundColor: '#d63031' });
      overlay.add([og, tag]);
      overlay.tag = tag;
      this.faultOverlays[id] = overlay;
      this.tweens.add({ targets: og, alpha: { from: 1, to: 0.4 }, duration: 700, yoyo: true, repeat: -1 });
    }
  }

  drawHouses(g, p) {
    for (let i = 0; i < 5; i++) {
      const hx = p.x + 20 + (i % 2) * 80;
      const hy = p.y + 60 + i * 115;
      g.fillStyle(0xffffff, 1).fillRect(hx, hy + 18, 60, 40);
      g.fillStyle(PALETTE.roof, 1).fillTriangle(hx - 6, hy + 20, hx + 30, hy - 6, hx + 66, hy + 20);
      g.fillStyle(0x8d6e63, 1).fillRect(hx + 24, hy + 36, 12, 22);
    }
  }

  drawCrates(g, p) {
    g.fillStyle(0xc49a6c, 1);
    for (let i = 0; i < 6; i++) g.fillRect(p.x + 130 + (i % 3) * 36, p.y + 120 + Math.floor(i / 3) * 36, 30, 30);
  }

  // ---------------- sprites ----------------
  makeSprite(agent) {
    const c = this.add.container(0, 0).setDepth(10);
    const g = this.add.graphics();
    const colour = agentColour(agent);
    g.fillStyle(0x000000, 0.15).fillEllipse(0, 13, 22, 7);
    g.fillStyle(colour, 1).fillCircle(0, 0, 12);
    g.lineStyle(2, 0xffffff, 1).strokeCircle(0, 0, 12);
    const letter = (agent.type === 'shopper' ? (agent.name || agent.id) : ({ assistant: 'A', fraud: 'F', sre: 'S', human: 'H', service: '⚙' }[agent.type] || agent.type)).charAt(0).toUpperCase();
    const t = this.add.text(0, 0, letter, { ...LABEL_STYLE, fontSize: '12px', color: '#ffffff' }).setOrigin(0.5);
    const tag = this.add.text(0, 16, agent.name || agent.id, TAG_STYLE).setOrigin(0.5, 0);
    const icon = this.add.text(13, -14, '', { fontFamily: FONT, fontSize: '13px' }).setOrigin(0.5);
    c.add([g, t, tag, icon]);
    c.tag = tag;
    c.icon = icon;
    c.setSize(28, 28);
    c.setInteractive({ useHandCursor: true });
    c.on('pointerdown', () => this.onAgentClick && this.onAgentClick(agent.id));
    return c;
  }

  positionsFor(world) {
    const byPlace = {};
    const pos = {};
    for (const a of Object.values(world.agents).sort((x, y) => x.order - y.order)) {
      const slot = byPlace[a.place] || 0;
      byPlace[a.place] = slot + 1;
      pos[a.id] = slotPosition(a.place, slot);
    }
    return pos;
  }

  syncWorld(world, animate) {
    const pos = this.positionsFor(world);
    for (const a of Object.values(world.agents)) {
      let s = this.sprites[a.id];
      const target = pos[a.id];
      if (!s) {
        s = this.makeSprite(a);
        s.setPosition(target.x, target.y);
        this.sprites[a.id] = s;
      }
      s.tag.setText(a.name);
      s.icon.setText(a.resting ? '💤' : (a.basket > 0 ? '🛒' : ''));
      if (s.x === target.x && s.y === target.y) continue;
      this.tweens.killTweensOf(s);
      if (animate) this.tweens.add({ targets: s, x: target.x, y: target.y, duration: 650 / this.speed, ease: 'Sine.easeInOut' });
      else s.setPosition(target.x, target.y);
    }
    for (const id of Object.keys(this.sprites)) {
      if (!world.agents[id]) { this.destroySprite(id); }
    }
    for (const [placeId, overlay] of Object.entries(this.faultOverlays)) {
      const label = world.faults[placeId];
      overlay.setVisible(Boolean(label));
      if (label) overlay.tag.setText(`⚠ Broken: ${label}`.slice(0, 42));
    }
    this.setBadge('approval-desk', world.pendingApprovals ? `🧾 ${world.pendingApprovals} waiting for a human` : '');
    this.setBadge('ops-room', world.counters.incidents ? `🚨 ${world.counters.incidents} incident${world.counters.incidents > 1 ? 's' : ''}` : '');
    this.setBadge('checkout', world.counters.orders ? `✅ ${world.counters.orders} orders` : '');
    this.setBadge('fraud-office', world.counters.handoffs ? `🤝 ${world.counters.handoffs} case${world.counters.handoffs > 1 ? 's' : ''}` : '');
  }

  setBadge(placeId, text) {
    const b = this.badges[placeId];
    if (!b) return;
    b.setText(text).setVisible(Boolean(text));
  }

  destroySprite(id) {
    const s = this.sprites[id];
    if (!s) return;
    if (s.bubble) s.bubble.destroy();
    if (s.ring) s.ring.destroy();
    s.destroy();
    delete this.sprites[id];
  }

  // ---------------- cues ----------------
  pointFor(ref) {
    if (this.sprites[ref]) return { x: this.sprites[ref].x, y: this.sprites[ref].y };
    const p = PLACES[ref];
    return p ? { x: p.x + p.w / 2, y: p.y + p.h / 2 } : null;
  }

  // Floating label above an agent describing what they just did.
  pill(agentId, text, colour = '#2d3436') {
    const s = this.sprites[agentId];
    if (!s || !text) return;
    if (s.pill) s.pill.destroy();
    const p = this.add.text(s.x, s.y - 26, text, { ...TAG_STYLE, fontSize: '11px', fontStyle: 'bold', color: '#ffffff', backgroundColor: colour, padding: { x: 5, y: 2 } })
      .setOrigin(0.5, 1).setDepth(21);
    s.pill = p;
    this.tweens.add({ targets: p, y: p.y - 14, alpha: { from: 1, to: 0 }, delay: 1100 / this.speed, duration: 900 / this.speed,
      onUpdate: () => { if (p.active) p.x = s.x; }, onComplete: () => { p.destroy(); if (s.pill === p) s.pill = null; } });
  }

  speak(agentId, text) {
    const s = this.sprites[agentId];
    if (!s || !text) return;
    if (s.bubble) s.bubble.destroy();
    const b = this.add.text(s.x, s.y - 28, `💬 ${text}`, BUBBLE_STYLE).setOrigin(0.5, 1).setDepth(20);
    s.bubble = b;
    const follow = this.time.addEvent({ delay: 50, loop: true, callback: () => b.active && b.setPosition(s.x, s.y - 28) });
    this.time.delayedCall(Math.max(1200, 4000 / this.speed), () => {
      follow.remove();
      if (b.active) b.destroy();
      if (s.bubble === b) s.bubble = null;
    });
  }

  // A dashed line linking two agents or places for a moment (e.g. a chat).
  link(fromRef, toRef, colour = 0x6c5ce7) {
    const a = this.pointFor(fromRef);
    const b = this.pointFor(toRef);
    if (!a || !b) return;
    const g = this.add.graphics().setDepth(8);
    g.lineStyle(3, colour, 0.8);
    const steps = 18;
    for (let i = 0; i < steps; i += 2) {
      const t0 = i / steps;
      const t1 = (i + 1) / steps;
      g.lineBetween(a.x + (b.x - a.x) * t0, a.y + (b.y - a.y) * t0, a.x + (b.x - a.x) * t1, a.y + (b.y - a.y) * t1);
    }
    this.tweens.add({ targets: g, alpha: 0, delay: 1500 / this.speed, duration: 800 / this.speed, onComplete: () => g.destroy() });
  }

  // A labelled token travelling from one agent or place to another (handoffs, approvals, orders).
  packet(fromRef, toRef, label, colour = '#8e44ad') {
    const a = this.pointFor(fromRef);
    const b = this.pointFor(toRef);
    if (!a || !b) return;
    this.link(fromRef, toRef, hex(colour));
    const t = this.add.text(a.x, a.y, label, { ...TAG_STYLE, fontStyle: 'bold', color: '#ffffff', backgroundColor: colour, padding: { x: 6, y: 3 } })
      .setOrigin(0.5).setDepth(22);
    this.tweens.add({ targets: t, x: b.x, y: b.y, duration: 1400 / this.speed, ease: 'Sine.easeInOut',
      onComplete: () => this.tweens.add({ targets: t, alpha: 0, duration: 500 / this.speed, onComplete: () => t.destroy() }) });
  }

  flashPlace(placeId) {
    const f = this.placeFlash && this.placeFlash[placeId];
    if (!f) return;
    this.tweens.killTweensOf(f);
    f.setAlpha(1);
    this.tweens.add({ targets: f, alpha: 0, duration: 1600, ease: 'Quad.easeIn' });
  }

  // Pulsing ring that marks the selected agent until something else is selected.
  highlight(agentId) {
    for (const s of Object.values(this.sprites)) if (s.ring) { s.ring.destroy(); s.ring = null; }
    const s = this.sprites[agentId];
    if (!s) return;
    const ring = this.add.graphics().setDepth(9);
    ring.lineStyle(3, 0xf0a500, 1).strokeCircle(0, 0, 19);
    s.ring = ring;
    const follow = () => { if (ring.active) ring.setPosition(s.x, s.y); };
    follow();
    this.tweens.add({ targets: ring, scale: { from: 1, to: 1.35 }, alpha: { from: 1, to: 0.35 }, duration: 600, yoyo: true, repeat: -1, onUpdate: follow });
  }

  // Animate the cue for one applied action.
  animate(effect) {
    if (!effect || !effect.desc) return;
    const { desc, kind, agentId, detail } = effect;
    const colour = (CATEGORIES[desc.cat] || {}).colour || '#2d3436';
    if (kind === 'chat.message') {
      this.speak(agentId, effect.text);
      const other = detail.role === 'user' ? 'assistant' : detail.to;
      if (agentId && other) this.link(agentId, other, 0x6c5ce7);
      return;
    }
    if (kind === 'handoff.sent') this.packet(agentId || 'help-desk', 'fraud-office', '🤝 case', colour);
    if (kind === 'approval.requested') this.packet(effect.requester || 'fraud-office', 'approval-desk', '🧾 approval', colour);
    if (kind === 'approval.decided') this.packet('approval-desk', detail.kind === 'flag_change' ? 'ops-room' : 'fraud-office', desc.icon, colour);
    if (kind === 'order.placed') { this.packet(agentId || 'checkout', 'warehouse', '📦 order', '#00a046'); this.flashPlace('checkout'); }
    if (kind === 'fault.injected' || kind === 'fault.cleared') this.flashPlace(effect.place);
    if (kind === 'incident.opened') this.flashPlace('ops-room');
    if (kind === 'guardrail.flag') this.flashPlace('help-desk');
    if (agentId && desc.pill && desc.important) this.pill(agentId, `${desc.icon} ${desc.pill}`, colour);
    else if (agentId && desc.pill && (desc.cat === 'memory')) this.pill(agentId, `${desc.icon} ${desc.pill}`, colour);
  }

  clear() {
    for (const id of Object.keys(this.sprites)) this.destroySprite(id);
    this.sprites = {};
  }
}
