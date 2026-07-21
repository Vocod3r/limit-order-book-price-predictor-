"""
Characterizes the residual (raw imbalance - filtered imbalance) - i.e.
what the Kalman/IMM filter treats as noise and discards - rather than
just scoring its AUC. Answers three questions:

1. Is the residual actually noise, or does it have structure?
   A textbook-correct Kalman filter's discarded part (the innovation)
   should be white noise - no autocorrelation - IF the filter's model
   assumptions match the data. If the residual autocorrelates with
   itself at short lags, that's evidence the filter is mis-specified
   and leaving real structure on the table, not just discarding noise.

2. Does the residual concentrate around regime changes?
   Checks whether |residual| is larger, on average, when the IMM's
   regime probabilities suggest a depletion event is starting
   (p_bid_depleting / p_ask_depleting rising) vs. when it's confident
   the market is STABLE. If yes, the filter is discarding information
   specifically around the events LOB_ssm.py is designed to detect.

3. Does the residual's distribution actually differ between up-moves
   and down-moves?
   Direct comparison of residual's mean/std conditioned on the real
   label, independent of whatever a logistic regression's linear
   boundary can or can't pick up.

Usage:
    py fi2010_diagnostics.py <file> [--horizon 0-4]
"""

import sys
import numpy as np

from fi2010_evaluate import load_diagnostics, HORIZON_NAMES


def autocorrelation(x: np.ndarray, max_lag: int = 5):
    """Simple sample autocorrelation at lags 1..max_lag. Near 0 at every
    lag = consistent with white noise (no exploitable structure left).
    Clearly nonzero at some lag = the residual has structure the filter
    isn't accounting for."""
    x = x - x.mean()
    var = np.dot(x, x) / len(x)
    if var < 1e-12:
        return [0.0] * max_lag
    acf = []
    for lag in range(1, max_lag + 1):
        cov = np.dot(x[:-lag], x[lag:]) / len(x)
        acf.append(cov / var)
    return acf


def diagnose(path: str, horizon: int):
    print(f"Loading + filtering {path} ...")
    d = load_diagnostics(path)

    raw, filtered, residual = d["raw"], d["filtered"], d["residual"]
    p_bid_dep, p_ask_dep, p_stable = d["p_bid_depleting"], d["p_ask_depleting"], d["p_stable"]
    labels_full = d["label_matrix"][horizon]

    print(f"\n=== Residual diagnostics: {path}, horizon {HORIZON_NAMES[horizon]} ===\n")

    # --- 1. Basic shape ---
    print(f"residual: mean={residual.mean():+.4f}  std={residual.std():.4f}  "
          f"|residual| mean={np.abs(residual).mean():.4f}")
    print(f"raw:      mean={raw.mean():+.4f}  std={raw.std():.4f}")
    print(f"filtered: mean={filtered.mean():+.4f}  std={filtered.std():.4f}\n")

    # --- 2. Structure (autocorrelation) ---
    acf = autocorrelation(residual, max_lag=5)
    print("Residual autocorrelation (lags 1-5):")
    print("  " + "  ".join(f"lag{i+1}={v:+.3f}" for i, v in enumerate(acf)))
    if max(abs(v) for v in acf) > 0.05:
        print("  -> Nonzero autocorrelation detected: residual is NOT pure white "
              "noise, meaning the filter is leaving structured information behind, "
              "not just discarding noise.\n")
    else:
        print("  -> Residual looks close to white noise (all |acf| < 0.05): "
              "consistent with the filter already extracting what structure exists, "
              "leaving little else to recover.\n")

    # --- 3. Residual magnitude vs. regime probability ---
    # Bucket ticks by how confident the filter is that a depletion is happening.
    depleting_conf = np.maximum(p_bid_dep, p_ask_dep)
    high_conf_mask = depleting_conf > 0.5
    low_conf_mask = ~high_conf_mask

    if high_conf_mask.sum() > 0 and low_conf_mask.sum() > 0:
        high_resid = np.abs(residual[high_conf_mask]).mean()
        low_resid = np.abs(residual[low_conf_mask]).mean()
        print(f"Mean |residual| when filter suspects depletion (p>0.5): {high_resid:.4f} "
              f"(n={high_conf_mask.sum()})")
        print(f"Mean |residual| when filter is confident STABLE:        {low_resid:.4f} "
              f"(n={low_conf_mask.sum()})")
        if high_resid > low_resid * 1.1:
            print("  -> Residual is LARGER around suspected depletion events: the filter "
                  "discards more information exactly when something is happening, not "
                  "uniformly at random.\n")
        else:
            print("  -> No clear difference: residual doesn't obviously concentrate "
                  "around depletion events.\n")
    else:
        print("Not enough ticks in one of the regime-confidence buckets to compare.\n")

    # --- 4. Residual distribution conditioned on actual outcome ---
    mask = labels_full != 2
    y = (labels_full[mask] == 1).astype(int)
    resid_masked = residual[mask]
    raw_masked = raw[mask]

    resid_up = resid_masked[y == 1]
    resid_down = resid_masked[y == 0]
    raw_up = raw_masked[y == 1]
    raw_down = raw_masked[y == 0]

    print(f"residual | price went UP:   mean={resid_up.mean():+.4f}  std={resid_up.std():.4f}  (n={len(resid_up)})")
    print(f"residual | price went DOWN: mean={resid_down.mean():+.4f}  std={resid_down.std():.4f}  (n={len(resid_down)})")
    mean_gap = resid_up.mean() - resid_down.mean()
    pooled_std = np.sqrt((resid_up.var() + resid_down.var()) / 2)
    effect_size = mean_gap / pooled_std if pooled_std > 1e-9 else 0.0
    print(f"  -> mean gap = {mean_gap:+.4f}, standardized effect size (Cohen's d) = {effect_size:+.4f}")

    print(f"\nraw      | price went UP:   mean={raw_up.mean():+.4f}  std={raw_up.std():.4f}")
    print(f"raw      | price went DOWN: mean={raw_down.mean():+.4f}  std={raw_down.std():.4f}")
    raw_gap = raw_up.mean() - raw_down.mean()
    raw_pooled_std = np.sqrt((raw_up.var() + raw_down.var()) / 2)
    raw_effect = raw_gap / raw_pooled_std if raw_pooled_std > 1e-9 else 0.0
    print(f"  -> mean gap = {raw_gap:+.4f}, standardized effect size (Cohen's d) = {raw_effect:+.4f}\n")

    if abs(effect_size) > abs(raw_effect) * 1.1:
        print("Residual separates up/down moves MORE cleanly than raw imbalance does "
              "- some real information may be concentrated in what the filter discards.")
    elif abs(raw_effect) > abs(effect_size) * 1.1:
        print("Raw imbalance separates up/down moves MORE cleanly than the residual - "
              "the discarded part isn't where the signal is; if anything it's still "
              "in what the filter kept.")
    else:
        print("Residual and raw show similarly weak (or similarly absent) separation "
              "between up/down moves - neither view shows a strong effect on this file.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: py fi2010_diagnostics.py <file> [--horizon 0-4]")
        sys.exit(1)

    path = sys.argv[1]
    horizon = 0
    if "--horizon" in sys.argv:
        horizon = int(sys.argv[sys.argv.index("--horizon") + 1])

    diagnose(path, horizon)