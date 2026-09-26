// Where the sandbox keeps its world between visits: one JSON text (the same
// text Export writes) under one key.  Every store is {load, save, clear},
// all async; load() resolves to null when there is nothing saved.
//
// idbStore keeps it in the browser's IndexedDB, private to this page's
// origin.  Storage can be missing or refused (a private window, blocked
// site data): then the sandbox runs from memory and says so.

const DB = "alberta-buck-sandbox";
const STORE = "worlds";
const KEY = "current";

/** A store that forgets on reload (tests; browsers without storage). */
export function memoryStore(text = null) {
  let saved = text;
  return {
    persistent: false,
    load: async () => saved,
    save: async (t) => { saved = t; },
    clear: async () => { saved = null; },
  };
}

const done = (req) => new Promise((resolve, reject) => {
  req.onsuccess = () => resolve(req.result);
  req.onerror = () => reject(req.error);
});

/** The browser's IndexedDB, or a memoryStore when it is unavailable. */
export async function idbStore(name = DB) {
  let db;
  try {
    const open = globalThis.indexedDB.open(name, 1);
    open.onupgradeneeded = () => open.result.createObjectStore(STORE);
    db = await done(open);
  } catch {
    return memoryStore();
  }
  const tx = (mode, fn) => {
    const store = db.transaction(STORE, mode).objectStore(STORE);
    return done(fn(store));
  };
  return {
    persistent: true,
    load: async () => (await tx("readonly", (s) => s.get(KEY))) ?? null,
    save: (text) => tx("readwrite", (s) => s.put(text, KEY)),
    clear: () => tx("readwrite", (s) => s.delete(KEY)),
  };
}
