#!/usr/bin/env python3
"""Render English quality figures from the retained M1 and M2 summary receipts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import StrMethodFormatter


POSITION = (
    ("valid", "Valid position", "#227E84"),
    ("low_confidence", "Low confidence", "#C38532"),
    ("no_position", "No position", "#84919F"),
)
LABELS = (
    ("eligible", "Eligible", "#227E84"),
    ("not_mature", "Not mature", "#C38532"),
    ("already_arrived", "Already arrived", "#607D98"),
    ("cancelled", "Cancelled", "#84919F"),
)


def read_summary(path: Path) -> dict:
    """Fail rather than silently draw an incomplete or non-synthetic summary."""
    summary = json.loads(path.read_text(encoding="utf-8"))
    if summary.get("source_kind") != "synthetic":
        raise ValueError(f"{path}: this figure template requires synthetic data")
    if summary.get("model_trained") is not False:
        raise ValueError(f"{path}: expected an untrained data milestone")
    if summary.get("metrics_are_model_performance") is not False:
        raise ValueError(f"{path}: expected data-stage counts, not model metrics")
    for key in ("trip_count", "snapshot_count"):
        if type(summary.get(key)) is not int or summary[key] <= 0:
            raise ValueError(f"{path}: {key} must be a positive integer")
    for key, categories in (("position_quality", POSITION), ("label_eligibility", LABELS)):
        counts = summary.get(key)
        expected = {category[0] for category in categories}
        if not isinstance(counts, dict) or set(counts) != expected:
            raise ValueError(f"{path}: unexpected {key} categories")
        if any(type(value) is not int or value < 0 for value in counts.values()):
            raise ValueError(f"{path}: {key} counts must be nonnegative integers")
        if sum(counts.values()) != summary["snapshot_count"]:
            raise ValueError(f"{path}: {key} counts do not match snapshot_count")
    return summary


def panel(axis, counts: dict, categories: tuple, total: int, title: str) -> None:
    values = [counts[key] for key, _, _ in categories]
    axis.barh(
        range(len(categories)), values,
        color=[color for _, _, color in categories], height=0.56,
    )
    axis.set_yticks(range(len(categories)), [label for _, label, _ in categories])
    axis.invert_yaxis()
    axis.set_xlim(0, max(max(values), 1) * 1.48)
    axis.set_title(title, loc="left", fontsize=13, fontweight="bold", pad=16)
    axis.set_xlabel("Snapshots", fontsize=10, labelpad=9)
    axis.xaxis.set_major_formatter(StrMethodFormatter("{x:,.0f}"))
    axis.grid(axis="x", color="#E7EDF1", linewidth=0.8)
    axis.set_axisbelow(True)
    axis.tick_params(axis="both", labelsize=10, length=0)
    axis.tick_params(axis="y", pad=10)
    for spine in axis.spines.values():
        spine.set_visible(False)
    offset = max(max(values), 1) * 0.025
    for index, value in enumerate(values):
        axis.text(
            value + offset, index, f"{value:,}  ({value / total:.1%})",
            va="center", fontsize=10, color="#25394C",
        )


def render(summary: dict, milestone: str, output: Path) -> None:
    with plt.rc_context({"font.family": "DejaVu Sans", "text.color": "#203448"}):
        figure, axes = plt.subplots(1, 2, figsize=(13, 5.5), dpi=160)
        figure.patch.set_facecolor("white")
        figure.subplots_adjust(left=0.125, right=0.975, top=0.72, bottom=0.27, wspace=0.60)
        figure.text(
            0.06, 0.91, f"{milestone} | Snapshot quality and label status",
            fontsize=20, fontweight="bold",
        )
        figure.text(
            0.06, 0.845,
            f"{summary['trip_count']:,} trips  |  {summary['snapshot_count']:,} snapshots"
            "  |  Original synthetic 2026 scenarios",
            fontsize=11, color="#52697C",
        )
        panel(axes[0], summary["position_quality"], POSITION, summary["snapshot_count"], "Position quality")
        panel(axes[1], summary["label_eligibility"], LABELS, summary["snapshot_count"], "Arrival-label status")
        figure.text(
            0.06, 0.165,
            "Designed scenario counts; not fleet prevalence or model performance.",
            fontsize=10, fontweight="bold", color="#52697C",
        )
        figure.text(
            0.06, 0.075,
            "Eligibility alone does not select a model dataset: excluded splits must still be removed.\n"
            "Multiple snapshots belong to the same trip and are not independent journeys.",
            fontsize=9, color="#52697C", linespacing=1.6,
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        figure.savefig(output, facecolor="white", metadata={"Software": "ds-m01 data-quality plot"})
        plt.close(figure)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    jobs = (
        ("M1", "reports/m1_reference/m1_summary.json", "reports/m1_reference/figures/m1_snapshot_quality.png"),
        ("M2", "reports/m2_summary.json", "reports/figures/m2_snapshot_quality.png"),
    )
    # Read and validate both inputs before updating either output.
    validated = [(milestone, read_summary(args.root / source), args.root / output)
                 for milestone, source, output in jobs]
    for milestone, summary, output in validated:
        render(summary, milestone, output)
        print(f"{milestone}: {output}")


if __name__ == "__main__":
    main()
