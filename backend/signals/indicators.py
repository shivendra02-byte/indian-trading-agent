"""Plain-Python indicators over lists of bars ({open, high, low, close, volume}).

All series are aligned with the input (``None`` until enough data). Wilder
smoothing is used for RSI, ATR and ADX, matching common charting platforms.
"""

from __future__ import annotations

import math
from typing import Sequence

Bar = dict


def ema(values: Sequence[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if len(values) < n:
        return out
    k = 2 / (n + 1)
    prev = sum(values[:n]) / n
    out[n - 1] = prev
    for i in range(n, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def sma(values: Sequence[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    total = 0.0
    for i, v in enumerate(values):
        total += v
        if i >= n:
            total -= values[i - n]
        if i >= n - 1:
            out[i] = total / n
    return out


def rsi(closes: Sequence[float], n: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = losses = 0.0
    for i in range(1, n + 1):
        change = closes[i] - closes[i - 1]
        gains += max(change, 0)
        losses += max(-change, 0)
    avg_gain, avg_loss = gains / n, losses / n

    def value(g, l):
        return 100.0 if l == 0 else 100 - 100 / (1 + g / l)

    out[n] = value(avg_gain, avg_loss)
    for i in range(n + 1, len(closes)):
        change = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (n - 1) + max(change, 0)) / n
        avg_loss = (avg_loss * (n - 1) + max(-change, 0)) / n
        out[i] = value(avg_gain, avg_loss)
    return out


def true_ranges(bars: Sequence[Bar]) -> list[float]:
    trs = []
    for i, b in enumerate(bars):
        if i == 0:
            trs.append(b["high"] - b["low"])
        else:
            pc = bars[i - 1]["close"]
            trs.append(max(b["high"] - b["low"], abs(b["high"] - pc), abs(b["low"] - pc)))
    return trs


def _wilder(values: Sequence[float], n: int, start: int = 0) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if len(values) - start < n:
        return out
    prev = sum(values[start:start + n]) / n
    out[start + n - 1] = prev
    for i in range(start + n, len(values)):
        prev = (prev * (n - 1) + values[i]) / n
        out[i] = prev
    return out


def atr(bars: Sequence[Bar], n: int = 14) -> list[float | None]:
    return _wilder(true_ranges(bars), n)


def adx(bars: Sequence[Bar], n: int = 14) -> list[float | None]:
    length = len(bars)
    out: list[float | None] = [None] * length
    if length < 2 * n + 1:
        return out
    plus_dm, minus_dm = [0.0], [0.0]
    for i in range(1, length):
        up = bars[i]["high"] - bars[i - 1]["high"]
        down = bars[i - 1]["low"] - bars[i]["low"]
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
    tr_s = _wilder(true_ranges(bars), n, start=1)
    p_s = _wilder(plus_dm, n, start=1)
    m_s = _wilder(minus_dm, n, start=1)
    dx: list[float] = []
    dx_index: list[int] = []
    for i in range(length):
        if tr_s[i] and p_s[i] is not None and m_s[i] is not None:
            pdi = 100 * p_s[i] / tr_s[i]
            mdi = 100 * m_s[i] / tr_s[i]
            total = pdi + mdi
            dx.append(0.0 if total == 0 else 100 * abs(pdi - mdi) / total)
            dx_index.append(i)
    smoothed = _wilder(dx, n)
    for j, value in enumerate(smoothed):
        if value is not None:
            out[dx_index[j]] = value
    return out


def realized_vol(closes: Sequence[float], n: int = 20) -> list[float | None]:
    """Annualised standard deviation of daily log returns over ``n`` bars."""
    out: list[float | None] = [None] * len(closes)
    rets = [0.0] + [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    for i in range(n, len(closes)):
        window = rets[i - n + 1:i + 1]
        mean = sum(window) / n
        var = sum((r - mean) ** 2 for r in window) / (n - 1)
        out[i] = math.sqrt(var) * math.sqrt(252)
    return out


def swing_points(bars: Sequence[Bar], k: int = 2) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    """Confirmed fractal swings: a high/low with ``k`` lower highs/higher lows on
    each side (a 5-bar fractal for k=2). The last ``k`` bars can't be confirmed."""
    highs, lows = [], []
    for i in range(k, len(bars) - k):
        h, l = bars[i]["high"], bars[i]["low"]
        if all(h > bars[j]["high"] for j in range(i - k, i + k + 1) if j != i):
            highs.append((i, h))
        if all(l < bars[j]["low"] for j in range(i - k, i + k + 1) if j != i):
            lows.append((i, l))
    return highs, lows
