"""The sim viewer (ORGANIC-SCALE.org, tracks T13 / T14): a static page
(index.html, viewer.js, viewer.css, vendor/uPlot) that reads a sim vector --
complete or checkpoint-partial -- and shows every collected metric and every
agent's telemetry and actions against one clock.

    python -m alberta_buck.sim.viewer [--port 8787] [--bind 0.0.0.0]

serves the page at / and, read-only, the repo's build/sim/ and test/vectors/
under /data/ (with /data/index.json listing every vector), so a browser on
another machine opens http://<host>:8787/?v=/data/build/sim/catalogue/cat-dump-all.json.
The vendor files come from node_modules/uplot (make viewer-vendor).
"""
