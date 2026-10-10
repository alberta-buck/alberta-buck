/* Offline, dependency-free charts over saved pyrevm results. No model runs here. */
"use strict";
(() => {
  const M = window.controlLabManifest;
  const $ = (tag, cls, text) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  };
  const svgNode = (tag, attrs = {}, text) => {
    const e = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
    if (text !== undefined) e.textContent = text;
    return e;
  };
  const valueFormat = new Intl.NumberFormat("en-US", {maximumFractionDigits: 6});
  const number = n => n == null ? "—" : Math.abs(n) > 0 && Math.abs(n) < .0001 ? n.toExponential(2) : valueFormat.format(n);
  if (!M) {
    document.querySelector("#campaign-status").textContent = "No campaign manifest found. Run make control-lab-interactive to generate the datasets and presentation.";
    return;
  }
  const completed = Object.keys(M.entries).length;
  document.querySelector("#campaign-status").textContent = `${M.days} days · ${completed}/${M.unique_runs} distinct runs available · ${M.visible_cases} selector combinations · seed ${M.seed}${M.status === "complete" ? " · Complete" : " · Campaign in progress"}`;
  document.querySelector("#method-details").textContent = `At 1×, each token receives an independent log-price random walk with ${100 * M.walk_daily_sigma}% daily volatility, starting at zero on day 0. Positive and negative Gaussian increments accumulate without a pull back to baseline. The 0.5× path has half its log displacement. Zero expected log drift describes an average over seeds, not a constraint on this path. Exponentiation gives price levels a small positive drift in expectation across seeds. Observations occur four times per day. Code and bytecode fingerprint: ${M.implementation.sha256.slice(0, 16)}.`;

  if (M.method) document.querySelector("#method-details").textContent = M.method;
  const cache = new Map(), pending = new Map();
  window.controlLabReceive = (key, data) => {
    cache.set(key, data);
    while (cache.size > 12) cache.delete(cache.keys().next().value);
    const job = pending.get(key);
    if (job) { clearTimeout(job.timer); pending.delete(key); job.resolve(data); }
  };
  function load(key) {
    if (cache.has(key)) return Promise.resolve(cache.get(key));
    if (pending.has(key)) return pending.get(key).promise;
    const entry = M.entries[key];
    if (!entry) return Promise.reject(new Error("This combination has not finished yet. Regenerate the presentation after the campaign completes."));
    let resolve, reject;
    const promise = new Promise((a, b) => { resolve = a; reject = b; });
    const script = document.createElement("script");
    const fail = () => { const job = pending.get(key); if (job) { clearTimeout(job.timer); pending.delete(key); job.reject(new Error("Could not load the saved plot data. Rebuild with make control-lab-interactive-render.")); } script.remove(); };
    const timer = setTimeout(fail, 20000);
    pending.set(key, {promise, resolve, reject, timer});
    script.src = entry.plot_file;
    script.onload = () => { script.remove(); if (pending.has(key)) fail(); };
    script.onerror = fail;
    document.head.append(script);
    return promise;
  }

  class Chart {
    constructor(pane, state, parent) {
      this.pane = pane; this.state = state; this.hidden = new Set(); this.hiddenMarkers = new Set();
      this.box = $("section", "chart"); this.box.dataset.pane = pane.id;
      const heading = $("div", "chart-header"), title = $("div");
      title.append($("h3", "", pane.title), $("div", "chart-unit", pane.unit));
      this.day = $("span", "chart-day"); heading.append(title, this.day);
      const scroll = $("div", "chart-scroll");
      this.svg = svgNode("svg", {viewBox:"0 0 1000 240", role:"img", tabindex:"0", "aria-label":`${pane.title}. Arrow keys inspect days. Units: ${pane.unit}.`});
      scroll.append(this.svg);
      this.legend = $("div", "legend"); this.values = [];
      pane.lines.forEach((line, i) => {
        const button = $("button"); button.type = "button"; button.setAttribute("aria-pressed", "true");
        button.setAttribute("aria-label", `Toggle ${line.label}`);
        const swatch = $("span", "swatch"); swatch.style.borderColor = line.color;
        if (line.dash) swatch.style.borderTopStyle = "dashed";
        const value = $("span", "value"); this.values.push(value);
        button.append(swatch, $("span", "", line.label), value);
        button.onclick = () => { this.hidden.has(i) ? this.hidden.delete(i) : this.hidden.add(i); button.setAttribute("aria-pressed", String(!this.hidden.has(i))); this.draw(); };
        this.legend.append(button);
      });
      for (const group of pane.markers || []) {
        const button = $("button"); button.type = "button"; button.setAttribute("aria-pressed", "true");
        button.setAttribute("aria-label", `Toggle ${group.label}`);
        const swatch = $("span", "marker-swatch", {up:"▲", down:"▼", diamond:"◆"}[group.shape]);
        swatch.style.color = group.color;
        button.append(swatch, $("span", "", `${group.label} (${group.points.reduce((n,p)=>n+p.count,0)})`));
        button.onclick = () => {
          this.hiddenMarkers.has(group.id) ? this.hiddenMarkers.delete(group.id) : this.hiddenMarkers.add(group.id);
          button.setAttribute("aria-pressed", String(!this.hiddenMarkers.has(group.id))); this.draw();
        };
        this.legend.append(button);
      }
      this.box.append(heading, scroll, this.legend, $("p", "chart-note", pane.note)); parent.append(this.box);
      this.svg.addEventListener("pointermove", event => {
        const box = this.svg.getBoundingClientRect();
        const x = (event.clientX - box.left) / box.width * 1000;
        const day = state.start + (x - 96) / 882 * (state.end - state.start);
        state.cursor(Math.round(Math.max(state.start, Math.min(state.end, day)) / state.step));
      });
      this.svg.addEventListener("keydown", event => {
        if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
        event.preventDefault();
        const lo = Math.ceil(state.start / state.step), hi = Math.floor(state.end / state.step);
        let idx = state.index;
        if (event.key === "Home") idx = lo;
        else if (event.key === "End") idx = hi;
        else idx += event.key === "ArrowLeft" ? -1 : 1;
        state.cursor(Math.max(lo, Math.min(hi, idx)));
      });
      this.draw();
    }
    draw() {
      const {pane, state, svg} = this, data = state.data, days = data.days;
      const chosen = pane.lines.filter((_, i) => !this.hidden.has(i));
      const markers = (pane.markers || []).filter(group => !this.hiddenMarkers.has(group.id));
      const group = `${state.selected.profile}:${Number(state.selected.walk)}`;
      // Allocation always follows the visible values, even while price
      // panes use common scales for comparing different experiments.
      let bounds = pane.id !== "allocation" && state.shared.checked && M.limits[group] && M.limits[group][pane.id];
      if (!bounds) {
        const values = chosen.flatMap(line => line.values.filter((v, i) => v != null && days[i] >= state.start && days[i] <= state.end));
        values.push(...markers.flatMap(group => group.points.filter(p => days[p.index] >= state.start && days[p.index] <= state.end).map(p => p.value)));
        bounds = values.length ? [Math.min(...values), Math.max(...values)] : [0, 1];
      }
      let [low, high] = bounds;
      // Small real movements must remain inspectable when shared scales are
      // disabled. Use a fallback span only for an exactly constant series.
      const margin = high > low ? (high - low) * .09 : Math.max(Math.abs(high) * .002, .0001);
      low -= margin; high += margin;
      if (pane.id === "allocation") { low = Math.max(0, low); high = Math.min(100, high); }
      const digits = Math.min(8, Math.max(0, 1 - Math.floor(Math.log10((high - low) / 4))));
      const X = day => 96 + (day - state.start) / (state.end - state.start) * 882;
      const Y = v => 202 - (v - low) / (high - low) * 182;
      this.X = X;
      svg.replaceChildren();
      const defs = svgNode("defs"), clip = svgNode("clipPath", {id:`clip-${state.id}-${pane.id}`});
      clip.append(svgNode("rect", {x:96,y:15,width:882,height:190})); defs.append(clip); svg.append(defs);
      for (let i = 0; i <= 4; i++) {
        const value = low + (high - low) * i / 4, y = Y(value);
        svg.append(svgNode("line", {x1:96,x2:978,y1:y,y2:y,stroke:"#e3e9e7","stroke-width":1}),
          svgNode("text", {x:85,y:y+4,"text-anchor":"end"}, value.toLocaleString("en-US", {maximumFractionDigits:digits})));
      }
      for (let i = 0; i <= 6; i++) {
        const day = state.start + (state.end - state.start) * i / 6, x = X(day);
        svg.append(svgNode("text", {x,y:227,"text-anchor":"middle"}, `${number(day)}d`));
      }
      const lines = svgNode("g", {"clip-path":`url(#clip-${state.id}-${pane.id})`});
      pane.lines.forEach((line, i) => {
        if (this.hidden.has(i)) return;
        let path = "", gap = true;
        line.values.forEach((v, j) => {
          if (v == null) { gap = true; return; }
          // Retain points immediately outside the window for accurate edge segments.
          if (days[j] < state.start - state.step || days[j] > state.end + state.step) { gap = true; return; }
          path += `${gap ? "M" : "L"}${X(days[j]).toFixed(2)},${Y(v).toFixed(2)}`; gap = false;
        });
        const attrs = {d:path,fill:"none",stroke:line.color,"stroke-width":line.dash ? 1.3 : 1.8,"vector-effect":"non-scaling-stroke"};
        if (line.dash) attrs["stroke-dasharray"] = line.dash;
        lines.append(svgNode("path", attrs));
      });
      // Dense arbitrage markers go behind the less frequent policy trades.
      for (const group of [...markers].sort((a,b)=>Number(b.id.startsWith("arb"))-Number(a.id.startsWith("arb")))) {
        const arb = group.id.startsWith("arb"), r = arb ? 3 : group.shape === "diamond" ? 4 : 5.5;
        for (const point of group.points) {
          const day = days[point.index];
          if (day < state.start || day > state.end) continue;
          const x = X(day), y = Y(point.value);
          const vertices = group.shape === "up" ? [[0,-r],[-r,r],[r,r]]
            : group.shape === "down" ? [[0,r],[-r,-r],[r,-r]] : [[0,-r],[r,0],[0,r],[-r,0]];
          const marker = svgNode("polygon", {points:vertices.map(([a,b])=>`${x+a},${y+b}`).join(" "),
            fill:group.color, "fill-opacity":arb ? .45 : .9, stroke:"white", "stroke-width":.6,
            class:"execution-marker", "data-kind":group.id, "data-index":point.index});
          const pair = group.shape === "diamond";
          marker.append(svgNode("title", {}, `Day ${number(day)} · TOKEN ${"ABC"[point.token]} · ${group.label}\n${point.count} ${pair ? "pairings" : "fills"} · ${number(point.token_amount)} TOKEN · ${number(point.buck_amount)} BUCK\n${number(point.value)} ${pane.unit}`));
          marker.onclick = () => { state.cursor(point.index); if (state.executionDetails) state.executionDetails.open = true; };
          lines.append(marker);
        }
      }
      svg.append(lines);
      this.crosshair = svgNode("line", {y1:15,y2:204,stroke:"#788d88","stroke-width":1,"stroke-dasharray":"3 3"});
      svg.append(this.crosshair); this.inspect(state.index);
    }
    inspect(index) {
      const day = this.state.data.days[index];
      this.day.textContent = `Day ${number(day)}`;
      this.crosshair.setAttribute("x1", this.X(day)); this.crosshair.setAttribute("x2", this.X(day));
      this.crosshair.style.display = day < this.state.start || day > this.state.end ? "none" : "";
      this.pane.lines.forEach((line, i) => { this.values[i].textContent = number(line.values[index]); });
    }
  }

  const states = new Map(), query = new URLSearchParams(location.search);
  let linkedSettings = query.get("linked") === "1";
  function saveSelection(state) {
    const params = new URLSearchParams(state.selected);
    if (linkedSettings) params.set("linked", "1");
    // File URLs may reject replaceState; the toggle still works normally.
    try { history.replaceState(null, "", `${location.pathname}?${params}#${state.id}`); } catch (_) { /* no navigation required */ }
  }
  function syncSelections(source) {
    for (const target of states.values()) {
      if (target === source || M.axes.every(axis => target.selected[axis.id] === source.selected[axis.id])) continue;
      target.selected = {...source.selected};
      for (const axis of M.axes) target.controls[axis.id].value = target.selected[axis.id];
      update(target); // Closed tests retain the selection and load on opening.
    }
  }
  function defaults() {
    return Object.fromEntries(M.axes.map(axis => {
      const fallback = axis.values.find(v => v.id === "medium") || axis.values[0];
      return [axis.id, axis.values.some(v => v.id === query.get(axis.id)) ? query.get(axis.id) : fallback.id];
    }));
  }
  function renderRun(state, data) {
    state.data = data; state.body.hidden = false; state.body.replaceChildren(); state.charts = [];
    state.step = data.days.length > 1 ? data.days[1] - data.days[0] : .25;
    state.index = data.days.length - 1;
    if (data.feed_source) state.body.append($("p", "explanation",
      `Historical inputs: ${data.feed_source.labels.map((label,i)=>`${"ABC"[i]} = ${label}`).join("; ")}. Each ends at its opening price after a log-price endpoint adjustment. Historical means use preceding data only. Stored daily quotes include generated texture between monthly anchors.`));
    const metrics = $("div", "metrics");
    const counts = data.execution_counts;
    const metricValues = [["Mean bvib", data.mean_bvib.toFixed(6)], ["Final K", data.summary.end_k.toFixed(6)],
      ["Arbitrage profit · BUCK", number(Math.round(data.summary.arb_profit_buck))], ["Desk operations", data.summary.desk_ops],
      ["Arbitrage cycles", number(data.summary.arb_cycles)],
      ["Basket TOKEN buys / sells", counts?.recorded ? `${counts.basket_buys} / ${counts.basket_sells}` : "Not recorded"],
      ["Basket LP pairings", counts?.recorded ? counts.basket_pairs : "Not recorded"]];
    if (data.basket_performance) {
      const p = data.basket_performance;
      metricValues.push(["Final basket net value · BUCK", number(Math.round(p.final_net_buck * 100) / 100)],
        ["Net gain / loss · BUCK", number(Math.round(p.gain_buck * 100) / 100)],
        ["Marked return on initial BUCK", `${p.return_pct >= 0 ? "+" : ""}${p.return_pct.toFixed(3)}%`],
        ["Final basket net value · USD", number(Math.round(p.final_net_usd * 100) / 100)],
        ["Net gain / loss · USD", number(Math.round(p.gain_usd * 100) / 100)],
        ["Marked return on initial USD", `${p.return_usd_pct >= 0 ? "+" : ""}${p.return_usd_pct.toFixed(3)}%`]);
      if (p.arb_beneficiary === "basket") metricValues.push(
        ["Arb income received · BUCK", number(Math.round(p.arb_income_buck*100)/100)],
        ["Other basket gain · BUCK", number(Math.round(p.other_gain_buck*100)/100)],
        ["Arb income received · USD", number(Math.round(p.arb_income_usd*100)/100)],
        ["Other basket gain · USD", number(Math.round(p.other_gain_usd*100)/100)]);
    }
    for (const [label, value] of metricValues) {
      const metric = $("div", "metric"); metric.append($("strong", "", value), $("span", "", label)); metrics.append(metric);
    }
    state.body.append(metrics);
    const executions = $("details", "execution-details"), executionTitle = $("summary"), executionBody = $("div", "execution-body");
    executions.append(executionTitle, executionBody); state.body.append(executions); state.executionDetails = executions;
    const byDay = new Map();
    for (const event of data.executions || []) {
      if (event.action === "mark") continue; // valuation-only updates remain in raw JSON
      if (!byDay.has(event.index)) byDay.set(event.index, []);
      byDay.get(event.index).push(event);
    }
    let lastIndex = -1;
    state.inspectExecutions = index => {
      const rows = byDay.get(index) || [];
      executionTitle.textContent = `Executed operations · day ${number(data.days[index])} · ${rows.length} operations`;
      if (!executions.open || lastIndex === index) return;
      lastIndex = index; executionBody.replaceChildren();
      if (!counts?.recorded) { executionBody.append($("p", "", "This cached run predates execution recording. Regenerate its dataset to see trades.")); return; }
      if (!rows.length) { executionBody.append($("p", "", "No desk trades, basket trades/LP operations or arbitrage cycles in this observation. Move the chart cursor or click a marker to inspect another day.")); return; }
      executionBody.append($("p", "", "Buy/sell means TOKEN direction. The desk supplies BUCK by buying TOKEN, and absorbs BUCK by selling TOKEN. Valuation-only basket updates are omitted here; raw JSON retains them."));
      const table = $("table"), head = $("tr"), body = $("tbody");
      for (const label of ["Actor / action", "Phase / transaction", "Executed legs or LP allocation", "Cycle profit"]) head.append($("th", "", label));
      const thead = $("thead"); thead.append(head); table.append(thead, body);
      for (const event of rows) {
        const row = $("tr"), trades = $("td");
        for (const t of event.trades) trades.append($("div", "", `${t.side.toUpperCase()} ${number(t.token_amount)} TOKEN ${"ABC"[t.token]} · ${number(t.quote_amount)} ${t.quote} · fill ${number(t.price_quote)} ${t.quote}/TOKEN`));
        if (!event.trades.length) trades.textContent = event.action === "pair"
          ? `Pair existing TOKEN ${"ABC"[event.buy_token]} with ${number(event.spent_buck)} BUCK; no token swap`
          : `No TOKEN swap; ${number(event.proceeds_buck)} BUCK proceeds, ${number(event.spent_buck)} BUCK deployed`;
        row.append($("td", "", `${event.actor} · ${event.action}`), $("td", "", `${event.phase} · #${event.transaction}`),
          trades, $("td", "", event.profit_buck == null ? "—" : `${number(event.profit_buck)} BUCK`));
        body.append(row);
      }
      executionBody.append(table);
    };
    executions.ontoggle = () => { if (executions.open) { lastIndex = -1; state.inspectExecutions(state.index); } };
    state.inspectExecutions(state.index);
    const zoom = $("div", "zoom"), output = $("output");
    const sliders = [];
    for (const [labelText, name] of [["From", "start"], ["To", "end"]]) {
      const label = $("label", "", labelText), input = $("input");
      input.type = "range"; input.min = 0; input.max = M.days; input.step = state.step; input.value = state[name];
      input.setAttribute("aria-label", `${labelText} day, ${state.id}`);
      input.oninput = () => {
        state[name] = Number(input.value);
        if (state.end <= state.start) state[name] = name === "start" ? state.end - state.step : state.start + state.step;
        input.value = state[name]; redraw();
      };
      label.append(input); zoom.append(label); sliders.push(input);
    }
    const reset = $("button", "", "Full 120 days"); reset.textContent = `Full ${M.days} days`;
    reset.onclick = () => { state.start = 0; state.end = M.days; sliders[0].value = 0; sliders[1].value = M.days; redraw(); };
    zoom.append(output, reset); state.body.append(zoom);
    const extra = $("details", "monitor-details"); extra.append($("summary", "", "Inspect sensor quality, TOKEN-pair signals and the seven EMA levels"));
    let extraDrawn = false;
    function addChart(pane, parent) { state.charts.push(new Chart(pane, state, parent)); }
    for (const pane of data.panes.filter(p => !p.group)) addChart(pane, state.body);
    if (data.panes.some(p => p.group)) {
      state.body.append(extra);
      extra.ontoggle = () => {
        if (extra.open && !extraDrawn) { extraDrawn = true; data.panes.filter(p => p.group).forEach(p => addChart(p, extra)); }
      };
    }
    function redraw() { output.textContent = `${number(state.start)}–${number(state.end)} days`; state.charts.forEach(c => c.draw()); }
    state.redraw = redraw; redraw();
  }
  async function update(state) {
    const version = ++state.version;
    delete state.section.dataset.loadedKey;
    const selection = M.axes.map(axis => state.selected[axis.id]).join(":");
    const key = M.cases[state.id][selection];
    state.body.hidden = true; state.status.classList.remove("error");
    state.status.textContent = "Loading the selected cached run…";
    state.link.hidden = true;
    if (!state.section.open) return;
    try {
      const data = await load(key);
      if (version !== state.version) return; // rapid selector changes cannot paint stale data
      state.status.textContent = `Saved pyrevm run ${key.slice(0, 12)} · ${data.days.length} observations · ${number(data.summary.transactions)} transactions`;
      state.link.href = M.entries[key].file; state.link.hidden = false;
      state.section.dataset.loadedKey = key;
      renderRun(state, data);
    } catch (error) {
      if (version !== state.version) return;
      state.body.hidden = true; state.link.hidden = true;
      state.status.textContent = error.message; state.status.classList.add("error");
      delete state.section.dataset.loadedKey;
    }
  }

  for (const [id, flags] of Object.entries(M.stages)) {
    const section = $("details", "stage"); section.id = id;
    section.append($("summary", "", flags.title));
    const content = $("div", "stage-content"), controls = $("div", "controls"), toolbar = $("div", "toolbar");
    content.append($("p", "explanation", M.explanations[id]), controls, toolbar);
    const status = $("p", "run-status"); status.setAttribute("role", "status"); status.setAttribute("aria-live", "polite");
    const body = $("div", "run-body");
    const state = {id, section, status, body, selected:defaults(), controls:{}, version:0, charts:[], start:0, end:M.days};
    state.cursor = index => { state.index = index; state.charts.forEach(c => c.inspect(index)); if (state.inspectExecutions) state.inspectExecutions(index); };
    for (const axis of M.axes) {
      const label = $("label", "", axis.label), select = $("select"); select.name = axis.id;
      select.setAttribute("aria-label", `${axis.label}, ${flags.title}`);
      for (const value of axis.values) { const option = $("option", "", value.label); option.value = value.id; select.append(option); }
      select.value = state.selected[axis.id];
      select.disabled = !!axis.requires && !flags[axis.requires];
      label.append(select);
      if (select.disabled) label.append($("span", "control-note", "Actor absent in this test"));
      select.onchange = () => {
        state.selected[axis.id] = select.value;
        if (linkedSettings) { syncSelections(state); saveSelection(state); }
        update(state);
      };
      controls.append(label); state.controls[axis.id] = select;
    }
    const linkedLabel = $("label"), linked = $("input");
    linked.type = "checkbox"; linked.name = "linked-settings"; linked.checked = linkedSettings;
    linked.setAttribute("role", "switch");
    linked.onchange = () => {
      linkedSettings = linked.checked;
      for (const target of states.values()) target.linked.checked = linkedSettings;
      if (linkedSettings) syncSelections(state);
      saveSelection(state);
    };
    linkedLabel.append(linked, document.createTextNode(" Keep settings linked across tests")); state.linked = linked;
    const sharedLabel = $("label"), shared = $("input"); shared.type = "checkbox"; shared.checked = true;
    shared.name = "shared-scales";
    shared.onchange = () => { if (state.redraw) state.redraw(); };
    sharedLabel.append(shared, document.createTextNode(" Compare on shared scales")); state.shared = shared;
    const link = $("a", "data-link", "Download raw JSON ↓"); link.download = ""; link.hidden = true; state.link = link;
    toolbar.append(linkedLabel, sharedLabel, link); content.append(status, body); section.append(content);
    section.ontoggle = () => { if (section.open) update(state); else ++state.version; };
    document.querySelector("#stages").append(section); states.set(id, state);
    const nav = $("a", "", flags.title); nav.href = `#${id}`;
    nav.onclick = () => { section.open = true; document.querySelectorAll("#stage-nav a").forEach(a => a.classList.toggle("active", a === nav)); };
    document.querySelector("#stage-nav").append(nav);
  }
  const requested = location.hash.slice(1), first = states.get(requested) || states.values().next().value;
  first.section.open = true;
  document.querySelector(`#stage-nav a[href="#${first.id}"]`).classList.add("active");
})();
