"""Offline probabilistic forecaster for the Roblox Up-and-Coming chart."""

import json
import math
import os
from datetime import datetime
from pathlib import Path


def _read_input(directory):
    task = json.loads((directory / "task.json").read_text(encoding="utf-8"))
    snapshots = []
    for line in (directory / "history.ndjson").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        try:
            at = datetime.fromisoformat(item["at"].replace("Z", "+00:00")).timestamp() / 3600
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        rows = item.get("up_and_coming")
        if item.get("ok") is not False and isinstance(rows, list):
            snapshots.append((at, rows))
    snapshots.sort(key=lambda entry: entry[0])
    return task, snapshots


def _weighted_slope(points, now, half_life, value_index):
    """Recency weighted least-squares slope, in value units per hour."""
    usable = []
    for at, row in points:
        value = row.get(value_index)
        if isinstance(value, (int, float)) and math.isfinite(value):
            usable.append((at, float(value)))
    if len(usable) < 2:
        return 0.0
    weights = [math.exp(-max(0.0, now - at) * math.log(2) / half_life) for at, _ in usable]
    sw = sum(weights)
    mean_t = sum(w * t for w, (t, _) in zip(weights, usable)) / sw
    mean_v = sum(w * v for w, (_, v) in zip(weights, usable)) / sw
    denom = sum(w * (t - mean_t) ** 2 for w, (t, _) in zip(weights, usable))
    if denom < 1e-9:
        return 0.0
    return sum(w * (t - mean_t) * (v - mean_v)
               for w, (t, v) in zip(weights, usable)) / denom


def _build_forecast(task, snapshots):
    if not snapshots:
        raise ValueError("history contains no valid up-and-coming snapshots")

    now, latest = snapshots[-1]
    places = max(1, min(10, int(task.get("places", 10))))
    window = task.get("window") or {}
    slots = max(1, int(window.get("slots", 72)))
    slot_hours = max(1, int(window.get("slot_minutes", 60))) / 60.0
    horizon = slots * slot_hours

    observations = {}
    chart_sizes = []
    for at, rows in snapshots:
        chart_sizes.append((at, len(rows)))
        for row in rows:
            uid = row.get("universe_id")
            rank = row.get("rank")
            if isinstance(uid, int) and not isinstance(uid, bool) and isinstance(rank, (int, float)):
                observations.setdefault(uid, []).append((at, row))

    latest_rows = {}
    for row in latest:
        uid = row.get("universe_id")
        if isinstance(uid, int) and not isinstance(uid, bool):
            latest_rows[uid] = row

    candidates = []
    for uid, history in observations.items():
        history.sort(key=lambda pair: pair[0])
        last_at, last_row = history[-1]
        current_rank = float(last_row.get("rank", 101))
        age = max(0.0, now - last_at)
        rank_points = [(at, row) for at, row in history if now - at <= 24 * 7]
        rank_slope = _weighted_slope(rank_points, now, 48.0, "rank")
        # Rank drift is noisy and changes with chart churn; shrink and cap it.
        rank_slope = max(-0.045, min(0.045, rank_slope * 0.58))
        player_points = [(at, dict(row, playing=math.log1p(row["playing"])))
                         for at, row in history
                         if isinstance(row.get("playing"), (int, float))
                         and row["playing"] >= 0 and now - at <= 24 * 5]
        player_slope = _weighted_slope(player_points, now, 36.0, "playing")
        # Relative player growth provides a modest leading signal before chart rank reacts.
        growth_adjustment = max(-0.012, min(0.012, player_slope * 0.48))
        rank_slope = max(-0.05, min(0.05, rank_slope - growth_adjustment))
        presence = sum(1 for at, _ in history if now - at <= 72)
        # A short absence is compatible with volatility; longer absences quickly fade.
        absent_penalty = min(35.0, age * 0.42)
        rank_at_horizon = current_rank + rank_slope * horizon * 0.68 + absent_penalty
        recent_rows = [(at, row) for at, row in history if now - at <= 72]
        recent_change = 0.0
        if len(recent_rows) >= 2:
            recent_change = abs(float(recent_rows[-1][1].get("rank", current_rank)) -
                                float(recent_rows[0][1].get("rank", current_rank)))
        uncertainty = 1.8 + 0.095 * math.sqrt(horizon) + 0.16 * recent_change
        uncertainty += min(8.0, age * 0.12)
        persistence_weight = 1.0 + min(0.35, presence * 0.035)
        candidates.append({
            "uid": uid,
            "rank": current_rank,
            "future_rank": max(1.0, min(140.0, rank_at_horizon)),
            "uncertainty": uncertainty,
            "weight": persistence_weight * math.exp(-age / 30.0),
        })

    # Name a broad set of plausible candidates while leaving mass for future entrants.
    candidates.sort(key=lambda c: (c["future_rank"], -c["weight"], c["uid"]))
    modeled = candidates[:100]
    if not modeled:
        raise ValueError("history has no usable universe ids")

    probabilities = []
    reserve = 0.18
    for place in range(1, places + 1):
        # Each place is a marginal distribution; the reserve represents unobserved entrants.
        likelihoods = []
        for candidate in modeled:
            sigma = candidate["uncertainty"]
            distance = (place - candidate["future_rank"]) / sigma
            likelihood = candidate["weight"] * math.exp(-0.5 * distance * distance) / sigma
            likelihoods.append(likelihood)
        total = sum(likelihoods)
        if total <= 0 or not math.isfinite(total):
            continue
        scale = (1.0 - reserve) / total
        for candidate, likelihood in zip(modeled, likelihoods):
            probability = likelihood * scale
            if probability >= 0.00015:
                probabilities.append({
                    "universe_id": candidate["uid"],
                    "place": place,
                    "p": round(probability, 8),
                })

    # Rank expected placement at the window midpoint, including conservative persistence.
    midpoint = horizon / 2
    ranked = []
    for uid, history in observations.items():
        history.sort(key=lambda pair: pair[0])
        at, row = history[-1]
        current_rank = float(row.get("rank", 101))
        slope = _weighted_slope([(t, r) for t, r in history if now - t <= 168], now, 48.0, "rank")
        slope = max(-0.045, min(0.045, slope * 0.58))
        absent = max(0.0, now - at)
        projected = current_rank + slope * midpoint * 0.68 + min(35.0, absent * 0.42)
        ranked.append((projected + max(0.0, absent - 3) * 0.025, uid))
    ranked.sort()
    top10 = [uid for _, uid in ranked[:10]]
    if len(top10) != 10 or len(set(top10)) != 10:
        raise ValueError("history must contain at least ten distinct games for top10")
    return {"probabilities": probabilities, "top10": top10}


def main():
    input_dir = Path(os.environ.get("MAKEFUN_INPUT_DIR", "/input"))
    output_dir = Path(os.environ.get("MAKEFUN_OUTPUT_DIR", "/output"))
    task, snapshots = _read_input(input_dir)
    forecast = _build_forecast(task, snapshots)
    output_name = task.get("output", "forecast.json")
    if not isinstance(output_name, str) or Path(output_name).name != output_name:
        output_name = "forecast.json"
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(forecast, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    if len(payload) > 1_048_576:
        raise ValueError("forecast exceeds the 1 MiB output limit")
    (output_dir / output_name).write_bytes(payload)


if __name__ == "__main__":
    main()
