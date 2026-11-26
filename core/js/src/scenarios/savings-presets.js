// Opening profiles for the local Savings world. Currency amounts are in
// thousands of USD, as in the shared household policy.
export const SAVINGS_BORROWERS = {
  farm: { label: "Farm land mortgage", settings: {
    home_k: 4000, ltv: 0.75, income_frac: 0.125, living_frac: 0.35,
    cash_k: 100, apr: 0.055,
  } },
  home: { label: "Home mortgage", settings: {
    home_k: 800, ltv: 0.75, income_frac: 0.2, living_frac: 0.55,
    cash_k: 40, apr: 0.05,
  } },
};
