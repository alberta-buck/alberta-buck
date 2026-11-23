"""Plot matched signal-grid captures, one page per regime, without filling missing cells."""
import argparse
import hashlib
import json
import math
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.lines import Line2D

REGIMES = ('three-phase', 'mixed', 'shock', 'history', 'revert')
LAWS = ('velocity', 'excursion', 'both')
COLORS = {'production': '#145a87', 'maintenance': '#a74d20', 'velocity': '#158064',
          'excursion': '#8a55a0', 'both': '#aa3867'}
TOKEN_COLORS = ('#145a87', '#a74d20', '#158064', '#8a55a0', '#aa3867')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('campaign', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--sigma', type=float, help='select daily noise (default: 0.01 when present, else first available)')
    parser.add_argument('--seed', type=int, help='select seed (default: 41404 when present, else first available)')
    args = parser.parse_args()
    manifest = json.loads((args.campaign / 'manifest.json').read_text())
    cells = manifest['cells']
    regimes = [r for r in REGIMES if any(c['regime'] == r for c in cells)]
    regimes += sorted({c['regime'] for c in cells} - set(regimes))
    args.output.parent.mkdir(parents=True, exist_ok=True)

    with PdfPages(args.output, metadata={'Title': 'Matched signal-grid comparison'}) as pdf:
        for regime in regimes:
            regime_cells = [c for c in cells if c['regime'] == regime]
            available_noise = [c.get('noise_sigma') for c in regime_cells]
            sigma = args.sigma if args.sigma is not None else (.01 if .01 in available_noise else available_noise[0])
            noise_cells = [c for c in regime_cells if c.get('noise_sigma') == sigma]
            seeds = [c['seed'] for c in noise_cells]
            seed = args.seed if args.seed is not None else (41404 if 41404 in seeds else seeds[0] if seeds else None)
            matched_cells = [c for c in noise_cells if c['seed'] == seed]
            groups = list(dict.fromkeys(c['group'] for c in matched_cells))
            group = groups[0] if groups else None
            chosen = [c for c in matched_cells if c['group'] == group and (c['arm'] in ('production', 'maintenance')
                      or (c['arm'] == 'candidate' and c['onset'] == 'standard' and c['speed'] == 'fast'
                          and c['response'] in LAWS))]
            arms = {c['response'] if c['arm'] == 'candidate' else c['arm']: c for c in chosen}
            captures = {}
            load_errors = {}
            for name, cell in arms.items():
                if cell['status'] != 'complete':
                    continue
                try:
                    raw = (args.campaign / cell['output']).read_bytes()
                    if cell.get('output_sha256') and hashlib.sha256(raw).hexdigest() != cell['output_sha256']:
                        load_errors[name] = 'SHA-256 mismatch'
                        continue
                    captures[name] = json.loads(raw)
                except (OSError, ValueError):
                    load_errors[name] = 'capture unavailable'
            production = captures.get('production')
            # Prefer production for observed traces; fallback prices have no trade markers.
            source = production or captures.get('maintenance') or next(iter(captures.values()), None)
            reference = arms.get('maintenance') or arms.get('production') or next(iter(arms.values()), None)
            digest = reference.get('price_digest') if reference else None
            baseline = captures.get('maintenance')
            reference_days = [f['day'] for f in baseline['frames']] if baseline else None
            fig, ax = plt.subplots(3, 1, figsize=(11.7, 8.3), sharex=True)
            fig.subplots_adjust(left=.09, right=.91, top=.86, bottom=.21, hspace=.45)
            fig.text(.09, .96, f'Signal grid  /  {regime}', fontsize=19, weight='bold')
            noise_label = 'archived path' if sigma is None else f'Noise σ {sigma:g}'
            candidate_names = [name for name in LAWS if name in arms]
            sizing_label = ' · standard onset / fast speed' if candidate_names else ''
            fig.text(.09, .915, f'{noise_label} · seed {seed}{sizing_label} · '
                     f'{manifest.get("days", "?")} days planned', fontsize=10)
            if source:
                frames, tokens = source['frames'], source['tokens']
                days = [f['day'] for f in frames]
                for i, token in enumerate(tokens):
                    prices = [f['prices'][i] / frames[0]['prices'][i] * 100 for f in frames]
                    color = TOKEN_COLORS[i % len(TOKEN_COLORS)]
                    ax[0].plot(days, prices, color=color, lw=1.2, label=token)
                    if production:
                        for side, marker in (('buy', '^'), ('sell', 'v')):
                            picked = [(f['day'], prices[j]) for j, f in enumerate(frames)
                                      if any(t.get('token') == i and t.get('side') == side
                                             for t in f.get('trades', []))]
                            if picked:
                                ax[0].scatter(*zip(*picked), marker=marker, s=25, color=color,
                                              edgecolor='white', linewidth=.3, zorder=4)
                handles, labels = ax[0].get_legend_handles_labels()
                handles += [Line2D([], [], color='#333', ls='', marker='^'),
                            Line2D([], [], color='#333', ls='', marker='v')]
                labels += ['Production buy', 'Production sell'] if production else ['Buy unavailable', 'Sell unavailable']
                ax[0].legend(handles, labels, ncol=4, fontsize=7, loc='best')
                ax[0].set_ylabel('External prices\n(open = 100)')
                pairs = frames[0].get('pairs', [])
                pair = pairs[0]['tokens'] if pairs else None
                if pair:
                    observations = [next((p for p in f.get('pairs', []) if p['tokens'] == pair), None)
                                    for f in frames]
                    valid = lambda p, key: p[key] if p and p.get('ready') == 3 else float('nan')
                    ax[2].plot(days, [valid(p, 'rate') for p in observations], color='#145a87', label='Relative velocity')
                    ax[2].fill_between(days, [valid(p, 'low') for p in observations],
                                       [valid(p, 'high') for p in observations], color='#c7daea', label='Allowance')
                    ax[2].axhline(0, color='.45', lw=.6)
                    ax[2].set_title(f'Observed canonical pair: {tokens[pair[0]]} / {tokens[pair[1]]} '
                                    '(only both routes ready)', loc='left', fontsize=9)
                    turn = ax[2].twinx()
                    for key, color, label in (('forward', '#158064', 'Forward turn'),
                                              ('reverse', '#aa3867', 'Reverse turn')):
                        turn.plot(days, [100 * valid(p, key) for p in observations], color=color,
                                  lw=.9, ls='--', label=label)
                    turn.set_ylabel('Turn rate (% / day)', fontsize=8)
                    handles, labels = ax[2].get_legend_handles_labels()
                    other_handles, other_labels = turn.get_legend_handles_labels()
                    ax[2].legend(handles + other_handles, labels + other_labels, fontsize=7, ncol=2, loc='best')
                else:
                    ax[2].text(.5, .5, 'Pair signal unavailable', transform=ax[2].transAxes, ha='center')
            else:
                for axes in ax:
                    axes.text(.5, .5, 'Capture unavailable', transform=axes.transAxes, ha='center')
            ax[2].set_ylabel('Pair velocity\n(bp / day)')
            ax[2].set_xlabel('Simulation day')
            for name in ('maintenance', 'production', *candidate_names):
                capture, cell = captures.get(name), arms.get(name)
                if not capture or not baseline or not cell or (digest and cell.get('price_digest') != digest):
                    continue
                rows = capture['frames']
                if [f['day'] for f in rows] != reference_days or not rows[0].get('share'):
                    continue
                returns = [100 * (f['share'] / rows[0]['share'] - 1) for f in rows]
                ax[1].plot(reference_days, returns, color=COLORS[name], lw=1.2,
                           label=f'{name} share {returns[-1]:+.2f}%')
                if name in ('maintenance', 'production') and rows[0].get('hold'):
                    hold = [100 * (f['hold'] / rows[0]['hold'] - 1) for f in rows]
                    ax[1].plot(reference_days, hold, ':', color=COLORS[name], lw=.9,
                               label=f'{name} passive {hold[-1]:+.2f}%')
            if ax[1].lines:
                ax[1].legend(ncol=3, fontsize=7, loc='best')
            else:
                ax[1].text(.5, .5, 'Matched return unavailable', transform=ax[1].transAxes, ha='center')
            ax[1].set_ylabel('Share / passive\nreturn (%)')
            delta_label = 'Production − maintenance: unavailable'
            prod_rows = production.get('frames', []) if production else []
            maint_rows = baseline.get('frames', []) if baseline else []
            if (prod_rows and maint_rows and [f['day'] for f in prod_rows] == reference_days
                    and arms.get('production', {}).get('price_digest') == digest
                    and prod_rows[0].get('share') and maint_rows[0].get('share')):
                delta = 100 * (prod_rows[-1]['share'] / prod_rows[0]['share']
                               - maint_rows[-1]['share'] / maint_rows[0]['share'])
                sign = 'positive' if delta > 0 else 'negative' if delta < 0 else 'zero'
                delta_label = f'Production − maintenance: {delta:+.3f} pp ({sign})'
            ax[1].set_title(delta_label, loc='left', fontsize=9)
            counts = []
            for name in ('production', *candidate_names):
                cap = captures.get(name)
                frames = cap.get('frames', []) if cap else []
                decisions = [d for f in frames for d in f.get('decisions', [])]
                keys = ('plannedBuy', 'plannedSell', 'planWithoutPairBuy', 'planWithoutPairSell')
                if not frames or not all('decisions' in f for f in frames) or not all(
                        all(isinstance(d.get(k), (int, float)) and math.isfinite(d[k]) for k in keys)
                        for d in decisions):
                    counts.append(f'{name}: unavailable')
                else:
                    buys = sum(d['plannedBuy'] != d['planWithoutPairBuy'] for d in decisions)
                    sells = sum(d['plannedSell'] != d['planWithoutPairSell'] for d in decisions)
                    counts.append(f'{name}: buy {buys}, sell {sells}')
            statuses = ', '.join(f'{name}: {load_errors.get(name, arms[name]["status"]) if name in arms else "not planned"}'
                                 for name in ('maintenance', 'production', *candidate_names))
            fig.text(.09, .12, 'Pair changes planned amount vs same-prestate no-pair counterfactual (decision counts):', fontsize=8)
            fig.text(.09, .10, '   ·   '.join(counts), fontsize=8)
            fig.text(.09, .075, 'Status: ' + statuses, fontsize=7)
            fig.text(.09, .05, 'Trade markers are observed production fills, not proof of signal causation. '
                     'TWAP shares and LOW passive marks are endogenous BUCK values; exclude gas.', fontsize=7)
            for axes in ax:
                axes.grid(axis='y', alpha=.2)
                axes.spines[['top', 'right']].set_visible(False)
            pdf.savefig(fig)
            plt.close(fig)
    print(args.output)


if __name__ == '__main__':
    main()
