"""Evidence-based deterministic veto thresholds (Fable edge recon).

Every threshold is a named, configurable constant with measured base-rate evidence
in the comment. Base rates are pulled from umbrella/edge_recon/data/*.csv — do not
eyeball or invent. Microcap band = entry $1–$20, median DDV $100k–$50M (hood-compatible).
"""

from __future__ import annotations

# --- V1: Going-concern PERSISTENCE (confirm + document; logic in auditor.py) ---
# Source: edge_recon/data/bt_gc_persist.csv, microcap-filtered (n=78 priced rows).
# ab_20d net abnormal −8.37% (t=−2.57); ab_60d net −13.63% (t=−2.44).
# Full priced cohort (n=187, same CSV): ab_20d net −9.55% (t=−3.84) — prose cross-check.
GOING_CONCERN_PERSIST_AB_20D_NET_PCT = -8.3723
GOING_CONCERN_PERSIST_AB_20D_NET_T = -2.5726
GOING_CONCERN_PERSIST_AB_20D_NET_N = 78
GOING_CONCERN_PERSIST_AB_60D_NET_PCT = -13.6338
GOING_CONCERN_PERSIST_AB_60D_NET_T = -2.4427
GOING_CONCERN_PERSIST_AB_60D_NET_N = 78

# --- V2: Earnings-POP chase veto ---
# Source: edge_recon/data/bt_8k.csv — cohort is 2-bar reaction >= +5%, micro n=443.
# Net abnormal: 5d −1.04% (t=−1.89); 60d −5.35% (t=−3.08, n=441). Reverses by 60d.
EARNINGS_POP_VETO_PCT = 5.0
EARNINGS_POP_AB_5D_NET_PCT = -1.0430
EARNINGS_POP_AB_5D_NET_T = -1.8864
EARNINGS_POP_AB_5D_NET_N = 443
EARNINGS_POP_AB_60D_NET_PCT = -5.3511
EARNINGS_POP_AB_60D_NET_T = -3.0809
EARNINGS_POP_AB_60D_NET_N = 441

# --- V3: Earnings-CRASH catch veto ---
# Source: FABLE_EDGE_RECON_RESULTS.md §2.4 crash cohort — earnings 8-Ks whose 2-bar reaction
# is <= -10%, marked like the pop cohort, micro band n=351.
# PROVENANCE NOTE (auditor 2026-07-05): unlike V1/V2/V4, the crash-cohort MARKED artifact was
# NOT saved to edge_recon/data — `events_8k.csv` holds only the event list (no return columns),
# so this figure is not re-derivable from a saved CSV. Regenerate via edge_recon/code/backtest.py
# with a `d0_ret <= -0.10` crash filter (needs a pandas env) and save `bt_8k_crash.csv` before V3
# goes live. The number below matches the audited recon prose but should be reconfirmed then.
# ab_20d net −3.61% (t=−3.24); no bounce — keeps falling.
EARNINGS_CRASH_VETO_PCT = -10.0
EARNINGS_CRASH_AB_20D_NET_PCT = -3.6072
EARNINGS_CRASH_AB_20D_NET_T = -3.24
EARNINGS_CRASH_AB_20D_NET_N = 351
EARNINGS_CRASH_AB_60D_NET_PCT = -4.9188
EARNINGS_CRASH_AB_60D_NET_T = -2.38
EARNINGS_CRASH_AB_60D_NET_N = 351

# --- V4: Raw 13D "not bullish" veto ---
# Source: edge_recon/data/bt_13d.csv, microcap-filtered n=3185.
# ab_20d gross −2.53% (t=−5.28); net −5.09% (t=−10.56).
# Full priced cohort (n=6767): ab_20d gross −2.27% (t=−6.56) — adverse selection.
RAW_13D_AB_20D_GROSS_PCT = -2.5321
RAW_13D_AB_20D_GROSS_T = -5.2804
RAW_13D_AB_20D_GROSS_N = 3185
RAW_13D_AB_20D_NET_PCT = -5.0865
RAW_13D_AB_20D_NET_T = -10.5626

# Hood maintains no activist filer watchlist yet → veto all raw 13D longs.
ACTIVIST_FILER_CIKS: frozenset[str] = frozenset()