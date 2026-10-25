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
Set the `agent-apply` environment's `AGENT_SCAN_KEYS` from the literal role-key
values as described in [secret setup](getting-started.md); do not use a
placeholder or leave a stale value when rotating a key.

## Budgets and alerts

Configure per-key token and rate limits on the gateway and alert at 80% of the
key's budget. Triage, review, and fix include the run ID in model prompt text,
but the gateway request has no structured run-ID metadata. Per-run limits and
reliable run correlation are unavailable; correlate by prompt content only if
the gateway retains it, or otherwise compare key usage by time with Actions
runs. The template does not automatically ingest gateway alerts into live
Tier-2 caps.

For a dry-run evaluation, count the 80% alerts received by each key in each
week, then pass the highest weekly per-key count to
`python3 scripts/dry-run-report.py --repo your-org/your-repo --bot-login '<app-slug>[bot]' --tier2-budget-alerts N`.
Use the value of `AGENT_BOT_LOGIN` for `--bot-login`; its format is
`<app-slug>[bot]`.
The report flags an abort criterion when one Tier-2 key reaches 80% of budget
at least twice in a week; its prescribed response is to set
`triage.rca_authors: known`. This manual evaluation input is separate from the
live repository-wide and per-author Tier-2 caps.

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
