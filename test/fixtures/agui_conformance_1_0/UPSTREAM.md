# AG-UI 1.0 conformance corpus (vendored, unmodified)

These files are copied unchanged from
[ag-ui-protocol/ag-ui](https://github.com/ag-ui-protocol/ag-ui) at commit
`08eae9dc13546e91272efb2d0ef863e21acc6ee3`:

- `streams/*.json` and `streams/MANIFEST.txt` are from `spec/1.0/conformance/streams/`
  (68 fixtures; the MANIFEST lists them).
- `README.upstream.md` is upstream `spec/1.0/conformance/README.md`.
- `LICENSE.upstream` is the upstream MIT license, which covers these files.

The repo uses them to self-test the Python pre-check in
`test/_support/agui_conformance.py`, and in the AC3 job's
`verify-ambient.mjs` self-test. Refresh them only by copying from a newer upstream
commit and updating the sha above.
