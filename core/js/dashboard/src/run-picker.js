import { runStatus } from "./series.js";

const natural = new Intl.Collator("en", { numeric: true });
const numberField = /^([a-z]*)(\d+(?:\.\d+)?)$/i;

function compareId(left, right) {
  const a = left.split("-"), b = right.split("-");
  for (let i = 0; i < Math.min(a.length, b.length); i++) {
    const x = numberField.exec(a[i]), y = numberField.exec(b[i]);
    const order = x && y && x[1] === y[1] ? Number(x[2]) - Number(y[2]) : natural.compare(a[i], b[i]);
    if (order) return order;
  }
  return a.length - b.length || natural.compare(left, right) || (left < right ? -1 : left > right ? 1 : 0);
}

export function runLabel(run, status = runStatus(run), frames = run.frames ?? 0) {
  const recorded = frames * (run.scenario?.day_step || 1);
  const days = Math.min(recorded, run.scenario?.days ?? recorded);
  return `${run.id} (${status}, ${days} ${days === 1 ? "day" : "days"})`;
}

export function runOptions(runs) {
  return [...runs].sort((a, b) => compareId(a.id, b.id)).map((run) => [run.id, runLabel(run)]);
}
