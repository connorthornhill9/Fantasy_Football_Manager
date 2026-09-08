"""Apply a league's scoring settings to a Sleeper stat line."""
from __future__ import annotations


def score_stats(stats: dict | None, scoring_settings: dict | None) -> float:
    """Dot product of a stat dictionary with the league's per-stat weights.

    This is how Sleeper computes fantasy points, so it works for custom scoring too.
    """
    if not stats or not scoring_settings:
        return 0.0
    total = 0.0
    for key, weight in scoring_settings.items():
        value = stats.get(key)
        if value:
            try:
                total += float(value) * float(weight)
            except (TypeError, ValueError):
                continue
    return round(total, 2)


def describe_scoring(scoring_settings: dict | None) -> str:
    """Short human-readable summary of the important scoring rules."""
    if not scoring_settings:
        return "unknown"
    s = scoring_settings
    parts = []
    rec = s.get("rec", 0) or 0
    if rec >= 1:
        parts.append("full PPR")
    elif rec > 0:
        parts.append(f"{rec:g} PPR")
    else:
        parts.append("standard (no PPR)")
    if s.get("bonus_rec_te"):
        parts.append(f"TE premium +{s['bonus_rec_te']:g}")
    parts.append(f"pass TD {s.get('pass_td', 4):g}")
    parts.append(f"rush/rec TD {s.get('rush_td', 6):g}")
    if s.get("pass_int"):
        parts.append(f"INT {s['pass_int']:g}")
    if s.get("fum_lost"):
        parts.append(f"fumble lost {s['fum_lost']:g}")
    for key, label in (
        ("bonus_rec_yd_100", "100 rec yd bonus"),
        ("bonus_rush_yd_100", "100 rush yd bonus"),
        ("bonus_pass_yd_300", "300 pass yd bonus"),
    ):
        if s.get(key):
            parts.append(f"{label} +{s[key]:g}")
    idp = [
        f"{label} {s[key]:g}"
        for key, label in (
            ("idp_tkl_solo", "solo tkl"),
            ("idp_tkl_ast", "ast tkl"),
            ("idp_tkl_loss", "TFL"),
            ("idp_sack", "sack"),
            ("idp_int", "INT"),
            ("idp_pass_def", "PD"),
            ("idp_ff", "FF"),
            ("idp_fum_rec", "FR"),
            ("idp_def_td", "def TD"),
        )
        if s.get(key)
    ]
    if idp:
        parts.append("IDP: " + ", ".join(idp))
    return ", ".join(parts)


def full_scoring_markdown(scoring_settings: dict | None) -> str:
    """Every non-zero rule, compact, so the model can reason about unusual settings."""
    if not scoring_settings:
        return "(none)"
    items = sorted((k, v) for k, v in scoring_settings.items() if v)
    return ", ".join(f"{k}={v:g}" for k, v in items)
