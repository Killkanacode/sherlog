"""Exponentially weighted mean/variance, the baseline behind every detector."""

import math


class Ewma:
    def __init__(self, alpha: float) -> None:
        self.alpha = alpha
        self.mean: float | None = None
        self.var = 0.0
        self.samples = 0

    def update(self, value: float) -> None:
        if self.mean is None:
            self.mean = value
        else:
            delta = value - self.mean
            self.mean += self.alpha * delta
            self.var = (1 - self.alpha) * (self.var + self.alpha * delta * delta)
        self.samples += 1

    @property
    def std(self) -> float:
        return math.sqrt(self.var)
