# Charter guide

The charter controls the language and documentation references used during
triage. Start from the worked example in [`evals/charter.md`](../evals/charter.md)
and adapt the consumer files [`config.yml`](../consumer/.agents/config.yml),
[`labels.yml`](../consumer/.agents/labels.yml), and
[`areas.yml`](../consumer/.agents/areas.yml) to your repository.

## Required sections

`lint_charter` requires exactly these section names, a non-empty Mission, and
priorities P0 through P3:

- `## Mission`
- `## In scope`
- `## Out of scope`
- `## Priorities`
- `## A good report includes`
- `## Support policy`
- `## Docs index`

Use one `## Docs index` heading. The repository's docs test gate requires one
in the canonical eval charter. The merged `agent_repo.charter` parser keeps
the last section for a repeated heading, while the triage workflow's context
builder reads the first `## Docs index` section. A repeated heading can
therefore give the two paths different references. Doctor runs
`lint_charter`, which checks required headings and priorities but does not
reject duplicate headings; remove duplicates yourself.

## Match the configuration

- `config.yml` requires the top-level `version`, `mode`, `models`, `thresholds`,
  `close`, `triage`, `limits`, `commands`, and `fix` keys; `system_one` is
  optional. It selects dry-run or live mode, model ids, thresholds, and limits.
- `labels.yml` declares the taxonomy that triage may apply. The example uses
  `type:bug`, `type:feature`, `area:core`, `P0` through `P3`, `needs-info`,
  `needs-maintainer`, `agent:closed`, `agent:fix`, `agent:skip`, and
  `agent:triage-capped`.
- `labels.yml` has the `labels` key. `areas.yml` has the `areas` key; each area
  has a description, path globs, owners, and a `sensitive` key. Keep
  paths narrow enough that reviewers can route changes to the right people.
- Priority descriptions and the `A good report includes` checklist are sent to
  triage. Docs-index links are also candidates for a documentation reference.

The `evals/charter.md` file is the fictional Larkspur example, not a consumer
charter. Doctor validates the consumer `.agents/charter.md` alongside
`.agents/config.yml`, `.agents/labels.yml`, and `.agents/areas.yml`.
