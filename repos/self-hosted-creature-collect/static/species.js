// Species names lookup. The server ships /creature-names as a JSON array
// of lowercase names (1-indexed: array[0] == "bulbasaur" == species #1).
// We cache it in localStorage so subsequent sessions don't re-fetch.
//
//   Species.ensureLoaded() -> Promise (kick off fetch, resolves when ready)
//   Species.nameFor(idx)    -> "Bulbasaur" or "#1" if not yet loaded

(function (global) {
  'use strict';

  const STORAGE_KEY = 'cc.speciesNames';
  let _names = null;
  let _loadPromise = null;

  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) _names = JSON.parse(raw);
  } catch { /* corrupt entry — re-fetch */ }

  function ensureLoaded() {
    if (_names && _names.length) return Promise.resolve();
    if (_loadPromise) return _loadPromise;
    _loadPromise = (async () => {
      try {
        const resp = await fetch('/creature-names');
        if (!resp.ok) return;
        const list = await resp.json();
        if (Array.isArray(list) && list.length) {
          _names = list;
          try { localStorage.setItem(STORAGE_KEY, JSON.stringify(list)); } catch {}
        }
      } catch { /* offline or server missing route — fall back to "#N" */ }
      _loadPromise = null;
    })();
    return _loadPromise;
  }

  // Title-case a word, respecting spaces and hyphens so "ho-oh" becomes
  // "Ho-Oh" and "mr. mime" becomes "Mr. Mime". Apostrophes and colons are
  // treated as non-separators (so "farfetch'd" stays "Farfetch'd", "type:
  // null" becomes "Type: Null").
  function titleCase(s) {
    return s.replace(/(^|[\s-])([a-z])/g, (_, sep, ch) => sep + ch.toUpperCase());
  }

  function nameFor(idx) {
    if (!_names) return `#${idx}`;
    const raw = _names[idx - 1];
    if (!raw) return `#${idx}`;
    return titleCase(raw);
  }

  global.Species = { nameFor, ensureLoaded };
  // Intentionally no auto-fetch. ensureLoaded() is invoked from the
  // sprite bulk-download flow so network requests only happen on an
  // explicit user action.
})(typeof window !== 'undefined' ? window : globalThis);
