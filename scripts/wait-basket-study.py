"""Wait for one stock campaign and publish its validated final return report."""
import argparse
from collections import Counter
import json
from pathlib import Path
import subprocess
import sys
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('campaign', type=Path)
    parser.add_argument('--runs', type=Path, default=Path('build/runs'))
    parser.add_argument('--interval', type=int, default=600)
    args = parser.parse_args()
    if args.interval < 1:
        parser.error('interval must be positive')
    last = None
    while True:
        cells = json.loads((args.campaign / 'manifest.json').read_bytes())['cells']
        if not cells:
            parser.error('campaign manifest contains no cells')
        counts = Counter(c['status'] for c in cells)
        if counts != last:
            print(args.campaign, dict(counts), flush=True)
            last = counts
        if sum(counts[s] for s in ('complete', 'failed')) == len(cells):
            break
        time.sleep(args.interval)
    subprocess.run([sys.executable, str(Path(__file__).with_name('summarize-basket-stock-grid.py')),
                    str(args.campaign), '--runs', str(args.runs), '--output', str(args.campaign / 'summary.md')],
                   check=True)
    return int(counts['failed'] > 0)


if __name__ == '__main__':
    sys.exit(main())
