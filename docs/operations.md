# Operations

## Rollout and reversal

| Stage | Action | Abort or reverse |
| --- | --- | --- |
| Preview | Run bootstrap with `--plan`; review every intended change and check that a first-install plan has no `A first install must start every role in dry-run` error. | Stop if the plan has errors, an unknown App identity, an unsafe ruleset state, or an unexpected file change. |
| Install | Run bootstrap without `--plan`; review its branch and draft pull request. Set secrets yourself in the listed scopes. | On partial failure, use the printed rollback commands. Rerunning the same command resumes the journal. |
| Validate | Run `./bootstrap.sh --check --repo your-org/your-repo`; resolve doctor errors and investigate CI exposure. | Do not switch any role to live while doctor reports errors or an unresolved exposure. |
| Dry-run | Exercise issue triage, review, and fix on representative events; review summaries, artifacts, labels, and proposed patches. | Disable all roles with `AGENTS_ENABLED=false`; keep or restore each `mode` value to `dry-run`. |
| Live | After maintainers approve the setup PR and review dry-run outputs, change only the intended role to live. Human review and merge remain required. | Set `AGENTS_ENABLED=false` immediately for an incident; rotate reached credentials and review App-authored refs before re-enabling. |
| Template update | Review the upstream template diff and update consumer action pins in a normal pull request. | Revert the consumer PR or pin the last reviewed release; do not push changes directly to the default branch. |

## Gate reasons and fixed messages

The catalog includes every reason `lib/agent_repo/gate.py` and its command
wrapper can emit. `tier2-cap` and `tier2-check-failed` are additional triage
decision-handoff reasons from `decide.py`. Gate's `run` output is true for
`ready`, `agent-off`, `tier2-global-cap`, and `tier2-author-cap`; `agent-off`
records the authorized stop command. On a Tier-2 cap, the workflow still runs
at Tier-1, and decide may hand off to a maintainer with `tier2-cap`.

| Reason | Cause | Operator action |
| --- | --- | --- |
| `disabled` | `AGENTS_ENABLED` is not `true`. | Leave disabled during an incident; enable only after review. |
| `invalid-metadata`, `gate-error` | Event, policy, or gate input could not be read or validated. | Check the workflow event and default-branch config; use the fixed error below to identify output failures. |
| `non-default-base` | A `pull_request_target` event targets a non-default branch. | Retarget or review manually; policy reads are bound to the default branch. |
| `unsupported-event`, `no-op-edit` | The event/action is unsupported or an edit changed neither title nor body. | Use a supported issue, review, or fix event; no-op edits do not start a run. |
| `self`, `bot` | The event came from the agent App or another bot. | No action; bot-originated requests are ignored. |
| `draft`, `not-agent-draft`, `unlinked-issue`, `invalid-agent-base` | The pull request is a draft that is not an eligible agent fix, lacks an issue link, or has invalid `Agent-Base` metadata. | Verify the bot author, `agent/fix-<n>` branch, linked issue, and single valid `Agent-Base` trailer. Do not bypass the gate. |
| `skip-label` | `agent:skip` is present. | Keep the issue skipped or have a maintainer remove the label after review. |
| `wrong-command`, `unauthorized`, `third-party-comment`, `not-needs-info`, `needs-review-command` | The comment command does not match the workflow, sender lacks permission, comment is not by the issue author, `needs-info` is absent, or the review needs a maintainer command. | Correct the command or have an authorized maintainer request the workflow. Writers need effective `admin` or `write` permission. |
| `off` | A trusted bot off-marker exists. | Keep automation off, or have a maintainer remove the marker after the incident review. |
| `ready`, `agent-off` | A request is eligible, or an authorized `/agent off` was accepted. | Continue the eligible workflow, or confirm the off-marker was recorded. |
| `cycle-cap` | The issue used its configured triage run cap within retained markers. | Wait for the marker window to expire or have a maintainer triage manually. Markers are retained 90 days. |
| `debounce` | An issue edit arrived within the configured edit debounce interval. | Let the debounce period pass; do not create repeated edits to force a run. |
| `tier2-global-cap`, `tier2-author-cap` | The repository-wide or per-author daily Tier-2 limit was reached. | The workflow continues without Tier-2. Decide may hand off to a maintainer with `tier2-cap`; otherwise wait for the next UTC day to regain Tier-2 capacity. |
| `tier2-cap`, `tier2-check-failed` | Tier-2 is capped or its decide-job re-check failed. | Review the maintainer handoff; check `actions: read` and artifact counter availability. |
| `request-budget`, `counter-overflow` | The gate exhausted its GitHub request budget or could not safely page through counter artifacts. | Retry after investigating API volume and artifact state; do not raise limits blindly. |
| `api-error-401`, `api-error-403`, `api-error-429`, `api-error` | GitHub returned an authentication, permission, rate-limit, or other API error. | Check App/token permissions and GitHub API availability; wait on 429 before retrying. |

Fixed command-wrapper messages:

| Message | Cause | Action |
| --- | --- | --- |
| `Gate input could not be loaded.` | Event, repository config, or trusted inputs could not be read. | Check the event files and default-branch `.agents` files. |
| `Gate outputs could not be written.` | GitHub Actions output file is unavailable. | Check runner state and file-command permissions. |
| `Gate summary could not be written.` | The fix gate could not append its job summary. | Inspect the runner's step-summary file and permissions. |
| `Fix was not run: {reason}.` | The fix gate refused the request for the listed reason. | Follow the matching reason row; do not infer a successful fix from a skipped job. |

## Schema evolution

The versioned JSON artifacts are `canary.json`, `decision.json`, `fix.json`,
`preflight.json`, and `review.json`; each carries `schema_version` and is
validated by its reader. Evolve their schemas additively. Removing a field
requires a major version and a prior deprecation release. Readers reject an
unknown major version. Propose and apply changes move in lockstep.
`triage-context.json` is unversioned; its reader requires exactly six string
fields: `config`, `charter`, `areas`, `labels`, `contributing`, and
`docs_index`. It rejects added fields, so change its producer and consumer
together.

## Triage workflow notes

- The `agent-triage.yml` propose (decide) job needs `actions: read` for the
  Tier-2 cap re-check.

## Fix workflow notes
- Both fix `work-path` and `policy-path` checkouts use
  `persist-credentials: false`. `commands.setup` must install dependencies
  inside the work tree; `commands.test` and `commands.lint` must not depend on
  `HOME`.
- On `ubuntu-24.04`, fix-agent preflight runs
  `sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0` itself and
  retries bwrap. The runner must permit passwordless sudo; otherwise preflight
  fails closed instead of running unsandboxed.
- Both fix checkouts set `submodules: false`. The fix-agent propose step refuses
  persisted credential settings and URL userinfo matching `://...@`; the fix
  applier protects `.gitmodules`. Submodules are not fetched, so there is no
  separate SSH-submodule refusal.
- Preflight analyzes GitHub Actions workflows only. Other CI systems that
  build `agent/fix-*` branches are outside its coverage.
- Rerun doctor after changing environment deployment policies, the default
  workflow-token permissions, `AGENT_SAFE_ENVIRONMENTS`, or
  `AGENT_CI_DEFAULT_TOKEN_READONLY`; fix apply trusts those recorded values
  between doctor runs.
- Any CI exposure currently yields
  `UNSAFE ON SHARED APP: patch retained; no push.` The config flag
  `fix.accept_ci_exposure` cannot override this fix-apply behavior.

## Dependency updates

Dependabot scans root workflow files and composite action directories using
the `github-actions` ecosystem, plus the Python `uv` dependencies. Its
`directories` option supports `*`; GitHub's Actions reference uses `/` for
workflow files and documents root action metadata scanning in the
[Dependabot options reference](https://docs.github.com/en/code-security/reference/supply-chain-security/dependabot-options-reference#directories-or-directory).
The consumer template source uses `@__TEMPLATE_SHA__` and
`#__TEMPLATE_VERSION__` placeholders, which bootstrap replaces when rendering
the installed workflows. Installed consumer workflows instead contain a
resolved 40-hex action SHA and a `# vX.Y.Z` comment. Once each month, review the
upstream release and security changes, update each installed action SHA and
version comment together in a pull request, then run doctor and CI. Review
critical security updates as soon as they are announced.
