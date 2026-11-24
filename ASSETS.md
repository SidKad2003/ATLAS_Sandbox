# Third-party assets and licences (svc-sim)

Last updated: 24 Nov 2026

| Asset | Location in repo | Source | Version | Licence | Notes |
|---|---|---|---|---|---|
| Phaser 3 (minified build) | `viz/web/vendor/phaser.min.js` | https://cdn.jsdelivr.net/npm/phaser@3.90.0/dist/phaser.min.js | 3.90.0 | MIT | Licence text kept at `viz/web/vendor/PHASER-LICENSE.md`. SHA-256 `e92ddef111ba42e92d316979c732311757093688ea1810591cb7aa2858eba7a7`. |
| Store town map, buildings, sprites, speech bubbles, fault overlays | `viz/web/src/scene.js`, `viz/web/src/layout.js` | Drawn procedurally in code by RAIT (Phaser Graphics) | n/a | RAIT proprietary | No image, tile or sprite files are used. |
| Fonts | n/a | System UI font stack of the viewer's browser | n/a | n/a | No font files are bundled or fetched. |
| nginx base image | `viz/web/Dockerfile` | `nginxinc/nginx-unprivileged:1.27-alpine` | 1.27 | BSD-2-Clause (nginx) | Runtime only; pin by digest in `UPSTREAM.md` at hardening. |

## Rules

- Only MIT-licensed code and CC0 art may be added to the visualisation. Record each addition in this table with its source URL, version and licence before committing.
- No code, maps or art from the Stanford "generative agents" project are used. The "agents in a small town" concept is the only thing taken.
- The front end makes no network calls at runtime other than to its own origin (`/api/`, `/ws/`).
