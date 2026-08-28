# Technology partner integration

The hackathon's Technology Implementation criterion references "other required
technologies" beyond Alpaca, and the sponsor list is published at kickoff. This repo
is built so a partner technology drops into the running pipeline without touching
core code.

## Featherless AI — the live reasoning provider

Featherless is not wired in as a stage adapter, and that is the point. It is the
LLM the agent *runs on*: `agents.llm.provider: featherless`, model
`Qwen/Qwen3-32B`. Every critic verdict, every macro regime read and every MCP
agent cycle on the judged account is produced by Featherless inference. Pull the
key and the system visibly degrades to deterministic rules — which is the
cleanest possible evidence that the technology is load-bearing rather than
decorative.

Three things made it the right provider rather than a second opinion:

- **Tool calling.** Qwen3 and Kimi-K2 support OpenAI-style function calling, so
  `FeatherlessClient.run_tools` lets the model drive Alpaca's MCP tools
  directly. The model reads the account, the chain and the open positions
  through the same seven-tool read allowlist the broker uses, then narrates what
  it did. This is the link between the strategy layer and Alpaca: the reasoning
  and the broker read the *same* surface, so a verdict can never be about a
  position the account does not hold.
- **Cost shape.** A seven-day event where the agent reasons on every cycle is a
  metering problem on a frontier API. Open-weight inference makes "reason on
  every cycle" affordable, which is why the live loop reasons at all.
- **No new dependency.** The API is OpenAI-compatible, so the client is ~150
  lines of `httpx` — already a core dependency. Nothing new to install on the
  competition host at 09:00 on Monday.

**What it still cannot do.** `RiskEngine` signs every ticket and
`ExecutionRouter` refuses unsigned ones. Featherless scores, explains and
narrates; it never authorises. The same boundary that applied to Anthropic
applies here, unchanged.

**Operational notes.**

- Key: `FEATHERLESS_API_KEY` in `.env`. `oaa doctor` shows the resolved provider,
  model and which env var the key came from.
- Cold starts and rate limits retry (`max_retries`, `retry_backoff_seconds`); a
  non-retryable 4xx fails fast rather than spending the cycle's latency budget.
- JSON mode is requested natively via `response_format`; if a model rejects it,
  the client falls back to prompted JSON **once** and remembers.
- `base_url` is configurable, so a proxy or a pinned region needs no code change.
- The backtest critic runs on Featherless too (`backtest.critic.llm`), at
  temperature 0 with a fixed seed. Gemini was removed on 28 Aug: open-weight
  inference dissolved the cost-shape argument that put a second vendor there,
  and one key beats two during a seven-day event.

**Proving it, rather than asserting it.** `oaa selftest` runs the real chain
once — provider round trip, native JSON mode, MCP session, the read allowlist,
the tool loop, and a cross-check that the account the model read through MCP is
the account the broker trades — and writes the full transcript, every tool call
and result included, to `runs/<profile>/selftest/`. That transcript is the
evidence the MCP integration is load-bearing; a passing table is not.

Two of its checks exist because of failures that actually happened this week,
both of which degraded *silently*:

- a provider key that had never authenticated, hidden by `fallback_to_rules`;
- an MCP bridge that could not start because `uvx` was missing, hidden by the
  bridge's warn-and-continue.

A third, `one account, two paths`, guards the most expensive failure available
here: reasoning about one account while trading another. It compares the raw MCP
payload against `broker.account()` — never the model's prose, since a model that
hallucinates a plausible equity would otherwise pass.

Run `make selftest-judged` before the first session. It is read-only: the
allowlist withholds every mutating tool.

Tests: `tests/test_llm_featherless.py` — wire contract, retry behaviour, JSON-mode
fallback and the tool-call loop, against a mock transport.
`tests/test_selftest.py` — mostly the failure paths, because a verification
command that can pass while the chain is broken is worse than none.

## The seven stages

Every stage already calls `PartnerHub.run(stage, payload)`. A stage with no adapters
is a no-op.

| Stage | Payload | What an adapter does there |
|---|---|---|
| `data_enrichment` | `MarketContext` | alternative data, sentiment, earnings dates, fundamentals → write into `.enrichment` |
| `signal` | `list[(Strategy, TradeIdea, MarketContext)]` | contribute or reshape candidates |
| `reasoning` | prompt context | extra tools or a second opinion for the LLM step |
| `risk` | `TradeIdea` | **veto only** — return falsy to block. The core engine stays final |
| `execution` | `OrderTicket` | smart routing, TCA, alternative venues |
| `telemetry` | `Decision` | ship events to an observability partner |
| `ui` | dashboard payload | extra panels |

## Adding one (target: 30 minutes on kickoff day)

```bash
cp src/oaa/partners/example_partner.py src/oaa/partners/<partner>.py
```

1. Set `partner_name`, `contribution`, and `required_env`.
2. Build the client in `setup()`, do the work in `run()`.
3. Add the block to `config/default.yaml`:

```yaml
partners:
  adapters:
    - name: acme_sentiment
      enabled: true
      module: "oaa.partners.acme_sentiment"
      stage: data_enrichment
      priority: 50
      params:
        api_key_env: "PARTNER_ACME_API_KEY"
```

4. Add the credential **name** (never the value) to `.env.example`.
5. Verify:

```bash
oaa partners          # loaded? credentials present?
oaa scan              # dry run - watch it fire in the journal
```

## Rules the adapters must follow

- **Never raise for a missing optional field.** Degrade and return the payload
  unchanged. `partners.on_error: skip` means a sponsor SDK failing at 14:00 on
  Thursday does not take the trading loop with it.
- **Return the payload, or `None` for "no change".** Only the `risk` stage uses a
  falsy return as a signal (a veto).
- **A partner can never approve a trade.** Only `RiskEngine` approves. This is not
  negotiable — it is the property that makes the whole system safe to leave running.
- **Respect the timeout.** `partners.timeout_seconds` is the budget; a slow adapter
  costs market data freshness.

## Multiple partners

Adapters at the same stage run in `priority` order, chained. Two enrichment partners
both write into `MarketContext.enrichment` under their own keys; nothing collides
unless you pick the same key.

## Judging note

"Thoughtful use of the technology" scores better than "the SDK is imported". When a
partner is wired in, say in the deck *which decision it changes* — an enrichment
partner that supplies earnings dates is what makes `earnings_calendar` viable at all,
and that is a concrete claim rather than a logo on a slide.
