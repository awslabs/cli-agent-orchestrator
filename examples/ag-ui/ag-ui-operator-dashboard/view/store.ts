import { createController, type Board, type Controller, type ControllerOptions, type Panel } from "../core/index.ts";

/** A controller plus a subscribe/snapshot pair for React's useSyncExternalStore. */
export interface Store {
  controller: Controller;
  subscribe(listener: () => void): () => void;
  getSnapshot(): { board: Board; panel: Panel };
}

export function createStore(opts: Omit<ControllerOptions, "onChange">): Store {
  const listeners = new Set<() => void>();
  let snapshot: { board: Board; panel: Panel } | undefined;
  const controller = createController({
    ...opts,
    onChange: (board, panel) => {
      snapshot = { board, panel };
      for (const listener of listeners) listener();
    },
  });
  return {
    controller,
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    getSnapshot() {
      snapshot ??= { board: controller.board, panel: controller.panel };
      return snapshot;
    },
  };
}
