#!/usr/bin/env python3
"""Render a relocatable, offline report from a signal-grid campaign."""
import argparse
import hashlib
import json
from pathlib import Path

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('campaign', type=Path, help='directory containing manifest.json')
p.add_argument('--output', type=Path, default=Path('doc/control-lab/signal-grid/index.html'))
p.add_argument('--mode', choices=('auto', 'embedded', 'sidecar'), default='auto',
               help='auto embeds up to 20 MiB of captures; sidecars load one selected pair at a time')
a = p.parse_args()
manifest = json.loads((a.campaign / 'manifest.json').read_text())
cells = manifest.get('cells', [])
for cell in cells:
    for field in ('capture', 'summary', 'asset', 'load_error'):
        cell.pop(field, None)
paths = [(cell, a.campaign / cell.get('output', '')) for cell in cells if cell.get('status') == 'complete']
size = sum(path.stat().st_size for _, path in paths if path.is_file())
mode = ('embedded' if size <= 20 * 1024 * 1024 else 'sidecar') if a.mode == 'auto' else a.mode
asset_dir = a.output.parent / (a.output.stem + '-data')
a.output.parent.mkdir(parents=True, exist_ok=True)
if mode == 'sidecar':
    asset_dir.mkdir(parents=True, exist_ok=True)
for cell, path in paths:
    if not path.is_file():
        cell['load_error'] = 'Capture file missing when report was rendered'
        continue
    try:
        raw = path.read_bytes()
        expected = cell.get('output_sha256')
        if expected and hashlib.sha256(raw).hexdigest() != expected:
            cell['load_error'] = 'Capture SHA-256 mismatch'
            continue
        capture = json.loads(raw)
        data = json.dumps(capture, separators=(',', ':'), allow_nan=False).replace('<', '\\u003c')
        frames = capture.get('frames', [])
        if frames and all(isinstance(f.get('share'), (int, float)) for f in frames):
            shares = [f['share'] for f in frames]
            if shares[0] > 0:
                peak = shares[0]
                drawdown = 0
                for share in shares:
                    peak = max(peak, share)
                    drawdown = min(drawdown, share / peak - 1)
                cell['summary'] = {'return': shares[-1] / shares[0] - 1, 'drawdown': drawdown,
                                   'start_day': frames[0].get('day'), 'end_day': frames[-1].get('day')}
                if all(isinstance(f.get('trades'), list) for f in frames):
                    cell['summary']['trades'] = sum(len(f['trades']) for f in frames)
        if mode == 'embedded':
            cell['capture'] = capture
        else:
            # Content-addressed names prevent an open report from loading a newer run's data.
            key = hashlib.sha256((cell['id'] + '\0' + data).encode()).hexdigest()
            name = key + '.js'
            (asset_dir / name).write_text('window.signalGridReceive(' + json.dumps(cell['id']) + ',' + data + ');\n')
            cell['asset'] = asset_dir.name + '/' + name
    except (ValueError, OSError) as exc:
        cell['load_error'] = str(exc)
manifest['report_mode'] = mode
template = Path(__file__).resolve().parent.parent / 'doc/control-lab/signal-grid/template.html'
data = json.dumps(manifest, separators=(',', ':'), allow_nan=False).replace('<', '\\u003c')
a.output.write_text(template.read_text().replace('/* CAMPAIGN_DATA */', 'const campaign = ' + data + ';'))
print(f'{a.output} ({mode}; {len(paths)} completed cells)')
