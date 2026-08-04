# Vendored client-side CRDT libraries (WS11 Phase B)

Fetched from the public npm registry (network was reachable from this build environment) and vendored
verbatim except for the mechanical import-specifier rewrite noted below — **no CDN reference at
runtime**, matching Lotek's CSP-strict hosting requirement. All three are MIT-licensed (see the adjacent
`LICENSE-*.txt` files).

| Package | Version | Path | Upstream |
|---|---|---|---|
| `yjs` | 13.6.18 | `yjs.mjs` (from the package's `dist/yjs.mjs`) | https://github.com/yjs/yjs |
| `lib0` | 0.2.99 | `lib0/*.js` | https://github.com/dmonad/lib0 |
| `y-protocols` | 1.0.6 | `y-protocols/sync.js`, `y-protocols/awareness.js` | https://github.com/yjs/y-protocols |

## What's here and why

- `yjs.mjs` — the real, official Yjs CRDT core (the same JS implementation `pycrdt`, Fraction's *server*
  CRDT, is a Rust/Python port of — `fraction/collab/crdt.py` speaks the exact wire protocol these two
  implement). Unmodified except for the import rewrite below.
- `lib0/*.js` — the transitive closure of `lib0` submodules `yjs.mjs` (and `y-protocols`) actually import
  (29 files: `array`, `binary`, `buffer`, `conditions`, `decoding`, `dom`, `encoding`, `environment`,
  `error`, `eventloop`, `function`, `iterator`, `json`, `logging`, `logging.common`, `map`, `math`,
  `metric`, `number`, `object`, `observable`, `pair`, `promise`, `random`, `set`, `storage`, `string`,
  `symbol`, `time`, `webcrypto`) — copied verbatim; lib0's own source already uses relative `./x.js`
  imports between its submodules, so nothing needed rewriting there.
- `y-protocols/sync.js`, `y-protocols/awareness.js` — the reference implementation of the Yjs sync and
  awareness wire protocols (message framing, `readSyncMessage`, `Awareness` class). `pycrdt`'s
  `handle_sync_message`/`create_sync_message`/`Awareness` (used by `fraction/collab/crdt.py`) are a
  faithful port of these exact modules, so a client built on these vendored files and Fraction's Python
  server are talking the *same* protocol, not two independent implementations that happen to agree.
  `y-protocols/auth.js` was **not** vendored — Fraction doesn't use protocol-level auth (the websocket
  route inherits whatever session/CSRF the host app puts in front of it).

## The one rewrite: bare specifiers -> relative paths

npm packages `import` each other by bare specifier (`import * as array from 'lib0/array'`,
`import * as Y from 'yjs'`), which only resolves via a bundler or a Node-style `node_modules` lookup —
neither exists in a static-file Flask deployment with no build step. Every such bare specifier was
mechanically rewritten to a relative path pointing at the vendored file:

- In `yjs.mjs`: `from 'lib0/xxx'` -> `from './lib0/xxx.js'`.
- In `lib0/random.js`: `from 'lib0/webcrypto'` -> `from './webcrypto.js'` (the one lib0-internal file that
  imports another lib0 module by bare specifier instead of a relative path).
- In `y-protocols/sync.js` and `y-protocols/awareness.js`: `from 'lib0/xxx'` -> `from '../lib0/xxx.js'`,
  `from 'yjs'` -> `from '../yjs.mjs'`.

No other line was touched. Diff against a fresh `npm pack` of the same version to audit.

## Verified (not just vendored)

Before shipping, this exact vendored bundle was smoke-tested with real Node.js (no bundler, native ES
module resolution) to prove it actually runs standalone and interoperates with the Python server:

1. Two independent `Y.Doc()`s (pure JS, this vendored bundle) synced and made concurrent, non-conflicting
   edits -- both converged and both edits survived (real merge, not last-write-wins), mirroring
   `tests/test_collab.py::test_two_clients_concurrent_edits_merge_for_real`.
2. `y-protocols/awareness.js`'s `Awareness` class propagated a local state update to a second instance.
3. **Cross-language interop, both directions**: `Y.encodeStateAsUpdate()` bytes produced by *this vendored
   JS bundle* were applied by Python's `pycrdt.Doc.apply_update()` and rendered correctly via
   `fraction/collab/pm_yjs.ydoc_to_doc()`; and bytes produced by the Python server
   (`fraction.collab.pm_yjs.doc_to_ydoc` + `Doc.get_update()`) were applied cleanly by this vendored JS
   bundle. Same result confirmed against the full `tests/test_collab.py::SAMPLE_DOC` fixture (every node
   type in the frozen content schema) -- the JS-side ProseMirror<->YXmlFragment mapping in
   `fraction/static/collab.js` was independently written to mirror `fraction/collab/pm_yjs.py` and
   produces a byte-identical rendered document for that fixture.

These checks were run ad hoc with a local Node.js during development (not part of the `pytest` gate,
which has no Node dependency) — see `docs/_patches/ws11-collab.md` for the exact commands if you need to
re-verify after touching either mapping.

## Known gap: no TipTap / y-prosemirror

This vendors Yjs's **core CRDT + sync/awareness protocol** only. `y-prosemirror` and
`@tiptap/extension-collaboration` (which would give real per-keystroke collaborative cursors inside the
rich-text editor) were deliberately **not** vendored: both require a genuine ProseMirror `EditorView`
instance to bind to, and `fraction/static/editor.js` — owned by WS4, out of scope for this workstream —
is a hand-rolled `contenteditable` fallback (PLAN.md §16 flagged this as needing a small bundling step
that was never scoped). `fraction/static/collab.js` bridges the gap honestly: it gets real CRDT sync,
persistence, and presence, at whole-document-per-debounce granularity, not per-keystroke merge. See the
docstring at the top of `collab.js` for the exact limitation and the upgrade path once a real
TipTap/ProseMirror bundle replaces `editor.js`.
