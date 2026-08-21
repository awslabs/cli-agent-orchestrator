// Vitest setup: ensure each test starts with a clean DOM and auto-cleanup of
// rendered React trees.

import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

afterEach(() => {
  cleanup();
});
