# Agent Repository Template

> **Pre-release:** This template is under active development and is not ready
> for production use. Review the [security model](docs/security.md) and run the
> installer in plan mode before enabling it in a repository.

This template installs GitHub Actions for issue triage, pull request review,
and issue-to-fix draft pull requests. Model jobs propose changes; separate
trusted jobs validate and apply them. Every role starts in dry-run mode, and a
human merges the resulting changes.

The installer requires repository-admin access and uses the maintainer's own
GitHub CLI authentication. The consumer repository owns the GitHub App, model
gateway, credentials, configuration, labels, and charter.

## Documentation

- [Get started](docs/getting-started.md) with a dry-run install.
- [Write the repository charter](docs/charter-guide.md).
- [Configure the model gateway](docs/gateway.md).
- [Create and install the GitHub App](docs/github-app.md).
- [Security runbook and residual risks](docs/security.md).
- [Operations, gate reasons, and rollback](docs/operations.md).
