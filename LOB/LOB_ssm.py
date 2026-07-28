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
import time
import xmlrpc.client
from datetime import datetime, timedelta
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
                         depletion_bias: float = -3.0,
                         queue_shock_q: float = 0.5
                         ) -> Tuple[np.ndarray, np.ndarray]:
    """
    Q_s : process noise covariance for regime s.
    bias : deterministic drift-shift used to push the model toward
    depletion in that regime (a simple way to fold a jump component into
    a linear-Gaussian filter without a full jump-diffusion model).

    queue_shock_q : direct process noise added to q_b/q_a themselves (not
    just their drift terms) in the depleting regimes, so a real queue-depth
    shock isn't forced to propagate only through the slower drift channel.
    """
    Q = np.eye(N_STATES) * base_q
    bias = np.zeros(N_STATES)

    if regime == "BID_DEPLETING":
        Q[IDX_MUB, IDX_MUB] = depletion_q
        Q[IDX_QB, IDX_QB] = queue_shock_q
        bias[IDX_MUB] = depletion_bias
    elif regime == "ASK_DEPLETING":
        Q[IDX_MUA, IDX_MUA] = depletion_q
        Q[IDX_QA, IDX_QA] = queue_shock_q
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


def build_observation_noise(regime: str,
                             base_R: np.ndarray = R,
                             depleting_scale: float = 0.15
                             ) -> np.ndarray:
    """
    Shrink observation noise on q_b/q_a when in a depleting regime, so the
    Kalman gain trusts the observed queue-depth jump instead of smoothing
    it away through the (slower) drift channel.
    """
    R_s = base_R.copy()
    if regime == "BID_DEPLETING":
        R_s[2, 2] *= depleting_scale   # q_b observation index in H/z
    elif regime == "ASK_DEPLETING":
        R_s[3, 3] *= depleting_scale   # q_a observation index in H/z
    return R_s


# Regime transition matrix PI[i, j] = P(s_t = j | s_{t-1} = i)
# Sticky regimes: staying put is the most likely outcome each tick.
PI = np.array([
    [0.75, 0.125, 0.125],   # from STABLE
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


def kf_update(state: KalmanState, z: np.ndarray, R: np.ndarray) -> Tuple[KalmanState, float]:
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
            R_s = build_observation_noise(regime)
            pred = kf_predict(mixed_states[j], F, Q, bias)
            upd, ll = kf_update(pred, z, R_s)
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
    lead_id: int
    buyer: str
    price: float
    quantity: float
    timestamp: datetime


# Matches the description string written by BidIngestionClient.register_bid:
#   "bid_ref=... price=45.02 qty=150 ts=2026-07-08T05:37:46.903003"
# NOTE: Odoo's crm.lead.description is an HTML field - plain text you write
# comes back wrapped in tags, e.g. "<p>...ts=2026-07-08T05:37:46.903003</p>".
# The ts group must stop at '<' as well as whitespace, or it swallows the
# trailing "</p>" and breaks datetime.fromisoformat().
_BID_DESC_RE = re.compile(
    r"price=(?P<price>[-\d.]+)\s+qty=(?P<qty>[-\d.]+)\s+ts=(?P<ts>[^\s<]+)"
)


# ---------------------------------------------------------------------------
# Short-TTL cache for fetch_bids_from_odoo.
#
# /bids, /diagnostics/:id, and /auction/:id/winner each independently called
# fetch_bids_from_odoo (a full crm.lead search_read over XML-RPC), so a
# single frontend refresh cycle - or a bid submission followed by the
# frontend's auto-refresh - triggered 3-4+ redundant full-table fetches of
# the SAME data for the SAME stock_id within the same second. This cache
# makes repeat calls within TTL_SECONDS free; _invalidate_bid_cache() lets
# a caller that just wrote a new bid force the next read to be fresh rather
# than serving a stale cached copy. Only caches the common case
# (since=None) - callers passing an explicit `since` window bypass it.
# ---------------------------------------------------------------------------
_BID_CACHE: Dict[str, Tuple[float, List["FetchedBid"]]] = {}
_BID_CACHE_TTL_SECONDS = 2.0


def _invalidate_bid_cache(stock_id: str) -> None:
    """Call right after writing a new bid so the next read is fresh
    instead of serving a stale cached copy."""
    _BID_CACHE.pop(stock_id, None)


def fetch_bids_from_odoo(config: OdooConfig, stock_id: str,
                          since: Optional[datetime] = None,
                          limit: int = 10000) -> List[FetchedBid]:
    """
    Reads every crm.lead of type "lead" whose name/description references
    stock_id, parses out price/qty/timestamp, and returns them time-ordered.
    Raises on auth failure rather than silently returning nothing - a
    connection problem should be loud, not look like "no bids yet".

    Cached in-process for _BID_CACHE_TTL_SECONDS when since=None (see
    _invalidate_bid_cache) - avoids the redundant full-table re-fetches
    that were happening 3-4x per frontend refresh cycle / bid submission.
    """
    if since is None:
        cached = _BID_CACHE.get(stock_id)
        if cached is not None and (time.monotonic() - cached[0]) < _BID_CACHE_TTL_SECONDS:
            return cached[1]

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
        [domain], {"fields": ["id", "partner_id", "description"], "limit": limit},
    )

    bids = []
    for rec in records:
        desc = rec.get("description") or ""
        m = _BID_DESC_RE.search(desc)
        if not m:
            continue  # not a bid-shaped lead (or format changed) - skip, don't crash
        buyer = rec["partner_id"][1] if rec.get("partner_id") else "unknown"
        bids.append(FetchedBid(
            lead_id=rec["id"],
            buyer=buyer,
            price=float(m.group("price")),
            quantity=float(m.group("qty")),
            timestamp=datetime.fromisoformat(m.group("ts")),
        ))

    bids.sort(key=lambda b: b.timestamp)

    if since is None:
        _BID_CACHE[stock_id] = (time.monotonic(), bids)

    return bids


# How long a resting bid still counts toward best-level depth before it
# "ages out". Without this, best_bid_qty is a lifetime running sum of
# every bid ever seen at the best price (it only ever goes up, aside from
# resetting when a strictly higher bid arrives) - while the ask side
# (AskBook) is naturally a live snapshot, since it only ever stores
# current state. That asymmetry mechanically drags queue_imbalance toward
# the bid-heavy extreme over a long-running test session instead of
# oscillating the way FI2010's real, continuously-refreshed order flow
# does. Treating bids as "currently resting" rather than "ever submitted"
# fixes the asymmetry without touching bid_ingestion.py/ask_book.py.
BID_RESTING_WINDOW_SECONDS = 600  # 10 minutes


def _best_bid_at(active_bids: List[Tuple[datetime, float, float]],
                  as_of: datetime,
                  window_seconds: float) -> Tuple[Optional[float], float]:
    """Recomputes best_bid_price/qty as of `as_of`, counting only bids
    whose timestamp falls within the trailing window - i.e. "what's
    resting right now", not "everything ever submitted"."""
    window_start = as_of - timedelta(seconds=window_seconds)
    live = [(p, q) for (t, p, q) in active_bids if window_start <= t <= as_of]
    if not live:
        return None, 0.0
    best_price = max(p for p, _ in live)
    best_qty = sum(q for p, q in live if p == best_price)
    return best_price, best_qty


def bids_to_ticks(bids: List[FetchedBid],
                   initial_ask_price: float,
                   initial_ask_qty: float,
                   resting_window_seconds: float = BID_RESTING_WINDOW_SECONDS
                   ) -> List[Dict[str, float]]:
    """
    Reconstructs a [best_bid, best_ask, bid_qty, ask_qty] tick stream from
    a resting-bid sequence. The ask side isn't in Odoo (bid_ingestion.py
    only registers incoming BUY bids, per Stage 1) so it's carried forward
    from initial_ask_price/qty and only moves if you pass in real ask data
    separately - this reconstruction is for the bid side of the book only.

    best_bid_qty reflects only bids within the trailing
    `resting_window_seconds` (see BID_RESTING_WINDOW_SECONDS) - bids
    below best_bid_price, or older than the window, aren't counted, same
    as real resting orders eventually getting cancelled/replaced.
    """
    ticks = []
    active_bids: List[Tuple[datetime, float, float]] = []

    for b in bids:
        active_bids.append((b.timestamp, b.price, b.quantity))
        best_bid_price, best_bid_qty = _best_bid_at(active_bids, b.timestamp, resting_window_seconds)

        ticks.append({
            "best_bid": best_bid_price,
            "best_ask": initial_ask_price,
            "bid_qty": best_bid_qty,
            "ask_qty": initial_ask_qty,
        })

    return ticks


def bids_and_asks_to_ticks(bids: List[FetchedBid], ask_events: List[dict],
                            initial_ask_price: float, initial_ask_qty: float,
                            resting_window_seconds: float = BID_RESTING_WINDOW_SECONDS
                            ) -> List[Dict[str, float]]:
    """
    Like bids_to_ticks(), but merges in REAL ask-side events from
    ask_book.py instead of a frozen constant. Walks both streams in
    chronological order, updating best_bid/best_ask/queues as either side
    changes - this is what makes ASK_DEPLETING a real, reachable regime
    instead of dead code.

    ask_events: AskBook.get_events(stock_id) - a list of dicts with
    "event_type" ("new_sell_limit_order"/"ask_reduced"/"ask_depleted"),
    "timestamp", and either "resulting_best_ask"/"resulting_qty" (for new
    orders) or "remaining_qty" (for depletion events).

    best_bid_qty reflects only bids within the trailing
    `resting_window_seconds` (see BID_RESTING_WINDOW_SECONDS), so it
    behaves like a live snapshot - the same way the ask side already
    does, since AskBook only ever stores current state - instead of a
    lifetime running sum that only ever grows.
    """
    # Tag each bid/ask event with a common "kind" so they can be merged
    # and walked in one pass, sorted purely by timestamp.
    merged = [("bid", b.timestamp, b) for b in bids]
    for e in ask_events:
        merged.append(("ask", datetime.fromisoformat(e["timestamp"]), e))
    merged.sort(key=lambda item: item[1])

    ticks = []
    active_bids: List[Tuple[datetime, float, float]] = []
    ask_price = initial_ask_price
    ask_qty = initial_ask_qty

    for kind, ts, item in merged:
        if kind == "bid":
            b = item
            active_bids.append((b.timestamp, b.price, b.quantity))
        else:
            e = item
            if e["event_type"] in ("new_sell_limit_order",):
                ask_price = e["resulting_best_ask"]
                ask_qty = e["resulting_qty"]
            elif e["event_type"] in ("ask_reduced", "ask_depleted"):
                ask_qty = e["remaining_qty"]

        best_bid_price, best_bid_qty = _best_bid_at(active_bids, ts, resting_window_seconds)

        ticks.append({
            "best_bid": best_bid_price if best_bid_price is not None else ask_price,
            "best_ask": ask_price,
            "bid_qty": best_bid_qty,
            "ask_qty": ask_qty,
        })

    return ticks


@dataclass
class EventFlags:
    """
    Explicit, deterministic yes/no answers to the 4 events in Step 2.4 -
    NOT probabilities. This is the "pinpoint whether X happened" layer,
    computed directly from consecutive ticks by simple comparison. The
    IMM's regime probabilities (p_bid_depleting etc.) are a SEPARATE,
    softer signal answering "how confident is the filter that we're in
    this kind of regime right now" - the two are complementary, not
    duplicates: EventFlags says "yes, this specific thing just happened
    on this specific tick"; the IMM says "the recent pattern of ticks
    looks like this kind of regime overall."
    """
    ask_depleted: bool       # Event 1
    new_higher_bid: bool     # Event 2
    bid_depleted: bool       # Event 3
    new_lower_ask: bool      # Event 4


def detect_events(prev_tick: Dict[str, float], curr_tick: Dict[str, float]) -> EventFlags:
    return EventFlags(
        ask_depleted=(prev_tick["ask_qty"] > 0 and curr_tick["ask_qty"] <= 0),
        new_higher_bid=(curr_tick["best_bid"] > prev_tick["best_bid"]),
        bid_depleted=(prev_tick["bid_qty"] > 0 and curr_tick["bid_qty"] <= 0),
        new_lower_ask=(curr_tick["best_ask"] < prev_tick["best_ask"]),
    )


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


def run_on_bids_and_asks(bids: List[FetchedBid], ask_events: List[dict],
                          initial_ask_price: float, initial_ask_qty: float,
                          dt: float = 1.0) -> List[Dict[str, float]]:
    """
    Like run_on_ticks(), but built from bids_and_asks_to_ticks() (real ask
    data, not a frozen constant) and additionally attaches deterministic
    EventFlags per tick alongside the IMM's probabilistic regime output.
    """
    ticks = bids_and_asks_to_ticks(bids, ask_events, initial_ask_price, initial_ask_qty)
    if not ticks:
        return []

    x0 = np.array([ticks[0]["best_bid"], ticks[0]["best_ask"],
                   ticks[0]["bid_qty"], ticks[0]["ask_qty"], 0.0, 0.0])
    P0 = np.eye(N_STATES) * 1.0
    imm = IMMFilter(dt, x0, P0)

    out = []
    prev_mid = (ticks[0]["best_bid"] + ticks[0]["best_ask"]) / 2.0
    prev_tick = ticks[0]

    for tick in ticks[1:]:
        z = np.array([tick["best_bid"], tick["best_ask"], tick["bid_qty"], tick["ask_qty"]])
        imm.step(z)
        feats = extract_features(imm, prev_mid)

        events = detect_events(prev_tick, tick)
        feats["event_ask_depleted"] = events.ask_depleted
        feats["event_new_higher_bid"] = events.new_higher_bid
        feats["event_bid_depleted"] = events.bid_depleted
        feats["event_new_lower_ask"] = events.new_lower_ask

        out.append(feats)
        prev_mid = feats["mid_price"]
        prev_tick = tick

    return out


def run_on_odoo_bids_and_asks(config: OdooConfig, stock_id: str,
                               ask_events: List[dict],
                               initial_ask_price: float, initial_ask_qty: float,
                               since: Optional[datetime] = None) -> List[Dict[str, float]]:
    """
    End-to-end: fetch real bids from Odoo AND real ask events (from
    ask_book.py's AskBook.get_events(stock_id)), merge them, run the IMM
    filter, and return per-tick features WITH explicit event flags. This
    is the version that actually answers all 4 of Step 2.4's questions
    with real data - use this instead of run_on_odoo_bids() going forward.
    """
    bids = fetch_bids_from_odoo(config, stock_id, since=since)
    if not bids:
        raise ValueError(
            f"No registered bids found in Odoo for stock_id={stock_id!r}. "
            "Has bid_ingestion.py been run for this stock yet?"
        )
    return run_on_bids_and_asks(bids, ask_events, initial_ask_price, initial_ask_qty)


def print_diagnostic_report(stock_id: str, feature_rows: List[Dict[str, float]]):
    """
    The SSM's actual job, stated plainly: answer Step 2.4's 4 questions
    out loud, plus the imbalance-based signal, so this reads as a
    diagnostic report rather than a wall of per-tick numbers. Odoo has no
    role here - this function never touches it. Odoo is purely the
    execution/record-keeping layer downstream (Stages 5-8); everything
    printed here is the SSM's own analysis of the bid/ask stream.
    """
    if not feature_rows:
        print(f"=== SSM Diagnostic Report: {stock_id} ===")
        print("No ticks to analyze.\n")
        return

    n = len(feature_rows)
    ask_depleted_count = sum(1 for r in feature_rows if r.get("event_ask_depleted"))
    new_higher_bid_count = sum(1 for r in feature_rows if r.get("event_new_higher_bid"))
    bid_depleted_count = sum(1 for r in feature_rows if r.get("event_bid_depleted"))
    new_lower_ask_count = sum(1 for r in feature_rows if r.get("event_new_lower_ask"))

    latest = feature_rows[-1]

    print(f"=== SSM Diagnostic Report: {stock_id} ({n} ticks analyzed) ===")
    print(f"Event 1 (Ask Queue Depleted):     {'YES' if ask_depleted_count else 'no'} "
          f"({ask_depleted_count} occurrence(s))")
    print(f"Event 2 (New Higher Buy Limit):   {'YES' if new_higher_bid_count else 'no'} "
          f"({new_higher_bid_count} occurrence(s))")
    print(f"Event 3 (Bid Queue Depleted):     {'YES' if bid_depleted_count else 'no'} "
          f"({bid_depleted_count} occurrence(s))")
    print(f"Event 4 (New Lower Sell Limit):   {'YES' if new_lower_ask_count else 'no'} "
          f"({new_lower_ask_count} occurrence(s))")
    print(f"\nLatest queue imbalance: {latest['queue_imbalance']:+.3f} "
          f"({'bid-heavy' if latest['queue_imbalance'] > 0 else 'ask-heavy'})")
    print(f"Latest regime estimate: stable={latest['p_stable']:.2f}  "
          f"bid_depleting={latest['p_bid_depleting']:.2f}  "
          f"ask_depleting={latest['p_ask_depleting']:.2f}")
    print(f"Latest mid price: {latest['mid_price']:.2f}  spread: {latest['spread']:.2f}\n")


def evaluate_against_baseline(feature_rows: List[Dict[str, float]], test_size: float = 0.2):
    """
    Gould & Bonart (2015), Section 5.4: compares a fitted imbalance->
    direction logistic regression against the paper's null model, which
    assumes I carries no information (ŷ(I) = 0.5 for all I, always).

    Reports the same two metrics the paper uses:
      - area under the ROC curve (null model's expected value is exactly
        0.5; a real model should exceed that)
      - mean squared residual (null model's is provably exactly 0.25,
        since every residual is ±0.5 regardless of the outcome)

    Uses an 80/20 train/test split when there's enough data to support
    one (paper uses the same split); falls back to in-sample reporting
    with a stated caveat when there isn't, since evaluating a model
    out-of-sample on that little data isn't meaningful.
    """
    from sklearn.linear_model import LogisticRegression
    from sklearn.model_selection import train_test_split
    from sklearn.metrics import roc_auc_score

    X, y = [], []
    for i in range(len(feature_rows) - 1):
        row = feature_rows[i]
        next_row = feature_rows[i + 1]
        event_fired = (row.get("event_ask_depleted") or row.get("event_new_higher_bid")
                       or row.get("event_bid_depleted") or row.get("event_new_lower_ask"))
        label = next_row.get("mid_price_label")
        if event_fired and label is not None:
            X.append([row["queue_imbalance"]])
            y.append(label)

    result = {"n_samples": len(y), "n_up": sum(y), "n_down": len(y) - sum(y)}

    if len(y) < 5 or len(set(y)) < 2:
        result["status"] = "insufficient_data"
        return result

    null_msr = 0.25  # provable constant for ŷ=0.5, see paper Eq. 20
    null_auc = 0.5    # expected value for a model with no discriminative power

    can_split = len(y) >= 10 and min(sum(y), len(y) - sum(y)) >= 2
    if can_split:
        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=test_size, random_state=0, stratify=y)
        result["split"] = "out_of_sample"
    else:
        X_train, X_test, y_train, y_test = X, X, y, y
        result["split"] = "in_sample (too little data for a held-out test set)"

    model = LogisticRegression()
    model.fit(X_train, y_train)
    probs = model.predict_proba(X_test)[:, 1]

    model_msr = sum((p - actual) ** 2 for p, actual in zip(probs, y_test)) / len(y_test)
    try:
        model_auc = roc_auc_score(y_test, probs)
    except ValueError:
        model_auc = None  # only one class present in the test split

    result.update({
        "status": "evaluated",
        "model_auc": model_auc, "null_auc": null_auc,
        "model_msr": model_msr, "null_msr": null_msr,
        "msr_improvement_pct": (null_msr - model_msr) / null_msr * 100,
    })
    return result


def print_baseline_comparison(stock_id: str, feature_rows: List[Dict[str, float]]):
    print(f"=== Model vs. baseline (Gould & Bonart null model): {stock_id} ===")
    r = evaluate_against_baseline(feature_rows)

    if r["status"] == "insufficient_data":
        print(f"Only {r['n_samples']} event-triggered tick(s) with both classes represented "
              f"(need >=5) - not enough to evaluate yet.\n")
        return r

    print(f"Evaluated on {r['n_samples']} event-triggered tick(s) "
          f"({r['n_up']} up, {r['n_down']} down), {r['split']}.")
    auc_str = f"{r['model_auc']:.3f}" if r["model_auc"] is not None else "n/a (single class in test set)"
    print(f"AUC-ROC:              model={auc_str}   null_baseline={r['null_auc']:.3f}")
    print(f"Mean squared residual: model={r['model_msr']:.3f}   null_baseline={r['null_msr']:.3f}"
          f"   ({r['msr_improvement_pct']:+.1f}% vs. baseline)\n")
    return r


def fit_event_conditioned_predictor(feature_rows: List[Dict[str, float]]):
    """
    Gould & Bonart (2015): fit a logistic regression of ONE-TICK-AHEAD
    price direction on queue imbalance. Here, restricted to ticks where
    one of the 4 Step 2.4 events just fired - that's the actual point of
    running the diagnostics first: imbalance is only a meaningful signal
    right after something diagnostically significant happened (a
    depletion, a new limit order), not on every quiet tick where nothing
    changed. This mirrors the paper's framing directly: the events are
    what CAUSE imbalance to shift, and it's that shift the regression is
    meant to explain.

    Returns (model, X, y) - model is None if there isn't enough
    event-triggered data with both price-direction classes represented to
    fit a meaningful regression yet.
    """
    from sklearn.linear_model import LogisticRegression

    X, y = [], []
    for i in range(len(feature_rows) - 1):
        row = feature_rows[i]
        next_row = feature_rows[i + 1]

        event_fired = (row.get("event_ask_depleted") or row.get("event_new_higher_bid")
                       or row.get("event_bid_depleted") or row.get("event_new_lower_ask"))
        label = next_row.get("mid_price_label")  # 1=up, 0=down, None=unchanged

        if event_fired and label is not None:
            X.append([row["queue_imbalance"]])
            y.append(label)

    if len(y) < 5 or len(set(y)) < 2:
        return None, X, y

    model = LogisticRegression()
    model.fit(X, y)
    return model, X, y


def print_event_conditioned_prediction(stock_id: str, feature_rows: List[Dict[str, float]]):
    """
    The actual Gould & Bonart step: given the diagnostics above found
    event-triggered imbalance shifts, fit the imbalance -> direction
    regression on those specific ticks and report what it says about the
    CURRENT imbalance right now.
    """
    print(f"=== Event-conditioned imbalance predictor: {stock_id} ===")
    model, X, y = fit_event_conditioned_predictor(feature_rows)

    if model is None:
        print(f"Only {len(y)} event-triggered tick(s) with a known next-tick "
              f"direction so far (need >=5, with both up and down represented) - "
              f"not enough to fit yet.\n")
        return None

    latest_imbalance = feature_rows[-1]["queue_imbalance"]
    p_up = model.predict_proba([[latest_imbalance]])[0][1]

    print(f"Fitted on {len(y)} event-triggered tick(s) "
          f"({sum(y)} up, {len(y) - sum(y)} down).")
    print(f"Given the current imbalance ({latest_imbalance:+.3f}): "
          f"P(price up next tick) = {p_up:.3f}\n")
    return p_up



# ---------------------------------------------------------------------------
# Stage 5: determine the auction winner from the Odoo-sourced bid stream.
#
# This is genuinely separate from the SSM/IMM analysis above - the SSM
# tells you what REGIME the market is in (stable/depleting), this tells
# you WHO WINS. Both read the same fetch_bids_from_odoo() stream but
# answer different questions, matching the Stage 2-4 vs Stage 5 split in
# the original pipeline doc.
# ---------------------------------------------------------------------------

@dataclass
class AuctionWinner:
    lead_id: int          # the exact Odoo crm.lead record this winner came from
    buyer: str
    price: float
    quantity: float
    timestamp: datetime


def determine_auction_winner(bids: List[FetchedBid],
                              ask_price: float,
                              ask_qty: float) -> List[AuctionWinner]:
    """
    Standard price-time priority: among bids at or above ask_price, the
    highest price wins first; ties broken by whoever bid earliest. Fills
    are allocated against ask_qty until it runs out - so this can return
    MULTIPLE winners if one bid doesn't consume the whole ask queue,
    matching Stage 5's "awards the available shares at the current asking
    price" (plural shares, potentially plural winners).

    Bids below ask_price never win - they just sit in Odoo as a permanent
    record that they existed and didn't clear, which is fine; Stage 1's
    job was recording them, not guaranteeing they'd win.
    """
    eligible = [b for b in bids if b.price >= ask_price]
    # price-time priority: highest price first, earliest timestamp breaks ties
    eligible.sort(key=lambda b: (-b.price, b.timestamp))

    winners = []
    remaining = ask_qty
    for b in eligible:
        if remaining <= 0:
            break
        fill_qty = min(b.quantity, remaining)
        winners.append(AuctionWinner(
            lead_id=b.lead_id,
            buyer=b.buyer, price=ask_price,  # awarded AT the ask price, not the bid price
            quantity=fill_qty, timestamp=b.timestamp,
        ))
        remaining -= fill_qty

    return winners



def find_highest_bid_from_odoo(config: OdooConfig, stock_id: str,
                                ask_price: float, ask_qty: float,
                                since: Optional[datetime] = None) -> dict:
    """
    One-call convenience: fetch bids from Odoo for stock_id, determine the
    winner(s) against the given ask, return a summary dict. Does NOT write
    anything back to Odoo - promoting the winning lead to an Opportunity
    and creating a Sales Order is a separate write-path step (Stage 6),
    deliberately not bundled in here since this function is read-only,
    same as the rest of lob_ssm.py's Odoo interaction.
    """
    bids = fetch_bids_from_odoo(config, stock_id, since=since)
    if not bids:
        raise ValueError(f"No registered bids found in Odoo for stock_id={stock_id!r}.")

    highest = max(bids, key=lambda b: b.price)
    winners = determine_auction_winner(bids, ask_price, ask_qty)

    return {
        "stock_id": stock_id,
        "total_bids_seen": len(bids),
        "highest_bid_price": highest.price,
        "highest_bid_buyer": highest.buyer,
        "highest_bid_timestamp": highest.timestamp,
        "ask_price": ask_price,
        "ask_qty": ask_qty,
        "winners": winners,
        "total_qty_filled": sum(w.quantity for w in winners),
        "qty_unfilled": ask_qty - sum(w.quantity for w in winners),
    }


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

        print(f"\n=== Stage 5: auction winner for {stock_id} ===")
        result = find_highest_bid_from_odoo(
            config, stock_id, ask_price=45.02, ask_qty=1000
        )
        print(f"Total bids seen: {result['total_bids_seen']}")
        print(f"Highest bid overall: {result['highest_bid_price']:.2f} "
              f"by {result['highest_bid_buyer']} at {result['highest_bid_timestamp']}")
        print(f"Ask price/qty used: {result['ask_price']:.2f} / {result['ask_qty']:.0f}")
        if result["winners"]:
            print(f"\n{len(result['winners'])} winner(s), "
                  f"{result['total_qty_filled']:.0f} of {result['ask_qty']:.0f} shares filled:")
            for w in result["winners"]:
                print(f"  {w.buyer:<20} won {w.quantity:.0f} @ {w.price:.2f}  ({w.timestamp})")
        else:
            print("No bids cleared the ask price - no winner this round.")

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