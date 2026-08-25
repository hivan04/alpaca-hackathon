# Technology partner integration

The hackathon's Technology Implementation criterion references "other required
technologies" beyond Alpaca, and the sponsor list is published at kickoff. This repo
is built so a partner technology drops into the running pipeline without touching
core code.

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
