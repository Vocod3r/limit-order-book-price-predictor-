"""
Limit Order Book State-Space Model with IMM Regime-Switching Kalman Filter
============================================================================

Implements Stage 2 of the auction pipeline:
    - Queue evolution as a linear-Gaussian state-space model (Kalman filter)
    - Regime-switching (IMM) to capture discrete market events
      (ask depletion, bid depletion -> Events 1 & 3 in the pipeline doc)
    - Feature extraction for Stage 3 (logistic regression price prediction)

Math summary
------------
State vector (per tick t):
    x_t = [ b(t), a(t), q_b(t), q_a(t), mu_b(t), mu_a(t) ]^T
      b, a       : best bid / best ask price
      q_b, q_a   : bid / ask queue depth at best price
      mu_b, mu_a : latent drift rate of each queue (unobserved)

State (transition) equation:
    x_t = F_s x_{t-1} + bias_s + w_t,   w_t ~ N(0, Q_s)
    (F_s, Q_s, bias_s depend on the latent regime s_t)

Observation equation:
    z_t = H x_t + v_t,         v_t ~ N(0, R)
    z_t = [ b(t), a(t), q_b(t), q_a(t) ]   (drift is never observed directly)

Regimes (s_t), governed by Markov transition matrix PI:
    STABLE          : queues evolve with small, symmetric drift noise
    BID_DEPLETING   : q_b forced toward a fast negative drift  (Event 1/3)
    ASK_DEPLETING   : q_a forced toward a fast negative drift  (Event 1/3)

IMM (Interacting Multiple Model) combines the three regime-conditional
Kalman filters into one estimate plus a probability distribution over
regimes, which becomes the "confidence" feature for event detection
(Step 2.4) instead of hard-coded thresholds.
"""

import re
import xmlrpc.client
from datetime import datetime
from dataclasses import dataclass
from typing import Dict, List, Tuple, Optional

import numpy as np

# ---------------------------------------------------------------------------
# Regime definitions
# ---------------------------------------------------------------------------

REGIMES = ["STABLE", "BID_DEPLETING", "ASK_DEPLETING"]
N_REGIMES = len(REGIMES)

# State vector indices
IDX_B, IDX_A, IDX_QB, IDX_QA, IDX_MUB, IDX_MUA = range(6)
N_STATES = 6


def build_transition_matrix(dt: float, regime: str) -> np.ndarray:
    """
    F_s : x_t = F_s x_{t-1}
    Local-trend model: queue depth += drift * dt ; drift is a random walk.
    Regimes mainly change the *noise* (Q) and *bias*; F stays structurally
    the same (drift terms integrate into their queue each step).
    """
    F = np.eye(N_STATES)
    F[IDX_QB, IDX_MUB] = dt   # q_b(t) = q_b(t-1) + mu_b(t-1) * dt
    F[IDX_QA, IDX_MUA] = dt   # q_a(t) = q_a(t-1) + mu_a(t-1) * dt
    return F


def build_process_noise(regime: str,
                         base_q: float = 0.05,
                         depletion_q: float = 2.0,
                         depletion_bias: float = -8.0
                         ) -> Tuple[np.ndarray, np.ndarray]:
    """
    Q_s : process noise covariance for regime s.
    bias : deterministic drift-shift used to push the model toward
    depletion in that regime (a simple way to fold a jump component into
    a linear-Gaussian filter without a full jump-diffusion model).
    """
    Q = np.eye(N_STATES) * base_q
    bias = np.zeros(N_STATES)

    if regime == "BID_DEPLETING":
        Q[IDX_MUB, IDX_MUB] = depletion_q
        bias[IDX_MUB] = depletion_bias
    elif regime == "ASK_DEPLETING":
        Q[IDX_MUA, IDX_MUA] = depletion_q
        bias[IDX_MUA] = depletion_bias

    return Q, bias


# Observation matrix H: we observe [b, a, q_b, q_a], never the drift terms
H = np.zeros((4, N_STATES))
H[0, IDX_B] = 1.0
H[1, IDX_A] = 1.0
H[2, IDX_QB] = 1.0
H[3, IDX_QA] = 1.0

# Observation noise R (tune per instrument / tick-size)
R = np.diag([0.0025, 0.0025, 1.0, 1.0])

# Regime transition matrix PI[i, j] = P(s_t = j | s_{t-1} = i)
# Sticky regimes: staying put is the most likely outcome each tick.
PI = np.array([
    [0.94, 0.03, 0.03],   # from STABLE
    [0.15, 0.80, 0.05],   # from BID_DEPLETING
    [0.15, 0.05, 0.80],   # from ASK_DEPLETING
])


# ---------------------------------------------------------------------------
# Single-regime Kalman filter step
# ---------------------------------------------------------------------------

@dataclass
class KalmanState:
    x: np.ndarray   # state mean, shape (N_STATES,)
    P: np.ndarray   # state covariance, shape (N_STATES, N_STATES)


def kf_predict(state: KalmanState, F: np.ndarray, Q: np.ndarray,
               bias: np.ndarray) -> KalmanState:
    x_pred = F @ state.x + bias
    P_pred = F @ state.P @ F.T + Q
    return KalmanState(x_pred, P_pred)


def kf_update(state: KalmanState, z: np.ndarray) -> Tuple[KalmanState, float]:
    """Returns (updated_state, log_likelihood_of_z_under_this_model)."""
    y = z - H @ state.x                       # innovation
    S = H @ state.P @ H.T + R                 # innovation covariance
    K = state.P @ H.T @ np.linalg.inv(S)      # Kalman gain

    x_new = state.x + K @ y
    P_new = (np.eye(N_STATES) - K @ H) @ state.P

    # Gaussian log-likelihood of the innovation (used for IMM model weighting)
    sign, logdet = np.linalg.slogdet(S)
    ll = -0.5 * (y.T @ np.linalg.inv(S) @ y + logdet + len(z) * np.log(2 * np.pi))

    return KalmanState(x_new, P_new), float(ll)


# ---------------------------------------------------------------------------
# IMM filter across regimes
# ---------------------------------------------------------------------------

class IMMFilter:
    """
    Interacting Multiple Model filter.

    Maintains N_REGIMES parallel Kalman filters (one per regime), mixes their
    estimates each step according to the Markov transition matrix PI and the
    observed-data likelihood under each regime, producing:
        - a combined state estimate  (self.combined_state)
        - a posterior regime probability vector (self.mu)  <-- feeds Step 2.4
    """

    def __init__(self, dt: float, x0: np.ndarray, P0: np.ndarray):
        self.dt = dt
        self.mu = np.ones(N_REGIMES) / N_REGIMES
        self.states: List[KalmanState] = [
            KalmanState(x0.copy(), P0.copy()) for _ in range(N_REGIMES)
        ]
        self.combined_state = KalmanState(x0.copy(), P0.copy())

    def step(self, z: np.ndarray):
        # 1. Mixing: compute mixed initial conditions for each target regime j
        c_j = PI.T @ self.mu
        c_j = np.maximum(c_j, 1e-12)  # avoid divide-by-zero
        mixed_states = []
        for j in range(N_REGIMES):
            x_mix = sum(PI[i, j] * self.mu[i] * self.states[i].x
                        for i in range(N_REGIMES)) / c_j[j]
            P_mix = np.zeros((N_STATES, N_STATES))
            for i in range(N_REGIMES):
                dx = (self.states[i].x - x_mix).reshape(-1, 1)
                P_mix += PI[i, j] * self.mu[i] * (self.states[i].P + dx @ dx.T)
            P_mix /= c_j[j]
            mixed_states.append(KalmanState(x_mix, P_mix))

        # 2. Mode-matched filtering: predict + update within each regime
        new_states = []
        log_likelihoods = np.zeros(N_REGIMES)
        for j, regime in enumerate(REGIMES):
            F = build_transition_matrix(self.dt, regime)
            Q, bias = build_process_noise(regime)
            pred = kf_predict(mixed_states[j], F, Q, bias)
            upd, ll = kf_update(pred, z)
            new_states.append(upd)
            log_likelihoods[j] = ll

        # 3. Regime probability update (Bayes rule using the likelihoods)
        likelihoods = np.exp(log_likelihoods - log_likelihoods.max())  # stability
        mu_unnorm = c_j * likelihoods
        self.mu = mu_unnorm / mu_unnorm.sum()
        self.states = new_states

        # 4. Combined estimate (for reporting / feature extraction)
        x_comb = sum(self.mu[j] * self.states[j].x for j in range(N_REGIMES))
        P_comb = np.zeros((N_STATES, N_STATES))
        for j in range(N_REGIMES):
            dx = (self.states[j].x - x_comb).reshape(-1, 1)
            P_comb += self.mu[j] * (self.states[j].P + dx @ dx.T)

        self.combined_state = KalmanState(x_comb, P_comb)
        return self.combined_state, self.mu


# ---------------------------------------------------------------------------
# Feature extraction (Stage 2, Step 2.5) -> dict consumed by Stage 3 (logit)
# ---------------------------------------------------------------------------

def extract_features(imm: IMMFilter, prev_mid: float = None) -> Dict[str, float]:
    x = imm.combined_state.x
    b, a, q_b, q_a, mu_b, mu_a = x
    mid = (a + b) / 2.0
    spread = a - b
    imbalance = (q_b - q_a) / (q_b + q_a + 1e-9)

    features = {
        "best_bid": b,
        "best_ask": a,
        "mid_price": mid,
        "spread": spread,
        "queue_imbalance": imbalance,
        "bid_queue_size": q_b,
        "ask_queue_size": q_a,
        "drift_signal": mu_b - mu_a,
        "p_stable": imm.mu[REGIMES.index("STABLE")],
        "p_bid_depleting": imm.mu[REGIMES.index("BID_DEPLETING")],
        "p_ask_depleting": imm.mu[REGIMES.index("ASK_DEPLETING")],
    }

    if prev_mid is not None:
        # y label per Step 2.3 : 1 if mid-price increased, 0 if decreased
        if mid > prev_mid:
            features["mid_price_label"] = 1
        elif mid < prev_mid:
            features["mid_price_label"] = 0
        else:
            features["mid_price_label"] = None

    return features


# ---------------------------------------------------------------------------
# Odoo data source: pick up the bid stream that bid_ingestion.py already
# wrote into Odoo CRM, and reconstruct a tick stream from it.
#
# This is the SSM's own independent read of the data - it does not go
# through bid_ingestion.py at all, and it never writes back to Odoo. It
# only reads crm.lead records of type "lead" (raw registered bids) and
# turns them into the [best_bid, best_ask, bid_qty, ask_qty] ticks that
# IMMFilter.step() expects.
# ---------------------------------------------------------------------------

@dataclass
class OdooConfig:
    url: str
    db: str
    username: str
    api_key: str


@dataclass
class FetchedBid:
    buyer: str
    price: float
    quantity: float
    timestamp: datetime


# Matches the description string written by BidIngestionClient.register_bid:
#   "bid_ref=... price=45.02 qty=150 ts=2026-07-08T05:37:46.903003"
_BID_DESC_RE = re.compile(
    r"price=(?P<price>[-\d.]+)\s+qty=(?P<qty>[-\d.]+)\s+ts=(?P<ts>\S+)"
)


def fetch_bids_from_odoo(config: OdooConfig, stock_id: str,
                          since: Optional[datetime] = None,
                          limit: int = 10000) -> List[FetchedBid]:
    """
    Reads every crm.lead of type "lead" whose name/description references
    stock_id, parses out price/qty/timestamp, and returns them time-ordered.
    Raises on auth failure rather than silently returning nothing - a
    connection problem should be loud, not look like "no bids yet".
    """
    common = xmlrpc.client.ServerProxy(f"{config.url}/xmlrpc/2/common")
    uid = common.authenticate(config.db, config.username, config.api_key, {})
    if not uid:
        raise RuntimeError("Odoo authentication failed - check credentials.")
    models = xmlrpc.client.ServerProxy(f"{config.url}/xmlrpc/2/object")

    domain = [("type", "=", "lead"), ("name", "like", stock_id)]
    if since is not None:
        domain.append(("create_date", ">=", since.strftime("%Y-%m-%d %H:%M:%S")))

    records = models.execute_kw(
        config.db, uid, config.api_key, "crm.lead", "search_read",
        [domain], {"fields": ["partner_id", "description"], "limit": limit},
    )

    bids = []
    for rec in records:
        desc = rec.get("description") or ""
        m = _BID_DESC_RE.search(desc)
        if not m:
            continue  # not a bid-shaped lead (or format changed) - skip, don't crash
        buyer = rec["partner_id"][1] if rec.get("partner_id") else "unknown"
        bids.append(FetchedBid(
            buyer=buyer,
            price=float(m.group("price")),
            quantity=float(m.group("qty")),
            timestamp=datetime.fromisoformat(m.group("ts")),
        ))

    bids.sort(key=lambda b: b.timestamp)
    return bids


def bids_to_ticks(bids: List[FetchedBid],
                   initial_ask_price: float,
                   initial_ask_qty: float) -> List[Dict[str, float]]:
    """
    Reconstructs a [best_bid, best_ask, bid_qty, ask_qty] tick stream from
    a resting-bid sequence. The ask side isn't in Odoo (bid_ingestion.py
    only registers incoming BUY bids, per Stage 1) so it's carried forward
    from initial_ask_price/qty and only moves if you pass in real ask data
    separately - this reconstruction is for the bid side of the book only.
    """
    ticks = []
    best_bid_price = None
    best_bid_qty = 0.0

    for b in bids:
        if best_bid_price is None or b.price > best_bid_price:
            best_bid_price = b.price
            best_bid_qty = b.quantity
        elif b.price == best_bid_price:
            best_bid_qty += b.quantity
        # bids below best_bid_price rest deeper in the book - not modeled
        # here since IMMFilter's state vector only tracks best-level depth

        ticks.append({
            "best_bid": best_bid_price,
            "best_ask": initial_ask_price,
            "bid_qty": best_bid_qty,
            "ask_qty": initial_ask_qty,
        })

    return ticks


def run_on_odoo_bids(config: OdooConfig, stock_id: str,
                      initial_ask_price: float, initial_ask_qty: float,
                      since: Optional[datetime] = None) -> List[Dict[str, float]]:
    """
    End-to-end: fetch the bid stream bid_ingestion.py already wrote to
    Odoo CRM for stock_id, reconstruct ticks, run the IMM filter, return
    per-tick feature dicts (same shape as run_on_ticks()'s output).
    """
    bids = fetch_bids_from_odoo(config, stock_id, since=since)
    if not bids:
        raise ValueError(
            f"No registered bids found in Odoo for stock_id={stock_id!r}. "
            "Has bid_ingestion.py been run for this stock yet?"
        )
    ticks = bids_to_ticks(bids, initial_ask_price, initial_ask_qty)
    return run_on_ticks(ticks)


# ---------------------------------------------------------------------------
# Example driver: run the filter over a stream of LOB ticks
# ---------------------------------------------------------------------------

def run_on_ticks(ticks: List[Dict[str, float]], dt: float = 1.0) -> List[Dict[str, float]]:
    """
    ticks: list of {"best_bid":..., "best_ask":..., "bid_qty":..., "ask_qty":...}
    Returns a list of feature dicts, one per tick after the first (which
    seeds the initial state x0).
    """
    first = ticks[0]
    x0 = np.array([first["best_bid"], first["best_ask"],
                   first["bid_qty"], first["ask_qty"], 0.0, 0.0])
    P0 = np.eye(N_STATES) * 1.0

    imm = IMMFilter(dt, x0, P0)
    out = []
    prev_mid = (first["best_bid"] + first["best_ask"]) / 2.0

    for tick in ticks[1:]:
        z = np.array([tick["best_bid"], tick["best_ask"],
                      tick["bid_qty"], tick["ask_qty"]])
        imm.step(z)
        feats = extract_features(imm, prev_mid)
        out.append(feats)
        prev_mid = feats["mid_price"]

    return out


def _print_feature_rows(feature_rows: List[Dict[str, float]]):
    for i, row in enumerate(feature_rows, start=1):
        print(f"tick {i}: mid={row['mid_price']:.3f}  imbalance={row['queue_imbalance']:.3f}  "
              f"P(bid_depleting)={row['p_bid_depleting']:.3f}  "
              f"P(ask_depleting)={row['p_ask_depleting']:.3f}  "
              f"label={row.get('mid_price_label')}")


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "--odoo":
        # Real usage: python3 lob_ssm.py --odoo <url> <db> <username> <api_key> <stock_id>
        _, _, url, db, username, api_key, stock_id = sys.argv
        config = OdooConfig(url=url, db=db, username=username, api_key=api_key)
        feature_rows = run_on_odoo_bids(
            config, stock_id, initial_ask_price=45.02, initial_ask_qty=1000
        )
        print(f"Fetched and ran SSM on {len(feature_rows)} ticks from Odoo for {stock_id}\n")
        _print_feature_rows(feature_rows)

    else:
        # Toy example: a bid queue depleting over a few ticks, then the best
        # bid steps down to the next price level (Event 1 / Event 3 in the doc).
        # Run with no args for this local sanity check; run with --odoo <url> <db>
        # <username> <api_key> <stock_id> to pull the real bid stream instead.
        synthetic_ticks = [
            {"best_bid": 45.00, "best_ask": 45.02, "bid_qty": 500, "ask_qty": 400},
            {"best_bid": 45.00, "best_ask": 45.02, "bid_qty": 350, "ask_qty": 410},
            {"best_bid": 45.00, "best_ask": 45.02, "bid_qty": 150, "ask_qty": 420},
            {"best_bid": 44.99, "best_ask": 45.02, "bid_qty": 300, "ask_qty": 430},
            {"best_bid": 44.99, "best_ask": 45.02, "bid_qty": 280, "ask_qty": 440},
        ]
        feature_rows = run_on_ticks(synthetic_ticks)
        _print_feature_rows(feature_rows)