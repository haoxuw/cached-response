# Working on cached-response

Keep this a small, general-purpose Python package. Public usage examples should
work locally without credentials. Keep private captures, application-specific
benchmarks, databases, and generated distributions out of commits.

## Package behavior

- Preserve the import-and-decorator interface and document changes to defaults.
- LLM caching defaults to disabled. Similarity can find candidates, but cannot
  authorize reuse. Preserve caller isolation and identifier relationships.
- Configure only the package's logger hierarchy. Keep console output opt-in and
  diagnostic text redacted by default; never reconfigure the importing app.
- Keep runtime code under `src/cached_response`, tests under `tests`, minimal
  examples under `examples/minimal`, and detailed reference docs under `docs`.

## Pull requests

Use the repository's [create-pr skill](.agents/skills/create-pr/SKILL.md) before
creating or updating a PR. Require a concise description, reproducible steps with
expected results, relevant validation, and an honest account of review findings.
Keep the PR in draft when the user asks to review a draft.

Check the current diff and related open PRs before starting overlapping work.
Fetch the target branch, preserve local changes, and use a topic branch. Keep
changes focused; include the tests and documentation needed to review them.

## Checks

Use `.github/workflows/ci.yaml` as the source of truth for automated checks:

```sh
python -m ruff check src examples
python -m ruff format --check src examples
python -m pytest -q
```

For package/API changes, also build wheel and source distributions, validate
their metadata, and exercise the installed wheel in an isolated environment.
Distinguish local tests, recorded-input replay, live tests, and remote CI results.
Do not repeat an unchanged successful check without a new reason.
