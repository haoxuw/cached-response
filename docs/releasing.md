# Releasing to PyPI

The package has not yet been published to PyPI. Build and inspect it before
uploading:

```sh
python -m pip install build twine
python -m pytest
python -m build
python -m twine check dist/*
```

Use a PyPI account with a verified email and two-factor authentication. Its
username can differ from the GitHub owner `haoxuw`. Author metadata currently
uses `haoxuw` and the license is MIT; no public personal email is required here.

For automated releases from `haoxuw/cached-response`, register a PyPI pending
Trusted Publisher with the package name, repository owner/name, workflow filename,
and optional GitHub environment. That allows a first release without a long-lived
upload token. See [PyPI Trusted Publishers](https://docs.pypi.org/trusted-publishers/).
A local release can instead use a PyPI API token supplied through a credential
store; do not put credentials in source code or chat.

Check the package name again before publishing. A missing public project does not
guarantee PyPI will accept its name. Publication and a remote repository push need
the owner's authorization; building locally does not publish anything.

Ship only library code, tests, and public documentation. Keep private captures,
SQLite files, credentials, and private inputs out of both distributions and the
remote repository. Once published, increment the version for the next release.
