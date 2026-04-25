// Creature-collect UI: the bottom-right monster-ball button and the
// inventory sheet it opens. Gated by the "Creature mode" setting
// (cc.creatureMode, default on). No spawn logic yet — the inventory is
// seeded with two dummy creatures so the UI has something to show.
//
//   Creatures.install(map) -> { setEnabled(on), isEnabled(), show(), hide() }
//
// Mirrors the trip-planner.js factory shape so index.html stays the only
// place that wires modules together.

(function (global) {
  'use strict';

  const STORAGE_KEY = 'cc.creatureMode';
  const CAPTURED_KEY = 'cc.capturedCreatures';
  const CAUGHT_SPAWNS_KEY = 'cc.caughtSpawnIds';

  // Captured inventory lives as an array of entries keyed by their own
  // `id`. We intentionally store speciesA/B (not the derived display
  // name) so names update if/when the species-names list loads later.
  function readCapturedCreatures() {
    try {
      const raw = localStorage.getItem(CAPTURED_KEY);
      return raw ? JSON.parse(raw) : [];
    } catch { return []; }
  }
  function writeCapturedCreatures(arr) {
    localStorage.setItem(CAPTURED_KEY, JSON.stringify(arr));
  }

  // Caught spawn IDs — once a spawn has been captured locally, we don't
  // want its marker to keep reappearing until the time bucket rotates.
  // Other players on other devices still see the spawn (no server).
  function readCaughtSpawnIds() {
    try {
      const raw = localStorage.getItem(CAUGHT_SPAWNS_KEY);
      return raw ? new Set(JSON.parse(raw)) : new Set();
    } catch { return new Set(); }
  }
  function writeCaughtSpawnIds(set) {
    localStorage.setItem(CAUGHT_SPAWNS_KEY, JSON.stringify([...set]));
  }
  function markSpawnCaught(spawnId) {
    const set = readCaughtSpawnIds();
    set.add(spawnId);
    writeCaughtSpawnIds(set);
  }
  // Prune caught-spawn IDs whose creature has already aged out of the
  // sliding window. Spawn IDs encode the birth-tick — anything older
  // than the current tick minus LIFETIME is already invisible and
  // just bloats the Set. Runs once per refresh.
  function pruneCaughtSpawnIds() {
    if (!global.Spawns || !global.Spawns.isSpawnIdStale) return;
    const set = readCaughtSpawnIds();
    let changed = false;
    for (const id of set) {
      if (global.Spawns.isSpawnIdStale(id)) {
        set.delete(id);
        changed = true;
      }
    }
    if (changed) writeCaughtSpawnIds(set);
  }

  // Inventory view: captured creatures, normalized to the shape the
  // render/sort/search code already expects (id, name, level, sizeM,
  // plus speciesA/B and caughtAt for the detail view).
  function getInventoryCreatures() {
    return readCapturedCreatures().map((e) => ({
      id: e.id,
      speciesA: e.speciesA,
      speciesB: e.speciesB,
      level: e.level,
      sizeM: e.sizeM,
      name: fusionName(e.speciesA, e.speciesB),
      caughtAt: e.caughtAt,
    }));
  }

  function fusionName(a, b) {
    if (global.Species) {
      return `${global.Species.nameFor(a)} × ${global.Species.nameFor(b)}`;
    }
    return `#${a} × #${b}`;
  }

  function formatSize(sizeM) {
    if (sizeM == null) return '';
    const imperial = localStorage.getItem('cc.units') === 'mi';
    if (imperial) {
      const inches = sizeM * 39.3701;
      if (inches < 12) return `${Math.round(inches)} in`;
      const feet = inches / 12;
      return feet < 10 ? `${feet.toFixed(1)} ft` : `${Math.round(feet)} ft`;
    }
    if (sizeM < 1) return `${Math.round(sizeM * 100)} cm`;
    return sizeM < 10 ? `${sizeM.toFixed(1)} m` : `${Math.round(sizeM)} m`;
  }

  function readEnabled() {
    const v = localStorage.getItem(STORAGE_KEY);
    return v === null || v === '1';
  }

  function writeEnabled(on) {
    localStorage.setItem(STORAGE_KEY, on ? '1' : '0');
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (ch) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    })[ch]);
  }

  // Nicknames are keyed by creature id and stored as a JSON map so a
  // single entry can be cleared ("reset to species name") by deleting the
  // key without disturbing the others.
  function readNicknames() {
    try {
      const raw = localStorage.getItem('cc.creatureNicknames');
      return raw ? JSON.parse(raw) : {};
    } catch { return {}; }
  }
  function writeNickname(id, nickname) {
    const map = readNicknames();
    const trimmed = (nickname || '').trim();
    if (trimmed) map[id] = trimmed; else delete map[id];
    localStorage.setItem('cc.creatureNicknames', JSON.stringify(map));
  }
  function displayName(c) {
    return readNicknames()[c.id] || c.name;
  }
  function findCreature(id) {
    return getInventoryCreatures().find((c) => c.id === id) || null;
  }

  const SORT_KEYS = new Set(['level', 'size', 'name', 'species']);
  const SORT_DIRS = new Set(['asc', 'desc']);

  function readSortKey() {
    const v = localStorage.getItem('cc.creatureSortBy');
    return SORT_KEYS.has(v) ? v : 'level';
  }
  function readSortDir() {
    const v = localStorage.getItem('cc.creatureSortDir');
    return SORT_DIRS.has(v) ? v : 'desc';
  }

  function sortedCreatures() {
    const key = readSortKey();
    const dir = readSortDir();
    const sign = dir === 'asc' ? 1 : -1;
    const copy = getInventoryCreatures();
    copy.sort((a, b) => {
      if (key === 'name') {
        return sign * displayName(a).localeCompare(displayName(b));
      }
      if (key === 'species') {
        return sign * a.name.localeCompare(b.name);
      }
      const field = key === 'size' ? 'sizeM' : 'level';
      const av = a[field], bv = b[field];
      // Missing values sort to the end regardless of direction.
      if (av == null && bv == null) return 0;
      if (av == null) return 1;
      if (bv == null) return -1;
      return sign * (av - bv);
    });
    return copy;
  }

  function injectStyles() {
    if (document.getElementById('creature-styles')) return;
    const s = document.createElement('style');
    s.id = 'creature-styles';
    s.textContent = `
      #creatureInventory {
        position: fixed; inset: 0; z-index: 30;
        background: rgba(0,0,0,0.45);
        display: none; align-items: center; justify-content: center;
      }
      #creatureInventory.show { display: flex; }
      #creatureInventory .sheet {
        width: calc(100% - 40px); max-width: 360px;
        padding: 18px 20px 14px;
        max-height: 85vh; overflow-y: auto;
        background: var(--ui-bg, #fff);
        color: var(--ui-text, #111);
        border: 1px solid var(--ui-border, rgba(0,0,0,0.15));
        border-radius: var(--ui-radius, 8px);
        box-shadow: 0 8px 24px rgba(0,0,0,0.25);
      }
      #creatureInventory h3 { margin: 0 0 14px; font-size: 16px; }
      #creatureInventory .sort-row {
        display: flex; align-items: center; gap: 8px;
        margin: 0 0 10px;
        font-size: 13px;
      }
      #creatureInventory .sort-row label {
        color: var(--ui-muted, #666);
      }
      #creatureInventory .sort-row select {
        padding: 4px 6px; font-size: 13px;
        background: var(--ui-bg, #fff);
        color: var(--ui-text, #111);
        border: 1px solid var(--ui-border, rgba(0,0,0,0.15));
        border-radius: var(--ui-radius, 8px);
      }
      #creatureInventory .sort-row button.dir {
        padding: 4px 10px; font-size: 13px;
        background: var(--ui-bg, #fff);
        color: var(--ui-text, #111);
        border: 1px solid var(--ui-border, rgba(0,0,0,0.15));
        border-radius: var(--ui-radius, 8px);
        cursor: pointer;
        min-width: 32px;
      }
      #creatureInventory .search-row {
        margin: 0 0 10px;
      }
      #creatureInventory .search-row input {
        width: 100%; box-sizing: border-box;
        padding: 6px 10px; font-size: 13px;
        background: var(--ui-bg, #fff);
        color: var(--ui-text, #111);
        border: 1px solid var(--ui-border, rgba(0,0,0,0.15));
        border-radius: var(--ui-radius, 8px);
      }
      #creatureInventory .creature-list {
        display: grid;
        grid-template-columns: repeat(3, 1fr);
        gap: 8px;
      }
      #creatureInventory .creature-card {
        display: flex; flex-direction: column; align-items: center; gap: 6px;
        padding: 10px 6px;
        background: var(--ui-hover, rgba(0,0,0,0.04));
        border-radius: var(--ui-radius, 8px);
        cursor: pointer;
        border: 1px solid transparent;
        transition: transform 0.08s ease, border-color 0.08s ease;
      }
      #creatureInventory .creature-card:hover {
        border-color: var(--ui-accent, #888);
        transform: translateY(-1px);
      }
      #creatureInventory .creature-card .art {
        width: 100%; aspect-ratio: 1 / 1;
        display: flex; align-items: center; justify-content: center;
        background: var(--ui-bg, #fff);
        border: 1px solid var(--ui-hairline, rgba(0,0,0,0.08));
        border-radius: var(--ui-radius, 8px);
        font-size: 40px; line-height: 1;
        overflow: hidden;
      }
      #creatureInventory .creature-card .art img.art-img {
        width: 100%; height: 100%; object-fit: contain; display: none;
        image-rendering: pixelated; image-rendering: crisp-edges;
      }
      #creatureInventory .creature-card .art .art-placeholder {
        font-size: 40px; line-height: 1;
      }
      #creatureInventory .creature-card .name {
        font-size: 13px; text-align: center; line-height: 1.2;
        word-break: break-word;
      }
      #creatureInventory .creature-card .stats {
        display: flex; justify-content: center; gap: 6px;
        font-size: 11px; color: var(--ui-muted, #666);
      }
      #creatureInventory .creature-card .stats .sep {
        opacity: 0.5;
      }
      #creatureInventory .creature-empty {
        text-align: center; font-size: 13px;
        color: var(--ui-muted, #666);
        padding: 24px 12px;
      }
      #creatureInventory .actions {
        display: flex; justify-content: flex-end; margin-top: 14px;
      }
      #creatureInventory button.close {
        padding: 8px 14px; font-size: 14px; cursor: pointer;
      }
      #creatureInventory .detail-view { display: none; }
      #creatureInventory .detail-view.show { display: block; }
      #creatureInventory .detail-back {
        padding: 4px 8px; font-size: 13px; cursor: pointer;
        background: transparent;
        color: var(--ui-text, #111);
        border: 1px solid var(--ui-border, rgba(0,0,0,0.15));
        border-radius: var(--ui-radius, 8px);
        margin-bottom: 10px;
      }
      #creatureInventory .detail-art {
        width: 140px; height: 140px; margin: 4px auto 12px;
        display: flex; align-items: center; justify-content: center;
        background: var(--ui-bg, #fff);
        border: 1px solid var(--ui-hairline, rgba(0,0,0,0.08));
        border-radius: var(--ui-radius, 8px);
        font-size: 72px; line-height: 1;
        overflow: hidden;
      }
      #creatureInventory .detail-art img {
        width: 100%; height: 100%; object-fit: contain; display: block;
      }
      #creatureInventory .detail-name-row {
        display: flex; align-items: center; justify-content: center;
        gap: 8px; margin: 0 0 4px;
      }
      #creatureInventory .detail-name {
        font-size: 18px; font-weight: 600;
        word-break: break-word; text-align: center;
      }
      #creatureInventory .icon-btn {
        padding: 4px 8px; font-size: 13px; cursor: pointer;
        background: transparent;
        color: var(--ui-text, #111);
        border: 1px solid var(--ui-border, rgba(0,0,0,0.15));
        border-radius: var(--ui-radius, 8px);
      }
      #creatureInventory .detail-species {
        text-align: center; font-size: 12px;
        color: var(--ui-muted, #666); margin: 0 0 10px;
      }
      #creatureInventory .detail-stats {
        display: flex; justify-content: center; gap: 8px;
        font-size: 13px; margin: 0 0 14px;
      }
      #creatureInventory .detail-stats .sep { opacity: 0.5; }
      #creatureInventory .detail-caught {
        text-align: center; font-size: 12px;
        color: var(--ui-muted, #666); margin: 0 0 8px;
      }
      #creatureInventory .detail-art img.detail-art-img {
        width: 100%; height: 100%; object-fit: contain;
        image-rendering: pixelated; image-rendering: crisp-edges;
      }
      #creatureInventory .rename-form {
        display: flex; gap: 6px; justify-content: center;
        flex-wrap: wrap; margin: 0 0 8px;
      }
      #creatureInventory .rename-form input {
        flex: 1; min-width: 0; max-width: 220px;
        padding: 6px 10px; font-size: 14px;
        background: var(--ui-bg, #fff);
        color: var(--ui-text, #111);
        border: 1px solid var(--ui-border, rgba(0,0,0,0.15));
        border-radius: var(--ui-radius, 8px);
      }
      .creature-marker {
        width: var(--creature-marker-size, ${MARKER_SIZE_PX}px);
        height: var(--creature-marker-size, ${MARKER_SIZE_PX}px);
        /* The root must let pointer events pass through — at high zoom
           the element is up to 336×336 and would otherwise swallow
           pinch/wheel gestures that start on its transparent area. Only
           the actual sprite/placeholder children take clicks. */
        pointer-events: none;
        /* Do NOT set position here. MapLibre's .maplibregl-marker rule
           applies position:absolute; overriding it (e.g. with relative)
           leaves the element in the normal document flow, so subsequent
           markers stack vertically inside the canvas container and each
           one accumulates an extra Y offset that rides on top of the
           translate that should put it at its lat/lng. The size is
           driven by a CSS variable the JS updates on every map zoom
           event so creatures stay the same *geographic* size. */
      }
      .creature-marker .creature-placeholder {
        position: absolute;
        top: 50%; left: 50%;
        transform: translate(-50%, -50%);
        width: 14px; height: 14px; border-radius: 50%;
        background: #ff3366; border: 2px solid #fff;
        box-shadow: 0 1px 3px rgba(0,0,0,0.45);
        pointer-events: auto;
        cursor: pointer;
      }
      .creature-marker img.creature-sprite {
        position: absolute;
        inset: 0;
        margin: auto;
        max-width: 100%;
        max-height: 100%;
        image-rendering: pixelated;
        image-rendering: crisp-edges;
        filter: drop-shadow(0 2px 3px rgba(0,0,0,0.45));
        display: none;
        /* Clickable so tapping the sprite opens the battle screen, but
           the transparent margin around it (everything up to the root's
           size) does not intercept. */
        pointer-events: auto;
        cursor: pointer;
      }
      .creature-marker.creature-marker-ready img.creature-sprite {
        display: block;
      }
      .creature-marker.creature-marker-ready .creature-placeholder {
        display: none;
      }
      #battleScreen {
        position: fixed; inset: 0;
        z-index: 40;
        display: none;
        background: rgba(0,0,0,0.85);
        color: #fff;
      }
      #battleScreen.show { display: block; }
      #battleScreen .battle-sprite-wrap {
        position: absolute;
        top: 8%;
        left: 50%;
        transform: translateX(-50%);
        width: min(550px, 85vw);
        height: min(550px, 60vh);
        display: flex; align-items: center; justify-content: center;
      }
      #battleScreen .battle-sprite-placeholder {
        width: 28px; height: 28px; border-radius: 50%;
        background: #ff3366; border: 3px solid #fff;
        box-shadow: 0 2px 6px rgba(0,0,0,0.6);
      }
      #battleScreen img.battle-sprite {
        display: none;
        max-width: 100%; max-height: 100%;
        image-rendering: pixelated;
        image-rendering: crisp-edges;
        filter: drop-shadow(0 6px 10px rgba(0,0,0,0.6));
      }
      #battleScreen.battle-sprite-ready img.battle-sprite { display: block; }
      #battleScreen.battle-sprite-ready .battle-sprite-placeholder { display: none; }
      #battleScreen .battle-info {
        position: absolute;
        top: 50%;
        left: 50%;
        transform: translateX(-50%);
        width: calc(100% - 40px);
        max-width: 320px;
        background: var(--ui-bg, #fff);
        color: var(--ui-text, #111);
        padding: 14px 18px;
        border-radius: var(--ui-radius, 8px);
        text-align: center;
        box-shadow: 0 6px 20px rgba(0,0,0,0.35);
      }
      #battleScreen .battle-name {
        font-size: 17px; font-weight: 600;
        word-break: break-word;
      }
      #battleScreen .battle-stats {
        font-size: 13px;
        color: var(--ui-muted, #666);
        margin-top: 4px;
      }
      #battleScreen .battle-actions {
        position: absolute;
        bottom: 10%;
        left: 50%;
        transform: translateX(-50%);
        display: flex;
        gap: 14px;
      }
      #battleScreen .battle-actions button {
        padding: 12px 28px;
        font-size: 15px;
        font-weight: 600;
        border-radius: var(--ui-radius, 8px);
        border: none;
        cursor: pointer;
        font-family: inherit;
      }
      #battleScreen .battle-actions button.catch {
        background: #ff3366;
        color: #fff;
      }
      #battleScreen .battle-actions button.flee {
        background: rgba(255,255,255,0.15);
        color: #fff;
      }
    `;
    document.head.appendChild(s);
  }

  function ensurePanel() {
    let panel = document.getElementById('creatureInventory');
    if (panel) return panel;
    panel = document.createElement('div');
    panel.id = 'creatureInventory';
    panel.innerHTML = `
      <div class="sheet">
        <div class="browse-view">
          <h3>Creatures</h3>
          <div class="search-row">
            <input id="creatureSearch" type="search" placeholder="Search by name" autocomplete="off">
          </div>
          <div class="sort-row">
            <label for="creatureSortBy">Sort</label>
            <select id="creatureSortBy">
              <option value="level">Level</option>
              <option value="size">Size</option>
              <option value="name">Name</option>
              <option value="species">Species</option>
            </select>
            <button class="dir" type="button" id="creatureSortDir" aria-label="toggle sort direction"></button>
          </div>
          <div class="creature-list"></div>
          <div class="actions"><button class="close" type="button">Done</button></div>
        </div>
        <div class="detail-view">
          <button class="detail-back" type="button">← Back</button>
          <div class="detail-body"></div>
          <div class="actions"><button class="close" type="button">Done</button></div>
        </div>
      </div>
    `;
    panel.addEventListener('click', (e) => {
      if (e.target === panel) hide();
    });
    panel.querySelectorAll('button.close').forEach((btn) => {
      btn.addEventListener('click', hide);
    });

    const browseView = panel.querySelector('.browse-view');
    const detailView = panel.querySelector('.detail-view');
    const sortBy = panel.querySelector('#creatureSortBy');
    const sortDir = panel.querySelector('#creatureSortDir');
    const search = panel.querySelector('#creatureSearch');
    const listEl = panel.querySelector('.creature-list');
    const syncDirButton = () => {
      const dir = readSortDir();
      sortDir.textContent = dir === 'asc' ? '↑' : '↓';
      sortDir.title = dir === 'asc' ? 'ascending (low to high)' : 'descending (high to low)';
    };
    sortBy.value = readSortKey();
    syncDirButton();
    sortBy.addEventListener('change', () => {
      localStorage.setItem('cc.creatureSortBy', sortBy.value);
      renderList(listEl);
    });
    sortDir.addEventListener('click', () => {
      const next = readSortDir() === 'asc' ? 'desc' : 'asc';
      localStorage.setItem('cc.creatureSortDir', next);
      syncDirButton();
      renderList(listEl);
    });
    search.addEventListener('input', () => renderList(listEl));

    // Delegated card click — rebinding per render would be noisier and
    // the grid is small enough that delegation is trivially fast.
    const openFromTarget = (target) => {
      const card = target.closest && target.closest('.creature-card');
      const id = card && card.getAttribute('data-id');
      if (id) showDetail(id);
    };
    listEl.addEventListener('click', (e) => openFromTarget(e.target));
    listEl.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        openFromTarget(e.target);
      }
    });

    panel.querySelector('.detail-back').addEventListener('click', showBrowse);

    document.body.appendChild(panel);
    return panel;
  }

  function showBrowse() {
    const panel = ensurePanel();
    panel.querySelector('.detail-view').classList.remove('show');
    panel.querySelector('.browse-view').style.display = '';
    renderList(panel.querySelector('.creature-list'));
  }

  function showDetail(id) {
    const panel = ensurePanel();
    const creature = findCreature(id);
    if (!creature) return;
    renderDetail(creature);
    panel.querySelector('.browse-view').style.display = 'none';
    panel.querySelector('.detail-view').classList.add('show');
  }

  function renderDetail(c) {
    const panel = document.getElementById('creatureInventory');
    if (!panel) return;
    const body = panel.querySelector('.detail-body');
    const nick = readNicknames()[c.id];
    const name = nick || c.name;
    const stats = [];
    if (c.level != null) stats.push(`Lv ${c.level}`);
    if (c.sizeM != null) stats.push(formatSize(c.sizeM));
    const statsHtml = stats.length
      ? `<div class="detail-stats">${stats.map((s, i) =>
          (i ? '<span class="sep">·</span>' : '') + `<span>${escapeHtml(s)}</span>`
        ).join('')}</div>`
      : '';
    const speciesLine = nick
      ? `<div class="detail-species">Species: ${escapeHtml(c.name)}</div>`
      : '';
    let caughtLine = '';
    if (c.caughtAt) {
      const when = c.caughtAt.timestamp
        ? new Date(c.caughtAt.timestamp).toLocaleDateString()
        : '';
      const where = c.caughtAt.poi && c.caughtAt.poi.name
        ? `${c.caughtAt.poi.name} (${Math.round(c.caughtAt.poi.distanceM)} m away)`
        : `${c.caughtAt.lat.toFixed(5)}, ${c.caughtAt.lng.toFixed(5)}`;
      const parts = [];
      if (where) parts.push(escapeHtml(where));
      if (when) parts.push(escapeHtml(when));
      caughtLine = `<div class="detail-caught">Caught at ${parts.join(' · ')}</div>`;
    }
    body.innerHTML = `
      <div class="detail-art">
        <span class="detail-art-placeholder" aria-hidden="true">${escapeHtml(c.emoji || '•')}</span>
        <img class="detail-art-img" alt="" style="display:none">
      </div>
      <div class="detail-name-row" data-mode="view">
        <div class="detail-name">${escapeHtml(name)}</div>
        <button class="icon-btn rename-edit" type="button" aria-label="rename" title="rename">✎</button>
      </div>
      ${speciesLine}
      ${statsHtml}
      ${caughtLine}
    `;
    body.querySelector('.rename-edit').addEventListener('click', () => {
      enterRenameMode(c);
    });
    if (global.Sprites && c.speciesA != null && c.speciesB != null) {
      global.Sprites.getSpriteUrl(c.speciesA, c.speciesB).then((url) => {
        if (!url) return;
        const img = body.querySelector('.detail-art-img');
        const ph = body.querySelector('.detail-art-placeholder');
        if (!img) { URL.revokeObjectURL(url); return; }
        img.onload = () => {
          URL.revokeObjectURL(url);
          if (ph) ph.style.display = 'none';
          img.style.display = 'block';
        };
        img.src = url;
      });
    }
  }

  function enterRenameMode(c) {
    const panel = document.getElementById('creatureInventory');
    if (!panel) return;
    const row = panel.querySelector('.detail-name-row');
    if (!row || row.dataset.mode === 'edit') return;
    const current = readNicknames()[c.id] || c.name;
    row.dataset.mode = 'edit';
    row.innerHTML = `
      <form class="rename-form">
        <input type="text" maxlength="40" value="${escapeHtml(current)}" aria-label="nickname">
        <button class="icon-btn rename-save" type="submit">Save</button>
        <button class="icon-btn rename-cancel" type="button">Cancel</button>
        <button class="icon-btn rename-reset" type="button" title="reset to species name">Reset</button>
      </form>
    `;
    const form = row.querySelector('form');
    const input = row.querySelector('input');
    input.focus();
    input.select();
    form.addEventListener('submit', (e) => {
      e.preventDefault();
      writeNickname(c.id, input.value);
      renderDetail(c);
    });
    row.querySelector('.rename-cancel').addEventListener('click', () => {
      renderDetail(c);
    });
    row.querySelector('.rename-reset').addEventListener('click', () => {
      writeNickname(c.id, '');
      renderDetail(c);
    });
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') { e.preventDefault(); renderDetail(c); }
    });
  }

  function renderList(listEl) {
    const searchEl = document.getElementById('creatureSearch');
    const q = (searchEl && searchEl.value || '').trim().toLowerCase();
    let items = sortedCreatures();
    if (q) {
      // Match against both nickname (what the user sees) and species name,
      // so a renamed creature can still be found by its original name.
      items = items.filter((c) =>
        displayName(c).toLowerCase().includes(q) ||
        c.name.toLowerCase().includes(q));
    }
    if (!items.length) {
      const msg = q
        ? 'No creatures match that name.'
        : 'No creatures yet — go exploring!';
      listEl.innerHTML = `<div class="creature-empty">${msg}</div>`;
      return;
    }
    listEl.innerHTML = items.map((c) => {
      const stats = [];
      if (c.level != null) stats.push(`Lv ${c.level}`);
      if (c.sizeM != null) stats.push(formatSize(c.sizeM));
      const statsHtml = stats.length
        ? `<div class="stats">${stats.map((s, i) =>
            (i ? '<span class="sep">·</span>' : '') + `<span>${escapeHtml(s)}</span>`
          ).join('')}</div>`
        : '';
      return `
        <div class="creature-card" data-id="${escapeHtml(c.id)}" role="button" tabindex="0">
          <div class="art">
            <span class="art-placeholder" aria-hidden="true">${escapeHtml(c.emoji || '•')}</span>
            <img class="art-img" alt="">
          </div>
          <div class="name">${escapeHtml(displayName(c))}</div>
          ${statsHtml}
        </div>
      `;
    }).join('');
    // Async sprite load per card (reads IDB only — no network per the
    // "zero automatic fetches" rule).
    if (global.Sprites) {
      for (const c of items) {
        if (c.speciesA == null || c.speciesB == null) continue;
        const card = listEl.querySelector(`.creature-card[data-id="${CSS.escape(c.id)}"]`);
        if (!card) continue;
        global.Sprites.getSpriteUrl(c.speciesA, c.speciesB).then((url) => {
          if (!url) return;
          const img = card.querySelector('.art-img');
          const ph = card.querySelector('.art-placeholder');
          if (!img) { URL.revokeObjectURL(url); return; }
          img.onload = () => {
            URL.revokeObjectURL(url);
            if (ph) ph.style.display = 'none';
            img.style.display = 'block';
          };
          img.src = url;
        });
      }
    }
  }

  function show() {
    const panel = ensurePanel();
    const search = panel.querySelector('#creatureSearch');
    if (search) search.value = '';
    showBrowse();
    panel.classList.add('show');
  }

  function hide() {
    const panel = document.getElementById('creatureInventory');
    if (panel) panel.classList.remove('show');
  }

  // Spawn rendering: each deterministic spawn becomes a MapLibre HTML
  // marker with the cropped fusion sprite (Sprites.getSpriteUrl). Only
  // spawns within VISIBILITY_RADIUS_M of the user's GPS fix are shown —
  // you have to actually be there to see a creature. Markers reconcile
  // by spawn id so bucket rollover replaces (not duplicates) the set.
  const VISIBILITY_RADIUS_M = 100;
  const MARKER_SIZE_PX = 168;
  // Size scales like the map: at MARKER_REF_ZOOM a creature is
  // MARKER_SIZE_PX pixels; each zoom level in or out doubles/halves that
  // so the creature covers the same geographic area at every zoom.
  // Clamps keep them tappable at low zoom and sane at very high zoom.
  const MARKER_REF_ZOOM = 18;
  const MARKER_MIN_PX = 36;
  const MARKER_MAX_PX = 336;
  let _overlayMap = null;
  let _overlayTimer = null;
  let _overlayPopup = null;
  let _geoWatchId = null;
  let _userLat = null;
  let _userLng = null;
  // _markers: spawn.id -> { marker, objectUrl, spawn }
  const _markers = new Map();
  // Dedupe cache: skip a refresh if the user has moved < 1 m AND the
  // last refresh was very recent. We can't dedupe by tick alone because
  // spawns expire mid-tick (a spawn born at tick T expires at T+5min,
  // which lands between ticks), so we cap the gap at REFRESH_MIN_GAP_MS
  // — GPS-fix storms collapse but expirations land within ~5 seconds.
  const REFRESH_MIN_GAP_MS = 5000;
  let _lastRefreshLat = null;
  let _lastRefreshLng = null;
  let _lastRefreshAt = 0;

  function metersBetween(lat1, lng1, lat2, lng2) {
    const R = 6371009;
    const toRad = Math.PI / 180;
    const dLat = (lat2 - lat1) * toRad;
    const dLng = (lng2 - lng1) * toRad;
    const a = Math.sin(dLat / 2) ** 2
      + Math.cos(lat1 * toRad) * Math.cos(lat2 * toRad) * Math.sin(dLng / 2) ** 2;
    return 2 * R * Math.asin(Math.sqrt(a));
  }

  function makeMarkerElement(spawn) {
    const el = document.createElement('div');
    el.className = 'creature-marker';
    el.innerHTML = `
      <div class="creature-placeholder"></div>
      <img class="creature-sprite" alt="" draggable="false">
    `;
    // The root is pointer-events: none so map gestures can pass through
    // — wire click only to the two elements that are visually "the
    // creature" (placeholder dot when sprite hasn't loaded, sprite img
    // when it has).
    const onClick = (e) => {
      e.stopPropagation();
      openBattleScreen(spawn);
    };
    el.querySelector('.creature-placeholder').addEventListener('click', onClick);
    el.querySelector('img.creature-sprite').addEventListener('click', onClick);
    return el;
  }

  // --- Battle screen ---------------------------------------------------
  // Full-screen overlay with the creature sprite in the top third and a
  // [Catch] [Flee] pair at the bottom. Catching adds the creature to the
  // local inventory and marks the spawn caught so its marker disappears
  // (locally only — other players still see it).

  let _currentBattleSpawn = null;
  let _battleSpriteUrl = null;

  function ensureBattleScreen() {
    let el = document.getElementById('battleScreen');
    if (el) return el;
    el = document.createElement('div');
    el.id = 'battleScreen';
    el.innerHTML = `
      <div class="battle-sprite-wrap">
        <div class="battle-sprite-placeholder"></div>
        <img class="battle-sprite" alt="" draggable="false">
      </div>
      <div class="battle-info">
        <div class="battle-name"></div>
        <div class="battle-stats"></div>
      </div>
      <div class="battle-actions">
        <button type="button" class="flee">Flee</button>
        <button type="button" class="catch">Catch</button>
      </div>
    `;
    el.querySelector('button.flee').addEventListener('click', closeBattleScreen);
    el.querySelector('button.catch').addEventListener('click', captureCurrentSpawn);
    el.addEventListener('click', (e) => {
      // Click on backdrop (outside the info/actions) dismisses.
      if (e.target === el) closeBattleScreen();
    });
    document.body.appendChild(el);
    return el;
  }

  function openBattleScreen(spawn) {
    const el = ensureBattleScreen();
    _currentBattleSpawn = spawn;
    const nameEl = el.querySelector('.battle-name');
    const statsEl = el.querySelector('.battle-stats');
    nameEl.textContent = fusionName(spawn.speciesA, spawn.speciesB);
    statsEl.textContent = `Lv ${spawn.level} · ${formatSize(spawn.sizeM)}`;
    const img = el.querySelector('img.battle-sprite');
    // Reset sprite until the fresh one loads.
    if (_battleSpriteUrl) { URL.revokeObjectURL(_battleSpriteUrl); _battleSpriteUrl = null; }
    img.removeAttribute('src');
    el.classList.remove('battle-sprite-ready');
    if (global.Sprites) {
      global.Sprites.getSpriteUrl(spawn.speciesA, spawn.speciesB).then((url) => {
        if (!url || _currentBattleSpawn !== spawn) {
          if (url) URL.revokeObjectURL(url);
          return;
        }
        _battleSpriteUrl = url;
        img.onload = () => { el.classList.add('battle-sprite-ready'); };
        img.src = url;
      });
    }
    el.classList.add('show');
  }

  function closeBattleScreen() {
    const el = document.getElementById('battleScreen');
    if (el) el.classList.remove('show');
    if (_battleSpriteUrl) { URL.revokeObjectURL(_battleSpriteUrl); _battleSpriteUrl = null; }
    _currentBattleSpawn = null;
  }

  function captureCurrentSpawn() {
    const spawn = _currentBattleSpawn;
    if (!spawn) return;
    const poiApi = global.CreatureCollectAPI;
    const poi = (poiApi && poiApi.findNearestNamedPoi)
      ? poiApi.findNearestNamedPoi(spawn.lat, spawn.lng)
      : null;
    const entry = {
      id: `c-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      spawnId: spawn.id,
      speciesA: spawn.speciesA,
      speciesB: spawn.speciesB,
      level: spawn.level,
      sizeM: spawn.sizeM,
      caughtAt: {
        timestamp: Date.now(),
        lat: spawn.lat,
        lng: spawn.lng,
        poi: poi || null,
      },
    };
    const list = readCapturedCreatures();
    list.push(entry);
    writeCapturedCreatures(list);
    markSpawnCaught(spawn.id);
    removeMarker(spawn.id);
    closeBattleScreen();
    // Ask for persistent storage the first time a creature is caught so
    // localStorage can't be evicted out from under someone's collection.
    // No-op if already granted; best-effort if the browser denies.
    if (list.length === 1 && navigator.storage && navigator.storage.persist) {
      navigator.storage.persist().catch(() => {});
    }
  }

  function loadMarkerSprite(record) {
    if (!global.Sprites) return;
    const { marker, spawn } = record;
    const el = marker.getElement();
    global.Sprites.getSpriteUrl(spawn.speciesA, spawn.speciesB)
      .then((url) => {
        // No cached sprite (user hasn't run the bulk download yet). Keep
        // the placeholder dot; never fetch on-demand.
        if (url == null) return;
        // Marker may have been removed while sprite was decoding — bail
        // and release the URL so we don't leak an object-URL handle.
        if (!_markers.has(spawn.id) || _markers.get(spawn.id) !== record) {
          URL.revokeObjectURL(url);
          return;
        }
        const img = el.querySelector('img.creature-sprite');
        if (!img) { URL.revokeObjectURL(url); return; }
        img.onload = () => {
          el.classList.add('creature-marker-ready');
          URL.revokeObjectURL(url);
          record.objectUrl = null;
        };
        record.objectUrl = url;
        img.src = url;
      })
      .catch(() => { /* leave the placeholder dot showing */ });
  }

  function addMarker(spawn) {
    if (!_overlayMap || !global.maplibregl) return;
    const el = makeMarkerElement(spawn);
    const marker = new global.maplibregl.Marker({ element: el, anchor: 'center' })
      .setLngLat([spawn.lng, spawn.lat])
      .addTo(_overlayMap);
    const record = { marker, objectUrl: null, spawn };
    _markers.set(spawn.id, record);
    loadMarkerSprite(record);
  }

  function removeMarker(id) {
    const rec = _markers.get(id);
    if (!rec) return;
    rec.marker.remove();
    if (rec.objectUrl) URL.revokeObjectURL(rec.objectUrl);
    _markers.delete(id);
  }

  function clearMarkers() {
    for (const id of Array.from(_markers.keys())) removeMarker(id);
  }

  function refreshSpawnOverlay() {
    if (!_overlayMap || !global.Spawns) return;
    // Without a GPS fix we can't compute distance — clear any existing
    // markers rather than leaving stale ones from a previous fix.
    if (_userLat == null || _userLng == null) {
      if (_markers.size) clearMarkers();
      _lastRefreshLat = _lastRefreshLng = null;
      _lastRefreshAt = 0;
      return;
    }
    const now = Date.now();
    const moved = _lastRefreshLat == null
      || metersBetween(_userLat, _userLng, _lastRefreshLat, _lastRefreshLng) > 1;
    if (!moved && now - _lastRefreshAt < REFRESH_MIN_GAP_MS) return;
    _lastRefreshLat = _userLat;
    _lastRefreshLng = _userLng;
    _lastRefreshAt = now;

    const padM = VISIBILITY_RADIUS_M + 15;
    const latPad = padM / 111000;
    const lngPad = padM / (111000 * Math.cos(_userLat * Math.PI / 180));
    const bbox = [
      _userLng - lngPad, _userLat - latPad,
      _userLng + lngPad, _userLat + latPad,
    ];
    pruneCaughtSpawnIds();
    const caught = readCaughtSpawnIds();
    const spawns = global.Spawns.spawnsInBbox(bbox);
    const within = spawns.filter((s) =>
      !caught.has(s.id)
      && metersBetween(_userLat, _userLng, s.lat, s.lng) <= VISIBILITY_RADIUS_M
    );

    // Reconcile markers: keep existing ids, add new ones, drop stale ones.
    const wanted = new Set(within.map((s) => s.id));
    for (const id of Array.from(_markers.keys())) {
      if (!wanted.has(id)) removeMarker(id);
    }
    for (const s of within) {
      if (!_markers.has(s.id)) addMarker(s);
    }
  }

  function startLocationWatch() {
    if (_geoWatchId != null || !navigator.geolocation) return;
    _geoWatchId = navigator.geolocation.watchPosition(
      (pos) => {
        _userLat = pos.coords.latitude;
        _userLng = pos.coords.longitude;
        refreshSpawnOverlay();
      },
      () => { /* ignore — user may have denied permission */ },
      { enableHighAccuracy: true, maximumAge: 5000 }
    );
  }

  function stopLocationWatch() {
    if (_geoWatchId != null && navigator.geolocation) {
      navigator.geolocation.clearWatch(_geoWatchId);
    }
    _geoWatchId = null;
    _userLat = null;
    _userLng = null;
  }

  function updateMarkerScale() {
    if (!_overlayMap) return;
    const z = _overlayMap.getZoom();
    const raw = MARKER_SIZE_PX * Math.pow(2, z - MARKER_REF_ZOOM);
    const px = Math.max(MARKER_MIN_PX, Math.min(MARKER_MAX_PX, raw));
    document.documentElement.style.setProperty('--creature-marker-size', `${px.toFixed(1)}px`);
  }

  let _zoomHandler = null;

  function attachSpawnOverlay(map) {
    if (_overlayMap === map) return;
    _overlayMap = map;
    startLocationWatch();
    updateMarkerScale();
    _zoomHandler = updateMarkerScale;
    map.on('zoom', _zoomHandler);
    // Safety net for tick rollover — GPS updates drive most refreshes,
    // but a stationary user still needs new births / expiries to land
    // promptly. Dedupe in refresh keeps this near-free when nothing
    // has changed.
    _overlayTimer = setInterval(refreshSpawnOverlay, 20 * 1000);
  }

  function detachSpawnOverlay() {
    if (!_overlayMap) return;
    clearMarkers();
    if (_overlayTimer) clearInterval(_overlayTimer);
    if (_overlayPopup) _overlayPopup.remove();
    if (_zoomHandler) _overlayMap.off('zoom', _zoomHandler);
    stopLocationWatch();
    _overlayMap = null;
    _overlayTimer = null;
    _overlayPopup = null;
    _zoomHandler = null;
    _lastRefreshLat = _lastRefreshLng = null;
    _lastRefreshAt = 0;
  }

  class CreatureBallControl {
    onAdd() {
      const c = document.createElement('div');
      c.className = 'maplibregl-ctrl maplibregl-ctrl-group';
      this._root = c;
      const b = document.createElement('button');
      b.type = 'button';
      b.title = 'creatures';
      b.setAttribute('aria-label', 'creatures');
      // Generic monster-ball: a circle with an equator and a small center
      // dot. Deliberately not a pokeball — this project is its own thing.
      b.innerHTML = `<svg width="20" height="20" viewBox="0 0 24 24" style="display:block;margin:auto">
        <circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" stroke-width="1.8"/>
        <path d="M3 12h18" stroke="currentColor" stroke-width="1.8" fill="none"/>
        <circle cx="12" cy="12" r="2.5" fill="var(--ui-bg, #fff)" stroke="currentColor" stroke-width="1.8"/>
      </svg>`;
      b.onclick = () => show();
      c.appendChild(b);
      c.style.display = readEnabled() ? '' : 'none';
      return c;
    }
    onRemove() {
      if (this._root && this._root.parentNode) {
        this._root.parentNode.removeChild(this._root);
      }
      this._root = null;
    }
    setVisible(on) {
      if (this._root) this._root.style.display = on ? '' : 'none';
      if (!on) hide();
    }
  }

  function install(map) {
    injectStyles();
    const ctrl = new CreatureBallControl();
    map.addControl(ctrl, 'bottom-right');
    if (readEnabled()) attachSpawnOverlay(map);
    return {
      setEnabled(on) {
        writeEnabled(on);
        ctrl.setVisible(on);
        if (on) attachSpawnOverlay(map);
        else detachSpawnOverlay();
      },
      isEnabled: readEnabled,
      show,
      hide,
    };
  }

  global.Creatures = { install, isEnabled: readEnabled };
})(typeof window !== 'undefined' ? window : globalThis);
