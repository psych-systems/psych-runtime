# Benchmarks

Four numbers, each with a recorded baseline and a tolerance, run as tests.

A benchmark that prints numbers gets ignored within two months. These fail the
build when they regress past their tolerance, which is what makes them a test
rather than a report nobody reads.

```sh
pytest benchmarks -q                    # measure, compare, fail on regression
pytest benchmarks -q --benchmark-record  # rewrite the baselines after a deliberate change
```

## What is measured, and why these four

| Benchmark | Answers |
|---|---|
| `test_dispatch_throughput` | How fast can Runs be admitted? This is a request handler's cost. |
| `test_claim_latency_under_contention` | How long does a Worker wait for work when many Workers compete? |
| `test_log_bytes_per_turn` | What does a Run cost to store? Multiply by your volume and retention. |
| `test_worker_scaling_ceiling` | **How many Workers can one database hold before `claim()` is the bottleneck?** |

The last one decides build-versus-buy. It is the number nobody publishes and the
first thing a platform team asks, because it is the difference between "add
another Worker" and "shard the store".

## Numbers are environment-bound

Every figure here belongs to the machine that produced it. The recorded
baselines were measured on the shape named in `baselines.json` under
`environment`, and a laptop, a shared CI runner and a dedicated box will
disagree by more than the tolerances allow.

That is why the tolerances start wide. A shared CI runner is noisy, and a
benchmark that fails on a noisy neighbour teaches people to rerun the job until
it passes, which is worse than having no benchmark. Tighten them once the
variance on your own runner is known.

**Do not quote these numbers without the hardware.** A benchmark without its
machine shape is a number people repeat back at you wrongly.

## The store the numbers come from

`InMemoryStore` by default, which measures the runtime and not the database. Set
`PSYCH_BENCH_POSTGRES_DSN` to measure against real PostgreSQL, which is the
configuration the scaling ceiling actually means anything in: an in-memory store
has no lock contention, no network round trip and no `claim()` to bottleneck.

The in-memory numbers are still worth having. They are the floor, and a
regression in them is a regression in the runtime rather than in somebody's
database tuning.
