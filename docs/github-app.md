# GitHub App

Use one App per user or organization by default, and install it only on each
repository that runs the agent. A separate App identity limits the impact if
one consumer repository exposes its credentials. Bootstrap can print a
prefilled registration link with `./bootstrap.sh app-link --owner your-org`;
the App has no webhook because GitHub Actions receives the events.

## Permissions

The App's default permissions need the union shown below. Each job mints a
short-lived installation token with only its own permissions; metadata read is
implicit in GitHub App tokens.

| Job | Token permissions |
| --- | --- |
| Triage apply | `issues: write`, `contents: read` |
| Review apply | `pull-requests: write`, `contents: read` |
| Fix apply | `contents: write`, `pull-requests: write`, `issues: write` |

The review bot token is exactly `pull-requests: write`, `contents: read`, and
metadata read. `AGENT_APP_PRIVATE_KEY` belongs in the `agent-apply`
environment. `AGENT_APP_CLIENT_ID` is a repository variable. The bot login
variable `AGENT_BOT_LOGIN` must be the App's canonical login in
`<app-slug>[bot]` form.

## Branch and tag rulesets

Bootstrap owns a branch and a tag ruleset. The branch ruleset uses `~ALL`,
excludes `agent/**`, and restricts creation, updates, deletion, and
non-fast-forward updates. The tag ruleset uses `~ALL` and restricts creation,
updates, and deletion. Bypass is limited to human repository roles; the App is
never granted bypass. Bootstrap fails if rulesets are unavailable because it
cannot protect non-agent refs.

The branch creation/update restriction can also block bot branches such as
Dependabot's. Bootstrap does not grant Dependabot bypass. Maintainers must
choose whether to keep that restriction and update dependencies through a
human-reviewed branch, or to grant a narrowly reviewed Dependabot bypass in
repository settings. Do not grant the agent App bypass.

Doctor compares the live rulesets with the bootstrap-owned pair. It also
checks the declared App identity, action pins, and configured credentials by
name, but does not verify App installation. Confirm installation access on the
App's installation page. If the App is shared across an organization, see the meaning of
[`UNSAFE ON SHARED APP`](security.md#shared-app-warning).
