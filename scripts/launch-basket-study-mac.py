"""Run a named Make study job under the current Mac login, independent of a terminal."""
import argparse
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
TARGETS = ('basket-study-dashboard', 'basket-study-sync', 'basket-study-run', 'basket-study-report')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('name', help='A stable local name, such as mirror-730')
    parser.add_argument('target', choices=TARGETS)
    parser.add_argument('settings', nargs='*', help='Make overrides supplied as individual KEY=value arguments')
    args = parser.parse_args()
    if sys.platform != 'darwin' or not re.fullmatch('[a-z0-9][a-z0-9-]{0,63}', args.name):
        parser.error('use a short lowercase job name and run under the current Mac login')
    for item in args.settings:
        if not re.fullmatch(r'(BASKET_[A-Z_]+|DASHBOARD_PORT)=.+', item):
            parser.error('expected a basket study setting as KEY=value')
    label = 'org.albertabuck.study.' + args.name
    domain = f'gui/{os.getuid()}'
    folder = ROOT / 'build/sim/mac-jobs'
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (args.name + '.plist')
    make = shutil.which('make')
    if not make:
        parser.error('make is unavailable')
    config = dict(Label=label, RunAtLoad=True, KeepAlive=False,
                  WorkingDirectory=str(ROOT), ProgramArguments=[make, args.target, *args.settings],
                  EnvironmentVariables=dict(PATH=os.environ['PATH'], PYTHONPATH=os.pathsep.join(
                      str(p) for p in (ROOT, ROOT / 'core/python', ROOT / 'core/contracts/python'))),
                  StandardOutPath=str(folder / (args.name + '.log')),
                  StandardErrorPath=str(folder / (args.name + '.err')))
    data = plistlib.dumps(config)
    registered = subprocess.run(['launchctl', 'print', f'{domain}/{label}'],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    if registered:
        if not path.is_file() or path.read_bytes() != data:
            parser.error(f'{label} is registered with different settings; choose another name')
        # kickstart without -k leaves an already running instance intact and
        # resumes a previously stopped worker with the same campaign arguments.
        subprocess.run(['launchctl', 'kickstart', f'{domain}/{label}'], check=True)
    else:
        path.write_bytes(data)
        subprocess.run(['launchctl', 'bootstrap', domain, str(path)], check=True)
    print(f'{label} registered; logs in {folder}')


if __name__ == '__main__':
    main()
