# Roblox Up-and-Coming forecast entry

This is a deterministic, offline Python 3.13 forecaster for Roblox's Up-and-Coming chart. It reads `task.json` and the complete chronological `history.ndjson` from `MAKEFUN_INPUT_DIR` (default `/input`) and writes the requested forecast file into `MAKEFUN_OUTPUT_DIR` (default `/output`).

The model tracks each observed game's rank history with a recency-weighted trend, shrinks and caps noisy rank drift, and adds a restrained signal from log player-count growth. It projects those signals partway toward the requested forecast horizon, penalizes stale or absent games, and converts projected ranks into broad per-place distributions. It leaves 18% of each place's mass unnamed to represent new games and other candidates outside the observed history. `top10` follows the projected ordering near the forecast window midpoint. It uses only the Python standard library and makes no network, model, random, or clock calls.

Run locally with:

```sh
mkdir -p out
MAKEFUN_INPUT_DIR=/path/to/input MAKEFUN_OUTPUT_DIR=out python3.13 main.py
```

The program writes `out/forecast.json` by default when the task requests `forecast.json`. This method is a baseline forecast; no official score or acceptance is claimed.
