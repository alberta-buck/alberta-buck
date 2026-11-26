import { uniform } from "../agents/household-policy.js";

export const cyclePeriod = (index) => 30 + index * 15;

function setting(value, fallback, min, max, label) {
  const n = value === undefined || value === "" ? fallback : Number(value);
  if (!Number.isFinite(n) || n < min || n > max) throw new Error(`${label} must be between ${min} and ${max}`);
  return n;
}

/** Resolve the once-per-world tape in USDC micro per whole commodity. */
export function savingsPriceTape(prices, mode, sets, seed) {
  const base = prices.series[mode === "sine" ? "history" : mode];
  if (!base?.length || base.length !== prices.tokens.length) throw new Error(`Price tape unavailable: ${mode}`);
  const noise = setting(sets["scenario.price_noise_pct"], 0, 0, 50, "Price noise (%)") / 100;
  const amplitude = mode === "sine" ? setting(sets["scenario.sine_amplitude_pct"], 12, 0, 50, "Sine amplitude (%)") / 100 : 0;
  const inflation = mode === "sine" ? setting(sets["scenario.usd_inflation_pct"], 2, -30, 50, "Annual USD inflation (%)") / 100 : 0;
  const days = Math.min(...base.map((feed) => feed.length));
  return base.map((feed, i) => {
    const symbol = prices.tokens[i].sym;
    const period = mode === "sine" ? setting(sets[`scenario.sine_period.${symbol}`], cyclePeriod(i), 2, 1827,
      `${symbol} period (days)`) : 1;
    return feed.slice(0, days).map((price, day) => {
      const reference = mode === "sine" ? feed[0] * (1 + amplitude * Math.sin(2 * Math.PI * day / period))
        * (1 + inflation) ** (day / 365.25) : price;
      const jitter = noise ? 1 + noise * (2 * uniform(seed, `savings-price/${symbol}`, day) - 1) : 1;
      return Math.max(1, Math.round(reference * jitter));
    });
  });
}
