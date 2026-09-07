# Releases

CI runs the tests and checks code style on every push to `main` and every pull
request, using Python 3.11–3.14 on Linux.

To publish a version:

1. Update `version` in `pyproject.toml`, commit, and push to `main`.
2. Wait for CI to pass.
3. Tag that commit with its version and push the tag:

```sh
git tag v0.1.0
git push origin v0.1.0
```

Use a new version for every release. The `release.yaml` workflow checks that the
tag matches the package version, runs tests, builds a wheel and source archive,
and tests the built wheel. A separate job uploads those files to PyPI. A final
job installs the published package from PyPI and runs both minimal examples.

Publishing uses the `pypi` GitHub environment and PyPI Trusted Publishing. The
registered workflow filename must be `release.yaml`. No upload token is needed.
See [PyPI's instructions](https://docs.pypi.org/trusted-publishers/using-a-publisher/).

Only version tags publish; ordinary commits and pull requests cannot publish.
To retry a failed release, rerun its failed jobs in GitHub Actions. Avoid rerunning
a successful upload: PyPI does not allow replacing an existing release file.
