# Changelog fragments

Add one fragment per pull request and type: `changelog.d/<PR>.<type>.md`,
where `<PR>` is the pull request number and `<type>` is exactly one of `Added`,
`Changed`, `Deprecated`, `Removed`, `Fixed`, or `Security`. Write one Markdown
bullet on one line in each file, for example:

```text
changelog.d/1350.Changed.md
- Release notes are collected from pull-request fragments.
```

The release bump combines fragments in type order, then numeric PR order, with
any hand-written `[Unreleased]` notes in `CHANGELOG.md`, adding `(#<PR>)` to
an entry when its PR number is not already cited. It removes collected
fragments after the version bump succeeds. GitHub generated release notes remain
unchanged.

If a hand-run bump is interrupted by SIGKILL, reset the partial changes with
git before re-running it; ordinary errors roll back automatically.
