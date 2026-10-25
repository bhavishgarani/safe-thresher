"""Safe Thresher CPU-only, offline sensor-signal prediction.

This is an advisory predictor of ADC threshold crossing, NOT physical water depth.
No cloud services, GPU, or scikit-learn required.
"""
from collections import deque
from dataclasses import dataclass, asdict
from statistics import median
import math

@dataclass
class Prediction:
    ready: bool = False
    trend: str = 'COLLECTING DATA'
    adc_per_min: float = 0.0
    eta_seconds: float | None = None
    signal_adc: float | None = None
    noise_adc: float = 0.0
    samples: int = 0
    span_seconds: float = 0.0
    confidence: str = 'LOW'
    warning: str = 'Collecting readings'
    def as_dict(self):
        return asdict(self)

class PredictionEngine:
    """Robust rolling slope from 2-second median bins; no external dependencies."""
    def __init__(self, window_seconds=30.0, max_gap_seconds=3.0):
        self.window_seconds = float(window_seconds)
        self.max_gap_seconds = float(max_gap_seconds)
        self.history = deque()
        self.last_time = None
        self.rapid_since = None
        self.last_result = Prediction()

    def reset(self):
        self.history.clear()
        self.last_time = None
        self.rapid_since = None
        self.last_result = Prediction()

    def update(self, adc, timestamp, critical_threshold):
        if not 0 <= adc <= 1023 or not math.isfinite(timestamp):
            raise ValueError('Invalid sensor reading or timestamp')
        if self.last_time is not None and (timestamp <= self.last_time or timestamp - self.last_time > self.max_gap_seconds):
            self.reset()
        self.last_time = timestamp
        self.history.append((timestamp, float(adc)))
        while self.history and timestamp - self.history[0][0] > self.window_seconds:
            self.history.popleft()
        samples = list(self.history)
        span = samples[-1][0] - samples[0][0]
        if len(samples) < 20 or span < 10:
            self.last_result = Prediction(samples=len(samples), span_seconds=round(span, 1))
            return self.last_result

        # Median within two-second bins reduces spikes without needing NumPy.
        buckets = {}
        for t, val in samples:
            bucket = int((t - samples[0][0]) / 2)
            buckets.setdefault(bucket, []).append((t, val))
        pts = [(median(t for t, _ in values), median(v for _, v in values))
               for values in buckets.values() if len(values) >= 2]
        if len(pts) < 5:
            self.last_result = Prediction(samples=len(samples), span_seconds=round(span, 1))
            return self.last_result
        # Least-squares trend on median-binned points.
        t0 = pts[0][0]
        xs = [t - t0 for t, _ in pts]
        ys = [v for _, v in pts]
        xm, ym = sum(xs)/len(xs), sum(ys)/len(ys)
        denom = sum((x-xm)**2 for x in xs)
        slope = sum((x-xm)*(y-ym) for x,y in zip(xs,ys)) / denom if denom else 0.0
        rate = slope * 60.0
        residuals = [abs(y-(ym+slope*(x-xm))) for x,y in zip(xs,ys)]
        noise = median(residuals)
        signal = median(v for _,v in samples[-min(7,len(samples)):])
        if rate >= 400:
            trend = 'RAPID RISE'
        elif rate >= 100:
            trend = 'MODERATE RISE'
        elif rate >= 20:
            trend = 'SLOW RISE'
        elif rate <= -20:
            trend = 'FALLING'
        else:
            trend = 'STABLE'
        # Don't extrapolate through a noisy / nonlinear trace.
        stable_trend = noise <= max(12, abs(rate)*0.10)
        confidence = 'HIGH' if span >= 20 and stable_trend else ('MEDIUM' if stable_trend else 'LOW')
        eta = None
        if critical_threshold is not None and signal >= critical_threshold:
            eta = 0.0
        elif critical_threshold is not None and rate >= 20 and stable_trend:
            eta = max(0.0, (critical_threshold - signal) / (rate / 60.0))
            if eta > 300:  # Don't pretend long-horizon extrapolations are reliable.
                eta = None
        # Require sustained rapid rise to avoid one-shot warnings.
        if trend == 'RAPID RISE' and stable_trend:
            if self.rapid_since is None:
                self.rapid_since = timestamp
        else:
            self.rapid_since = None
        rapid_confirmed = self.rapid_since is not None and timestamp - self.rapid_since >= 3
        if rapid_confirmed:
            warning = 'PREDICTIVE WARNING: sustained rapid sensor rise'
        elif eta is not None and 0 < eta <= 30 and confidence != 'LOW':
            warning = 'PREDICTIVE WARNING: critical ADC band approaching'
        elif trend in ('RAPID RISE','MODERATE RISE'):
            warning = 'Upward trend under observation'
        elif trend == 'FALLING':
            warning = 'Sensor signal decreasing'
        else:
            warning = 'No predictive warning'
        self.last_result = Prediction(True, trend, round(rate, 1), round(eta,1) if eta is not None else None,
                                      round(signal,1), round(noise,1), len(samples), round(span,1), confidence, warning)
        return self.last_result
