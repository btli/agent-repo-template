# Get started

Use this checklist to preview an install with an existing owner GitHub App.
Allow about 10–15 minutes. Plan mode reads the target repository and prints
the intended changes; it does not create a branch, write settings, or install
the App.

## Before you start

- Check out this template on a machine with Python 3.11 or newer, the GitHub
  CLI (`gh`), and Mike Farah `yq` v4.
- Sign in to `gh` as a repository administrator for the target repository.
- Have an App owned by the target user or organization, its App slug and Client
  ID, and an HTTPS model gateway URL. Confirm the App is installed on the target
  repository in the App's installation page; doctor does not verify installation.
  Do not put an App private key or model key on the command line.
- Keep `.agents/config.yml` roles in `dry-run`; this is the template default and
  first install refuses a live mode.

## Preview an install

From the template checkout, substitute your owner, repository, App identity,
gateway details, and a 40-hex commit that is on this template's upstream `main`.
Before a release is available, use `--candidate` with `--version`; for a
published release use `--ref vX.Y.Z` instead. Do not combine `--ref` and
`--candidate`.

```sh
./bootstrap.sh --repo your-org/your-repo --candidate <40-hex-main-commit> --version v0.1.0 --app-slug repo-agent --app-client-id Iv23liExample --gateway-url https://gateway.example.com --gateway-kind litellm --system-one-url https://system-one.example.com --plan
```

If you need an App registration link, this command prints a prefilled GitHub
App form with the union of the role permissions and no webhook:

```sh
./bootstrap.sh app-link --owner your-org
```

For a first install, check that the preview has no
`A first install must start every role in dry-run` error. Bootstrap does not
print each role's mode or verify the App installation. Its notes include
`Install the GitHub App on <owner>/<repo> only` with an installation link. For
a new install, planned changes list the four `agent-propose-triage`,
`agent-propose-review`, `agent-propose-fix`, and `agent-apply` environments,
the branch and tag rulesets, and a `bootstrap/agent-repo-template-<version>`
draft-PR branch when files need updating. Already-correct resources may not
appear in the change list. Bootstrap prints `gh secret set` commands for the
required scopes but never reads, creates, or prints secret values. A private
App's `GET /apps/{slug}` returning 404 is expected; bootstrap checks the App
bot account and supplied Client ID, not installation access.

To install after reviewing the preview, run the same bootstrap command without
`--plan`. It creates or updates configuration and settings, then opens a draft
pull request for rendered files. Enter the printed secret-setting commands in
the intended repository or environment scope, then run the doctor:

```sh
./bootstrap.sh --check --repo your-org/your-repo
```

Doctor checks repository and environment secrets by name; it does not count
organization secrets. Do not enable live mode until doctor reports no errors,
the CI exposure check is acceptable, and maintainers have reviewed the draft
pull request.

### What bootstrap changes

Bootstrap manages labels, four deployment environments, non-secret repository
variables, and a pair of rulesets. It never changes the repository's default
workflow-token permission, and it opens a draft pull request instead of
pushing to the default branch. `--plan` is the read-only preview;
`--force` permits replacing managed files and should be used only after
reviewing the preview.

If the target repository has no App yet, register and install the App before
re-running the preview. See [GitHub App setup](github-app.md) and the
[security runbook](security.md).
