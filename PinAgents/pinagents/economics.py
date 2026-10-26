"""The one number that decides everything: does a click earn more than it costs?"""


def expected_epc(conversion_rate: float, avg_order: float, commission_rate: float) -> float:
    """Earnings per click before you have data: share of clicks that buy x order size x commission."""
    return conversion_rate * avg_order * commission_rate


def measured_epc(earnings: float, clicks: float) -> float:
    """Earnings per click from real results."""
    return earnings / clicks if clicks else 0.0


def max_cpc(epc: float, margin: float = 0.3) -> float:
    """Highest cost per click you can pay for ads and still keep `margin` of each click's earnings."""
    return max(0.0, epc * (1 - margin))


def ad_profit(epc: float, cpc: float, clicks: int) -> float:
    return (epc - cpc) * clicks
