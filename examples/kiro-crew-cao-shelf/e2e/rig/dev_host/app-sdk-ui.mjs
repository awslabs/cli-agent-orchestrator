// `make dev` serves the same '@kirocrew/app-sdk/ui' stub the component tests use, so the two cannot
// drift (ui/test/sdk-surface.test.ts checks this file). esbuild compiles the TSX with ui/tsconfig.json.
export * from "../../../ui/test/sdk-stub/ui.tsx";
