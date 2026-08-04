/*!
 * Fraction live co-editing client (WS11 Phase B) — fraction/static/collab.js
 * =====================================================================================
 *
 * Bridges the existing rich-text editor (`fraction/static/editor.js`) to the server-side CRDT room
 * (`fraction/collab/crdt.py`) over the `/ws/findings/<id>/blocks/<block>` websocket, using a **real,
 * vendored Yjs client** (`fraction/static/lib/yjs.mjs` + `fraction/static/lib/y-protocols/*`, see
 * `fraction/static/lib/VENDOR.md` for exact versions/provenance/verification) — not a stub. The wire
 * protocol (sync step1/step2/update + awareness) is the same one `pycrdt` implements server-side, so
 * this is genuinely interoperable, not two independent implementations that happen to agree (see
 * VENDOR.md for the cross-language interop checks that proved this before shipping).
 *
 * ── WHAT THIS DOES ────────────────────────────────────────────────────────────────────────────────
 * - Opens the collab websocket and performs the real Yjs sync + awareness handshake.
 * - Maintains a `Y.Doc` whose root `XmlFragment` mirrors the block's ProseMirror JSON via the *same*
 *   mapping as the server's `fraction/collab/pm_yjs.py` (re-implemented here in JS — see
 *   `docToYFragment`/`yFragmentToDoc` below; verified byte-for-byte against the Python module's test
 *   fixture, see VENDOR.md).
 * - On a remote peer's change arriving, re-renders the merged document into the editor via the mount
 *   handle's `setDoc()`.
 * - On the user's own edits (debounced), reads the editor's current document via `getDoc()` and applies
 *   it into the shared `Y.Doc`, whose resulting update is broadcast to every other connected peer.
 * - Wires real Yjs **awareness** for presence (who's here right now), as a live alternative to Phase A's
 *   HTTP-polling presence (`fraction/collab/presence.py`) — presence.py keeps working wherever this
 *   script isn't loaded/active.
 *
 * ── THE HONEST LIMITATION ─────────────────────────────────────────────────────────────────────────
 * True per-keystroke collaborative editing (two people typing in the same paragraph at the same time
 * with live remote cursors) requires `y-prosemirror` bound to a real ProseMirror `EditorView` — that
 * needs `fraction/static/editor.js` itself to *be* a ProseMirror/TipTap instance. It currently isn't:
 * PLAN.md §8/§16 always described it as a fallback `contenteditable` editor pending a small bundling
 * step that was never scoped, and `editor.js` isn't this workstream's file to change (see
 * `plans/CONTRACTS.md` ownership map). So this bridge operates at **whole-document granularity**: each
 * local edit (after the debounce) replaces the entire shared document's content in one CRDT transaction,
 * and each remote change re-renders the entire block. Two people editing the *same* block concurrently
 * still converge (Yjs's CRDT merge guarantees that — see the guard tests in tests/test_collab.py for the
 * server-side proof) and neither side's edit is silently dropped the way Phase A's plain autosave
 * would, but the merge granularity is "whichever document snapshot commits into the CRDT" rather than
 * character-level interleaving, and remote changes will reset local cursor position in the fallback
 * `contenteditable` surface (no character-preserving reconciliation there). Multi-cursor rendering isn't
 * implemented for the same reason — `renderPresence` here just lists names.
 *
 * ── WIRING (done by the driver/WS3, not this file — this file isn't loaded automatically) ──────────
 * `fraction/templates/fraction/_editor.html` (owned by WS3/WS4) mounts the editor via
 * `FractionEditor.mount(container, opts)`, which returns a handle `{getDoc, setDoc, save, destroy}`.
 * To turn on live collab for that block, load this file as an ES module *after* editor.js and call:
 *
 *   <script type="module">
 *     import { attach, isSupported } from "{{ url_for('fraction.static', filename='collab.js') }}";
 *     var handle = FractionEditor.mount(containerEl, opts);
 *     if (isSupported()) {
 *       var collab = attach(containerEl, handle, {
 *         findingId: opts.findingId, block: opts.block, apiBase: opts.apiBase, user: opts.user,
 *       });
 *       window.addEventListener('pagehide', function () { collab.destroy(); });
 *     }
 *   </script>
 *
 * (`window.FractionCollab.attach`/`.isSupported` are also exposed as a non-module-script fallback.)
 * This is opt-in and additive: a page that never calls `attach()` behaves exactly as it does today
 * (Phase A autosave + polling presence only).
 */

import * as Y from "./lib/yjs.mjs";
import * as syncProtocol from "./lib/y-protocols/sync.js";
import * as awarenessProtocol from "./lib/y-protocols/awareness.js";
import * as encoding from "./lib/lib0/encoding.js";
import * as decoding from "./lib/lib0/decoding.js";

// ---------------------------------------------------------------------------- wire message types
// Matches fraction/collab/crdt.py exactly (pycrdt.YMessageType): 0 = sync, 1 = awareness.
var MESSAGE_SYNC = 0;
var MESSAGE_AWARENESS = 1;

// ---------------------------------------------------------------------------- ProseMirror JSON <-> Yjs
// A JS port of fraction/collab/pm_yjs.py's mapping. Kept in lockstep with that module -- see
// fraction/static/lib/VENDOR.md for how the two were verified to agree on an identical fixture.

var PARAGRAPH = "paragraph", TEXT = "text", HEADING = "heading";
var BULLET_LIST = "bulletList", ORDERED_LIST = "orderedList", LIST_ITEM = "listItem";
var BLOCKQUOTE = "blockquote", CODE_BLOCK = "codeBlock", HARD_BREAK = "hardBreak";
var IMAGE = "image", VARIABLE = "variable", INLINE_IMAGE = "inlineImage", FIGURE = "figure";
var DOC_TYPE = "doc";

var INLINE_CONTENT_TYPES = { paragraph: 1, heading: 1, codeBlock: 1, figure: 1 };
var BLOCK_CONTENT_TYPES = { bulletList: 1, orderedList: 1, listItem: 1, blockquote: 1 };
var INLINE_LEAF_TYPES = { text: 1, hardBreak: 1, image: 1, variable: 1, inlineImage: 1 };
// Leaf types carry NO child content as their own block element (kept in lockstep with pm_yjs.py's
// _LEAF_TYPES -- avoids a spurious `content: []` churning content_json on a no-op round trip; N1).
var LEAF_TYPES = { hardBreak: 1, image: 1, variable: 1, inlineImage: 1 };

function canonicalMarks(marks) {
  return marks.slice().sort(function (a, b) {
    return a.type < b.type ? -1 : a.type > b.type ? 1 : 0;
  });
}

function isInlineHolder(nodeType, content) {
  if (INLINE_CONTENT_TYPES[nodeType]) return true;
  if (BLOCK_CONTENT_TYPES[nodeType] || nodeType === DOC_TYPE) return false;
  return content.length > 0 && !!INLINE_LEAF_TYPES[content[0].type];
}

function appendNode(parent, node) {
  var nodeType = node.type || "unknown";
  var el = new Y.XmlElement(nodeType);
  parent.insert(parent.length, [el]);
  var attrs = node.attrs || {};
  Object.keys(attrs).forEach(function (k) {
    el.setAttribute(k, attrs[k]);
  });
  if (LEAF_TYPES[nodeType]) return; // leaf: attributes only, never child content (N1)
  var content = node.content || [];
  if (isInlineHolder(nodeType, content)) {
    var text = new Y.XmlText();
    el.insert(0, [text]);
    populateInline(text, content);
  } else {
    content.forEach(function (child) {
      appendNode(el, child);
    });
  }
}

function populateInline(xmltext, content) {
  var index = 0;
  content.forEach(function (node) {
    if (node.type === TEXT) {
      var text = node.text || "";
      var marks = node.marks || [];
      var fmt = {};
      marks.forEach(function (m) {
        fmt[m.type] = m.attrs && Object.keys(m.attrs).length ? m.attrs : true;
      });
      xmltext.insert(index, text, fmt);
      index += text.length;
    } else {
      var attrs = node.attrs || {};
      xmltext.insertEmbed(index, { type: node.type, attrs: attrs }, {});
      index += 1;
    }
  });
}

function xmlElementToNode(el) {
  var nodeType = el.nodeName;
  var attrs = el.getAttributes();
  var node = { type: nodeType };
  if (attrs && Object.keys(attrs).length) node.attrs = attrs;
  if (LEAF_TYPES[nodeType]) return node; // leaf: no content key (mirrors appendNode / pm_yjs.py; N1)
  var children = el.toArray();
  if (children.length === 1 && children[0] instanceof Y.XmlText) {
    node.content = readInline(children[0]);
  } else {
    node.content = children.map(xmlElementToNode);
  }
  return node;
}

function readInline(xmltext) {
  var out = [];
  xmltext.toDelta().forEach(function (op) {
    var value = op.insert;
    var fmt = op.attributes;
    if (typeof value === "string") {
      var textNode = { type: TEXT, text: value };
      if (fmt && Object.keys(fmt).length) {
        var marks = Object.keys(fmt).map(function (k) {
          return fmt[k] === true ? { type: k } : { type: k, attrs: fmt[k] };
        });
        textNode.marks = canonicalMarks(marks);
      }
      out.push(textNode);
    } else {
      var embed = value || {};
      var leaf = { type: embed.type || "unknown" };
      if (embed.attrs && Object.keys(embed.attrs).length) leaf.attrs = embed.attrs;
      out.push(leaf);
    }
  });
  return out;
}

/** Replace `fragment`'s entire content with `pmDoc.content`. Caller must wrap in `ydoc.transact()`. */
function docToYFragment(pmDoc, fragment) {
  var content = (pmDoc && pmDoc.content) || [];
  if (fragment.length) fragment.delete(0, fragment.length);
  content.forEach(function (child) {
    appendNode(fragment, child);
  });
}

/** Render `fragment` back to a ProseMirror JSON doc. */
function yFragmentToDoc(fragment) {
  return { type: DOC_TYPE, content: fragment.toArray().map(xmlElementToNode) };
}

// ---------------------------------------------------------------------------- websocket URL

function deriveWsUrl(opts) {
  if (opts.wsUrl) return opts.wsUrl;
  var apiBase = opts.apiBase || "/fraction/api";
  var urlPrefix = apiBase.replace(/\/api\/?$/, "");
  var path = urlPrefix + "/ws/findings/" + opts.findingId + "/blocks/" + encodeURIComponent(opts.block);
  var scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
  return scheme + "//" + window.location.host + path;
}

// ---------------------------------------------------------------------------- attach()

var RECONNECT_BASE_MS = 1000;
var RECONNECT_MAX_MS = 15000;
var LOCAL_EDIT_DEBOUNCE_MS = 600;

/**
 * Wire live CRDT co-editing onto an already-mounted editor.
 *
 * @param {HTMLElement} container the element passed to FractionEditor.mount()
 * @param {{getDoc:Function,setDoc:Function}} handle the handle FractionEditor.mount() returned
 * @param {{findingId:number,block:string,apiBase?:string,wsUrl?:string,user?:string,
 *          onPresenceChange?:Function}} opts
 * @returns {{destroy:Function, doc:Y.Doc, awareness:Awareness}}
 */
export function attach(container, handle, opts) {
  opts = opts || {};
  var user = opts.user || "anonymous";
  var wsUrl = deriveWsUrl(opts);

  var ydoc = new Y.Doc();
  var fragment = ydoc.getXmlFragment("prosemirror");
  var awareness = new awarenessProtocol.Awareness(ydoc);
  awareness.setLocalState({ user: user, block: opts.block });

  // Sentinel marking "this transaction/state change came from the network" so the update/awareness
  // observers below don't immediately re-broadcast back to the server the very thing it just told us
  // (the standard y-websocket echo-avoidance pattern).
  var REMOTE_ORIGIN = {};

  var socket = null;
  var destroyed = false;
  var reconnectDelay = RECONNECT_BASE_MS;
  var reconnectTimer = null;
  var lastPushedJson = null;
  var editableEl = container && container.querySelector && container.querySelector(".fr-editor-surface");
  var localEditTimer = null;

  function send(bytes) {
    if (socket && socket.readyState === WebSocket.OPEN) socket.send(bytes);
  }

  function sendSyncStep1() {
    var encoder = encoding.createEncoder();
    encoding.writeVarUint(encoder, MESSAGE_SYNC);
    syncProtocol.writeSyncStep1(encoder, ydoc);
    send(encoding.toUint8Array(encoder));
  }

  function sendAwareness(changedClientIds) {
    var encoder = encoding.createEncoder();
    encoding.writeVarUint(encoder, MESSAGE_AWARENESS);
    encoding.writeVarUint8Array(encoder, awarenessProtocol.encodeAwarenessUpdate(awareness, changedClientIds));
    send(encoding.toUint8Array(encoder));
  }

  function handleMessage(event) {
    var bytes = new Uint8Array(event.data);
    var decoder = decoding.createDecoder(bytes);
    var messageType = decoding.readVarUint(decoder);
    if (messageType === MESSAGE_SYNC) {
      var encoder = encoding.createEncoder();
      encoding.writeVarUint(encoder, MESSAGE_SYNC);
      syncProtocol.readSyncMessage(decoder, encoder, ydoc, REMOTE_ORIGIN);
      if (encoding.length(encoder) > 1) send(encoding.toUint8Array(encoder));
    } else if (messageType === MESSAGE_AWARENESS) {
      awarenessProtocol.applyAwarenessUpdate(awareness, decoding.readVarUint8Array(decoder), REMOTE_ORIGIN);
    }
  }

  // A single observer handles both directions: broadcast our own edits, apply-and-render remote ones.
  // Cf. the module docstring's "honest limitation" -- re-rendering on every remote change means the
  // fallback contenteditable surface's caret is not preserved across a remote update.
  ydoc.on("update", function (update, origin) {
    if (origin === REMOTE_ORIGIN) {
      try {
        var doc = yFragmentToDoc(fragment);
        lastPushedJson = JSON.stringify(doc);
        handle.setDoc(doc);
      } catch (e) {
        console.error("[fraction-collab] failed to render remote update", e);
      }
      return;
    }
    var encoder = encoding.createEncoder();
    encoding.writeVarUint(encoder, MESSAGE_SYNC);
    syncProtocol.writeUpdate(encoder, update);
    send(encoding.toUint8Array(encoder));
  });

  awareness.on("update", function (changes, origin) {
    if (origin === REMOTE_ORIGIN) {
      if (typeof opts.onPresenceChange === "function") {
        var others = [];
        awareness.getStates().forEach(function (state, clientId) {
          if (clientId !== ydoc.clientID && state && state.user) others.push(state);
        });
        opts.onPresenceChange(others);
      }
      return;
    }
    var changed = changes.added.concat(changes.updated, changes.removed);
    if (changed.length) sendAwareness(changed);
  });

  function pushLocalDoc() {
    var doc;
    try {
      doc = handle.getDoc();
    } catch (e) {
      return;
    }
    var serialized = JSON.stringify(doc);
    if (serialized === lastPushedJson) return; // no-op guard: nothing actually changed
    lastPushedJson = serialized;
    ydoc.transact(function () {
      docToYFragment(doc, fragment);
    });
  }

  function scheduleLocalPush() {
    clearTimeout(localEditTimer);
    localEditTimer = setTimeout(pushLocalDoc, LOCAL_EDIT_DEBOUNCE_MS);
  }

  if (editableEl) {
    editableEl.addEventListener("input", scheduleLocalPush);
  }

  function connect() {
    if (destroyed) return;
    socket = new WebSocket(wsUrl);
    socket.binaryType = "arraybuffer";
    socket.onopen = function () {
      reconnectDelay = RECONNECT_BASE_MS;
      sendSyncStep1();
      sendAwareness([ydoc.clientID]);
    };
    socket.onmessage = handleMessage;
    socket.onclose = scheduleReconnect;
    socket.onerror = function () {
      try {
        socket.close();
      } catch (e) {
        /* already closing */
      }
    };
  }

  function scheduleReconnect() {
    if (destroyed) return;
    reconnectTimer = setTimeout(function () {
      reconnectDelay = Math.min(reconnectDelay * 2, RECONNECT_MAX_MS);
      connect();
    }, reconnectDelay);
  }

  connect();

  return {
    doc: ydoc,
    awareness: awareness,
    destroy: function () {
      destroyed = true;
      clearTimeout(reconnectTimer);
      clearTimeout(localEditTimer);
      if (editableEl) editableEl.removeEventListener("input", scheduleLocalPush);
      try {
        awarenessProtocol.removeAwarenessStates(awareness, [ydoc.clientID], "destroy");
      } catch (e) {
        /* ignore */
      }
      if (socket) {
        try {
          socket.close();
        } catch (e) {
          /* ignore */
        }
      }
    },
  };
}

export function isSupported() {
  return typeof WebSocket !== "undefined";
}

// Non-module-script fallback (mirrors how fraction/static/editor.js exposes window.FractionEditor),
// in case the host page can't load this as `type="module"`.
if (typeof window !== "undefined") {
  window.FractionCollab = {
    attach: attach,
    isSupported: isSupported,
    // Exposed for tests/tooling and for a future y-prosemirror adapter that wants to reuse the mapping
    // instead of reimplementing it.
    _internal: { docToYFragment: docToYFragment, yFragmentToDoc: yFragmentToDoc },
  };
}
