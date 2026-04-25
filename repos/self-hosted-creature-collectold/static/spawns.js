// Deterministic, server-free spawn generator. Given (lat, lon, time),
// any two devices produce the same creatures at the same coordinates.
//
// Based on Richard Brent's xor4096 PRNG (http://arxiv.org/pdf/1104.3115.pdf),
// ported from an earlier project. Adaptations vs. the original:
//   - Time-bucketed seed (BUCKET_MS) so spawns rotate without a server.
//   - Bbox-driven cell iteration instead of a user-centered 11×11 scan,
//     so the map can paint spawns across the whole viewport.
//   - GPS/touch wrappers stripped — MapLibre owns those.
//
// Cell size is ~11 m (1/SCALE degrees of lat; longitude cells are
// narrower away from the equator, fine for v1). Density/species/level
// knobs live near the top — tune to taste.

(function (global) {
  'use strict';

  // --- Brent xor4096 PRNG (verbatim) ---
  function XorGen(seed) {
    var me = this;
    me.next = function () {
      var w = me.w, X = me.X, i = me.i, t, v;
      me.w = w = (w + 0x61c88647) | 0;
      v = X[(i + 34) & 127];
      t = X[i = ((i + 1) & 127)];
      v ^= v << 13;
      t ^= t << 17;
      v ^= v >>> 15;
      t ^= t >>> 12;
      v = X[i] = v ^ t;
      me.i = i;
      return (v + (w ^ (w >>> 16))) | 0;
    };
    (function init(me, seed) {
      var t, v, i, j, w, X = [], limit = 128;
      if (seed === (seed | 0)) { v = seed; seed = null; }
      else { seed = seed + '\0'; v = 0; limit = Math.max(limit, seed.length); }
      for (i = 0, j = -32; j < limit; ++j) {
        if (seed) v ^= seed.charCodeAt((j + 32) % seed.length);
        if (j === 0) w = v;
        v ^= v << 10; v ^= v >>> 15; v ^= v << 4; v ^= v >>> 13;
        if (j >= 0) {
          w = (w + 0x61c88647) | 0;
          t = (X[j & 127] ^= (v + w));
          i = (0 == t) ? i + 1 : 0;
        }
      }
      if (i >= 128) X[(seed && seed.length || 0) & 127] = -1;
      i = 127;
      for (j = 4 * 128; j > 0; --j) {
        v = X[(i + 34) & 127];
        t = X[i = ((i + 1) & 127)];
        v ^= v << 13; t ^= t << 17;
        v ^= v >>> 15; t ^= t >>> 12;
        X[i] = v ^ t;
      }
      me.w = w; me.X = X; me.i = i;
    })(me, seed);
  }

  function getxor4069(seed) {
    if (seed == null) seed = +(new Date);
    var xg = new XorGen(seed);
    var prng = function () { return (xg.next() >>> 0) / ((1 << 30) * 4); };
    prng.int32 = xg.next;
    return prng;
  }

  // --- Spawn tuning constants ---
  // Spawning is a sliding window: each (cell, minute-tick) is an
  // independent deterministic slot. If the PRNG draw for that slot is
  // below SPAWN_CHANCE_PER_TICK, a creature is born at that tick and
  // lives LIFETIME_MS. A query at time t scans the LIFETIME_TICKS most
  // recent ticks per cell. The result: at any moment some spawns are
  // freshly born and others are about to expire — no synchronized mass
  // rollover the way fixed-bucket designs produce.
  const SCALE = 10000.0;                 // 1/SCALE deg ≈ 11 m at the equator
  const LAT_MOD = 5001;                  // prime wrap-around for hash
  const LON_MOD = 5503;
  const TICK_MS = 60 * 1000;             // birth-tick granularity (1 min)
  const LIFETIME_MS = 5 * 60 * 1000;     // each spawn lives 5 min
  const LIFETIME_TICKS = Math.ceil(LIFETIME_MS / TICK_MS);  // 5
  const DAY_SALT = 0x1F3B2C;             // bump to invalidate every seed
  // Probability a given (cell, tick) hosts a spawn. Tuned so expected
  // active count ≈ pHit × LIFETIME_TICKS roughly matches the prior
  // fixed-bucket density (~2% of cells active at any time).
  const SPAWN_CHANCE_PER_TICK = 0.0035;
  const SPECIES_MAX = 150;               // v1 sprite download is sheets 1–150
  const MAX_CELLS = 40000;               // bail when zoomed out too far

  function goodMod(a, b) { return ((a % b) + b) % b; }
  function expDistr(avg, max, v) {
    return Math.min(max, Math.round(-Math.log(v || 1e-12) * avg));
  }

  function currentTick(nowMs) {
    return Math.floor((nowMs == null ? Date.now() : nowMs) / TICK_MS);
  }

  // Per-tick PRNG seed for one cell. Mixes cell coordinates with the
  // tick (XOR'd with DAY_SALT so bumping the salt invalidates everything).
  function cellTickSeed(cellX, cellY, tick) {
    const curX = goodMod(cellX, LAT_MOD);
    const curY = goodMod(cellY, LON_MOD);
    return (Math.round(
      ((curX + 1) * LAT_MOD + (curY + 1) * LAT_MOD * LON_MOD) * 7477
    ) + ((tick ^ DAY_SALT) >>> 0) * 983) | 0;
  }

  // One slot's spawn (or null). cellX/cellY are integer cell indices;
  // tick is the integer minute-tick at which the spawn was born. The
  // ID format is `${cellX}:${cellY}:${tick}:0` — the trailing 0 leaves
  // room for >1 spawn per cell-tick in the future without changing
  // existing caught-spawn keys.
  function generateCellAtTick(cellX, cellY, tick) {
    const arng = getxor4069(cellTickSeed(cellX, cellY, tick));
    if (arng() >= SPAWN_CHANCE_PER_TICK) return null;
    const fx = arng();
    const fy = arng();
    const lat = (cellX + fx) / SCALE - 90;
    const lng = (cellY + fy) / SCALE - 180;
    const speciesA = 1 + Math.floor(arng() * SPECIES_MAX);
    const speciesB = 1 + Math.floor(arng() * SPECIES_MAX);
    const level = expDistr(5, 50, arng()) + 1;
    const sizeM = 0.15 + arng() * 2.0;
    const startMs = tick * TICK_MS;
    return {
      id: `${cellX}:${cellY}:${tick}:0`,
      lat, lng, speciesA, speciesB, level, sizeM,
      startMs, expireMs: startMs + LIFETIME_MS,
    };
  }

  // bbox is [west, south, east, north] (lng/lat MapLibre order).
  function spawnsInBbox(bbox, nowMs) {
    const [west, south, east, north] = bbox;
    const now = nowMs == null ? Date.now() : nowMs;
    const curTick = currentTick(now);

    const minLatCell = Math.floor((south + 90) * SCALE);
    const maxLatCell = Math.ceil((north + 90) * SCALE);
    const minLngCell = Math.floor((west + 180) * SCALE);
    const maxLngCell = Math.ceil((east + 180) * SCALE);

    const cellsX = maxLatCell - minLatCell + 1;
    const cellsY = maxLngCell - minLngCell + 1;
    if (cellsX * cellsY > MAX_CELLS) return [];

    const firstTick = curTick - LIFETIME_TICKS + 1;
    const out = [];
    for (let cx = minLatCell; cx <= maxLatCell; cx++) {
      for (let cy = minLngCell; cy <= maxLngCell; cy++) {
        for (let t = firstTick; t <= curTick; t++) {
          const p = generateCellAtTick(cx, cy, t);
          if (!p) continue;
          if (now < p.startMs || now >= p.expireMs) continue;
          if (p.lat < south || p.lat > north
              || p.lng < west || p.lng > east) continue;
          out.push(p);
        }
      }
    }
    return out;
  }

  // Caught-spawn IDs include the birth-tick. Once the tick is older
  // than the sliding window, the creature is gone and the ID just
  // bloats localStorage — return true so creatures.js can prune it.
  function isSpawnIdStale(id, nowMs) {
    if (typeof id !== 'string') return true;
    const parts = id.split(':');
    if (parts.length < 3) return true;
    const tick = +parts[2];
    if (!Number.isFinite(tick)) return true;
    return tick < currentTick(nowMs) - LIFETIME_TICKS + 1;
  }

  global.Spawns = {
    spawnsInBbox, generateCellAtTick, currentTick, isSpawnIdStale,
    TICK_MS, LIFETIME_MS,
  };
})(typeof window !== 'undefined' ? window : globalThis);
