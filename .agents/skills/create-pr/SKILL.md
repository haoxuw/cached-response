---
name: create-pr
description: Prepare, review, and create or update a cached-response pull request with a concise description and reproducible validation. Use when asked to create a PR or prepare changes for review.
---

# Create a reviewable pull request

## Prepare the change

- Confirm the intended repository, base branch, and scope. Inspect existing local
  work and open PRs; preserve others' edits. Fetch the base and use a topic branch.
- Keep one coherent change per PR unless the user requests combined work. Keep
  related tests and docs with the implementation; omit unrelated cleanup.
- Use a short, concrete title describing the resulting behavior. Follow this
  repository's `feat:`, `fix:`, `docs:`, or `ci:` commit-title convention.

## Review and validate

- Read the complete diff, including new files. Check correctness, API/default
  changes, compatibility, logging isolation, sensitive-data handling, and docs.
- Run the relevant checks from `AGENTS.md`. Exercise the documented reproduction
  using the proposed code. For packaging changes, verify an installed wheel as
  well as the source checkout.
- Fix confirmed findings. Record what was reviewed, remaining questions, and the
  reason for any deferred finding. A bare "reviewed" or "no findings" is not a
  review record. Say whether review was by the author or an independent reviewer.
- Preserve valid earlier evidence; rerun checks when subsequent changes invalidate
  it. Never describe recorded-input replay as a live-model test or pending CI as
  passing. Keep credentials, private captures, and local filesystem links out of
  the public PR; use a small synthetic example or shareable artifact instead.

## Required PR body

Write for a reviewer who has not read the conversation. Prefer one or two short
paragraphs plus the commands needed to reproduce; add detail only when it helps
assess the change. Do not recount the conversation or list every changed file.

- **Summary:** Lead with the problem and resulting behavior. Explain why the
  change is needed and mention any changed defaults. Link a related issue if one
  exists; do not invent one.
- **Steps to reproduce:** State prerequisites and setup, then give ordered steps
  or copy-paste commands. Show expected behavior on this branch and, for a fix,
  the prior failure. Include required flags or sample inputs. A test-suite command
  alone is insufficient when the user-visible behavior can be demonstrated.
  Documentation-only changes can give a concrete inspection procedure and the
  expected checks instead of runtime steps.
- **Validation:** Name the checks actually run and their results. Identify limits
  such as untested platforms, external services, or pending remote CI.
- **Review and risks:** Briefly record review findings and their dispositions,
  material compatibility/privacy limitations, and any requested reviewer focus.

## Publish and follow through

- Use existing user authorization to push the topic branch and create the PR.
  Creating a PR does not authorize merging it or publishing a package release.
- Write the complete body to a UTF-8 file and use `gh pr create --body-file` with
  explicit `--repo`, `--base`, and `--head`. Use `--draft` when requested; do not
  rely on an auto-generated commit summary as the description.
- Check the resulting URL, base/head branches, draft state, and changed files.
  Inspect available CI results, fix failures attributable to this change, and
  report any checks still pending. Keep title, reproduction, and validation
  current after follow-up edits. Leave draft-to-ready and merge decisions to the
  user's requested workflow.

## Sources

These sources support focused changes, informative descriptions, self-review,
and explicit draft creation. The required reproduction section is this
repository's convention.

- [GitHub: help others review your changes](https://docs.github.com/en/pull-requests/concepts/helping-others-review-your-changes)
- [Google: writing good change descriptions](https://google.github.io/eng-practices/review/developer/cl-descriptions.html)
- [Google: small changes](https://google.github.io/eng-practices/review/developer/small-cls.html)
- [GitHub: creating a pull request](https://docs.github.com/en/pull-requests/how-tos/create-pull-requests/creating-a-pull-request)
