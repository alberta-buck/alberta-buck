"""Pre-run spectral diagnostics and an explicit, testable response heuristic.

Price spectra identify observed time scales, not causal production lags.
The returned parameters are frozen before the live experiment starts.
"""
from dataclasses import asdict
import math

import numpy as np

from .pair_observer import ResponseConfig


def spectrum(values, step_days=1.):
    x = np.asarray(values, dtype=float)
    if x.ndim != 1 or len(x) < 64 or not np.all(np.isfinite(x)) or step_days <= 0:
        raise ValueError("need at least 64 finite, regularly spaced historical samples")
    t = np.arange(len(x)) * step_days
    x = x - np.polyval(np.polyfit(t, x, 1), t)
    window = np.hanning(len(x))
    # One-sided periodogram; integral (sum over bins) is signal variance.
    power = abs(np.fft.rfft(x * window))**2 / (len(x) * sum(window**2))
    power[1:-1] *= 2
    frequency = np.fft.rfftfreq(len(x), step_days)
    # High-frequency median / ln(2) estimates white-noise bin mean under
    # exponential periodogram statistics. Colored noise breaks this model.
    high = frequency >= .25 / step_days
    floor = float(np.median(power[high]) / math.log(2))
    band = (frequency >= 2 / (len(x) * step_days)) & (frequency <= 1 / 8)
    signal = np.maximum(power - floor, 0) * band
    reliable = bool(signal.sum() > 1e-6 and np.max(power[band]) > max(10 * floor, 1e-9))
    if reliable:
        f = frequency[band]
        weights = signal[band] / signal.sum()
        period = float(np.exp(np.sum(weights * np.log(1 / f))))
        rms = float(np.sqrt(signal.sum()))
        velocity = float(np.sqrt(np.sum((2*np.pi*frequency)**2 * signal)))
        acceleration = float(np.sqrt(np.sum((2*np.pi*frequency)**4 * signal)))
        response = ResponseConfig(release_speed=max(.00005, .15 * velocity),
            release_rate=.6 / period, closure_gain=3 / max(rms, .02),
            speed_floor=max(.00001, .015 * velocity),
            acceleration_floor=max(1e-7, .05 * acceleration))
    else:
        period, rms, velocity, acceleration = None, None, None, None
        response = ResponseConfig()
    return dict(period_days=period, rms_spread=rms, rms_velocity=velocity,
        rms_acceleration=acceleration, resolved=reliable,
        periods_observed=None if period is None else len(x) * step_days / period,
        warning="observed relative spectrum, not an identified physical commodity cycle",
        frequency_per_day=frequency.tolist(), power=power.tolist(), noise_bin_floor=floor,
        response=asdict(response))


def historical_spectra(log_prices, pairs):
    """Daily, pre-run, two-route averaged normalized logs, N tokens.

    Centering cancels the baseline currency. It also mixes commodity dynamics:
    neither per-token contrasts nor pairs uniquely recover physical cycles.
    """
    values = np.asarray(log_prices, dtype=float)
    if values.ndim != 2:
        raise ValueError("history must be time x token")
    centered = values - values.mean(axis=1, keepdims=True)
    return dict(tokens=[spectrum(centered[:, i]) for i in range(values.shape[1])],
                pairs=[spectrum(centered[:, i] - centered[:, j]) for i, j in pairs],
                samples=len(values), fitted_through_day=-1)
