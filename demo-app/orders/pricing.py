"""Order pricing.

`order_total` is the production pricing path. `order_total_volume_discount` is
the new pricing engine, rolled out behind the `new_pricing` feature flag.
"""

VOLUME_DISCOUNT_TIERS = ((10, 0.10), (5, 0.05))  # (min units, discount)


def order_total(lines: list[dict]) -> int:
    """Total in cents for reserved lines (each with unit_price in cents)."""
    return sum(line["unit_price"] * line["qty"] for line in lines)


def order_total_volume_discount(lines: list[dict]) -> float:
    units = sum(line["qty"] for line in lines)
    subtotal = sum(line["unit_price"] * line["qty"] for line in lines) / 100
    discount = next((rate for min_units, rate in VOLUME_DISCOUNT_TIERS if units >= min_units), 0.0)
    return round(subtotal * (1 - discount), 2)
