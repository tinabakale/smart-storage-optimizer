"""
migration_simulator.py -- Migration Simulation
==============================================

Part of: "Smart Storage Optimization for Modern Workloads"

Turns a recommendations DataFrame into a MIGRATION PLAN and a before/after
picture of storage. It is a pure simulation: no files are moved, nothing
connects to any cloud provider, and the input DataFrame is never modified.

Prices come from `cost_calculator` and are DEMO prices unless you pass your
own `pricing` (see cost_calculator.py). All costs are estimated MONTHLY costs.

Quick start
-----------
    from src.migration_simulator import simulate_migration, run_migration_simulation

    plan = simulate_migration(df)              # the migration plan table

    result = run_migration_simulation(df)      # everything Developer 5 needs
    result["plan"]                 # DataFrame, same as simulate_migration(df)
    result["summary"]              # dict of headline numbers
    result["routes"]               # DataFrame: one row per route, e.g. "HOT -> COLD"
    result["before_distribution"]  # DataFrame: storage per tier today
    result["after_distribution"]   # DataFrame: storage per tier after migration

Input DataFrame (from Developer 3)
----------------------------------
Required columns: file_name, size_gb, current_tier, recommended_tier
(optimization_score, recommendation_reason, ... are ignored here.)

Migration plan columns (`simulate_migration`)
---------------------------------------------
    file_name            str
    current_tier         str    upper-cased; "UNKNOWN" if missing
    recommended_tier     str    upper-cased; "UNKNOWN" if missing
    action               str    "Migrate HOT -> COLD" | "No migration required" |
                                "Review required: tier not recognised"
    estimated_savings    float  monthly size_gb x (current price - recommended price).
                                0.0 when no migration is needed; NaN for rows that
                                need review or have a non-numeric size_gb. Negative
                                means the move ADDS cost.
    size_gb              float  convenience column for charts/filters
    migration_required   bool   True only for rows with a real tier change
                                (use this to filter the plan)

(The arrow in the real `action` text is the single character U+2192.)

Rows whose current or recommended tier is not HOT/WARM/COLD/ARCHIVE (e.g. the
"UNKNOWN" tier data_loader.py assigns to blank values) are never "migrated":
they are flagged for review and stay where they are in the simulated "after"
state, so the before/after numbers always stay consistent.

Distribution DataFrame columns (`get_tier_distribution`)
--------------------------------------------------------
    tier, file_count, total_size_gb, size_percent, estimated_monthly_cost
One row for each of HOT, WARM, COLD, ARCHIVE (zeros if empty), plus an
"UNKNOWN" row only when some rows have an unrecognised tier.

Summary dictionary keys (`run_migration_simulation()["summary"]`)
-----------------------------------------------------------------
    simulation_only (always True), note, total_files, files_to_migrate,
    files_no_migration_required, files_needing_review, data_to_migrate_gb,
    cost_before, cost_after, total_estimated_savings, savings_percent,
    currency, billing_period, prices_per_gb_month, pricing_note
`total_estimated_savings` always equals `cost_before - cost_after`.
"""

from __future__ import annotations

from typing import Tuple

import numpy as np
import pandas as pd

from src.cost_calculator import (
    TIERS,
    PricingConfig,
    PricingInput,
    _normalise_tiers,
    _numeric_sizes,
    _price_per_gb,
    _require_columns,
    resolve_pricing,
)

ARROW = "\u2192"  # →
NO_MIGRATION_ACTION = "No migration required"
REVIEW_ACTION = "Review required: tier not recognised"

PLAN_COLUMNS = [
    "file_name",
    "current_tier",
    "recommended_tier",
    "action",
    "estimated_savings",
    "size_gb",
    "migration_required",
]
ROUTE_COLUMNS = ["route", "file_count", "size_gb", "estimated_savings"]
DISTRIBUTION_COLUMNS = ["tier", "file_count", "total_size_gb", "size_percent", "estimated_monthly_cost"]
_UNKNOWN = "UNKNOWN"


# --------------------------------------------------------------------------- #
# Migration plan
# --------------------------------------------------------------------------- #

def simulate_migration(df: pd.DataFrame, pricing: PricingInput = None) -> pd.DataFrame:
    """
    Build the migration plan. Does NOT move anything.

    Returns a new DataFrame with the columns listed in the module docstring,
    in the same row order as `df` (index reset to 0..n-1).
    """
    _require_columns(df, ["file_name", "size_gb", "current_tier", "recommended_tier"])
    config = resolve_pricing(pricing)

    src = df.reset_index(drop=True)
    current = _normalise_tiers(src["current_tier"])
    recommended = _normalise_tiers(src["recommended_tier"])
    size = _numeric_sizes(src)

    valid = current.isin(TIERS) & recommended.isin(TIERS)
    same = valid & (current == recommended)
    migrate = valid & (current != recommended)

    actions = []
    for cur, rec, is_valid, is_same in zip(current, recommended, valid, same):
        if not is_valid:
            actions.append(REVIEW_ACTION)
        elif is_same:
            actions.append(NO_MIGRATION_ACTION)
        else:
            actions.append(f"Migrate {cur} {ARROW} {rec}")

    price_cur = _price_per_gb(current, config)
    price_rec = _price_per_gb(recommended, config)
    savings = size * (price_cur - price_rec)  # NaN if size or either price is unknown
    savings = savings.where(~same, 0.0)       # staying put costs/saves nothing
    savings = savings.where(valid, np.nan)    # needs review -> no estimate

    plan = pd.DataFrame(
        {
            "file_name": src["file_name"],
            "current_tier": current.fillna(_UNKNOWN),
            "recommended_tier": recommended.fillna(_UNKNOWN),
            "action": actions,
            "estimated_savings": savings.astype("float64"),
            "size_gb": size,
            "migration_required": migrate.astype(bool),
        }
    )
    return plan[PLAN_COLUMNS]


# --------------------------------------------------------------------------- #
# Distributions (before / after)
# --------------------------------------------------------------------------- #

def get_tier_distribution(
    tiers: pd.Series, sizes: pd.Series, pricing: PricingInput = None
) -> pd.DataFrame:
    """
    Storage and estimated monthly cost per tier.

    Parameters
    ----------
    tiers  : tier name for each dataset (any mix of case/whitespace)
    sizes  : size_gb for each dataset, aligned with `tiers`

    Returns a DataFrame with `DISTRIBUTION_COLUMNS`; always has the four real
    tiers (in HOT, WARM, COLD, ARCHIVE order), plus "UNKNOWN" if needed.
    """
    config = resolve_pricing(pricing)
    tier = _normalise_tiers(pd.Series(tiers).reset_index(drop=True))
    size = pd.to_numeric(pd.Series(sizes).reset_index(drop=True), errors="coerce").astype("float64")
    tier = tier.where(tier.isin(TIERS), _UNKNOWN)

    labels = list(TIERS) + ([_UNKNOWN] if (tier == _UNKNOWN).any() else [])
    total_size = float(size.sum(skipna=True))

    rows = []
    for label in labels:
        in_tier = tier == label
        gb = float(size[in_tier].sum(skipna=True))
        price = config.prices_per_gb_month.get(label)
        rows.append(
            {
                "tier": label,
                "file_count": int(in_tier.sum()),
                "total_size_gb": round(gb, 2),
                "size_percent": round(gb / total_size * 100, 2) if total_size > 0 else 0.0,
                "estimated_monthly_cost": round(gb * price, 2) if price is not None else 0.0,
            }
        )
    return pd.DataFrame(rows, columns=DISTRIBUTION_COLUMNS)


# --------------------------------------------------------------------------- #
# Full simulation
# --------------------------------------------------------------------------- #

def run_migration_simulation(df: pd.DataFrame, pricing: PricingInput = None) -> dict:
    """
    Run the whole simulation and return everything the dashboard needs as a
    dict: plan, summary, routes, before_distribution, after_distribution
    (see module docstring). Nothing is moved; `df` is not modified.
    """
    config = resolve_pricing(pricing)
    plan = simulate_migration(df, config)

    after_tier = plan["recommended_tier"].where(plan["migration_required"], plan["current_tier"])
    before = get_tier_distribution(plan["current_tier"], plan["size_gb"], config)
    after = get_tier_distribution(after_tier, plan["size_gb"], config)

    moving = plan[plan["migration_required"]]
    routes = _build_routes(moving)

    cost_before = float(before["estimated_monthly_cost"].sum())
    cost_after = float(after["estimated_monthly_cost"].sum())
    savings = cost_before - cost_after

    summary = {
        "simulation_only": True,
        "note": "Simulation only. No files were moved and no storage system was contacted.",
        "total_files": int(len(plan)),
        "files_to_migrate": int(plan["migration_required"].sum()),
        "files_no_migration_required": int((plan["action"] == NO_MIGRATION_ACTION).sum()),
        "files_needing_review": int((plan["action"] == REVIEW_ACTION).sum()),
        "data_to_migrate_gb": round(float(moving["size_gb"].sum(skipna=True)), 2),
        "cost_before": round(cost_before, 2),
        "cost_after": round(cost_after, 2),
        "total_estimated_savings": round(savings, 2),
        "savings_percent": round(savings / cost_before * 100, 2) if cost_before > 0 else 0.0,
        "currency": config.currency,
        "billing_period": "month",
        "prices_per_gb_month": dict(config.prices_per_gb_month),
        "pricing_note": config.source_note,
    }

    return {
        "plan": plan,
        "summary": summary,
        "routes": routes,
        "before_distribution": before,
        "after_distribution": after,
    }


def _build_routes(moving: pd.DataFrame) -> pd.DataFrame:
    """One row per 'HOT -> COLD' style route, biggest savings first."""
    if moving.empty:
        return pd.DataFrame(columns=ROUTE_COLUMNS)
    grouped = (
        moving.assign(route=moving["current_tier"] + f" {ARROW} " + moving["recommended_tier"])
        .groupby("route", as_index=False)
        .agg(
            file_count=("file_name", "size"),
            size_gb=("size_gb", lambda s: round(float(s.sum(skipna=True)), 2)),
            estimated_savings=("estimated_savings", lambda s: round(float(s.sum(skipna=True)), 2)),
        )
        .sort_values("estimated_savings", ascending=False)
        .reset_index(drop=True)
    )
    return grouped[ROUTE_COLUMNS]


# --------------------------------------------------------------------------- #
# Demo:  python -m src.migration_simulator
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    demo = pd.DataFrame(
        {
            "file_name": ["orders.db", "logs_2024.csv", "backup_2019.tar", "mystery.dat"],
            "size_gb": [100.0, 250.0, 1000.0, 50.0],
            "current_tier": ["HOT", "HOT", "WARM", "UNKNOWN"],
            "recommended_tier": ["HOT", "COLD", "ARCHIVE", "WARM"],
        }
    )
    out = run_migration_simulation(demo)
    print(out["plan"].to_string(index=False), end="\n\n")
    print(out["routes"].to_string(index=False), end="\n\n")
    print(out["before_distribution"].to_string(index=False), end="\n\n")
    print(out["after_distribution"].to_string(index=False), end="\n\n")
    for key, value in out["summary"].items():
        print(f"{key}: {value}")
