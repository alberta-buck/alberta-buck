// The Overview tab: the run's headline figures for its latest day, then K, BVIB and price,
// who holds the BUCK, where it came from, the population and its refusals -- each chart
// marked with the run's interventions.

import { TimeChart } from "./chart.js";
import { OVERVIEW, format, tiles, windowFrames } from "./series.js";

const html = (tag, attrs = {}, text = null) => {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  if (text !== null) node.textContent = text;
  return node;
};

export class OverviewTab {
  constructor(root, bus) {
    this.root = root;
    this.tiles = html("div", { class: "tiles" });
    this.grid = html("div", { class: "charts" });
    root.replaceChildren(this.tiles, this.grid);
    this.charts = OVERVIEW.map((spec) => {
      const card = html("section", { class: "card", "aria-label": spec.title });
      this.grid.append(card);
      return new TimeChart(card, spec, bus);
    });
  }

  update({ frames, manifest, interventions, range }) {
    const shown = windowFrames(frames, range);
    this.tiles.replaceChildren(...tiles(frames, manifest).map((t) => {
      const tile = html("div", { class: "tile" });
      tile.append(html("div", { class: "tile-label" }, t.label));
      tile.append(html("div", { class: "tile-value" }, format(t.value, t.unit)));
      if (t.detail) tile.append(html("div", { class: "tile-detail" }, t.detail));
      return tile;
    }));
    // Steering a session (pause, step, pace) changes nothing in the world: not marked.
    const markers = (interventions ?? []).filter((iv) => iv.replay !== false).map((iv) => ({
      day: iv.day ?? iv.applied_day,
      label: `${iv.source === "operator" ? "operator: " : ""}${iv.action ?? "intervention"}${iv.ok === false ? " (refused)" : ""}`,
    }));
    for (const chart of this.charts) chart.update(shown, markers);
  }
}
