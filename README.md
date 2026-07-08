# Limit Order Book Auction Simulator

A research implementation of a **Limit Order Book (LOB) auction simulator** built around **Queue Imbalance theory** for short-term price movement prediction and integrated with an ERP-style auction execution workflow.

The project combines market microstructure concepts with a simulated business pipeline, beginning with bid ingestion and ending with accounting and inventory updates.

---

## Overview

This simulator models the lifecycle of an auction:

```
Bid Collection
      │
      ▼
Market State Construction
      │
      ▼
Queue Imbalance Prediction
      │
      ▼
Auction Execution
      │
      ▼
Sales Workflow
      │
      ▼
Accounting
      │
      ▼
Inventory Update
```

---

# Pipeline

## 1. Bid Collection (CRM)

Incoming bids are collected from buyers.

Each bid contains:

- Buyer ID
- Stock ID
- Bid Price
- Quantity
- Timestamp

The validated bids form the order book used throughout the simulation.

---

## 2. Market State Construction

Before any prediction or auction takes place, the incoming bids undergo validation.

### Tick Size Validation

Ensures every bid complies with the minimum exchange tick size (default: **0.01**).

Invalid bids are rejected before entering the order book.

### Order Book Construction

Validated bids populate the:

- Bid Queue
- Ask Queue

The simulator maintains both queues independently.

### Queue Dynamics

Queue evolution is modelled using stochastic dynamics inspired by Brownian Motion:

\[
dq_t = \mu dt + \sigma dW_t
\]

where

- μ represents average queue growth
- σ captures queue volatility
- dWₜ models random market fluctuations

### Sample Construction

The mid-price is computed as

\[
m(t)=\frac{BestBid+BestAsk}{2}
\]

Movement labels are generated from successive mid-price changes:

- 1 → price increases
- 0 → otherwise

### Market Events

The simulator reacts to events including:

- Queue depletion
- New buy limit orders
- New sell limit orders

---

## 3. Price Movement Prediction

Queue imbalance is used as the primary explanatory variable.

The implementation evaluates whether queue imbalance provides predictive power beyond a simple baseline (null model).

---

## 4. Auction Execution

Once prediction is complete, the auction engine executes trades.

Execution steps include:

- Identify the latest active order book
- Select the highest valid bid
- Verify asking price has not changed
- Allocate shares to the winning bidder

---

## 5. Sales Workflow

After a successful auction:

```
CRM Opportunity
        │
        ▼
Quotation
        │
        ▼
Sales Order
```

The auction outcome is converted into a commercial transaction.

---

## 6. Accounting

Following execution:

- Generate invoice
- Receive payment
- Record accounting entries
- Support blanket order processing for partial executions

Blanket orders allow multiple auction executions before final settlement.

---

## 7. Inventory

Inventory is updated after settlement.

If shares remain:

- Continue allocation in future auctions.

Otherwise:

- Close the offering.

---

# Repository Structure

```
Paper_implementation/
│
├── Auction/
│   ├── 1.Auction_Zscore/
│   ├── 2.Auction_MinMax/
│   └── 3.Auction_DecPre/
│
├── LOB/
│   ├── bid_ingestion.py
│   ├── Broker_Data.py
│   ├── LOB_ssm.py
│   ├── Test_bid_generation.py
│   └── ...
│
└── NoAuction/
```

---

# Running

Clone the repository:

```bash
git clone https://github.com/Vocod3r/limit-order-book-price-predictor-.git
cd limit-order-book-price-predictor-
```

Run the simulator:

```bash
python LOB/LOB_ssm.py
```

---

# Research Motivation

The project explores how **queue imbalance**, **order book dynamics**, and **auction execution** can be combined to simulate realistic financial markets and serve as a foundation for machine learning models for price prediction.

---

# Future Work

- Multi-stock auctions
- Brownian queue calibration using real market data
- Deep learning price prediction models
- Odoo ERP integration
- LOBSTER dataset support
- Reinforcement learning-based bidding agents

---

## Author

**Vipin Lokesh**

B.Tech Computer Science (AI & ML)  
PES University
