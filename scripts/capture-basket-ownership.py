#!/usr/bin/env python3
"""Measure daily receipt ownership and transaction-level share flows in one simulation.

Boundary is immediately before the first day's ticks, then each preceding close to
next close. Day-start activity after the first close belongs to the next window.
No production contracts or simulator rules are changed.
"""
import argparse
import json
import os
from pathlib import Path

from alberta_buck.sim import experiment
from alberta_buck.sim.loop import run
from alberta_buck.sim.pyrevm_backend import PyrevmAnvil


def ownership_recorder():
    """Return reusable (on_day_start, on_frame, rows) hooks for loop.run."""
    deployment = None
    active_agents = None
    rows = []
    window = []
    errors = []
    opening = None
    last_shares = None
    last_treasury = None

    def state():
        d = deployment
        basket = d.basket
        total = int(basket.functions.totalShares().call())
        equity = int(basket.functions.equity().call())
        treasury = int(basket.functions.treasuryShares().call())
        receipt = d.chain.at('BuckBasketReceipt', basket.functions.receipt().call())
        fund = d.net_stack.fund
        fund_id = int(fund.functions.positionId().call())
        seen = set()
        owners = {'time': 0, 'household': 0, 'jubilee': 0}

        def collect(kind, rid, owner):
            if not rid:
                return
            rid = int(rid)
            if rid in seen:
                raise ValueError(f'duplicate tracked receipt {rid}')
            seen.add(rid)
            if receipt.functions.ownerOf(rid).call().lower() != owner.lower():
                raise ValueError(f'receipt {rid} has unexpected owner')
            owners[kind] += int(basket.functions.holdings(rid).call()[0])

        collect('jubilee', fund_id, fund.address)
        for agent in active_agents:
            rid = getattr(agent, '_receipt_id', None)
            if rid is not None and not getattr(agent, '_exited', False):
                collect('time', rid, agent.address)
            ids = getattr(agent, '_basket_receipts', [])
            savings = getattr(agent, '_savings_proxy', None)
            for rid in ids:
                collect('household', rid, savings.address if savings else agent.address)
        other = total - treasury - sum(owners.values())
        if other < 0:
            raise ValueError('tracked shares exceed total')
        return {'block': int(d.w3.eth.block_number), 'total_shares': str(total), 'equity_buck': str(equity),
                'receipt_shares': {**{k: str(v) for k, v in owners.items()},
                                   'other_untracked': str(other)}, 'treasury_shares': str(treasury)}

    def start(day, d, agents, counters):
        nonlocal deployment, active_agents, opening, last_shares, last_treasury
        active_agents = agents
        if deployment is not None:
            return
        deployment = d
        opening = state()
        last_shares = int(opening['total_shares'])
        last_treasury = int(opening['treasury_shares'])
        send = d.chain.send
        events = {name: getattr(d.basket.events, name)() for name in ('Deposited', 'Redeemed')}
        topics = {d.w3.keccak(text=e.signature): (name, e) for name, e in events.items()}
        fund = d.net_stack.fund.address.lower()

        def record(*a, **kw):
            nonlocal last_shares, last_treasury
            receipt = send(*a, **kw)
            current = int(d.basket.functions.totalShares().call())
            delta = current - last_shares
            treasury = int(d.basket.functions.treasuryShares().call())
            flows = []
            for log in receipt['logs'] if receipt else ():
                if log['address'].lower() != d.basket.address.lower() or not log['topics']:
                    continue
                found = topics.get(log['topics'][0])
                if found:
                    name, decoder = found
                    values = decoder.process_log(log)['args']
                    flows.append((name, values['who'].lower()))
            if flows and treasury < last_treasury:
                errors.append('treasury retirement combined with receipt flow: gross flow ambiguous')
                raise ValueError(errors[-1])
            if len(flows) > 1:
                errors.append('multiple share-flow events in one transaction: gross flow ambiguous')
                raise ValueError(errors[-1])
            if delta:
                if flows and ((delta > 0) != (flows[0][0] == 'Deposited')):
                    errors.append('share-supply change disagrees with flow event')
                    raise ValueError(errors[-1])
                owner = ('jubilee' if flows[0][1] == fund else 'other') if flows else 'unattributed'
                window.append({'tx': receipt['transactionHash'].hex() if receipt else None,
                               'kind': 'minted' if delta > 0 else 'retired',
                               'owner': owner, 'shares': str(abs(delta)),
                               'event': flows[0][0] if flows else None})
            elif flows:
                errors.append('flow event without a net supply change: gross flow ambiguous')
                raise ValueError(errors[-1])
            last_shares = current
            last_treasury = treasury
            return receipt
        d.chain.send = record

    def capture(frame):
        nonlocal opening
        if errors:
            raise ValueError('share-flow measurement failed: ' + errors[0])
        close = state()
        flow = {kind: {owner: 0 for owner in ('jubilee', 'other', 'unattributed')} for kind in ('minted', 'retired')}
        for item in window:
            flow[item['kind']][item['owner']] += int(item['shares'])
        minted = sum(flow['minted'].values())
        retired = sum(flow['retired'].values())
        if int(opening['total_shares']) + minted - retired != int(close['total_shares']):
            raise ValueError(f"day {frame['day']}: supply does not reconcile")
        rows.append({'day': frame['day'], 'opening': opening, 'closing': close,
                     'flows': {kind: {owner: str(value) for owner, value in values.items()}
                               for kind, values in flow.items()}, 'transactions': window.copy()})
        window.clear()
        opening = close

    return start, capture, rows



def dashboard_recorder(recorder, capture_hook, rows):
    """Pair ownership hook with loop.run's earlier RunRecorder.frame callback.

    Returns a proxy recorder and a replacement on_frame hook. The proxy delays
    each ordinary frame until the ownership close has been measured.
    """
    class DelayedRecorder:
        def __init__(self):
            self.pending = None

        def __getattr__(self, name):
            return getattr(recorder, name)

        def frame(self, flat, applied=()):
            if self.pending is not None:
                raise ValueError('previous frame was not completed')
            self.pending = (flat, applied)

    proxy = DelayedRecorder()

    def on_frame(flat):
        capture_hook(flat)
        if proxy.pending is None:
            raise ValueError('standard frame missing before ownership capture')
        pending, applied = proxy.pending
        if pending['day'] != rows[-1]['day']:
            raise ValueError('ownership and standard frame days disagree')
        observation = {key: value for key, value in rows[-1].items() if key != 'transactions'}
        recorder.frame({**pending, 'ownership': observation}, applied=applied)
        proxy.pending = None

    return proxy, on_frame

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('output', type=Path)
    p.add_argument('--experiment', default='alberta_buck/sim/experiments/economy.toml')
    p.add_argument('--days', type=int, default=3)
    p.add_argument('--set', dest='settings', action='append', default=[])
    p.add_argument('--record', type=Path, help='standard runrecord directory; defaults to SIM_RUNS when set')
    p.add_argument('--run-id', help='standard runrecord ID; defaults to SIM_NAME when set')
    args = p.parse_args()
    if args.days < 1:
        p.error('--days must be positive')
    exp = experiment.load(args.experiment, sets=args.settings)
    exp.scenario['days'] = args.days
    scenario = experiment.build(exp)
    start, capture, rows = ownership_recorder()

    recorder = None
    run_id = args.run_id or os.environ.get('SIM_NAME')
    if args.record or os.environ.get('SIM_RUNS') or run_id:
        from alberta_buck.sim.runrecord import RunRecorder
        recorder = RunRecorder(args.record, run_id or args.output.stem, run_id=run_id, engine='pyrevm')
    proxy = recorder
    if recorder:
        proxy, capture = dashboard_recorder(recorder, capture, rows)
    try:
        with PyrevmAnvil() as backend:
            summary = run(scenario, backend, on_day_start=start, on_frame=capture, recorder=proxy)
        if len(rows) != args.days:
            raise ValueError('capture does not cover requested daily horizon')
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps({'schema_version': 1, 'config': exp.resolved(),
            'units': {'shares': 'raw contract share units (bootstrap uses raw BUCK units)', 'equity_buck': 'raw 1e6 BUCK'},
            'boundary': 'first pre-tick opening, then preceding close through current close',
            'limitations': ['time is tracked investor receipts; household is tracked household receipts; untracked receipts are residual', 'gross is resolved per transaction; multiple flow events or zero-net flow events fail capture',
                            'receipt shares moved to treasury as redemption cut are not supply retirement',
                            'unattributed flows have no matching basket deposit or redemption event'],
            'days': rows}, separators=(',', ':')))
    except BaseException as exc:
        if recorder and recorder.manifest:
            recorder.close('failed', error=repr(exc))
        raise
    if recorder and recorder.manifest:
        recorder.close('complete', outputs={'ownership': args.output.resolve(), 'vector': summary.get('json'), 'pdf': summary.get('pdf')})
        print(f'Run record: {recorder.dir}')
    print(args.output)


if __name__ == '__main__':
    main()
