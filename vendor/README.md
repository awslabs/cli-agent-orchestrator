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

## Guarded picomatch backport

`cao-picomatch-guard-4.0.7-cao.2.tgz` is built from the **official npm
`picomatch@4.0.7` archive**, corresponding to [tag `4.0.7` at `6bb40679`](https://github.com/micromatch/picomatch/tree/6bb40679c218cefba5d4e9662408c5fdf699a8bb).
It retains the original MIT license and copyright notice. No runtime source file
except `lib/parse.js` differs from the release. The package metadata marks this
as a private CAO backport, not an upstream fix.

[The complete patch](picomatch-4.0.7.patch) backports the three single-branch
guard changes from [the MIT-licensed fix at `3ab5a762`](https://github.com/ryotsu-kankichi/secured-picomatch/commit/3ab5a7627d480259e3d9ca67fbc14c125d374f0e)
and extends the release's checks to quoted and multi-character literal alternatives
and to character classes that admit path separators.
It does not include that fork's unreleased bracket-negation, globstar, or POSIX
punctuation changes. No install or build step downloads the fork.

To reproduce the archive, run from the root of this checkout on Linux:

```bash
set -eu
vendor="$PWD/vendor"
work=$(mktemp -d)
cd "$work"
curl -fsSLo upstream.tgz https://registry.npmjs.org/picomatch/-/picomatch-4.0.7.tgz
echo 'a9c26ef3c436216a89b030f9dbd24a31dc069bf76f2275b81ef427470887f49b62849bf318eb1c0ed1c4df1d6904a794393cac43c91598b9c9da426eef285c18  upstream.tgz' | sha512sum -c -
tar -xzf upstream.tgz
cd package
git apply -p1 "$vendor/picomatch-4.0.7.patch"
corepack npm@10.9.4 pack --ignore-scripts --pack-destination "$work"
cmp "$work/cao-picomatch-guard-4.0.7-cao.2.tgz" "$vendor/cao-picomatch-guard-4.0.7-cao.2.tgz"
```

The four lockfiles record the archive SHA-512. The separate postinstall patch for
legacy Windows callers is not part of this archive. See
[SECURITY.md](../SECURITY.md#guarded-picomatch-parser) for guard behavior and upstream monitoring.
