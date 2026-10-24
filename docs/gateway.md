# Model gateway

The template uses an adopter-owned HTTPS gateway for Claude-family model ids.
`AGENT_GATEWAY_KIND` accepts `databricks` or `litellm`; `AGENT_GATEWAY_URL` is
the HTTPS base URL. The setup action appends `/anthropic` for `databricks` and
uses the URL as-is for `litellm`. Gateway URLs must not contain credentials,
queries, or fragments.

Create a separate virtual key for each role: `AGENT_TRIAGE_KEY`,
`AGENT_REVIEW_KEY`, and `AGENT_FIX_KEY`. Store each key in its corresponding
proposal environment. Use a distinct `AGENT_SYSTEM_ONE_KEY` in
`agent-propose-triage` when the config enables System One. The setup action
accepts only Anthropic-family model ids and does not write key values into its
gateway configuration.

## Budgets and alerts

Configure the gateway to enforce per-key spend and rate limits, and a per-run
token budget for each request chain. Alert at 80% of budget and attach the
workflow run id to gateway usage records so an operator can find the matching
Actions run. These are gateway-side controls: merged main does not enforce
gateway spend or rate limits and does not ingest a gateway budget-alert feed.

Triage additionally enforces repository-wide and per-author daily Tier-2 caps
with workflow artifacts. The decide job needs `actions: read` to re-check
those caps. A cancelled pending Tier-2 job may already have spent its cap
counter, so a small overshoot is an accepted residual. Fix has no separate
daily cap of its own. Do not treat a gateway alert as an automatic disable
signal; use the operational kill switch in the [runbook](operations.md).

## System One and LLM-only

System One providers in the merged client are `jev`, `databricks-openjev`,
and `gateway` (LiteLLM's decisions endpoint). The repository config's
`system_one.provider` and `system_one.model` select the provider and model;
`AGENT_SYSTEM_ONE_URL` supplies the HTTPS endpoint. The
`AGENT_SYSTEM_ONE_KEY` is required only when System One is configured.

Removing `system_one` selects LLM-only triage. It skips System One and depends
on the triage model path for issue classification; expect different model
usage and budget behavior. No System One key is required. Doctor reports
LLM-only mode and warns when `close.enabled` is true, because automatic close
decisions require extra care without the System One comparison.

The config's `models.triage`, `models.review`, and `models.fix` fields seed
`AGENT_TRIAGE_MODEL`, `AGENT_REVIEW_MODEL`, and `AGENT_FIX_MODEL` at install.
Workflows read the repository variables. Doctor reports drift; after a
reviewed config change, use `./bootstrap.sh --sync-models --repo your-org/your-repo`
to update only those three variables.
