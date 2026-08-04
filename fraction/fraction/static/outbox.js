/*!
 * fraction/static/outbox.js — WS14 client resilience: a dependency-free, IndexedDB-backed upload
 * outbox. PLAN.md §19 "don't lose in-progress work (crash / offline)".
 *
 * Why IndexedDB and not localStorage: localStorage can only hold strings, and the whole point of this
 * module is to durably queue *file bytes* (pasted screenshots, dropped files) across a reload/crash --
 * you cannot base64 a multi-MB screenshot into localStorage without blowing the quota and burning CPU.
 * IndexedDB stores Blobs natively (structured clone) and survives a tab close/reload, which is exactly
 * what "the network drops mid-upload" or "the server crashes mid-upload" needs.
 *
 * Contract (consumed by fraction/static/artifacts.js and fraction/static/editor.js):
 *
 *   var tempId = FractionOutbox.enqueueUpload({
 *     tempId: "optional-caller-chosen-id",   // generated if omitted; returned either way
 *     url: "/fraction/api/artifacts",         // POST target -- reused as-is, no new server routes
 *     blob: fileOrBlob,
 *     filename: "screenshot.png",
 *     fields: { engagement_id: "1", finding_id: "2", caption: "..." },  // extra multipart fields
 *   });
 *
 *   FractionOutbox.on("resolved", function (tempId, serverJson, op) { ... });  // 2xx: op succeeded
 *   FractionOutbox.on("failed",   function (tempId, error, op) { ... });      // 4xx: dropped, won't retry
 *   FractionOutbox.pendingCount() -> number                                   // outbox + external pending
 *   FractionOutbox.isGuardArmed() -> boolean                                  // beforeunload currently armed
 *   FractionOutbox.setExternalPending(key, isPending)  // let other subsystems (e.g. editor.js's text
 *                                                       // autosave) share this module's beforeunload guard
 *   FractionOutbox.flush() -> Promise<void>            // re-attempt every durably-queued pending op now
 *
 * Retry policy: a network error or an HTTP 5xx (or any other non-2xx/4xx status) is treated as
 * *transient* -- the op stays queued and is retried with capped exponential backoff + jitter (so many
 * ops recovering at once after an outage don't all hammer the server in lockstep). An HTTP 4xx is
 * treated as a *permanent* rejection (bad request -- retrying won't help) -- the op is dropped
 * immediately and "failed" fires so the UI can show a retry *affordance* (re-enqueueing is a fresh,
 * caller-driven decision, not something this module does on its own for 4xx).
 *
 * Auto-flush on load: this file's IIFE calls init() at the bottom, which loads every durably-queued
 * "pending" op left over from a previous page life (e.g. the tab was closed, or the server/network was
 * down when the op was queued) and immediately (re)attempts it. That is what makes "queue an upload
 * while offline, reload, reconnect" self-heal without any caller action.
 */
(function (window) {
  "use strict";

  // Re-including this script tag more than once on a page (defensive; every current call site loads it
  // exactly once) must not double-initialize IndexedDB or double-arm listeners.
  if (window.FractionOutbox) return;

  var DB_NAME = "fraction-outbox";
  var DB_VERSION = 1;
  var STORE = "uploads";
  var LOCK_NAME = "fraction-outbox-flush";

  // Retry/backoff tunables. Defaults are the production values; a page may override them by setting
  // `window.__fractionOutboxConfig` BEFORE this script loads (used by the e2e suite to exercise the
  // attempt cap without waiting out the full production backoff schedule).
  var _cfg = (window && window.__fractionOutboxConfig) || {};
  var BASE_DELAY_MS = _cfg.baseDelayMs || 300;
  var MAX_DELAY_MS = _cfg.maxDelayMs || 8000;
  var BACKOFF_FACTOR = _cfg.backoffFactor || 2;
  // W3: cap total attempts so a permanently-failing transient (server down forever, a blob the server
  // keeps 5xx-ing on) can't retry forever -- which would pin pendingCount()>0, keep the beforeunload
  // guard armed indefinitely, and never drain the op from IndexedDB. After the cap the op is treated as
  // a permanent failure (fires "failed", storeDelete, decrementPending) exactly like a 4xx.
  var MAX_ATTEMPTS = _cfg.maxAttempts || 8;

  var _dbPromise = null;

  // In-memory fallback store, used only when IndexedDB is entirely unavailable/broken (old browser,
  // private-mode restrictions, blocked by policy) or when a specific op's IDB write fails (e.g. quota
  // exceeded storing a large blob). Ops here still get a real, retried upload attempt within this page
  // life -- they just won't survive a reload, which is the honestly-documented degraded behavior for
  // that edge case (see docs/_patches/ws14-resilience.md).
  var _memoryOps = {};

  var _inFlight = {}; // tempId -> true while a fetch attempt for that op is outstanding (dedupe guard)
  var _timers = {}; // tempId -> pending retry setTimeout handle
  var _settlers = {}; // tempId -> resolve fn; resolved when an op reaches a terminal state (W2 flush lock)
  var _pendingTempIds = {}; // tempId -> true, for ops this module is tracking as unresolved
  var _externalPending = {}; // key -> true, for callers sharing the beforeunload guard (e.g. text autosave)
  var _listeners = { resolved: [], failed: [], change: [] };
  var _guardArmed = false;
  var _initPromise = null;

  // ------------------------------------------------------------------------------- IndexedDB plumbing

  function openDb() {
    if (_dbPromise) return _dbPromise;
    _dbPromise = new Promise(function (resolve) {
      if (!window.indexedDB) {
        resolve(null);
        return;
      }
      var req;
      try {
        req = window.indexedDB.open(DB_NAME, DB_VERSION);
      } catch (e) {
        resolve(null);
        return;
      }
      req.onupgradeneeded = function () {
        var db = req.result;
        if (!db.objectStoreNames.contains(STORE)) {
          db.createObjectStore(STORE, { keyPath: "tempId" });
        }
      };
      req.onsuccess = function () {
        resolve(req.result);
      };
      req.onerror = function () {
        resolve(null);
      };
      req.onblocked = function () {
        resolve(null);
      };
    });
    return _dbPromise;
  }

  // Durably persist an op. Falls back to the in-memory store (silently, not an error the caller needs
  // to handle) if IndexedDB is unavailable or the write itself fails (quota, DataCloneError, etc.) -- the
  // upload attempt proceeds regardless; only reload-survival is lost for that op.
  function storePut(op) {
    return openDb().then(function (db) {
      if (!db) {
        _memoryOps[op.tempId] = op;
        return;
      }
      return new Promise(function (resolve) {
        try {
          var tx = db.transaction(STORE, "readwrite");
          tx.objectStore(STORE).put(op);
          tx.oncomplete = function () {
            resolve();
          };
          tx.onerror = function () {
            _memoryOps[op.tempId] = op;
            resolve();
          };
          tx.onabort = function () {
            _memoryOps[op.tempId] = op;
            resolve();
          };
        } catch (e) {
          _memoryOps[op.tempId] = op;
          resolve();
        }
      });
    });
  }

  function storeUpdate(tempId, patch) {
    if (_memoryOps[tempId]) {
      Object.keys(patch).forEach(function (k) {
        _memoryOps[tempId][k] = patch[k];
      });
    }
    return openDb().then(function (db) {
      if (!db) return;
      return new Promise(function (resolve) {
        try {
          var tx = db.transaction(STORE, "readwrite");
          var store = tx.objectStore(STORE);
          var getReq = store.get(tempId);
          getReq.onsuccess = function () {
            var rec = getReq.result;
            if (rec) {
              Object.keys(patch).forEach(function (k) {
                rec[k] = patch[k];
              });
              store.put(rec);
            }
          };
          getReq.onerror = function () {};
          tx.oncomplete = function () {
            resolve();
          };
          tx.onerror = function () {
            resolve();
          };
          tx.onabort = function () {
            resolve();
          };
        } catch (e) {
          resolve();
        }
      });
    });
  }

  function storeDelete(tempId) {
    delete _memoryOps[tempId];
    return openDb().then(function (db) {
      if (!db) return;
      return new Promise(function (resolve) {
        try {
          var tx = db.transaction(STORE, "readwrite");
          tx.objectStore(STORE).delete(tempId);
          tx.oncomplete = function () {
            resolve();
          };
          tx.onerror = function () {
            resolve();
          };
          tx.onabort = function () {
            resolve();
          };
        } catch (e) {
          resolve();
        }
      });
    });
  }

  // Every op durably queued (status "pending") from *any* page life -- this is what auto-flush-on-load
  // reads to resume ops a previous tab left behind after a crash/close while offline.
  function storeGetAllPending() {
    return openDb().then(function (db) {
      if (!db) {
        return objectValues(_memoryOps);
      }
      return new Promise(function (resolve) {
        var out = [];
        try {
          var tx = db.transaction(STORE, "readonly");
          var store = tx.objectStore(STORE);
          if (typeof store.getAll === "function") {
            var req = store.getAll();
            req.onsuccess = function () {
              out = req.result || [];
            };
            req.onerror = function () {};
          } else {
            var cur = store.openCursor();
            cur.onsuccess = function (ev) {
              var c = ev.target.result;
              if (c) {
                out.push(c.value);
                c.continue();
              }
            };
            cur.onerror = function () {};
          }
          tx.oncomplete = function () {
            resolve(out);
          };
          tx.onerror = function () {
            resolve([]);
          };
          tx.onabort = function () {
            resolve([]);
          };
        } catch (e) {
          resolve([]);
        }
      });
    });
  }

  function objectValues(obj) {
    return Object.keys(obj).map(function (k) {
      return obj[k];
    });
  }

  // ------------------------------------------------------------------------------- pending / guard

  function incrementPending(tempId) {
    if (!_pendingTempIds[tempId]) {
      _pendingTempIds[tempId] = true;
      afterPendingChange();
    }
  }

  function decrementPending(tempId) {
    if (_pendingTempIds[tempId]) {
      delete _pendingTempIds[tempId];
      afterPendingChange();
    }
  }

  function afterPendingChange() {
    updateGuard();
    emit("change", pendingCount());
  }

  function pendingCount() {
    return Object.keys(_pendingTempIds).length + Object.keys(_externalPending).length;
  }

  function setExternalPending(key, isPending) {
    if (isPending) {
      if (!_externalPending[key]) {
        _externalPending[key] = true;
        afterPendingChange();
      }
    } else if (_externalPending[key]) {
      delete _externalPending[key];
      afterPendingChange();
    }
  }

  function updateGuard() {
    var should = pendingCount() > 0;
    if (should && !_guardArmed) {
      window.addEventListener("beforeunload", onBeforeUnload);
      _guardArmed = true;
    } else if (!should && _guardArmed) {
      window.removeEventListener("beforeunload", onBeforeUnload);
      _guardArmed = false;
    }
  }

  function onBeforeUnload(ev) {
    var msg = "Uploads or edits are still saving.";
    ev.preventDefault();
    ev.returnValue = msg;
    return msg;
  }

  function isGuardArmed() {
    return _guardArmed;
  }

  // ------------------------------------------------------------------------------- event emitter

  function on(event, fn) {
    if (!_listeners[event]) _listeners[event] = [];
    _listeners[event].push(fn);
  }

  function off(event, fn) {
    if (!_listeners[event]) return;
    var idx = _listeners[event].indexOf(fn);
    if (idx !== -1) _listeners[event].splice(idx, 1);
  }

  function emit(event) {
    var args = Array.prototype.slice.call(arguments, 1);
    (_listeners[event] || []).slice().forEach(function (fn) {
      try {
        fn.apply(null, args);
      } catch (e) {
        // A listener throwing must never break the outbox's own retry/reconciliation bookkeeping.
        if (window.console && window.console.error) {
          window.console.error("[fraction-outbox] listener error", e);
        }
      }
    });
  }

  // ------------------------------------------------------------------------------- upload execution

  function genId() {
    if (window.crypto && typeof window.crypto.randomUUID === "function") {
      return window.crypto.randomUUID();
    }
    return "tmp-" + Date.now() + "-" + Math.random().toString(36).slice(2, 10);
  }

  function enqueueUpload(opts) {
    opts = opts || {};
    var tempId = opts.tempId || genId();
    var op = {
      tempId: tempId,
      url: opts.url,
      filename: opts.filename || "upload",
      fields: opts.fields || {},
      blob: opts.blob,
      // W1: optional, serializable dedupe descriptor {listUrl, filename, notBeforeId}. Consulted only
      // on a network error (no HTTP response received) to detect the "the POST actually landed but the
      // success response was lost" case and avoid a duplicate row. Serializable so it survives reload.
      dedupe: opts.dedupe || null,
      attempts: 0,
      createdAt: Date.now(),
    };
    incrementPending(tempId);
    storePut(op).then(function () {
      runOp(op);
    });
    return tempId;
  }

  // Terminal-state signalling (W2): flush() awaits these so it can hold the cross-tab lock until every
  // op it is responsible for has actually finished, not merely been kicked off. Multiple awaiters per
  // op are supported (flush may attach to an op enqueue already started in this tab).
  function settlePromiseFor(tempId) {
    return new Promise(function (resolve) {
      (_settlers[tempId] = _settlers[tempId] || []).push(resolve);
    });
  }

  function settle(tempId) {
    var arr = _settlers[tempId];
    if (arr) {
      delete _settlers[tempId];
      arr.forEach(function (r) {
        try {
          r();
        } catch (e) {
          /* ignore */
        }
      });
    }
  }

  // Executes (or re-executes) one attempt for `op`. Safe to call repeatedly for the same op -- the
  // in-flight guard means a second call while a fetch is already outstanding is a no-op, so a manual
  // flush() racing an already-scheduled retry timer can never fire two concurrent POSTs for one op.
  function runOp(op) {
    if (_inFlight[op.tempId]) return;
    _inFlight[op.tempId] = true;
    if (_timers[op.tempId]) {
      clearTimeout(_timers[op.tempId]);
      delete _timers[op.tempId];
    }

    var fd = new FormData();
    fd.append("file", op.blob, op.filename || "upload");
    var fields = op.fields || {};
    Object.keys(fields).forEach(function (k) {
      var v = fields[k];
      if (v !== undefined && v !== null) fd.append(k, String(v));
    });

    fetch(op.url, { method: "POST", credentials: "same-origin", body: fd })
      .then(function (res) {
        if (res.ok) {
          return res
            .json()
            .catch(function () {
              return {};
            })
            .then(function (data) {
              resolveOp(op, data);
            });
        }
        if (res.status >= 400 && res.status < 500) {
          return res
            .json()
            .catch(function () {
              return {};
            })
            .then(function (data) {
              failOp(op, { status: res.status, body: data });
            });
        }
        // 5xx (or anything else non-2xx/4xx) is transient from the client's point of view: the server
        // received the request and errored (so no row was created) -- retry, no dedupe needed.
        retryOp(op, { status: res.status });
      })
      .catch(function (err) {
        // fetch() rejects on network failure (offline, connection reset, an aborted route in tests).
        // Here it's ambiguous whether the POST reached the server: if it did and only the *response*
        // was lost, a naive retry would create a duplicate. W1: when a dedupe descriptor is present,
        // check whether the row already landed before re-POSTing.
        var netError = { networkError: true, message: err && err.message };
        if (op.dedupe) {
          checkDedupe(op).then(function (match) {
            if (match) resolveOp(op, match);
            else retryOp(op, netError);
          });
        } else {
          retryOp(op, netError);
        }
      });
  }

  // W1: GET the finding's current artifacts and return the row that this op most likely already
  // created, or null. Matches on filename and (when known) requires the row to be newer than any that
  // existed when the op was enqueued (`notBeforeId`), which keeps a pre-existing same-named artifact
  // from being mistaken for this upload. A failed GET returns null -> the op just retries.
  function checkDedupe(op) {
    var d = op.dedupe;
    if (!d || !d.listUrl) return Promise.resolve(null);
    return fetch(d.listUrl, { credentials: "same-origin" })
      .then(function (res) {
        return res.ok ? res.json() : null;
      })
      .then(function (data) {
        var arts = (data && data.artifacts) || [];
        for (var i = 0; i < arts.length; i++) {
          var a = arts[i];
          if (d.filename != null && a.filename !== d.filename) continue;
          if (d.notBeforeId != null && !(a.id > d.notBeforeId)) continue;
          return a; // {id, url, filename, ...} -- enough for callers' resolved handlers
        }
        return null;
      })
      .catch(function () {
        return null;
      });
  }

  function resolveOp(op, data) {
    delete _inFlight[op.tempId];
    storeDelete(op.tempId).then(function () {
      decrementPending(op.tempId);
      emit("resolved", op.tempId, data, op);
      settle(op.tempId);
    });
  }

  function failOp(op, error) {
    delete _inFlight[op.tempId];
    storeDelete(op.tempId).then(function () {
      decrementPending(op.tempId);
      emit("failed", op.tempId, error, op);
      settle(op.tempId);
    });
  }

  function retryOp(op, error) {
    delete _inFlight[op.tempId];
    op.attempts = (op.attempts || 0) + 1;
    if (op.attempts >= MAX_ATTEMPTS) {
      // W3: give up rather than retry forever -- treat an exhausted transient like a permanent failure
      // so the queue drains and the beforeunload guard can clear.
      failOp(op, { exhausted: true, attempts: op.attempts, lastError: error });
      return;
    }
    var delay = Math.min(MAX_DELAY_MS, BASE_DELAY_MS * Math.pow(BACKOFF_FACTOR, op.attempts - 1));
    delay = delay * (0.75 + Math.random() * 0.5); // jitter: avoid every queued op retrying in lockstep
    storeUpdate(op.tempId, { attempts: op.attempts });
    _timers[op.tempId] = setTimeout(function () {
      runOp(op);
    }, delay);
    emit("retry-scheduled", op.tempId, error, op.attempts, delay);
  }

  // W2: two tabs share the per-origin IndexedDB queue, so if both auto-flushed the same durable ops
  // they'd each POST them -> duplicate artifacts. Serialize flush() across tabs with the Web Locks
  // API, holding the exclusive lock until every op this flush is responsible for has settled (so a
  // second tab that acquires the lock afterward finds the queue already drained). Degrades to running
  // lock-free where navigator.locks is unavailable.
  function withLock(fn) {
    try {
      if (window.navigator && navigator.locks && typeof navigator.locks.request === "function") {
        return navigator.locks.request(LOCK_NAME, { mode: "exclusive" }, function () {
          return fn();
        });
      }
    } catch (e) {
      /* fall through to lock-free */
    }
    return Promise.resolve().then(fn);
  }

  // Re-attempts every durably-queued pending op right now, under the cross-tab lock. Used both for the
  // automatic "auto-flush-on-load" pass (below) and as a manually callable API (e.g. an explicit
  // "retry now" control or a page's own `online`-event listener).
  function flush() {
    return withLock(function () {
      return storeGetAllPending().then(function (ops) {
        return Promise.all(
          ops.map(function (op) {
            var waiter = settlePromiseFor(op.tempId);
            if (!_inFlight[op.tempId]) {
              incrementPending(op.tempId);
              runOp(op);
            }
            return waiter;
          })
        );
      });
    }).catch(function () {
      // Never let a flush rejection escape (e.g. a lock-callback error) -- the guard/queue bookkeeping
      // is driven by per-op resolve/fail, not by this promise.
    });
  }

  function init() {
    if (_initPromise) return _initPromise;
    _initPromise = flush();
    return _initPromise;
  }

  window.FractionOutbox = {
    enqueueUpload: enqueueUpload,
    flush: flush,
    pendingCount: pendingCount,
    hasPending: function () {
      return pendingCount() > 0;
    },
    isGuardArmed: isGuardArmed,
    setExternalPending: setExternalPending,
    on: on,
    off: off,
  };

  init();
})(window);
