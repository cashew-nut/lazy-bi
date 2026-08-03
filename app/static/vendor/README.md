# app/static/vendor/

Third-party frontend assets, committed to the repo rather than fetched from a
CDN at runtime. Nothing in this directory is written by hand; nothing here is
modified from what upstream published.

Vendoring (rather than a CDN reference) keeps the deployment posture every
other feature has maintained: one self-contained Docker image, no external
call at request time, and an install that can run air-gapped. See
`specs/016-instant-cross-filter/research.md` R4.

## perspective/ — `@finos/perspective` 3.8.0 (Apache-2.0)

The project's first external frontend dependency, used **headless**: its
`Table`/`View` API is a columnar aggregation engine for instant-mode
dashboards, and nothing more. `perspective-viewer` and every other
Perspective rendering component is deliberately absent — the hand-rolled SVG
renderers in `app/static/js/charts/` remain the entire rendering layer.

Loaded only by `app/static/js/instant.js`, behind a dynamic `import()` that
runs solely for a dashboard with `instant: true`. No other view pays for it.

| path | upstream path in the npm tarball |
| --- | --- |
| `perspective/cdn/perspective.js` | `dist/cdn/perspective.js` |
| `perspective/cdn/perspective.js.map` | `dist/cdn/perspective.js.map` |
| `perspective/wasm/perspective-server.wasm` | `dist/wasm/perspective-server.wasm` |
| `perspective/wasm/perspective-js.wasm` | `dist/wasm/perspective-js.wasm` |
| `perspective/LICENSE.md` | `LICENSE.md` |

The `cdn` build is the one that boots with no bundler: it is a plain ES
module, and it resolves its own server wasm as `../wasm/perspective-server.wasm`
relative to its module URL — which is why the two directories have to stay
siblings, and why the layout above is not flattened. The client wasm
(`perspective-js.wasm`) has no such self-registration and is handed to
`init_client()` explicitly by `instant.js`. The worker Perspective runs its
engine in is inlined in that same module as a Blob, so there is no third
asset to serve.

### Refreshing it

```sh
npm pack @finos/perspective@<version>
tar xzf finos-perspective-<version>.tgz
cp package/dist/cdn/perspective.js*        app/static/vendor/perspective/cdn/
cp package/dist/wasm/perspective-*.wasm    app/static/vendor/perspective/wasm/
cp package/LICENSE.md                      app/static/vendor/perspective/
```

Then re-run `tests/test_instant_assets.py`, which pins the layout above, and
drive an instant dashboard in the browser per Constitution IV — a Perspective
API change surfaces there, not in the Python suite.
