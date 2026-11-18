"""Ads autopilot: rule-based problem detection plus safe, capped fixes.

analyze() is pure (no network, no database) so it is easy to test.
Applying a fix on a real ad platform (Google Ads, Meta, ...) needs that platform's
credentials. Until an adapter is connected, NoopAdapter only RECOMMENDS the fix.
"""


def _f(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def analyze(campaign, m):
    """campaign: daily_budget, max_daily_budget, target_cpa. m: metrics for the latest period."""
    spend, imp, clicks, conv = _f(m.get("spend")), _f(m.get("impressions")), _f(m.get("clicks")), _f(m.get("conversions"))
    days = _f(m.get("days_running"))
    status = str(m.get("status", "active")).lower()
    budget = _f(campaign.get("daily_budget"))
    cap = _f(campaign.get("max_daily_budget"))
    target = _f(campaign.get("target_cpa"))
    ctr = (clicks / imp * 100) if imp else 0.0
    cpa = (spend / conv) if conv else 0.0
    out = []

    def add(code, severity, diagnosis, action, params=None, auto=True):
        out.append({"code": code, "severity": severity, "diagnosis": diagnosis, "action": action, "params": params or {}, "auto": auto})

    if status in ("disapproved", "rejected"):
        reason = str(m.get("disapproval_reason") or "no reason given")[:300]
        add("AD_DISAPPROVED", "high", f"The ad was rejected by the platform: {reason}.", "rewrite_and_resubmit", {"reason": reason})
    elif status in ("limited", "paused_by_platform"):
        add("LIMITED_DELIVERY", "medium", "The platform is limiting delivery (billing, policy or learning phase).", "review_targeting_and_budget", {}, auto=False)

    if status == "active" and imp == 0 and days >= 1:
        add("NO_DELIVERY", "high", "The campaign is active but got zero impressions. Check billing, targeting size and bids.", "broaden_audience", {}, auto=False)

    if imp >= 1000 and ctr < 0.5:
        add("LOW_CTR", "medium", f"Click-through rate is {ctr:.2f}% (below 0.5%). The creative is not attracting clicks.", "refresh_creative", {"ctr": round(ctr, 2)})

    if budget > 0 and spend >= 0.95 * budget and conv > 0 and (target == 0 or cpa <= target):
        new_budget = min(round(budget * 1.2, 2), cap) if cap > budget else budget
        add(
            "BUDGET_CAPPED_GOOD_PERFORMANCE", "low",
            "The daily budget is used up while results are good. A small increase can bring more sales.",
            "increase_budget", {"from": budget, "to": new_budget},
            auto=cap > budget,  # only automatic when the owner set a maximum budget
        )

    if target > 0:
        if conv > 0 and cpa > 1.5 * target:
            add("HIGH_CPA", "high", f"Cost per sale is {cpa:.2f}, over 1.5x your target of {target:.2f}.", "decrease_budget", {"from": budget, "to": round(budget * 0.8, 2)})
        elif conv == 0 and spend >= 3 * target:
            add("NO_CONVERSIONS", "high", f"Spent {spend:.2f} (3x target cost per sale) with no sales.", "pause_campaign", {})
    elif clicks >= 50 and conv == 0:
        add("CLICKS_NO_SALES", "medium", "People click but nobody buys. Check the landing page, price and checkout.", "check_landing_page", {}, auto=False)

    return out


class NoopAdapter:
    """Used when no ad platform is connected: the fix is recommended, not applied."""

    name = "not_connected"

    def apply(self, action, params):
        return "recommended"


def execute(adapter, problem):
    """Return action status: applied | recommended | needs_human."""
    if not problem["auto"]:
        return "needs_human"
    return adapter.apply(problem["action"], problem["params"])
