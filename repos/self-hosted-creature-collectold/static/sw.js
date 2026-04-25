const APP_CACHE = 'app-v1';
const TILES_CACHE = 'tiles-v1';

const APP_SHELL = [
  '/',
  '/manifest.webmanifest',
  '/static/icon.svg',
  '/static/trip-planner.js',
  '/static/vendor/maplibre-gl.css',
  '/static/vendor/maplibre-gl.js',
];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(APP_CACHE).then(async (c) => {
    await Promise.all(APP_SHELL.map(async (url) => {
      try {
        // Delete first so Safari reliably frees the old response body;
        // cache.put alone sometimes leaves the old blob hanging around and
        // each hard refresh grows navigator.storage.estimate() a bit.
        await c.delete(url);
        // `no-store` skips the HTTP cache entirely on both read and write.
        // `reload` bypasses on read but still populates, which on Safari
        // causes storage.estimate() to grow ~1-2 MB per hard refresh as
        // old response blobs linger in the browser's HTTP cache.
        const res = await fetch(url, { cache: 'no-store' });
        if (res.ok) await c.put(url, res);
      } catch {}
    }));
  }));
  self.skipWaiting();
});

self.addEventListener('activate', (e) => {
  e.waitUntil(self.clients.claim());
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (url.origin === location.origin && url.pathname.startsWith('/tiles/')) {
    if (e.request.headers.get('X-Download') === '1') return;
    e.respondWith(
      caches.open(TILES_CACHE)
        .then(c => c.match(e.request))
        .then(hit => hit || new Response(null, { status: 204 }))
    );
    return;
  }
  if (url.origin === location.origin && url.pathname.startsWith('/poi')) {
    if (e.request.headers.get('X-Download') === '1') return;
    e.respondWith(new Response(JSON.stringify({ pois: [] }), {
      status: 200, headers: { 'Content-Type': 'application/json' }
    }));
    return;
  }
  if (url.origin === location.origin && url.pathname.startsWith('/routes')) {
    if (e.request.headers.get('X-Download') === '1') return;
    e.respondWith(new Response(JSON.stringify({ type: 'FeatureCollection', features: [] }), {
      status: 200, headers: { 'Content-Type': 'application/json' }
    }));
    return;
  }
  if (url.origin === location.origin && url.pathname.startsWith('/walk-graph')) {
    if (e.request.headers.get('X-Download') === '1') return;
    e.respondWith(new Response(JSON.stringify({ nodes: [], edges: [] }), {
      status: 200, headers: { 'Content-Type': 'application/json' }
    }));
    return;
  }
  if (url.origin === location.origin &&
      (url.pathname.startsWith('/fonts') || url.pathname.startsWith('/icons'))) {
    if (e.request.headers.get('X-Download') === '1') {
      e.respondWith(
        caches.open(APP_CACHE).then(async (c) => {
          const hit = await c.match(e.request);
          if (hit) return hit;
          const res = await fetch(e.request);
          if (res.ok) c.put(e.request, res.clone());
          return res;
        })
      );
      return;
    }
    e.respondWith(
      caches.open(APP_CACHE)
        .then(c => c.match(e.request))
        .then(hit => hit || new Response(null, { status: 204 }))
    );
    return;
  }
  e.respondWith(caches.match(e.request).then(hit => hit || fetch(e.request)));
});

self.addEventListener('message', (e) => {
  const msg = e.data || {};
  if (msg.type === 'download') {
    e.waitUntil(downloadRegion(msg, e.source));
  } else if (msg.type === 'deleteTiles') {
    e.waitUntil(deleteTiles(msg.urls));
  } else if (msg.type === 'refreshAppShell') {
    e.waitUntil(refreshAppShell(e.source));
  }
});

async function downloadRegion({ bbox, minZoom, maxZoom, extraUrls = [], id, workers = 6 }, client) {
  const tiles = tileUrls(bbox, minZoom, maxZoom);
  const queue = [
    ...tiles.map(u => ({ url: u, cacheName: TILES_CACHE })),
    ...extraUrls.map(u => ({ url: u, cacheName: APP_CACHE }))
  ];
  const total = queue.length;
  let done = 0, failed = 0;
  // Worker count chosen by the main thread: 6 on mobile (iOS backgrounds the
  // SW aggressively, high concurrency stalls) and 60 on desktop (modern
  // browsers handle 50+ parallel connections comfortably, ~10x throughput).
  const workerPool = Array(workers).fill(0).map(async () => {
    while (queue.length) {
      const item = queue.pop();
      try {
        const res = await fetch(item.url, { headers: { 'X-Download': '1' } });
        if (res.ok && res.status !== 204) {
          const cache = await caches.open(item.cacheName);
          await cache.put(item.url, res.clone());
        } else if (res.status !== 204 && !res.ok) {
          failed++;
        }
      } catch { failed++; }
      done++;
      if (client && done % 5 === 0) {
        client.postMessage({ type: 'progress', id, done, total, failed });
      }
    }
  });
  await Promise.all(workerPool);
  if (client) client.postMessage({ type: 'done', id, done, total, failed, urls: tiles });
}

async function deleteTiles(urls) {
  const cache = await caches.open(TILES_CACHE);
  for (const u of urls) await cache.delete(u);
}

async function refreshAppShell(client) {
  const cache = await caches.open(APP_CACHE);
  for (const url of APP_SHELL) {
    await cache.delete(url);
    try {
      const res = await fetch(url, { cache: 'no-store' });
      if (res.ok) await cache.put(url, res);
    } catch {}
  }
  if (client) client.postMessage({ type: 'appShellRefreshed' });
}

function tileUrls(bbox, minZoom, maxZoom) {
  const [w, s, e, n] = bbox;
  const urls = [];
  for (let z = minZoom; z <= maxZoom; z++) {
    const [xMin, yMin] = lonLatToTile(w, n, z);
    const [xMax, yMax] = lonLatToTile(e, s, z);
    for (let x = Math.min(xMin, xMax); x <= Math.max(xMin, xMax); x++) {
      for (let y = Math.min(yMin, yMax); y <= Math.max(yMin, yMax); y++) {
        urls.push(`/tiles/${z}/${x}/${y}.pbf`);
      }
    }
  }
  return urls;
}

function lonLatToTile(lon, lat, z) {
  const n = 2 ** z;
  const x = Math.floor((lon + 180) / 360 * n);
  const latRad = lat * Math.PI / 180;
  const y = Math.floor((1 - Math.log(Math.tan(latRad) + 1 / Math.cos(latRad)) / Math.PI) / 2 * n);
  return [Math.max(0, Math.min(n - 1, x)), Math.max(0, Math.min(n - 1, y))];
}
