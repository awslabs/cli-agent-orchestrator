# Agent Plugins dog-food — shift-left recorder

Build/CI tooling that produces a **gated** proof-of-work GIF for the Agent
Plugins pipeline. It is not required to install or use agent plugins.

## What it does

It runs the asserting example
[`../run.sh`](../run.sh) — CAO installing its own `cao` agent plugin through its
own new plugin pipeline — captures the terminal output, renders it into a
terminal-styled page recorded by headless Chromium, and exports an optimized GIF
to `docs/media/agent-plugins-dogfood-demo.gif`.

The example runs **offline by default**: no provider binary, network, or
secrets, under a scratch `HOME`/`CAO_HOME_DIR`, so no real config is read or
written and no real home path is rendered.

## The shift-left gate

The recording **is** the test. A GIF is only exported if `run.sh` exits `0` and
prints its `[agent-plugins-dogfood] PASS` marker. If the pipeline regresses (a
skill stops projecting, `PLUGIN_ROOT` stops expanding, the `x-cao-pre-expanded`
marker leaks into a provider config, the OpenCode collision guard stops firing,
or removal stops withdrawing/disabling the server), `run.sh` exits non-zero, the
recorder exits non-zero, and the CI job
`Agent Plugins dog-food (shift-left recording)` goes red. A broken pipeline
cannot produce a green recording.

## Running

```sh
cd examples/agent-plugins/agent-plugins-dogfood/tools
npm install
npm run playwright:install
npm run record                              # offline (what CI records)
CAO_DOGFOOD_LIVE=1 npm run record           # + live OpenCode observational step
```

The live step adds the observational OpenCode proof (`opencode mcp list` reports
the removed server as `disabled` and a sentinel confirms no subprocess spawns).
It needs the `opencode` binary, which is not on CI, so CI records the offline
path. See [`../README.md`](../README.md) for the full offline-vs-live matrix.

`ffmpeg` is provided by the `ffmpeg-static` npm package (gif-capable), so no
system ffmpeg install is needed. Override with `FFMPEG_BIN=/path/to/ffmpeg`.

The GIF is committed under `docs/media/` and re-generated + uploaded as the
`agent-plugins-dogfood` CI artifact on every run.
