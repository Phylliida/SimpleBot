// Per-icon sprite cache. Each 96×96 fusion sprite is cropped out of its
// sheet once and stored as a standalone PNG Blob in IndexedDB keyed by
// "${a}-${b}". Sheets are never persisted — they're decoded in memory,
// cropped, then discarded once the crops they contained have been saved.
// A small LRU of in-flight sheet ImageBitmaps keeps us from re-downloading
// when multiple spawns on screen share the same partner-B.
//
//   Sprites.getSpriteUrl(a, b) -> Promise<objectURL string>
//
// Caller is responsible for URL.revokeObjectURL once the image has loaded.

(function (global) {
  'use strict';

  const DB_NAME = 'creature-sprites-v1';
  const DB_VERSION = 1;
  const STORE_ICONS = 'icons';
  const SPRITE_SIZE = 96;
  const SHEET_COLS = 10;
  const MAX_SHEET_CACHE = 8;

  let _dbPromise = null;

  function openDb() {
    if (_dbPromise) return _dbPromise;
    _dbPromise = new Promise((resolve, reject) => {
      const req = indexedDB.open(DB_NAME, DB_VERSION);
      req.onupgradeneeded = () => {
        const db = req.result;
        if (!db.objectStoreNames.contains(STORE_ICONS)) {
          db.createObjectStore(STORE_ICONS);
        }
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
    return _dbPromise;
  }

  async function idbGet(key) {
    const db = await openDb();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(STORE_ICONS, 'readonly');
      const req = tx.objectStore(STORE_ICONS).get(key);
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
  }

  async function idbPut(key, value) {
    const db = await openDb();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(STORE_ICONS, 'readwrite');
      tx.objectStore(STORE_ICONS).put(value, key);
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error);
    });
  }

  // LRU of decoded sheets. Keyed by sheet number; values are ImageBitmaps.
  // Insertion order on a Map gives us free LRU semantics — we just
  // delete-then-set on access to bump to the newest slot.
  const _sheetCache = new Map();
  const _sheetPromises = new Map();

  async function getSheetBitmap(sheetIdx) {
    if (_sheetCache.has(sheetIdx)) {
      const bmp = _sheetCache.get(sheetIdx);
      _sheetCache.delete(sheetIdx);
      _sheetCache.set(sheetIdx, bmp);
      return bmp;
    }
    if (_sheetPromises.has(sheetIdx)) return _sheetPromises.get(sheetIdx);

    const p = (async () => {
      const resp = await fetch(`/creature-sprite/${sheetIdx}`);
      if (!resp.ok) throw new Error(`sheet ${sheetIdx}: HTTP ${resp.status}`);
      const blob = await resp.blob();
      const bmp = await createImageBitmap(blob);
      _sheetCache.set(sheetIdx, bmp);
      while (_sheetCache.size > MAX_SHEET_CACHE) {
        const firstKey = _sheetCache.keys().next().value;
        const old = _sheetCache.get(firstKey);
        _sheetCache.delete(firstKey);
        if (old && old.close) old.close();
      }
      return bmp;
    })();
    _sheetPromises.set(sheetIdx, p);
    try { return await p; }
    finally { _sheetPromises.delete(sheetIdx); }
  }

  function makeCanvas(w, h) {
    if (typeof OffscreenCanvas !== 'undefined') return new OffscreenCanvas(w, h);
    const c = document.createElement('canvas');
    c.width = w; c.height = h;
    return c;
  }

  function canvasToBlob(canvas) {
    if (canvas.convertToBlob) return canvas.convertToBlob({ type: 'image/png' });
    return new Promise((resolve) => canvas.toBlob(resolve, 'image/png'));
  }

  // Scan pixel alpha and return a Blob cropped to the non-transparent
  // bbox. Null for fully-transparent inputs. Alpha threshold is set low
  // enough to keep soft-edged outlines and shadow pixels.
  async function trimToOpaqueBbox(srcBitmap, sx, sy, sw, sh) {
    const ALPHA_MIN = 8;
    const scan = makeCanvas(sw, sh);
    const sctx = scan.getContext('2d');
    sctx.drawImage(srcBitmap, sx, sy, sw, sh, 0, 0, sw, sh);
    const data = sctx.getImageData(0, 0, sw, sh).data;
    let minX = sw, minY = sh, maxX = -1, maxY = -1;
    for (let y = 0; y < sh; y++) {
      const rowOff = y * sw * 4 + 3;
      for (let x = 0; x < sw; x++) {
        if (data[rowOff + x * 4] > ALPHA_MIN) {
          if (x < minX) minX = x;
          if (x > maxX) maxX = x;
          if (y < minY) minY = y;
          if (y > maxY) maxY = y;
        }
      }
    }
    if (maxX < 0) return null;
    const cw = maxX - minX + 1;
    const ch = maxY - minY + 1;
    const out = makeCanvas(cw, ch);
    out.getContext('2d').drawImage(scan, minX, minY, cw, ch, 0, 0, cw, ch);
    return canvasToBlob(out);
  }

  async function cropSprite(bitmap, index) {
    const col = index % SHEET_COLS;
    const row = Math.floor(index / SHEET_COLS);
    const blob = await trimToOpaqueBbox(
      bitmap,
      col * SPRITE_SIZE, row * SPRITE_SIZE,
      SPRITE_SIZE, SPRITE_SIZE
    );
    // Fully-transparent slot (e.g. malformed input) — fall back to an
    // untrimmed 96×96 so the caller still gets a valid Blob.
    if (blob) return blob;
    const fallback = makeCanvas(SPRITE_SIZE, SPRITE_SIZE);
    fallback.getContext('2d').drawImage(
      bitmap,
      col * SPRITE_SIZE, row * SPRITE_SIZE, SPRITE_SIZE, SPRITE_SIZE,
      0, 0, SPRITE_SIZE, SPRITE_SIZE
    );
    return canvasToBlob(fallback);
  }

  async function getSpriteBlob(a, b) {
    const key = `${a}-${b}`;
    const cached = await idbGet(key);
    if (cached) {
      // Self-heal: legacy v1 crops were stored as full 96×96 PNGs with
      // transparent padding around the creature. Since every sprite has
      // its opaque content at a slightly different place in the cell,
      // padded blobs visually drift by different amounts when pinned to
      // a lat/lng. Trim on first access so markers self-correct without
      // needing a full bulk re-download. The IHDR check is ~24 bytes,
      // so the fast path (already trimmed) is cheap.
      if (await isLegacyPaddedPng(cached)) {
        const trimmed = await retrimStoredBlob(cached);
        if (trimmed) {
          idbPut(key, trimmed).catch(() => {});
          return trimmed;
        }
      }
      return cached;
    }
    // No cache hit. By design this module does NOT fetch missing sheets
    // on-demand — all network work is gated behind the explicit
    // "↓ download" button (bulkDownload). Callers should render a
    // placeholder when a sprite isn't cached yet.
    return null;
  }

  async function getSpriteUrl(a, b) {
    const blob = await getSpriteBlob(a, b);
    if (!blob) return null;
    return URL.createObjectURL(blob);
  }

  // Prefetch tracking. A sheet is "done" only when every crop in
  // [indexFrom..indexTo] has been written to IDB successfully, so a
  // mid-download refresh resumes correctly instead of silently skipping
  // holes.
  const DOWNLOADED_KEY = 'cc.spritesDownloaded';

  function getDownloadedSheets() {
    try {
      const raw = localStorage.getItem(DOWNLOADED_KEY);
      return raw ? new Set(JSON.parse(raw)) : new Set();
    } catch { return new Set(); }
  }

  function markSheetDownloaded(sheetIdx) {
    const set = getDownloadedSheets();
    set.add(sheetIdx);
    localStorage.setItem(DOWNLOADED_KEY, JSON.stringify([...set].sort((a, b) => a - b)));
  }

  async function idbHas(key) {
    const v = await idbGet(key);
    return v != null;
  }

  // Pre-v2 cropSprite stored every sprite as a 96×96 PNG even when the
  // creature only filled a small middle region, so those legacy entries
  // visually drift when pinned to their lng/lat. This check reads just
  // the PNG IHDR (8-byte signature + 4-byte length + 4-byte 'IHDR'
  // + width/height u32s, big-endian) to detect them without a full
  // decode — a blob slice is orders of magnitude cheaper than
  // createImageBitmap for the fast-path "already trimmed" case.
  async function isLegacyPaddedPng(blob) {
    if (!blob || blob.size < 24) return false;
    const buf = new Uint8Array(await blob.slice(0, 24).arrayBuffer());
    if (buf[0] !== 0x89 || buf[1] !== 0x50) return false;
    const w = (buf[16] << 24) | (buf[17] << 16) | (buf[18] << 8) | buf[19];
    const h = (buf[20] << 24) | (buf[21] << 16) | (buf[22] << 8) | buf[23];
    return w === SPRITE_SIZE && h === SPRITE_SIZE;
  }

  async function retrimStoredBlob(blob) {
    const bmp = await createImageBitmap(blob);
    try {
      const trimmed = await trimToOpaqueBbox(bmp, 0, 0, bmp.width, bmp.height);
      return trimmed;
    } finally {
      if (bmp.close) bmp.close();
    }
  }

  async function getAllIconKeys() {
    const db = await openDb();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(STORE_ICONS, 'readonly');
      const req = tx.objectStore(STORE_ICONS).getAllKeys();
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
  }

  // Source-of-truth scan. Returns what actually lives in IDB right now
  // across the requested range, so a refresh / localStorage clear / IDB
  // wipe all surface accurately. Callers should prefer this over
  // getDownloadedSheets() for anything user-facing.
  async function getDownloadStatus(opts = {}) {
    const sheetFrom = opts.sheetFrom || 1;
    const sheetTo = opts.sheetTo || 150;
    const indexFrom = opts.indexFrom || 1;
    const indexTo = opts.indexTo || 150;
    const keys = await getAllIconKeys();
    const have = new Set(keys);
    const sheetsComplete = new Set();
    const neededPerSheet = indexTo - indexFrom + 1;
    let iconsPresent = 0;
    for (let b = sheetFrom; b <= sheetTo; b++) {
      let count = 0;
      for (let a = indexFrom; a <= indexTo; a++) {
        if (have.has(`${a}-${b}`)) count++;
      }
      iconsPresent += count;
      if (count === neededPerSheet) sheetsComplete.add(b);
    }
    return {
      sheetsComplete,
      sheetsTotal: sheetTo - sheetFrom + 1,
      iconsPresent,
      iconsTotal: (sheetTo - sheetFrom + 1) * neededPerSheet,
    };
  }

  // opts:
  //   sheetFrom / sheetTo: partner-B sheet range (1..150 by default)
  //   indexFrom / indexTo: partner-A crop range (1..150)
  //   signal: AbortSignal — caller can cancel
  //   onProgress({ sheetsDone, sheetsTotal, currentSheet, indexInSheet, phase })
  async function bulkDownload(opts = {}) {
    const sheetFrom = opts.sheetFrom || 1;
    const sheetTo = opts.sheetTo || 150;
    const indexFrom = opts.indexFrom || 1;
    const indexTo = opts.indexTo || 150;
    const signal = opts.signal;
    const onProgress = opts.onProgress || (() => {});

    // Fetch species-name list alongside sprites so names populate on
    // the same explicit user action. Tiny (~15 KB), cached in
    // localStorage by species.js.
    if (global.Species && typeof global.Species.ensureLoaded === 'function') {
      try { await global.Species.ensureLoaded(); } catch {}
    }

    // One upfront scan of IDB keys so per-icon resume is a Set.has() check
    // (not a round-trip transaction) AND the localStorage tracker gets
    // rebuilt from ground truth — users can wipe it without losing data,
    // or wipe IDB without a stale tracker claiming "all done".
    const keys = await getAllIconKeys();

    // Before fetching anything new, migrate any legacy 96×96 padded PNGs
    // to tight-bbox crops in place. Purely local work — no network — so
    // users who already downloaded the whole set don't pay to fix them.
    for (let i = 0; i < keys.length; i++) {
      if (signal && signal.aborted) return { cancelled: true };
      const key = keys[i];
      const blob = await idbGet(key);
      if (!(await isLegacyPaddedPng(blob))) continue;
      const trimmed = await retrimStoredBlob(blob);
      if (trimmed) await idbPut(key, trimmed);
      if (i % 50 === 0) {
        onProgress({
          sheetsDone: 0, sheetsTotal: sheetTo - sheetFrom + 1,
          currentSheet: 0, indexInSheet: i, phase: 'repairing',
        });
      }
    }

    const have = new Set(keys);
    const neededPerSheet = indexTo - indexFrom + 1;
    const sheetComplete = new Set();
    for (let b = sheetFrom; b <= sheetTo; b++) {
      let count = 0;
      for (let a = indexFrom; a <= indexTo; a++) {
        if (have.has(`${a}-${b}`)) count++;
      }
      if (count === neededPerSheet) sheetComplete.add(b);
    }
    localStorage.setItem(DOWNLOADED_KEY,
      JSON.stringify([...sheetComplete].sort((a, b) => a - b)));

    const totalSheets = sheetTo - sheetFrom + 1;
    let finished = sheetComplete.size;

    for (let b = sheetFrom; b <= sheetTo; b++) {
      if (signal && signal.aborted) return { cancelled: true };
      if (sheetComplete.has(b)) continue;

      onProgress({
        sheetsDone: finished, sheetsTotal: totalSheets,
        currentSheet: b, indexInSheet: 0, phase: 'fetching',
      });

      const bmp = await getSheetBitmap(b);

      for (let a = indexFrom; a <= indexTo; a++) {
        if (signal && signal.aborted) {
          if (bmp && bmp.close) bmp.close();
          _sheetCache.delete(b);
          return { cancelled: true };
        }
        const key = `${a}-${b}`;
        if (have.has(key)) continue;
        const blob = await cropSprite(bmp, a);
        await idbPut(key, blob);
        have.add(key);
        if (a % 20 === 0) {
          onProgress({
            sheetsDone: finished, sheetsTotal: totalSheets,
            currentSheet: b, indexInSheet: a, phase: 'cropping',
          });
        }
      }

      // Release the decoded sheet before moving to the next — otherwise
      // eight ~18 MB ImageBitmaps pile up in the LRU.
      _sheetCache.delete(b);
      if (bmp && bmp.close) bmp.close();

      markSheetDownloaded(b);
      finished++;
      onProgress({
        sheetsDone: finished, sheetsTotal: totalSheets,
        currentSheet: b, indexInSheet: indexTo, phase: 'done',
      });
    }
    return { cancelled: false };
  }

  async function deleteAllSprites() {
    localStorage.removeItem(DOWNLOADED_KEY);
    const db = await openDb();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(STORE_ICONS, 'readwrite');
      tx.objectStore(STORE_ICONS).clear();
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error);
    });
  }

  global.Sprites = {
    getSpriteUrl, getSpriteBlob,
    bulkDownload, getDownloadedSheets, getDownloadStatus, deleteAllSprites,
  };
})(typeof window !== 'undefined' ? window : globalThis);
