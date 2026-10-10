"""Causal, Python-only shadow diagnostics over the EVM's interval observations.

Roughness is a measurement-noise proxy under locally linear motion and white
observation noise. Curvature, jumps and correlated noise violate that model.
Neither it nor the Kalman covariance is a calibrated confidence guarantee.
"""
from collections import deque
import math

import numpy as np

from alberta_buck.sim.pair_observer import contrasts

PARAMS = dict(noise_memory_days=5., noise_prior=.002, noise_floor=.0001,
              noise_cap=.05, rate_process_variance=9e-6, warmup_days=2.)
PAIRS = ((0, 1), (0, 2), (1, 2))
LOG_TICK = math.log(1.0001)


class RouteFilter:
    """Two relative coordinates, each with level/rate, and full covariance."""
    def __init__(self, params=None):
        self.params = dict(PARAMS if params is None else params)
        self.basis = contrasts(3)
        self.edges = np.array([self.basis[i]-self.basis[j] for i,j in PAIRS])
        self.history = deque(maxlen=2)
        self.at = self.started = None
        self.x = np.zeros(4)
        self.p = np.diag([self.params["noise_prior"]**2]*2 + [.02**2]*2)
        self.r = np.eye(2)*self.params["noise_prior"]**2
        self.rough = np.zeros((2,2))

    def update(self, day, logs):
        z = self.basis.T @ np.asarray(logs, dtype=float)
        if not math.isfinite(day) or not np.all(np.isfinite(z)):
            raise ValueError("finite observations required")
        dt = 0 if self.at is None else day-self.at
        if self.at is not None and dt <= 0:
            raise ValueError("shadow observations must advance time")
        nis = None
        if self.at is None:
            self.x[:2] = z
            self.started = day
        else:
            eye = np.eye(2)
            f = np.block([[eye,dt*eye],[np.zeros((2,2)),eye]])
            q = self.params["rate_process_variance"] * np.block(
                [[dt**3/3*eye,dt**2/2*eye],[dt**2/2*eye,dt*eye]])
            self.x = f @ self.x
            self.p = f @ self.p @ f.T + q
            innovation = z-self.x[:2]
            s = self.p[:2,:2]+self.r
            gain = np.linalg.solve(s,self.p[:2,:]).T
            nis = float(innovation @ np.linalg.solve(s,innovation)/2)
            self.x += gain @ innovation
            a = np.eye(4)
            a[:,:2] -= gain
            self.p = a @ self.p @ a.T + gain @ self.r @ gain.T
            self.p = (self.p+self.p.T)/2
        # Update noise for the NEXT sample. An observation cannot change R
        # before its own innovation and state correction have been assessed.
        if len(self.history) == 2:
            (t0,z0),(t1,z1) = self.history
            ratio = (day-t1)/(t1-t0)
            d2 = z-(1+ratio)*z1+ratio*z0
            denom = 1+(1+ratio)**2+ratio**2
            sample = np.outer(d2,d2)/denom
            alpha = -math.expm1(-dt/self.params["noise_memory_days"])
            self.rough = (1-alpha)*self.rough+alpha*sample
            # Bound adaptation without hiding the uncapped roughness/NIS.
            cap = self.params["noise_cap"]**2
            trace = float(np.trace(sample))
            if trace > cap:
                sample *= cap/trace
            self.r = ((1-alpha)*self.r + alpha*
                      (sample+np.eye(2)*self.params["noise_floor"]**2))
        self.history.append((day,z.copy()))
        self.at = day
        ready = len(self.history) == 2 and day-self.started >= self.params["warmup_days"]
        def variance(block):
            return np.maximum(0,np.einsum('ij,jk,ik->i',self.edges,block,self.edges))
        return dict(ready=ready, level=(self.edges @ self.x[:2]).tolist(),
            rate=(self.edges @ self.x[2:]).tolist(), rate_sd=np.sqrt(variance(self.p[2:,2:])).tolist(),
            level_sd=np.sqrt(variance(self.p[:2,:2])).tolist(),
            roughness_sd=np.sqrt(variance(self.rough)).tolist(),
            measurement_sd=np.sqrt(variance(self.r)).tolist(), innovation_nis=nis,
            observed=(self.edges @ z).tolist())


class NoiseComparison:
    def __init__(self):
        self.filters = [RouteFilter(),RouteFilter()]
        self.disagreement = np.zeros(3)
        self.at = None

    def update(self, day, logs):
        routes = [f.update(day,z) for f,z in zip(self.filters,logs)]
        dt = 0 if self.at is None else day-self.at
        if dt:
            delta = np.array(routes[0]["observed"])-routes[1]["observed"]
            alpha = -math.expm1(-dt/PARAMS["noise_memory_days"])
            self.disagreement = (1-alpha)*self.disagreement+alpha*delta**2/2
        self.at = day
        ready = all(r["ready"] for r in routes)
        pairs = []
        for p in range(3):
            rate = sum(r["rate"][p] for r in routes)/2
            low = min(r["rate"][p]-2*r["rate_sd"][p] for r in routes)
            high = max(r["rate"][p]+2*r["rate_sd"][p] for r in routes)
            pairs.append(dict(rate_bp_day=rate*10000, low_bp_day=low*10000,
                high_bp_day=high*10000,
                level_bp=sum(r["level"][p] for r in routes)/2*10000,
                roughness_bp=math.sqrt(sum(r["roughness_sd"][p]**2 for r in routes)/2)*10000,
                measurement_bp=math.sqrt(sum(r["measurement_sd"][p]**2 for r in routes)/2)*10000,
                disagreement_bp=math.sqrt(self.disagreement[p])*10000,
                route_rate_bp_day=[r["rate"][p]*10000 for r in routes],
                route_rate_sd_bp_day=[r["rate_sd"][p]*10000 for r in routes]))
        return dict(ready=ready, midpoint_day=day, pairs=pairs,
                    innovation_nis=[r["innovation_nis"] for r in routes])


def shadow_frames(result):
    """Use the exact cumulative-tick intervals, not post-trade spot substitutes.

    A missing/stale route resets both shadow routes. No extrapolated stale
    estimate is published. The current Solidity decision remains untouched.
    """
    metadata = result.get("observer_routes")
    if metadata is None:
        return [None]*len(result["frames"])
    previous = [None,None]
    model = NoiseComparison()
    out = []
    base = np.log(result["config"]["feed"]["base"])
    max_gap = result["observer_params"]["max_gap"]
    for frame in result["frames"]:
        signal = frame.get("pair_signal")
        if signal is None:
            out.append(None)
            continue
        if all(previous[q] is not None and signal["venues"][q]["readable"]
               and signal["venues"][q]["endpoint"] == previous[q][0] for q in range(2)):
            out.append(out[-1] if out else None)
            continue
        observations, midpoints = [], []
        complete = True
        for q in range(2):
            venue = signal["venues"][q]
            at = venue["endpoint"]
            cumulative = [int(p["cumulative"]) for p in signal["pool_samples"][q*3:q*3+3]]
            before = previous[q]
            previous[q] = (at,cumulative) if venue["readable"] else None
            if not venue["readable"] or before is None or not 0 < at-before[0] <= max_gap:
                complete = False
                continue
            dt = at-before[0]
            mean = []
            for i,(now,old) in enumerate(zip(cumulative,before[1])):
                delta = (now-old+(1<<55)) % (1<<56)-(1<<55)
                route = metadata[q*3+i]
                tick = delta/dt * (1 if route["token0"] else -1) + route["decimal_offset_ticks"]
                mean.append(tick*LOG_TICK-base[i])
            observations.append(mean)
            midpoints.append((at+before[0])/2)
        if not complete or midpoints[0] != midpoints[1]:
            model = NoiseComparison()
            out.append(None)
            continue
        midpoint_day = signal["venues"][0]["midpoint_day"]
        diagnostic = model.update(midpoint_day,observations)
        diagnostic["ready"] &= signal["ready_mask"] == 3
        out.append(diagnostic)
    return out
