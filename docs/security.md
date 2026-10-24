# Security runbook

Give every role a separate, revocable key. Keep role keys in the matching
GitHub environment, limit access to two named maintainers, and use a second
maintainer to verify credential rotation and the follow-up doctor check.
Never paste credentials into issues, workflow inputs, or logs.

## Detection and response targets

| Credential or signal | Detection | Revoke or contain target |
| --- | --- | --- |
| `AGENT_TRIAGE_KEY`, `AGENT_REVIEW_KEY`, or `AGENT_FIX_KEY` | Gateway key usage by role and run id; unexpected spend, rate, or geography; outbound scan refusal | Disable the affected virtual key within 15 minutes, issue a replacement, update its environment secret, and confirm a dry-run workflow. |
| `AGENT_SCAN_KEYS` and the affected role key | Fix/apply scan refusal, leaked-key alert, or suspicious model output | Rotate the role key and `AGENT_SCAN_KEYS` together within 15 minutes so the scanner recognizes the replacement and the exposed value is revoked. |
| `AGENT_APP_PRIVATE_KEY` | GitHub audit events, unexpected App-authored refs or pull requests, doctor findings, or unusual installation API activity | Revoke and replace the App key within 30 minutes. Run doctor for every repository where the App is installed, update each `agent-apply` environment, and verify all before resuming agents. |
| Preflight read token | Unexpected workflow access or a token exposure in a job using the preflight action | Cancel the owning run immediately; its GitHub Actions token is short-lived. Confirm the job has only the repository read permissions it needs. Fix apply's preflight uses the apply App token, so treat an exposure there as an App-token incident. |
| CI exposure or shared App | `./bootstrap.sh --check --repo your-org/your-repo`, preflight findings, or the exact shared-App warning below | Disable agent workflows immediately, isolate the App installation, then remove the exposed workflow path and rotate any reached credentials before re-enabling. |

For every rotation, one maintainer revokes or replaces the credential and the
second confirms the old value is unusable, the new value is stored in the
intended scope, and doctor is clean. Disable all agent workflows with
`AGENTS_ENABLED=false` while containment is in progress.

## Shared-App warning

Doctor prints `UNSAFE ON SHARED APP` when consumer CI has an ordinary credential
exposure and the App is confirmed to reach all repositories of an organization
or its reach cannot be read with maintainer credentials. The latter is
reported as **assumed** shared; an organization App with
`repository_selection=all` is **confirmed** shared. Doctor checks repository
and environment secrets by name, not organization secrets.

Fix apply currently prints `UNSAFE ON SHARED APP: patch retained; no push.` for
every CI exposure. A repository-scoped App token cannot prove installation-wide
reach, so `fix.accept_ci_exposure` cannot override this behavior until a later
installation-endpoint verification change. Review the retained patch as
untrusted input.

## Accepted residual risks

- Trusted `commands.setup` runs outside bwrap. A compromised setup dependency
  can tamper with the runner and may read `AGENT_FIX_KEY` through an ancestor's
  initial environment. Keep the fix key low privilege and rotate it promptly.
  The egress block limits destinations; it is not an exfiltration control for
  setup.
- The current template disables the setup action's dependency cache and apply
  does not restore a cache. A consumer workflow that introduces shared caches
  must treat them as untrusted: read-only API permissions do not prevent cache
  poisoning for a later privileged job.
- Tier-2 counters may overshoot when a pending job was cancelled after spending
  its counter. Triage cycle markers are retained for 90 days, so its per-issue
  cap only sees markers within that retention window.
- Commit-pinned template and third-party action code is still trusted code at
  the selected SHA. Review updates and follow the manual template-pin cadence
  in [operations](operations.md#dependency-updates).
- Outbound scans can miss encodings such as base32 or rot13. A clean scan is
  not proof that arbitrary model output contains no transformed secret.
- A workflow's default-branch deployment policy does not make setup safe from
  every platform or runner compromise. Whether a sudo-capable step can read
  other steps' secrets is unverified; protect the environment and runner as
  credential-bearing infrastructure.

## Preflight and update exposure

Treat preflight findings as a stop signal. Do not use `fix.accept_ci_exposure`
to dismiss a finding. If a preflight or fix report records exposed secret
names, treat each named credential as compromised: rotate it using the table
above, remove the accepted-exposure setting, and review the workflow path
before another run. The accepted `template-fix-propose` exposure is recorded
by doctor and in fix output, names `AGENT_FIX_KEY`, and trusts the pinned
template code. The template also refuses `.gitmodules` changes and Git checkout
URL userinfo; use HTTPS submodule URLs without embedded credentials.
