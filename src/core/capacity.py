"""Capacity-Awareness Layer (Section 5).

Every thesis tagged with estimated tradeable capacity at *current* portfolio size.
As capital scales, eligible universe auto-shifts from thinnest microcaps toward liquid names.
Untradeable-at-size theses are flagged and capital routed elsewhere (ballast or next idea).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

from .schemas import EVThesis


@dataclass
class CapacityTag:
    tradeable_capacity_usd: float
    status: Literal["ok", "marginal", "too_large", "illiquid"]
    notes: str
    adv_used: float  # avg daily volume used in calc


def estimate_tradeable_capacity(
    avg_daily_volume_shares: float,
    last_price: float,
    current_portfolio_usd: float,
    target_pct_of_adv: float = 0.05,  # never more than 5% ADV on entry
    spread_pct: float = 0.01,
    off_hours: bool = False,
) -> CapacityTag:
    """Simple but load-bearing math. Liquidity first, story second. off_hours tightens per spec."""
    if avg_daily_volume_shares <= 0 or last_price <= 0:
        return CapacityTag(
            tradeable_capacity_usd=0.0,
            status="illiquid",
            notes="no_volume_or_price",
            adv_used=0.0,
        )

    adv_usd = avg_daily_volume_shares * last_price
    raw_cap = adv_usd * target_pct_of_adv

    # Further haircut for wide spreads (friction + exit risk)
    if spread_pct > 0.03:
        raw_cap *= 0.4
    elif spread_pct > 0.015:
        raw_cap *= 0.7

    if off_hours:
        raw_cap *= 0.6  # tighter for off-hours entries (spec)

    # As portfolio grows, we naturally get pushed to names with higher ADV.
    # The caller (EV engine) will compare thesis.tradeable_capacity_usd vs raw_cap
    # and set status.

    status: Literal["ok", "marginal", "too_large", "illiquid"] = "ok"
    notes = ""
    if raw_cap < 500:
        status = "illiquid"
        notes = "capacity_below_min"
    elif raw_cap < current_portfolio_usd * 0.01:  # less than 1% of book is too small to bother?
        status = "marginal"
        notes = "capacity_small_vs_book"

    return CapacityTag(
        tradeable_capacity_usd=round(raw_cap, 2),
        status=status,
        notes=notes,
        adv_used=avg_daily_volume_shares,
    )


def tag_thesis(thesis: EVThesis, tag: CapacityTag) -> EVThesis:
    """Mutate-in-place style: attach capacity info to the thesis object."""
    thesis.tradeable_capacity_usd = tag.tradeable_capacity_usd
    thesis.capacity_tag = tag.status
    if tag.notes:
        if "capacity" not in thesis.event_risk_flags:
            thesis.event_risk_flags = list(thesis.event_risk_flags) + [f"capacity_{tag.status}"]
    return thesis


def is_tradeable_at_size(thesis: EVThesis, current_book_size_usd: float, intended_position_frac: float = 0.10, min_coverage: float = 0.6) -> bool:
    """Used by engines to drop ideas that would move the market against us (Section 5).

    desired = what we would *want* to put on (book * frac), independent of capacity.
    If the name's liquidity capacity cannot support even 60% of that, reject.
    """
    if thesis.tradeable_capacity_usd <= 0:
        return False
    desired = current_book_size_usd * intended_position_frac
    return thesis.tradeable_capacity_usd >= desired * min_coverage


if __name__ == "__main__":
    tag = estimate_tradeable_capacity(80000, 3.25, 12000, spread_pct=0.022)
    print(tag)
    t = EVThesis(
        ticker="TINY",
        event_type="8k",
        upside_pct=30,
        p_upside=0.45,
        downside_pct=-35,
        p_downside=0.3,
        expected_value_pct=7.5,
        prior_accuracy_on_name=0.5,
        what_informed_holders_may_know_that_we_dont="We don't know what management is telling key distributors.",
        tradeable_capacity_usd=0.0,  # will be overwritten
    )
    tagged = tag_thesis(t, tag)
    print("Tagged:", tagged.tradeable_capacity_usd, tagged.capacity_tag)
    print("Tradeable at size?", is_tradeable_at_size(tagged, 12000))


def get_capacity_status(thesis: EVThesis, current_book_size_usd: float, intended_position_frac: float = 0.10, min_coverage: float = 0.6) -> str:
    """P1-C: returns 'ok' | 'marginal' | 'too_large' | 'illiquid' based on book size.
    As book grows, thin names become ineligible (universe shift, spec Section 5).
    """
    if thesis.tradeable_capacity_usd <= 0:
        return "illiquid"
    desired = current_book_size_usd * intended_position_frac
    cov = thesis.tradeable_capacity_usd / max(desired, 1)
    if cov < 0.3:
        return "illiquid"
    if cov < min_coverage:
        return "too_large"
    if cov < 0.85:
        return "marginal"
    return "ok"


def reroute_if_untradeable(thesis: EVThesis, book_usd: float, ballast_targets: Optional[list[str]] = None) -> tuple[str, Optional[str]]:
    """P1-C: if too_large/illiquid, suggest reroute target (e.g. first ballast name) instead of forcing.
    Returns (status, reroute_ticker or None).
    """
    status = get_capacity_status(thesis, book_usd)
    if status in ("too_large", "illiquid"):
        target = (ballast_targets or ["SPY"])[0]
        return status, target
    return status, None
