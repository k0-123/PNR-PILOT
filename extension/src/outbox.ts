// IndexedDB outbox: every capture/status is stored here first and removed only after the server
// answers 2xx (or refuses it for good). Nothing is lost if the network or the backend blips.

export interface OutboxItem {
  id?: number;
  jobId: number;
  pnr: string;
  kind: "capture" | "status";
  body: Record<string, unknown>;
  tries: number;
  nextAt: number; // epoch ms
  lastError?: string;
}

const DB_NAME = "gds-outbox";
const STORE = "items";

function open(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, 1);
    req.onupgradeneeded = () => req.result.createObjectStore(STORE, { keyPath: "id", autoIncrement: true });
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function run<T>(mode: IDBTransactionMode, fn: (s: IDBObjectStore) => IDBRequest<T>): Promise<T> {
  const db = await open();
  try {
    return await new Promise<T>((resolve, reject) => {
      const tx = db.transaction(STORE, mode);
      const req = fn(tx.objectStore(STORE));
      tx.oncomplete = () => resolve(req.result);
      tx.onerror = () => reject(tx.error);
      tx.onabort = () => reject(tx.error);
    });
  } finally {
    db.close();
  }
}

export const outbox = {
  add: (item: Omit<OutboxItem, "id">) => run("readwrite", (s) => s.add(item)),
  all: () => run<OutboxItem[]>("readonly", (s) => s.getAll() as IDBRequest<OutboxItem[]>),
  put: (item: OutboxItem) => run("readwrite", (s) => s.put(item)),
  remove: (id: number) => run("readwrite", (s) => s.delete(id)),
  count: () => run<number>("readonly", (s) => s.count()),
};
