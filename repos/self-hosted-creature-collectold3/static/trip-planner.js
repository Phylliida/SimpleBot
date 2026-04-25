// Pure time-dependent Dijkstra over a walk + transit graph.
//
// Node keys are `w:<walkNodeId>` or `s:<gtfsStopId>`.
// PQ entries are `[cost, key, wallClock]`. Priority is cost only;
// wall-clock rides along for schedule lookups.
//
// Cost model (the only assumption):
//   walking second       = walkWeight
//   non-walking second   = 1      (wait + ride on transit)
// At walkWeight=1 cost == wall-clock; at walkWeight=5 a walking second
// costs 5× a transit second. Nothing else is baked in.
//
// Time units everywhere are SECONDS. Walk edges carry `.walkSec`;
// walkNodeToStops / stopToWalkNode use `walkSec` for the connector time.
// No meter-to-second conversion inside this module.
//
// `deps`:
//   walkGraph        Typed-array graph exposing index-based accessors:
//                      hasNode(idx), lng(idx), lat(idx),
//                      neighborStart(idx), neighborEnd(idx),
//                      neighborTo(ai), neighborEdge(ai), neighborReverse(ai),
//                      edgeWeight(e), edgeWalkSec(e),
//                      edgeName(e), edgeShape(e).
//                    Node identifiers are internal uint32 indices (not OSM IDs).
//   scheduleIdx      { stopPatterns, patternBboxStops, stopToWalkNode,
//                      walkNodeToStops }
//   MinHeap          class
//
// Per-call `activeTrips(patternId)` → array of `{ trip, f, cum }`:
//   f    = effective first-stop departure (seconds; caller applies any
//          service-day offset).
//   cum  = cumulative stop-to-stop delta array, so trip reaches stop
//          at index `seq` at time `f + cum[seq]`.
//   trip = opaque reference echoed back in the 'transit' step.
//
// Returned plan:
//   { steps: [...], startSec, endSec }
// where `steps` is the raw origin→dest chain with entries:
//   { type: 'walk',    edge, fromNode, toNode, tDep, tArr }
//   { type: 'access',  stopId, fromNode, walkSec, tDep, tArr }
//   { type: 'egress',  stopId, toNode,   walkSec, tDep, tArr }
//   { type: 'transit', trip, patternId, fromStopId, toStopId,
//                      fromSeq, toSeq, tDep, tArr }

(function (global) {
  'use strict';

  function create(deps) {
    const { MinHeap } = deps;

    // ---- forward: depart at t0, find fastest arrival ------------------

    function planForward(originNodeId, destNodeId, t0, walkWeight, transferSec, activeTrips) {
      const wg = deps.walkGraph, si = deps.scheduleIdx;
      if (!wg.hasNode(originNodeId) || !wg.hasNode(destNodeId)) return null;
      const destKey = 'w:' + destNodeId;
      const bestCost = new Map(), came = new Map(), pq = new MinHeap();
      bestCost.set('w:' + originNodeId, 0);
      pq.push([0, 'w:' + originNodeId, t0]);

      const relax = (nk, newCost, newT, step) => {
        if (newCost < (bestCost.get(nk) ?? Infinity)) {
          bestCost.set(nk, newCost);
          came.set(nk, step);
          pq.push([newCost, nk, newT]);
        }
      };

      while (pq.size) {
        const [cost, key, t] = pq.pop();
        if (cost > bestCost.get(key)) continue;
        if (key === destKey) return traceback(came, destKey, 'prev', 'unshift', t0, t);

        if (key[0] === 'w') {
          const nid = +key.slice(2);
          const ns = wg.neighborStart(nid), ne = wg.neighborEnd(nid);
          for (let i = ns; i < ne; i++) {
            const to = wg.neighborTo(i);
            const edgeIdx = wg.neighborEdge(i);
            const reverse = wg.neighborReverse(i);
            const walkSec = wg.edgeWalkSec(edgeIdx);
            relax('w:' + to, cost + walkSec * walkWeight, t + walkSec, {
              prev: key, type: 'walk', edge: { edgeIdx, reverse },
              fromNode: nid, toNode: to, tDep: t, tArr: t + walkSec,
            });
          }
          for (const [stopId, walkSec] of (si.walkNodeToStops.get(nid) || [])) {
            relax('s:' + stopId, cost + walkSec * walkWeight, t + walkSec, {
              prev: key, type: 'access', stopId, walkSec, fromNode: nid,
              tDep: t, tArr: t + walkSec,
            });
          }
        } else {
          const stopId = key.slice(2);
          const link = si.stopToWalkNode.get(stopId);
          if (link) {
            relax('w:' + link.node, cost + link.walkSec * walkWeight, t + link.walkSec, {
              prev: key, type: 'egress', stopId, walkSec: link.walkSec,
              toNode: link.node, tDep: t, tArr: t + link.walkSec,
            });
          }
          const seqs = si.stopPatterns.get(stopId);
          if (!seqs) continue;
          const boardAfter = t + transferSec;
          for (const [patternId, seq] of seqs) {
            const bboxStops = si.patternBboxStops.get(patternId);
            if (!bboxStops || bboxStops.length <= 1) continue;
            let best = null;
            for (const entry of activeTrips(patternId)) {
              const dep = entry.f + (entry.cum[seq] || 0);
              if (dep < boardAfter) continue;
              if (!best || dep < best.dep) best = { entry, dep };
            }
            if (!best) continue;
            for (const [seqJ, stopJ] of bboxStops) {
              if (seqJ <= seq) continue;
              const arr = best.entry.f + (best.entry.cum[seqJ] || 0);
              if (arr <= best.dep) continue;
              relax('s:' + stopJ, cost + (arr - t), arr, {
                prev: key, type: 'transit',
                trip: best.entry.trip, patternId,
                fromStopId: stopId, toStopId: stopJ,
                fromSeq: seq, toSeq: seqJ,
                tDep: best.dep, tArr: arr,
              });
            }
          }
        }
      }
      return null;
    }

    // ---- reverse: arrive by tArr, find latest departure ---------------

    function planReverse(originNodeId, destNodeId, tArr, walkWeight, transferSec, activeTrips) {
      const wg = deps.walkGraph, si = deps.scheduleIdx;
      if (!wg.hasNode(originNodeId) || !wg.hasNode(destNodeId)) return null;
      const origKey = 'w:' + originNodeId;
      const destKey = 'w:' + destNodeId;
      const bestCost = new Map(), came = new Map(), pq = new MinHeap();
      bestCost.set(destKey, 0);
      pq.push([0, destKey, tArr]);

      const relax = (nk, newCost, newT, step) => {
        if (newCost < (bestCost.get(nk) ?? Infinity)) {
          bestCost.set(nk, newCost);
          came.set(nk, step);
          pq.push([newCost, nk, newT]);
        }
      };

      while (pq.size) {
        const [cost, key, t] = pq.pop();
        if (cost > bestCost.get(key)) continue;
        if (key === origKey) return traceback(came, origKey, 'next', 'push', t, tArr);

        if (key[0] === 'w') {
          const nid = +key.slice(2);
          const ns = wg.neighborStart(nid), ne = wg.neighborEnd(nid);
          for (let i = ns; i < ne; i++) {
            const to = wg.neighborTo(i);
            const edgeIdx = wg.neighborEdge(i);
            const rev = wg.neighborReverse(i);
            const walkSec = wg.edgeWalkSec(edgeIdx);
            // In reverse search we're traversing neighbour→nid backward in time;
            // in forward order the leg runs to→nid, so the stored direction
            // bit needs to be flipped.
            relax('w:' + to, cost + walkSec * walkWeight, t - walkSec, {
              next: key, type: 'walk', edge: { edgeIdx, reverse: !rev },
              fromNode: to, toNode: nid,
              tDep: t - walkSec, tArr: t,
            });
          }
          for (const [stopId, walkSec] of (si.walkNodeToStops.get(nid) || [])) {
            relax('s:' + stopId, cost + walkSec * walkWeight, t - walkSec, {
              next: key, type: 'egress', stopId, walkSec, toNode: nid,
              tDep: t - walkSec, tArr: t,
            });
          }
        } else {
          const stopId = key.slice(2);
          const link = si.stopToWalkNode.get(stopId);
          if (link) {
            relax('w:' + link.node, cost + link.walkSec * walkWeight, t - link.walkSec, {
              next: key, type: 'access', stopId, walkSec: link.walkSec,
              fromNode: link.node, tDep: t - link.walkSec, tArr: t,
            });
          }
          const seqs = si.stopPatterns.get(stopId);
          if (!seqs) continue;
          for (const [patternId, seq] of seqs) {
            const bboxStops = si.patternBboxStops.get(patternId);
            if (!bboxStops || bboxStops.length <= 1) continue;
            let best = null;
            for (const entry of activeTrips(patternId)) {
              const arr = entry.f + (entry.cum[seq] || 0);
              if (arr > t) continue;
              if (!best || arr > best.arr) best = { entry, arr };
            }
            if (!best) continue;
            for (const [seqJ, stopJ] of bboxStops) {
              if (seqJ >= seq) continue;
              const dep = best.entry.f + (best.entry.cum[seqJ] || 0);
              if (dep >= best.arr) continue;
              const arrAtJ = dep - transferSec;
              relax('s:' + stopJ, cost + (t - arrAtJ), arrAtJ, {
                next: key, type: 'transit',
                trip: best.entry.trip, patternId,
                fromStopId: stopJ, toStopId: stopId,
                fromSeq: seqJ, toSeq: seq,
                tDep: dep, tArr: best.arr,
              });
            }
          }
        }
      }
      return null;
    }

    return { planForward, planReverse };
  }

  function traceback(came, terminal, linkField, orderOp, startSec, endSec) {
    const steps = [];
    let cursor = terminal;
    while (came.has(cursor)) {
      const step = came.get(cursor);
      steps[orderOp]({ ...step });
      cursor = step[linkField];
    }
    return { steps, startSec, endSec };
  }

  global.TripPlanner = { create };
})(typeof window !== 'undefined' ? window : globalThis);
