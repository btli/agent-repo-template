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

Configure per-key token and rate limits on the gateway and alert at 80% of the
key's budget. The template gives each role a static key, URL, and model; it does
not send a workflow run id to the gateway. The integration therefore cannot
enforce per-run gateway limits or correlate requests with an Actions run.
These limits and alerts are gateway-side controls. Main does not ingest gateway
budget alerts or expose a CLI option that feeds them into Tier-2; triage uses
its own workflow artifact counters.

Triage additionally enforces repository-wide and per-author daily Tier-2 caps
with workflow artifacts. Fix has no daily cap of its own. Do not treat a
gateway alert as an automatic disable signal; use the operational kill switch
in the [runbook](operations.md).

## System One and LLM-only

System One providers in the merged client are `jev`, `databricks-openjev`,
and `gateway` (LiteLLM's decisions endpoint). The repository config's
`system_one.provider` and `system_one.model` select the provider and model;
`AGENT_SYSTEM_ONE_URL` supplies the HTTPS endpoint.

Removing `system_one` selects LLM-only triage. It skips System One and depends
on the triage model path for issue classification; expect different model
usage and budget behavior. LLM-only mode never automatically closes an issue:
`close.enabled` cannot enable an automatic close, and doctor warns
`close.enabled is true in LLM-only mode; auto-close stays off.` If a close
proposal is present, apply presents it to a maintainer for confirmation.
Without System One, an uncertain security answer does not take the dedicated
`security-uncertain` escalation path; it follows ordinary low-confidence
action routing.

The config's `models.triage`, `models.review`, and `models.fix` fields seed
`AGENT_TRIAGE_MODEL`, `AGENT_REVIEW_MODEL`, and `AGENT_FIX_MODEL` at install.
Workflows read the repository variables. Doctor reports drift; after a
reviewed config change, use `./bootstrap.sh --sync-models --repo your-org/your-repo`
to update only those three variables.
