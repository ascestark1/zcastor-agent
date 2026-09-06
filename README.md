# Zcastor

**An execution agent that records what it refuses.**

Built for the Binance Agent OS Mini Hackathon. Reads live market state through
Agent OS, decides, and anchors every decision on NVNM Chain **before the outcome
is known** — including the decisions to do nothing.

```
python3 ops/demo/run_demo.py
```

---

## The problem

Every agent demo shows what the agent did. A trade fired, a swap executed, a
position opened.

But an agent that allocates capital spends most of its time **declining**. This
one evaluates a thousand signals and trades a handful. The trades are on the
exchange record — adversarially verified, easy to show, and selected. The
refusals are where the actual behaviour lives, and they appear in no performance
report ever published.

That asymmetry is the problem. If you cannot see what a system declined, you
cannot tell discipline from luck, and you certainly cannot tell it from a system
that is simply broken in a quiet way.

## What this does

```
Agent OS  →  21 gates  →  decision  →  document  →  hash  →  NVNM Chain
                ↓
          most signals refused, each with a reason
```

Every decision — allow, route, or refuse — produces a record carrying:

- **the reason**, named specifically (`london_sell_bias`, not "risk")
- **the gate that bound**, and the gates that would also have objected
- **the policy hash** in force, resolving to a published document
- **three proofs**: what the agent was told, how it reasons, what the market was
- **a timestamp that predates the outcome**, because the hash is anchored first

A sample record: [decisions/](https://github.com/ascestark1/zcastor-dossiers)

## Verifying one

Nothing here requires our cooperation:

```bash
curl -s https://raw.githubusercontent.com/ascestark1/zcastor-dossiers/main/<path> -o record.json
sha256sum record.json
# compare to the checksum on NVNM Chain, registry 4297
```

Paths carry the checksum, so a URL resolves to exactly one byte sequence
permanently. Republishing writes a new file and marks the old record
`Superseded` — it never rewrites what was already anchored.

---

## Running it

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install web3 eth-account            # only needed to anchor
python3 tests/run_all.py                # 442 tests, no network, no venue
python3 ops/demo/run_demo.py            # the full loop, offline
```

Against live Binance data through Agent OS:

```bash
python3 ops/demo/run_demo.py --market market.json
```

Against Binance directly (testnet keys from testnet.binance.vision):

```bash
export BINANCE_API_KEY=... BINANCE_API_SECRET=...
python3 ops/scripts/smoke_binance.py
```

## Layout

```
zcastor/
  gates/       21 gates; each returns a verdict, none mutate shared state
  entry/       zone entry watcher — arm at a level, fill on confirmation
  execution/   orders, booking, and the engine that orchestrates
  market/      Binance and MT5 behind one port; hourly swing levels
  record/      canonical hashing, proofs, DGML, publishing, session rollover
  anchor/      NVNM client, durable outbox, anchored-record index
  risk/        zone memory, coherence
  data/        journal, edge ledger
tests/         442 tests, no external dependencies
```

## Design notes

**Gates return verdicts; the pipeline decides what runs.** A gate declares
`runs_when_routed = False` once and the pipeline honours it everywhere. The
previous version repeated that guard by hand at each call site, and when the
block was split into three statements two copies were lost — routed signals were
being killed by gates judging a stop they would never use. That class of bug is
no longer expressible.

**`REJECT` and `SUPPRESS` are different.** A malformed payload produces nothing;
there is no decision to be accountable for. A refusal under policy always
produces a record. Two enum values carrying the whole thesis.

**Hash the bytes you publish.** The canonical form written to disk is the form
hashed, so a verifier runs `sha256sum` on the file they downloaded. Any scheme
requiring them to re-serialise first breaks silently the first time a float
renders differently in their language.

**Anchoring never blocks trading.** A durable outbox persists every pending
write and drains when the chain returns. MANTRA halted on 20 August and NVNM
inherits settlement from it; a naive emitter would have dropped that day.

**Cost is not spread.** A broker charging 40 points of spread and no commission
costs less than an exchange quoting 0.01 and charging 0.1% per side — about 160
points on an 80,000 instrument. The gates reason about total cost, because
judging a venue by its spread gets one of them badly wrong.

**One port, two venues.** Adding Binance changed nothing above `market/`. The
gate stack, record layer, resolver and anchoring all ran unmodified. There is a
test asserting both adapters satisfy the same interface.

---

## Honest status

**No demonstrated edge.** Every segment of the performance ledger stays in
shadow until it has at least forty measured outcomes and a positive lower
confidence bound. None have reached it, and the ledger reports that rather than
hiding it. Stops were recently widened after measuring that spread was consuming
half the risk on fast timeframes, which reset the evidence to zero. That is the
cost of measuring honestly.

**The desk is human-managed.** Finance directors set the mandate and approve
what trades. This runs alongside them and takes fewer positions. The
accountability layer arrives before the autonomy, not after it.

**Spot holds no stops.** On Binance spot a stop is enforced by the watcher's
polling loop, not by the exchange. If the process dies with a position open,
nothing closes it. An exchange-side OCO would fix this and is not built yet.

**One month of measurement**, one instrument. Anything derived from it is a
hypothesis to test, not a result.

---

An accountability system whose first honest output is "we cannot yet say this
works" is doing what it was built for. If it reported otherwise on the available
evidence, the reporting would be the broken part.
