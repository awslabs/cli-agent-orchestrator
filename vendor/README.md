# Vendored dependency archives

## Guarded braces fork

`dieub-braces-depth-guard-3.0.3-pn.3.tgz` is the unmodified published archive of
`@dieub/braces-depth-guard@3.0.3-pn.3`, used by the docs, web UI, and MCP Apps.
Keeping these bytes under repository control avoids a hard install dependency
on a third-party owner's continued npm publication.

- Original [registry tarball](https://registry.npmjs.org/@dieub/braces-depth-guard/-/braces-depth-guard-3.0.3-pn.3.tgz).
- Immutable [source commit `305a2e4bfe324bb53c336c1b03387ee1251c926f`](https://github.com/dieub/braces-depth-guard/tree/305a2e4bfe324bb53c336c1b03387ee1251c926f);
  all ten archive files match that commit.
- License: **MIT**. The original copyright notice and license are preserved in
  `package/LICENSE` inside the archive.
- Original registry SHA-512 integrity, also retained in all three npm lockfiles:

  ```text
  sha512-QY+Uq4s42STyIMPoRkBuUZfYyvz0uZuwuUburLwMx5N+lWqnHHaBxcKPtgKVKjTyFnS1q4ivKu9Wxi4VG7FE9Q==
  ```

The archive was obtained with:

```bash
npm pack @dieub/braces-depth-guard@3.0.3-pn.3 --ignore-scripts --pack-destination vendor
```

Installations use the local archive, not this command or the registry URL.
Do not repack, edit, or rename the package's metadata. The existing
[`patch-package` patch](../patches/@dieub+braces-depth-guard+3.0.3-pn.3.patch)
is still applied after installation. Regenerate affected lockfiles with npm
when deliberately changing an archive, and verify package identity, license,
integrity, scanner coverage, and all consumers.

For recovery, restore this tracked archive from the same trusted repository
revision as its lockfiles. Do not substitute another registry tag or create a
different tarball under this version. See
[SECURITY.md](../SECURITY.md#guarded-fork-maintenance) for the upstream-advisory
limitation, concrete maintenance checkpoints, and eventual replacement policy.
