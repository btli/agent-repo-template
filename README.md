# Agent Repository Template

> **Pre-release:** This template is under active development. Do not use it for
> production repositories yet.

This repository will provide a reusable, safety-focused foundation for GitHub
repository agents. See the [MVP specification summary](#mvp-specification-summary)
for the intended capabilities and boundaries.

## MVP specification summary

- Issue triage, pull request review, and issue-to-fix draft pull requests.
- Model jobs propose changes; separate trusted jobs apply them after validation.
- Every role starts in dry-run mode, and a human always merges changes.
- The template is generic and uses an adopter-owned GitHub App and model gateway.
