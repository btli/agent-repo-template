# Larkspur project charter

Larkspur is a fictional Python library that loads, merges and validates layered
configuration files. It is the worked example used by the golden evals.

## Mission

Give Python applications one predictable way to load layered configuration and
report every validation error with the file and line that caused it.

## In scope

- Parsing TOML, YAML and INI files into one merged configuration tree.
- Schema validation with typed errors that name the file, line and key.
- Merge order, environment-variable overrides and default values.
- The `larkspur check` command-line tool.
- Documentation, examples and type hints for the public API.

## Out of scope

- Remote configuration stores such as key-value services or secret managers ([docs](https://example.org/larkspur/faq#remote-stores))
- Graphical configuration editors ([docs](https://example.org/larkspur/faq#editors))
- Support for Python versions that have reached end of life ([docs](https://example.org/larkspur/support#python-versions))

## Priorities

- P0: Silently wrong configuration values, data loss, or a crash on every load.
- P1: A common workflow is unusable for many users and has no practical workaround.
- P2: A defect in a limited workflow that has a practical workaround.
- P3: Cosmetic issues, documentation polish or a small convenience.

## A good report includes

- The Larkspur and Python versions.
- A minimal configuration file and code snippet that reproduce the problem.
- The expected result and the actual result, including the full error message.

## Support policy

Maintainers support the latest minor release. Usage questions belong in the
discussion forum. Report security issues through the private process in SECURITY.md.

## Docs index

- [User guide](https://example.org/larkspur/guide)
- [Merge order](https://example.org/larkspur/guide/merge-order)
- [Schema reference](https://example.org/larkspur/reference/schema)
- [Command-line tool](https://example.org/larkspur/reference/cli)
- [FAQ](https://example.org/larkspur/faq)
- [Support policy](https://example.org/larkspur/support)
