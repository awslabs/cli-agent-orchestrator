import {
  createController,
  type Board,
  type Controller,
  type Fleet,
  type ControllerOptions,
  type Panel,
  type Timeline,
} from "../core/index.ts";

/** A controller plus a subscribe/snapshot pair for React's useSyncExternalStore. */
export interface Store {
  controller: Controller;
  subscribe(listener: () => void): () => void;
  getSnapshot(): { board: Board; panel: Panel; timeline: Timeline; fleet: Fleet };
}

export function createStore(opts: Omit<ControllerOptions, "onChange">): Store {
  const listeners = new Set<() => void>();
  let snapshot: { board: Board; panel: Panel; timeline: Timeline; fleet: Fleet } | undefined;
  const controller = createController({
    ...opts,
    onChange: (board, panel, timeline, fleet) => {
      snapshot = { board, panel, timeline, fleet };
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
      snapshot ??= { board: controller.board, panel: controller.panel, timeline: controller.timeline, fleet: controller.fleet };
      return snapshot;
    },
  };
}
