# Held run — on-call incident-investigation agent (synthetic, deterministic)

> **This is the raw material the operator holds.** None of the text below is in
> the ledger — the capsules commit only SHA-256 *digests* of the per-action
> input and output. This file is what those digests are digests *of*. The run
> is synthetic: no model was called and no repository was really read.

- **Operator:** `northwind-retail`
- **Agent (developer):** `oncall-investigator@v2`
- **Model:** `anthropic / claude-opus-4-6`
- **Ledger it produced:** `incident-investigation-agent/ledger.jsonl` (4 capsules)

## System prompt

> You are an on-call incident-investigation agent. Given a failed deploy,
> gather read-only evidence — the repo state, the error logs, the diff — and
> report the most likely root cause with citations. You have no write access;
> you investigate and advise, you do not change anything.

## User prompt

> "deploy-4471 for checkout-svc failed. What happened?"

## Agent reasoning (held, not sealed)

Start from the repo to confirm the failed deploy, then pull the error logs for
that deploy to find the first failure, then read the diff between the last good
deploy and this one to explain it. Every step is a read — nothing is changed.

| # | Tool call (args) | Result | Capsule |
|---|---|---|---|
| 1 | `fetch_repo(repo="northwind/checkout-svc", ref="main")` | head d4e9f10, last deploy deploy-4471 = failed | `2e3d7361…` (fyi) |
| 2 | `search_logs(service="checkout-svc", deploy="deploy-4471", query="level=error", window="15m")` | 3 matches; first error `ECONNREFUSED redis:6379`; `logs/deploy-4471#L2210` | `1ca2cd78…` (fyi) |
| 3 | `read_diff(repo="northwind/checkout-svc", from="deploy-4470", to="deploy-4471")` | 2 files changed; `config/redis.yaml` host `cache-redis` → `redis`; `compare/deploy-4470...deploy-4471` | `b20d4e4e…` (fyi) |
| 4 | `summarize_findings(incident="deploy-4471-failed")` | root cause + evidence citations; write_performed=False | `be6e389c…` (fyi) |

## Outcome (the advisory answer, held)

> **Root cause:** deploy-4471 changed the Redis host in `config/redis.yaml` from
> `cache-redis` to `redis`, which does not resolve in this cluster; the startup
> probe then fails on `ECONNREFUSED` and the deploy is marked failed.
>
> **Evidence:** `logs/deploy-4471#L2210`, `compare/deploy-4470...deploy-4471`
>
> **Recommendation:** revert the `config/redis.yaml` host change.

No write was performed. Every capsule is an `fyi` observation with **no effect
record** — the value is a verifiable record of exactly what the agent *looked
at* before it advised.
