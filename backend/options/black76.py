"""Black-76 option pricing, implied volatility and Greeks (pure Python).

Index options in India are European and cash-settled, so they are priced off
the forward (the futures price for that expiry). ``T`` is in years
(calendar days / 365), ``r`` is the annual risk-free rate, ``kind`` is
"CE" (call) or "PE" (put).
"""

from __future__ import annotations

import math

_SQRT2 = math.sqrt(2.0)
_SQRT2PI = math.sqrt(2.0 * math.pi)
SIGMA_LOW, SIGMA_HIGH = 1e-4, 5.0


def _cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / _SQRT2))


def _pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / _SQRT2PI


def _d1_d2(F: float, K: float, T: float, sigma: float) -> tuple[float, float]:
    v = sigma * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * v * v) / v
    return d1, d1 - v


def price(F: float, K: float, T: float, r: float, sigma: float, kind: str) -> float:
    df = math.exp(-r * max(T, 0.0))
    if T <= 0 or sigma <= 0:
        return df * max(F - K, 0.0) if kind == "CE" else df * max(K - F, 0.0)
    d1, d2 = _d1_d2(F, K, T, sigma)
    if kind == "CE":
        return df * (F * _cdf(d1) - K * _cdf(d2))
    return df * (K * _cdf(-d2) - F * _cdf(-d1))


def implied_vol(premium: float, F: float, K: float, T: float, r: float, kind: str,
                tol: float = 1e-6, max_iter: int = 200) -> float | None:
    """Volatility that reproduces ``premium``; None when no volatility can
    (premium below intrinsic, above the no-arbitrage bound, or expiry day)."""
    if not (premium and premium > 0 and F > 0 and K > 0 and T > 0):
        return None
    df = math.exp(-r * T)
    intrinsic = df * max(F - K, 0.0) if kind == "CE" else df * max(K - F, 0.0)
    upper = df * F if kind == "CE" else df * K
    if premium <= intrinsic + 1e-9 or premium >= upper:
        return None
    lo, hi = SIGMA_LOW, SIGMA_HIGH
    if price(F, K, T, r, hi, kind) < premium:
        return None
    for _ in range(max_iter):   # bisection: price is increasing in sigma
        mid = 0.5 * (lo + hi)
        if price(F, K, T, r, mid, kind) < premium:
            lo = mid
        else:
            hi = mid
        if hi - lo < tol:
            break
    return 0.5 * (lo + hi)


def greeks(F: float, K: float, T: float, r: float, sigma: float, kind: str) -> dict:
    """delta, gamma (per 1 point of the forward), vega (per 1 volatility point)
    and theta (per calendar day)."""
    df = math.exp(-r * T)
    d1, _ = _d1_d2(F, K, T, sigma)
    sqrt_t = math.sqrt(T)
    n1 = _pdf(d1)
    value = price(F, K, T, r, sigma, kind)
    delta = df * _cdf(d1) if kind == "CE" else -df * _cdf(-d1)
    gamma = df * n1 / (F * sigma * sqrt_t)
    vega = df * F * n1 * sqrt_t / 100.0
    theta = (r * value - df * F * n1 * sigma / (2.0 * sqrt_t)) / 365.0
    return {"delta": delta, "gamma": gamma, "vega": vega, "theta": theta}


def forward_from_parity(call: float, put: float, K: float, T: float, r: float) -> float:
    """Put-call parity under Black-76: C - P = e^(-rT) (F - K)."""
    return K + math.exp(r * T) * (call - put)
