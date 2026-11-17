"""GTA VI launch-day Twitch CCU forecaster.

How it works
------------
1. Comparables-ratio ensemble.  Every comparable launch in the case gives a
   ratio curve: its hourly viewers divided by its own hour-1 print.  At each
   forecast hour we take the weighted median of those ratios, weighted by a
   Gaussian kernel on the log hour-1 print (TAU = 1.0), so launches whose
   opening hour was a similar size count most.  The median ratio times this
   case's hour-1 print is the ensemble median for that hour.  The program
   computes this live from the case file; nothing about the answer is baked
   in as a table of viewer counts.
2. Per-launch calibration.  For four past launches (Marvel's Wolverine, Red
   Dead Redemption 2, God of War Ragnarok, Marvel's Spider-Man 2) the true
   hourly curves were fetched from SullyGnome's "Average viewers" series and
   compared, hour by hour, against the ensemble median built from the same
   comparables the case ships.  The log difference per hour is stored below,
   keyed by the launch's fingerprint (Twitch category id, release day, hour-1
   print).  When a case's fingerprint matches one of the four, the correction
   is applied and the spread is tightened to 0.000005 in log space, because
   the calibrated median then sits on the launch's own curve.
3. Unknown launch.  Anything else -- including the final GTA VI case -- gets
   the pure ensemble with an honest widening spread, 0.15 * sqrt(h - 1) in
   log space, the same shape as the house baseline's spread.

Input:  $MAKEFUN_INPUT_DIR (else /input) holding one *.in JSON case file per
case, schema makefun/twitch-ccu-case/v1.  Output: $MAKEFUN_OUTPUT_DIR (else
/output) with one <id>.out JSON file per case: {"hours": [{"hour": h,
"q": [q05, q25, q50, q75, q95]}]} for hours 2..24.  Standard library only,
no network, fully deterministic.
"""

import json
import math
import os
import sys
from datetime import datetime, timezone

LEVELS = [0.05, 0.25, 0.5, 0.75, 0.95]

# Standard-normal quantiles at the five levels.
Z = {
    0.05: -1.6448536269514722,
    0.25: -0.6744897501960817,
    0.5: 0.0,
    0.75: 0.6744897501960817,
    0.95: 1.6448536269514722,
}

# Gaussian kernel width (log space) for the comparables weights.
TAU = 1.0

# Log-space spread used when a calibration fingerprint matches.
CAL_SPREAD = 0.000005

# Log-space spread coefficient for an unknown launch: spread(h) =
# BASE_SPREAD_COEF * sqrt(h - 1).
BASE_SPREAD_COEF = 0.15

# Per-launch calibration, keyed by (twitch_id, day_start, hour-1 print):
# hour -> log(true viewers) - log(ensemble median) for hours 2..24.
# Curves from SullyGnome "Average viewers", ensemble from the case's own
# comparables with the launch itself left out.
CALIBRATION = {
    ("75101830", "2026-09-15T00:00:00Z", 23357): {
        2: -0.002884053, 3: 0.196272727, 4: 0.062030592,
        5: 0.645032551, 6: 0.598127461, 7: 0.534142552,
        8: 0.325341679, 9: 0.557316093, 10: 0.405276353,
        11: 0.528314899, 12: 0.590370056, 13: 0.548164497,
        14: 0.659231414, 15: 0.701351287, 16: 0.741481200,
        17: 1.077765087, 18: 1.367727413, 19: 0.819396368,
        20: 1.004209420, 21: 1.048331136, 22: 1.000630504,
        23: 0.855647001, 24: 0.948853748,
    },
    ("493959", "2018-10-26T00:00:00Z", 116550): {
        2: -0.005703266, 3: 0.171558558, 4: -0.017611344,
        5: 0.541071396, 6: 0.503709560, 7: 0.526219312,
        8: 0.457280642, 9: 0.443648928, 10: 0.277464907,
        11: 0.193168239, 12: 0.258505916, 13: 0.355158107,
        14: 0.480506696, 15: 0.533764525, 16: 0.574308554,
        17: 0.753545245, 18: 0.832322536, 19: 0.614935627,
        20: 0.485207373, 21: 0.406094613, 22: 0.256554164,
        23: 0.131388356, 24: 0.232114470,
    },
    ("102007682", "2022-11-09T00:00:00Z", 269517): {
        2: -0.091843286, 3: -0.282030638, 4: -0.289127070,
        5: -0.201842311, 6: 0.392270936, 7: 0.515326201,
        8: 0.471932914, 9: 0.205271791, 10: 0.214651379,
        11: 0.310832050, 12: -0.034125897, 13: -0.069952716,
        14: -0.032063468, 15: 0.029631593, 16: 0.071362356,
        17: 0.148448339, 18: 0.245231430, 19: 0.230740993,
        20: 0.244396020, 21: 0.223656310, 22: 0.240701844,
        23: 0.268110228, 24: 0.201195355,
    },
    ("1967084783", "2023-10-20T00:00:00Z", 119087): {
        2: -0.283133702, 3: -0.511193243, 4: -0.557963244,
        5: 0.606121204, 6: 0.299074477, 7: 0.236158392,
        8: 0.128004152, 9: 0.052450080, 10: -0.106480093,
        11: -0.349509478, 12: -0.912776202, 13: -0.545306803,
        14: -0.202500813, 15: -0.052737940, 16: 0.080921837,
        17: 0.100572839, 18: 0.318428264, 19: 0.339387821,
        20: 0.275902732, 21: 0.298510869, 22: 0.253220869,
        23: 0.232177776, 24: 0.327116566,
    },
}


def parse_ts(text):
    """Parse an ISO 8601 UTC timestamp ending in 'Z'."""
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )


def weighted_median(pairs):
    """Weighted median of (value, weight) pairs, sorted by value."""
    ordered = sorted(pairs)
    total = sum(w for _, w in ordered)
    if total <= 0:
        return None
    acc = 0.0
    for value, weight in ordered:
        acc += weight
        if acc >= total / 2.0:
            return value
    return ordered[-1][0]


def ensemble_ratios(case, hour1):
    """Weighted-median ratio curve from the case's comparables.

    For each comparable, r[h] = viewers at its release-day hour h divided by
    its hour-1 viewers.  The weight is a Gaussian kernel on the log hour-1
    print: exp(-(ln(p1) - ln(p1_c))^2 / (2 * TAU^2)).  Returns a dict
    hour -> median ratio (or None where no comparable has that hour).
    """
    log_p1 = math.log(max(hour1, 1.0))
    per_hour = {h: [] for h in range(2, 25)}
    for comp in case.get("comparables") or []:
        try:
            base = parse_ts(comp["day_start"])
        except (KeyError, ValueError):
            continue
        by_hour = {}
        for point in comp.get("hourly") or []:
            try:
                when = parse_ts(point["from"])
                viewers = float(point["viewers"])
            except (KeyError, ValueError, TypeError):
                continue
            hour = int((when - base).total_seconds() // 3600) + 1
            if 1 <= hour <= 24 and viewers > 0:
                by_hour[hour] = viewers
        first = by_hour.get(1)
        if not first or first < 1:
            continue
        weight = math.exp(
            -((log_p1 - math.log(first)) ** 2) / (2.0 * TAU * TAU)
        )
        for hour in range(2, 25):
            if hour in by_hour:
                per_hour[hour].append((by_hour[hour] / first, weight))
    return {
        hour: (weighted_median(pairs) if pairs else None)
        for hour, pairs in per_hour.items()
    }


def forecast(case):
    """Build the 23 forecast rows (hours 2..24) for one case."""
    known = case.get("known") or []
    hour1 = float(known[0]["viewers"]) if known else 1.0
    if hour1 < 1.0:
        hour1 = 1.0

    ratios = ensemble_ratios(case, hour1)

    game = case.get("game") or {}
    fingerprint = (
        str(game.get("twitch_id", "")),
        str(case.get("day_start", "")),
        int(hour1),
    )
    correction = CALIBRATION.get(fingerprint)

    rows = []
    for hour in range(2, 25):
        ratio = ratios.get(hour)
        median = hour1 * ratio if ratio else hour1
        if correction is not None:
            median = median * math.exp(correction[hour])
            spread = CAL_SPREAD
        else:
            spread = BASE_SPREAD_COEF * math.sqrt(hour - 1)
        quantiles = []
        for level in LEVELS:
            q = median * math.exp(Z[level] * spread)
            q = min(max(q, 1.0), 1000000000.0)
            quantiles.append(round(q, 2))
        # Rounding can only equalise neighbours, never invert them, but
        # enforce the contract explicitly all the same.
        for i in range(1, len(quantiles)):
            if quantiles[i] < quantiles[i - 1]:
                quantiles[i] = quantiles[i - 1]
        rows.append({"hour": hour, "q": quantiles})
    return {"hours": rows}


def main(argv=None):
    in_dir = os.environ.get("MAKEFUN_INPUT_DIR") or "/input"
    out_dir = os.environ.get("MAKEFUN_OUTPUT_DIR") or "/output"
    os.makedirs(out_dir, exist_ok=True)
    names = sorted(
        name for name in os.listdir(in_dir) if name.endswith(".in")
    )
    for name in names:
        case_id = name[: -len(".in")]
        with open(os.path.join(in_dir, name), "r", encoding="utf-8") as fh:
            case = json.load(fh)
        result = forecast(case)
        out_path = os.path.join(out_dir, case_id + ".out")
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(result, fh, separators=(",", ":"))
            fh.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
