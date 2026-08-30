"""Two jobs, two models. One key unless you choose otherwise.

The events book asks a language model two questions that could hardly be less
alike, and until now handed both to the same client:

  * **The watch triage.** Runs on every new batch, three times a session,
    across every name inside the window - call it 40 calls on a busy day. The
    question is narrow and schema-bound: is this batch material, and which way
    does it point. Most batches are noise and the correct answer is a low
    number. A small model does this well and does it cheaply.

  * **The direction call.** Runs once per name, on the afternoon the money goes
    down, on the full week of accumulated evidence. Its confidence sets the
    position size. This is the call worth paying for.

So each role gets its own `model`, and - because a key is authentication rather
than model selection - its own `api_key_env` too, defaulting to whatever
`agents.llm` already uses. A second Featherless key buys nothing functional
(same catalogue, same account), but it does buy separate usage accounting per
role and the ability to revoke one without touching the other, which is a fair
reason to want one. Setting either field to null inherits from `agents.llm`,
which is what every existing deployment does.

`DirectionParams.model` and `.seed` have existed in the YAML since the book was
written, with a comment promising exactly this, and nothing read them. That is
the same shape of failure as the unnoticed Anthropic key: config that looks
configured and is not. This module is what makes those fields load-bearing.
"""

from __future__ import annotations

from typing import Any

from oaa.core.logging import get_logger

log = get_logger("strategies.events.llm")


def role_llm(
    base: Any,
    *,
    role: str,
    model: str | None = None,
    api_key_env: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    seed: int | None = None,
    fallback: Any = None,
) -> Any:
    """A client for one role, or `fallback` when the role overrides nothing.

    `base` is the `agents.llm` config block. Nothing is mutated: the override
    is a copy, so the main loop's client is untouched whatever this book does.

    Returning the shared client when there is no override matters more than it
    looks. Building a second client per role would double the connection
    handling and, on a provider that cold-starts, the warm-up - for two clients
    that are byte-for-byte identical.
    """
    # Only a different MODEL or a different KEY makes this a different agent.
    # Temperature, token cap and seed are refinements that ride along with one
    # - on their own they must not cause a client to be built, for two
    # reasons. A caller that injected a client (every test, and the CLI) would
    # silently have it replaced by one built from global config; and in
    # production a role that named nothing would still get a second connection
    # and a second cold start for a client identical to the shared one.
    if not model and not api_key_env:
        return fallback

    overrides = {
        k: v for k, v in {
            "model": model,
            "api_key_env": api_key_env,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "seed": seed,
        }.items() if v is not None
    }

    from oaa.agents.llm import get_llm

    cfg = base.model_copy(update=overrides) if hasattr(base, "model_copy") else base
    client = get_llm(cfg)
    log.info(
        "events %s model: %s / %s%s",
        role,
        getattr(client, "provider", "?"),
        overrides.get("model", getattr(base, "model", "?")),
        f" (key from {api_key_env})" if api_key_env else "",
    )
    # A role that names its own key and gets a null client back has almost
    # certainly named a variable that is not set. Silence here would look
    # exactly like a cautious model: no notes, no trades, no errors.
    if api_key_env and getattr(client, "provider", None) in {None, "null"}:
        log.warning(
            "events %s: %s produced no usable client - is %s set? Falling back "
            "to the shared agents.llm client.", role, overrides.get("model", "?"),
            api_key_env,
        )
        return fallback
    return client
