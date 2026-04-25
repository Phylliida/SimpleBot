// Test suite for static/trip-planner.js.  Run with `node --test tests/`.
//
// Pure helpers are exercised directly via TripPlanner.helpers.
// The two planners are driven through tiny hand-built walk-graph + schedule
// fixtures so the expected plans can be reasoned about on paper.
//
// No npm deps — uses the built-in node:test + node:assert.

'use strict';

const test   = require('node:test');
const assert = require('node:assert/strict');

// trip-planner.js is written as a `(function (global) { ... })(globalThis)`
// IIFE that attaches to `global.TripPlanner`. require()ing it executes the
// IIFE and sets globalThis.TripPlanner.
require('../static/trip-planner.js');
const { create, helpers, constants } = globalThis.TripPlanner;

// ---------- MinHeap (copy of static/index.html's — tests need one too) ---

class MinHeap {
  constructor() { this.a = []; }
  get size() { return this.a.length; }
  push(item) {
    this.a.push(item);
    let i = this.a.length - 1;
    while (i > 0) {
      const p = (i - 1) >> 1;
      if (this.a[p][0] <= this.a[i][0]) break;
      [this.a[p], this.a[i]] = [this.a[i], this.a[p]];
      i = p;
    }
  }
  pop() {
    const top = this.a[0];
    const last = this.a.pop();
    if (this.a.length) {
      this.a[0] = last;
      let i = 0;
      while (true) {
        const l = 2 * i + 1, r = 2 * i + 2;
        let s = i;
        if (l < this.a.length && this.a[l][0] < this.a[s][0]) s = l;
        if (r < this.a.length && this.a[r][0] < this.a[s][0]) s = r;
        if (s === i) break;
        [this.a[s], this.a[i]] = [this.a[i], this.a[s]];
        i = s;
      }
    }
    return top;
  }
}

// ---------- fixture helpers ---------------------------------------------

function makeWalkGraph(nodes, edges) {
  const g = { nodes: new Map(), adj: new Map() };
  for (const [id, coord] of nodes) g.nodes.set(id, coord);
  for (const [a, b, weight] of edges) {
    if (!g.adj.has(a)) g.adj.set(a, []);
    if (!g.adj.has(b)) g.adj.set(b, []);
    g.adj.get(a).push({ to: b, weight, shape: null, reverse: false, name: null });
    g.adj.get(b).push({ to: a, weight, shape: null, reverse: true,  name: null });
  }
  return g;
}

function makeScheduleIdx({ stops, patterns, routes, trips, services }) {
  const idx = {
    stops:              new Map(),
    stopPatterns:       new Map(),
    patterns:           new Map(),
    patternBboxStops:   new Map(),
    tripsByPattern:     new Map(),
    routesById:         new Map(),
    stopToWalkNode:     new Map(),
    walkNodeToStops:    new Map(),
    services:           new Map(),
    serviceExceptions:  [],
    osmToGtfs:          new Map(),
  };
  for (const s of stops) {
    idx.stops.set(s.id, { lng: s.lng, lat: s.lat, name: s.name });
    idx.stopToWalkNode.set(s.id, { node: s.walkNode, distance: s.walkDist });
    if (!idx.walkNodeToStops.has(s.walkNode)) idx.walkNodeToStops.set(s.walkNode, []);
    idx.walkNodeToStops.get(s.walkNode).push([s.id, s.walkDist]);
  }
  for (const p of patterns) {
    idx.patterns.set(p.id, { id: p.id, route_id: p.routeId });
    idx.patternBboxStops.set(p.id, p.stops.map((sid, i) => [i, sid]));
    for (let i = 0; i < p.stops.length; i++) {
      const sid = p.stops[i];
      if (!idx.stopPatterns.has(sid)) idx.stopPatterns.set(sid, []);
      idx.stopPatterns.get(sid).push([p.id, i]);
    }
  }
  for (const r of routes) idx.routesById.set(r.id, r);
  for (const t of trips) {
    const rec = { p: t.pattern_id, f: t.f, s: t.svc_id, _deltas: t.deltas };
    if (!idx.tripsByPattern.has(t.pattern_id)) idx.tripsByPattern.set(t.pattern_id, []);
    idx.tripsByPattern.get(t.pattern_id).push(rec);
  }
  for (const sid of services) idx.services.set(sid, { id: sid });
  return idx;
}

// Dead-simple service-day mocks: only 'svc1' runs today.
const mockTodayInfo  = (date) => ({ nowSec: Math.floor(date.getTime() / 1000) });
const mockSvcSets    = () => ({ today: new Set(['svc1']), yesterday: new Set(), tomorrow: new Set() });
const mockDayOffset  = (trip, sets) => {
  if (sets.today.has(trip.s))     return 0;
  if (sets.yesterday.has(trip.s)) return -86400;
  if (sets.tomorrow.has(trip.s))  return  86400;
  return null;
};
const mockCumulative = (trip) => {
  if (trip._cum) return trip._cum;
  const cum = [0];
  for (const d of trip._deltas || []) cum.push(cum[cum.length - 1] + d);
  trip._cum = cum;
  return cum;
};

// ---------- pure-helper unit tests ---------------------------------------

test('groupLegs: empty input → empty output', () => {
  assert.deepEqual(helpers.groupLegs([]), []);
});

test('groupLegs: consecutive walk steps collapse into one walk leg', () => {
  const legs = helpers.groupLegs([
    { type: 'walk', edge: { weight: 100 }, fromNode: 1, toNode: 2, tDep: 0,  tArr: 10 },
    { type: 'walk', edge: { weight: 100 }, fromNode: 2, toNode: 3, tDep: 10, tArr: 20 },
  ]);
  assert.equal(legs.length, 1);
  assert.equal(legs[0].type, 'walk');
  assert.equal(legs[0].edges.length, 2);
  assert.equal(legs[0].from, 1);
  assert.equal(legs[0].to, 3);
  assert.equal(legs[0].tDep, 0);
  assert.equal(legs[0].tArr, 20);
});

test('groupLegs: walk / access / transit / egress / walk → 5 separate legs', () => {
  const steps = [
    { type: 'walk',    edge: {}, fromNode: 1, toNode: 2, tDep: 0,  tArr: 10 },
    { type: 'access',  stopId: 'A', distance: 5, fromNode: 2, tDep: 10, tArr: 14 },
    { type: 'transit', trip: {}, patternId: 'P', fromStopId: 'A', toStopId: 'B',
                       fromSeq: 0, toSeq: 1, tDep: 14, tArr: 40 },
    { type: 'egress',  stopId: 'B', distance: 5, toNode: 4, tDep: 40, tArr: 44 },
    { type: 'walk',    edge: {}, fromNode: 4, toNode: 5, tDep: 44, tArr: 50 },
  ];
  const legs = helpers.groupLegs(steps);
  assert.equal(legs.length, 5);
  assert.deepEqual(
    legs.map(l => l.type),
    ['walk', 'access', 'transit', 'egress', 'walk']);
});

test('groupLegs: unknown step type is ignored', () => {
  const legs = helpers.groupLegs([
    { type: 'walk',  edge: {}, fromNode: 1, toNode: 2, tDep: 0,  tArr: 10 },
    { type: 'mystery' },
    { type: 'walk',  edge: {}, fromNode: 2, toNode: 3, tDep: 10, tArr: 20 },
  ]);
  // Mystery step doesn't flush. Walks still collapse.
  assert.equal(legs.length, 1);
  assert.equal(legs[0].edges.length, 2);
});

test('costOfPlan: walk-only scales linearly with walkWeight', () => {
  const plan = { legs: [{ type: 'walk', tDep: 0, tArr: 1800 }], startSec: 0, endSec: 1800 };
  assert.equal(helpers.costOfPlan(plan, 1), 1800);
  assert.equal(helpers.costOfPlan(plan, 5), 1800 * 5);
  assert.equal(helpers.costOfPlan(plan, 10), 1800 * 10);
});

test('costOfPlan: transit-only is 1× regardless of walkWeight', () => {
  const plan = { legs: [{ type: 'transit', tDep: 0, tArr: 1800 }], startSec: 0, endSec: 1800 };
  assert.equal(helpers.costOfPlan(plan, 1),  1800);
  assert.equal(helpers.costOfPlan(plan, 5),  1800);
  assert.equal(helpers.costOfPlan(plan, 10), 1800);
});

test('costOfPlan: inter-leg gaps (wait-at-stop) count as non-walk', () => {
  // 100s walk (access), 200s gap, 200s transit ride.
  const plan = {
    legs: [
      { type: 'access',  tDep: 0,   tArr: 100 },
      { type: 'transit', tDep: 300, tArr: 500 },
    ],
    startSec: 0, endSec: 500,
  };
  // walk = 100, nonWalk = 200 ride + 200 gap = 400.
  assert.equal(helpers.costOfPlan(plan, 1), 100 + 400);
  assert.equal(helpers.costOfPlan(plan, 5), 100 * 5 + 400);
});

test('costOfPlan: trailing idle (endSec > last leg tArr) is NOT charged', () => {
  // The reverse planner used to park arriving-early slack into endSec; retime
  // now moves it outside the plan. costOfPlan should not charge it either.
  const plan = {
    legs:     [{ type: 'walk', tDep: 0, tArr: 100 }],
    startSec: 0, endSec: 150,
  };
  assert.equal(helpers.costOfPlan(plan, 1), 100);
  assert.equal(helpers.costOfPlan(plan, 5), 500);
});

test('relax: writes when cheaper, skips when not', () => {
  const state = { bestCost: new Map(), came: new Map(), pq: new MinHeap() };
  helpers.relax(state, 'A', 10, 100, { prev: '_', type: 'walk' });
  assert.equal(state.bestCost.get('A'), 10);
  assert.equal(state.pq.size, 1);

  helpers.relax(state, 'A', 20, 50, { prev: '_', type: 'walk' });      // worse — ignored
  assert.equal(state.bestCost.get('A'), 10);
  assert.equal(state.came.get('A').type, 'walk');

  helpers.relax(state, 'A', 5, 30, { prev: '_', type: 'transit' });    // better — replaces
  assert.equal(state.bestCost.get('A'), 5);
  assert.equal(state.came.get('A').type, 'transit');
  // The old PQ entry is NOT removed (Dijkstra filters via the cost check on pop).
  assert.equal(state.pq.size, 2);
});

test('initSearch: seeds bestCost[start]=0 and pushes [0, start, t] onto pq', () => {
  const st = helpers.initSearch('ORIG', 1000, MinHeap);
  assert.equal(st.bestCost.get('ORIG'), 0);
  assert.equal(st.pq.size, 1);
  const [c, k, t] = st.pq.pop();
  assert.equal(c, 0);
  assert.equal(k, 'ORIG');
  assert.equal(t, 1000);
});

test('makeTripLookup: filters by active service + memoizes', () => {
  const tripsByPattern = new Map([
    ['P', [
      { p: 'P', f: 100, s: 'svc1' },
      { p: 'P', f: 200, s: 'svc_not_running' },
      { p: 'P', f: 300, s: 'svc1' },
    ]],
  ]);
  const sets = mockSvcSets();
  const lookup = helpers.makeTripLookup(tripsByPattern, mockDayOffset, sets);
  const first = lookup('P');
  assert.equal(first.length, 2);
  assert.deepEqual(first.map(x => x.trip.f), [100, 300]);
  assert.equal(first[0].offset, 0);
  // Returns the same cached array.
  assert.equal(lookup('P'), first);
  // Unknown pattern → empty array.
  assert.deepEqual(lookup('MISSING'), []);
});

test('pickBestTrip forward: picks earliest dep ≥ bound', () => {
  const trips = [
    { trip: { f: 100 }, offset: 0 },
    { trip: { f: 300 }, offset: 0 },
    { trip: { f: 50  }, offset: 0 },
  ];
  const cum = () => [0];
  const pick = helpers.pickBestTrip(trips, 0, 'forward', 80, cum);
  assert.equal(pick.val, 100);
  assert.equal(pick.trip.f, 100);

  const latestOnly = helpers.pickBestTrip(trips, 0, 'forward', 150, cum);
  assert.equal(latestOnly.val, 300);

  const none = helpers.pickBestTrip(trips, 0, 'forward', 400, cum);
  assert.equal(none, null);
});

test('pickBestTrip reverse: picks latest arrival ≤ bound', () => {
  const trips = [
    { trip: { f: 100 }, offset: 0 },
    { trip: { f: 300 }, offset: 0 },
  ];
  const cum = () => [0];
  const pick = helpers.pickBestTrip(trips, 0, 'reverse', 250, cum);
  assert.equal(pick.val, 100); // 300 > 250 so excluded

  const latest = helpers.pickBestTrip(trips, 0, 'reverse', 1000, cum);
  assert.equal(latest.val, 300);

  const none = helpers.pickBestTrip(trips, 0, 'reverse', 50, cum);
  assert.equal(none, null);
});

test('pickBestTrip: day-offset propagates into val', () => {
  const trips = [
    { trip: { f: 100 }, offset: 0      },
    { trip: { f: 100 }, offset: 86400  },  // tomorrow
  ];
  const cum = () => [0];
  const pick = helpers.pickBestTrip(trips, 0, 'forward', 150, cum);
  // today's trip is at f=100 < 150, so we take tomorrow's at f+86400=86500.
  assert.equal(pick.val, 86500);
  assert.equal(pick.offset, 86400);
});

test('reconstruct: forward (dest → origin) unshifts so output reads origin → dest', () => {
  const came = new Map();
  came.set('B', { prev: 'A', type: 'walk', tDep: 0,  tArr: 10 });
  came.set('C', { prev: 'B', type: 'walk', tDep: 10, tArr: 20 });
  const raw = helpers.reconstruct(came, 'C', 'prev', 'unshift');
  assert.equal(raw.length, 2);
  assert.equal(raw[0].key, 'B');
  assert.equal(raw[1].key, 'C');
});

test('reconstruct: reverse (origin → dest) pushes so output reads origin → dest', () => {
  const came = new Map();
  came.set('A', { next: 'B', type: 'walk' });
  came.set('B', { next: 'C', type: 'walk' });
  const raw = helpers.reconstruct(came, 'A', 'next', 'push');
  assert.equal(raw.length, 2);
  assert.equal(raw[0].key, 'A');
  assert.equal(raw[1].key, 'B');
});

test('reconstruct: terminal key missing from came → empty', () => {
  assert.deepEqual(helpers.reconstruct(new Map(), 'X', 'prev', 'unshift'), []);
});

// ---------- constants sanity ---------------------------------------------

test('constants: exported and finite', () => {
  for (const k of ['WALK_SPEED_MPS', 'MAX_SEARCH_SECS', 'MAX_MULTIMODAL_ITERS',
                    'MAX_ALTERNATIVES', 'ALT_SHIFT_SEC', 'FORCE_TRANSIT_WEIGHT']) {
    assert.equal(Number.isFinite(constants[k]), true, k);
    assert.equal(constants[k] > 0, true, k);
  }
});

// ---------- integration tests with a tiny walk graph + schedule ----------

// 1 ─100m─ 2 ─100m─ 3 ─100m─ 4        (walk 300m total ≈ 230s)
//           │        │
//           S1       S2                 (each stop 5m off its walk-node)
//           └── pattern P1, T1 dep 150s, ride 200s to S2 ──┘
// walk-only wall-clock ≈ 230s.  Transit wall-clock ≈ 430s (with wait).
// Simple-cost crossover walkWeight: ~3.9 — so walkWeight=1 picks walk,
// walkWeight=5 picks transit.
function tinyFixture() {
  const walkGraph = makeWalkGraph(
    [
      [1, { lng: 0.00, lat: 0 }],
      [2, { lng: 0.01, lat: 0 }],
      [3, { lng: 0.02, lat: 0 }],
      [4, { lng: 0.03, lat: 0 }],
    ],
    [ [1, 2, 100], [2, 3, 100], [3, 4, 100] ],
  );
  const scheduleIdx = makeScheduleIdx({
    stops: [
      { id: 'S1', lng: 0.01, lat: 0, name: 'Stop 1', walkNode: 2, walkDist: 5 },
      { id: 'S2', lng: 0.02, lat: 0, name: 'Stop 2', walkNode: 3, walkDist: 5 },
    ],
    patterns: [{ id: 'P1', stops: ['S1', 'S2'], routeId: 'R1' }],
    routes:   [{ id: 'R1', short: '1', long: 'Test bus', colour: '#123456', mode: 3 }],
    trips:    [{ pattern_id: 'P1', f: 150, svc_id: 'svc1', deltas: [200] }],
    services: ['svc1'],
  });
  return { walkGraph, scheduleIdx };
}

function tinyPlanner({ walkGraph, scheduleIdx }) {
  return create({
    get walkGraph()   { return walkGraph;   },
    get scheduleIdx() { return scheduleIdx; },
    MinHeap,
    todayInfo:           mockTodayInfo,
    expandedServiceSets: mockSvcSets,
    tripDayOffset:       mockDayOffset,
    tripCumulative:      mockCumulative,
  });
}

test('planTransitTrip: unknown origin or dest → null', () => {
  const planner = tinyPlanner(tinyFixture());
  assert.equal(planner.planTransitTrip(999, 4,   new Date(0), 1, 30), null);
  assert.equal(planner.planTransitTrip(1,   999, new Date(0), 1, 30), null);
});

test('planTransitTrip: walkWeight=1 → short direct walk beats transit ride', () => {
  const planner = tinyPlanner(tinyFixture());
  // Walk 300m ≈ 231s. Transit wall-clock ≈ 880s.  At weight=1 the optimal
  // cost-min plan is the direct walk.
  const plan = planner.planTransitTrip(1, 4, new Date(0), 1, 30);
  assert.ok(plan, 'plan should exist');
  assert.equal(plan.legs.filter(l => l.type === 'transit').length, 0);
  assert.equal(plan.startSec, 0);
  assert.ok(plan.totalSec > 200 && plan.totalSec < 260, `walk totalSec=${plan.totalSec}`);
});

test('planTransitTrip: walkWeight=5 → transit beats the same direct walk', () => {
  const planner = tinyPlanner(tinyFixture());
  const plan = planner.planTransitTrip(1, 4, new Date(0), 5, 30);
  assert.ok(plan, 'plan should exist');
  const transitLegs = plan.legs.filter(l => l.type === 'transit');
  assert.equal(transitLegs.length, 1);
  assert.equal(transitLegs[0].patternId, 'P1');
  assert.equal(transitLegs[0].fromStopId, 'S1');
  assert.equal(transitLegs[0].toStopId,   'S2');
  // Order must be origin → dest: first leg begins at the origin walk-node.
  assert.equal(plan.legs[0].tDep, 0);
  // And finishes at dest walk-node 4 at or after the bus's arrival time.
  assert.equal(plan.legs[plan.legs.length - 1].tArr, plan.endSec);
});

test('planTransitTrip: missed-boarding → next trip used (or null)', () => {
  // Force the user to arrive at S1 just after the only bus departs.
  // trip.f = 200, walk to S1 takes ~80s. Starting at date=150 means we reach
  // S1 at ~230 — after dep 200 + transferSec=30 cutoff wait, so bus missed.
  // With no other trips the planner should fall back to walking.
  const planner = tinyPlanner(tinyFixture());
  const plan = planner.planTransitTrip(1, 4, new Date(150 * 1000), 5, 30);
  assert.ok(plan);
  // Bus is gone; plan is walk-only from 1 → 4.
  assert.equal(plan.legs.filter(l => l.type === 'transit').length, 0);
});

test('planTransitTripArriveBy: finds the forward-order plan', () => {
  const planner = tinyPlanner(tinyFixture());
  // Bus arrives S2 at 350, walk 3→4 ≈ 77s, actual arrival ≈ 430s.
  const plan = planner.planTransitTripArriveBy(1, 4, new Date(1000 * 1000), 10, 30);
  assert.ok(plan);
  assert.ok(plan.endSec >= 420 && plan.endSec <= 445, `endSec=${plan.endSec}`);
  assert.ok(plan.endSec <= 1000, 'actual arrival must still be by tArr');
  assert.equal(plan.legs.filter(l => l.type === 'transit').length, 1);
  // Legs read origin → dest (forward order).
  assert.equal(plan.legs[0].tDep, plan.startSec);
  assert.equal(plan.legs[plan.legs.length - 1].tArr, plan.endSec);
});

test('planTransitTripArriveBy: retime eliminates phantom slack from a far-future tArr', () => {
  const planner = tinyPlanner(tinyFixture());
  // tArr far beyond when transit naturally delivers us. Without retime the
  // plan would pad the gap after the last bus — we should now arrive ~430s.
  const plan = planner.planTransitTripArriveBy(1, 4, new Date(5000 * 1000), 10, 30);
  assert.ok(plan);
  assert.ok(plan.endSec < 600,
            `expected retimed endSec ≈ 430, got ${plan.endSec}`);
  // No gap should exceed the transfer buffer (30s) between consecutive legs.
  for (let i = 1; i < plan.legs.length; i++) {
    const gap = plan.legs[i].tDep - plan.legs[i - 1].tArr;
    // Only bus-boarding waits can exceed the buffer. Here the only wait is
    // arriving at S1 a bit early and waiting for the 200s bus.
    if (plan.legs[i].type !== 'transit') {
      assert.ok(gap <= 30.5, `non-transit gap too large: ${gap}`);
    }
  }
});

test('retimeLegs: walks pack; transits stay scheduled', () => {
  const legs = [
    { type: 'walk',    tDep: 0,   tArr: 10 },
    { type: 'access',  tDep: 10,  tArr: 14 },
    { type: 'transit', tDep: 100, tArr: 200 },  // fixed bus times
    { type: 'egress',  tDep: 500, tArr: 504 },  // slack before egress
    { type: 'walk',    tDep: 504, tArr: 580 },
  ];
  const { legs: retimed, endSec } = helpers.retimeLegs(legs, 0);
  // Walks/access at the start stay packed.
  assert.equal(retimed[0].tDep, 0);
  assert.equal(retimed[0].tArr, 10);
  assert.equal(retimed[1].tDep, 10);
  assert.equal(retimed[1].tArr, 14);
  // Transit is unchanged.
  assert.equal(retimed[2].tDep, 100);
  assert.equal(retimed[2].tArr, 200);
  // Egress now starts at transit.tArr (200), not 500.
  assert.equal(retimed[3].tDep, 200);
  assert.equal(retimed[3].tArr, 204);
  // Final walk packs against egress, arriving at 280 instead of 580.
  assert.equal(retimed[4].tDep, 204);
  assert.equal(retimed[4].tArr, 280);
  assert.equal(endSec, 280);
});

test('retimeLegs: forward-style plan (already tight) is a no-op', () => {
  // A forward Dijkstra plan has walks tight and waits only before transit.
  const legs = [
    { type: 'walk',    tDep: 0,  tArr: 77 },
    { type: 'access',  tDep: 77, tArr: 81 },
    { type: 'transit', tDep: 200, tArr: 800 },  // 119s real wait before boarding
    { type: 'egress',  tDep: 800, tArr: 804 },
    { type: 'walk',    tDep: 804, tArr: 880 },
  ];
  const { legs: retimed, endSec } = helpers.retimeLegs(legs, 0);
  for (let i = 0; i < legs.length; i++) {
    assert.equal(retimed[i].tDep, legs[i].tDep, `leg ${i} tDep`);
    assert.equal(retimed[i].tArr, legs[i].tArr, `leg ${i} tArr`);
  }
  assert.equal(endSec, 880);
});

test('retimeLegs: empty legs returns startSec as endSec', () => {
  const { legs, endSec } = helpers.retimeLegs([], 1234);
  assert.deepEqual(legs, []);
  assert.equal(endSec, 1234);
});

test('planTripAlternatives: force-transit fallback surfaces a transit option', () => {
  const planner = tinyPlanner(tinyFixture());
  // At weight=1 the primary is walk-only; the FORCE_TRANSIT_WEIGHT fallback
  // runs and a transit plan is added. Sort then puts walk-only first
  // (weight=1 tie goes to lower wall-clock, walk is faster).
  const alts = planner.planTripAlternatives(1, 4, 'depart', new Date(0), 1, 30);
  assert.ok(alts.length >= 2);
  const anyTransit = alts.some(p => p.legs.some(l => l.type === 'transit'));
  assert.equal(anyTransit, true);
  assert.ok(alts[0].legs.every(l => l.type !== 'transit'),
            'walk-only should sort first at walkWeight=1');
});

test('planTripAlternatives: at walkWeight=5, transit sorts first', () => {
  const planner = tinyPlanner(tinyFixture());
  const alts = planner.planTripAlternatives(1, 4, 'depart', new Date(0), 5, 30);
  assert.ok(alts.length >= 1);
  assert.ok(alts[0].legs.some(l => l.type === 'transit'),
            'at walkWeight=5, transit should sort first');
});

test('planTripAlternatives: arrive mode also returns a transit alternative', () => {
  const planner = tinyPlanner(tinyFixture());
  const alts = planner.planTripAlternatives(
    1, 4, 'arrive', new Date(1000 * 1000), 5, 30);
  assert.ok(alts.length >= 1);
  assert.ok(alts.some(p => p.legs.some(l => l.type === 'transit')));
});

test('planTransitTrip: obeys tripDayOffset (svc not running → no transit)', () => {
  const fixture = tinyFixture();
  // Swap the only trip's service onto something not running today.
  const trips = fixture.scheduleIdx.tripsByPattern.get('P1');
  trips[0].s = 'svc_not_today';
  const planner = tinyPlanner(fixture);
  const plan = planner.planTransitTrip(1, 4, new Date(0), 10, 30);
  assert.ok(plan);
  // Even at weight=10 no transit — the trip isn't active today.
  assert.equal(plan.legs.filter(l => l.type === 'transit').length, 0);
});

test('planTransitTrip: cost model — higher walkWeight prefers waiting for a late bus', () => {
  // Walk 1→2=50m, 2→3=200m (250m total, ~192s walk wall-clock).
  // Bus leaves at t=900 (15 min wait), 5 min ride, arrives t=1200. Transit
  // wall-clock is ~1204s — much longer than walking — but at high walkWeight
  // the scaled cost should still prefer transit.
  const walkGraph = makeWalkGraph(
    [[1, { lng: 0, lat: 0 }], [2, { lng: 0.001, lat: 0 }], [3, { lng: 0.003, lat: 0 }]],
    [[1, 2, 50], [2, 3, 200]],
  );
  const scheduleIdx = makeScheduleIdx({
    stops: [
      { id: 'SA', lng: 0.001, lat: 0, name: 'A', walkNode: 2, walkDist: 5 },
      { id: 'SB', lng: 0.003, lat: 0, name: 'B', walkNode: 3, walkDist: 5 },
    ],
    patterns: [{ id: 'P', stops: ['SA', 'SB'], routeId: 'R' }],
    routes:   [{ id: 'R', short: '1', long: 'Late bus', colour: '#fff', mode: 3 }],
    trips:    [{ pattern_id: 'P', f: 900, svc_id: 'svc1', deltas: [300] }],
    services: ['svc1'],
  });
  const planner = tinyPlanner({ walkGraph, scheduleIdx });
  const walkOnly   = planner.planTransitTrip(1, 3, new Date(0),  1, 30);
  const transitRun = planner.planTransitTrip(1, 3, new Date(0), 10, 30);
  assert.ok(walkOnly);
  assert.equal(walkOnly.legs.filter(l => l.type === 'transit').length, 0,
               'walking wins at walkWeight=1 since wall-clock is shorter');
  assert.ok(transitRun);
  assert.equal(transitRun.legs.filter(l => l.type === 'transit').length, 1,
               'transit wins at walkWeight=10 despite longer wall-clock');
});

test('planTransitTrip is deterministic (same inputs → same plan)', () => {
  const a = tinyPlanner(tinyFixture()).planTransitTrip(1, 4, new Date(0), 5, 30);
  const b = tinyPlanner(tinyFixture()).planTransitTrip(1, 4, new Date(0), 5, 30);
  assert.equal(a.startSec, b.startSec);
  assert.equal(a.endSec,   b.endSec);
  assert.equal(a.legs.length, b.legs.length);
  for (let i = 0; i < a.legs.length; i++) {
    assert.equal(a.legs[i].type, b.legs[i].type);
    assert.equal(a.legs[i].tDep, b.legs[i].tDep);
    assert.equal(a.legs[i].tArr, b.legs[i].tArr);
  }
});

test('forward and reverse agree on plan shape for a simple single-bus trip', () => {
  // At high walkWeight both directions should pick the same bus. Their
  // wall-clock START may differ (depart-at floors to t0 = 0, arrive-by finds
  // the latest possible departure), but the leg sequence + transit leg
  // identity should match.
  const planner = tinyPlanner(tinyFixture());
  const fwd = planner.planTransitTrip(1, 4, new Date(0), 10, 30);
  const rev = planner.planTransitTripArriveBy(1, 4, new Date(1000 * 1000), 10, 30);
  assert.ok(fwd && rev);
  assert.deepEqual(fwd.legs.map(l => l.type), rev.legs.map(l => l.type));
  const fwdT = fwd.legs.find(l => l.type === 'transit');
  const revT = rev.legs.find(l => l.type === 'transit');
  assert.equal(fwdT.patternId, revT.patternId);
  assert.equal(fwdT.fromStopId, revT.fromStopId);
  assert.equal(fwdT.toStopId,   revT.toStopId);
});
