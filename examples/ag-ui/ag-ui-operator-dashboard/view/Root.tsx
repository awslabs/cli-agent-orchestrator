import { useEffect, useMemo, useState } from "react";
import { openAmbient } from "../core/index.ts";
import { App } from "./App.tsx";
import { createStore } from "./store.ts";
import type { TokenChoice } from "./token.ts";

/** The page: a refusal for a bad `?server=`, otherwise the dashboard wired to that server. */
export function Root({ choice, threadId }: { choice: TokenChoice; threadId: string }) {
  if (!choice.ok) {
    return (
      <main>
        <h1>CAO operator dashboard</h1>
        <p role="alert">
          Not connecting to <code>{choice.server}</code>: {choice.note}.
        </p>
      </main>
    );
  }
  return <Connected server={choice.server} token={choice.token} note={choice.note} threadId={threadId} />;
}

function Connected({ server, token, note, threadId }: { server: string; token?: string; note: string; threadId: string }) {
  const store = useMemo(() => createStore({ base: server, threadId, token }), [server, threadId, token]);
  const [ambientStatus, setAmbientStatus] = useState("connecting");
  useEffect(() => {
    let handle: { close(): void } | undefined;
    try {
      handle = openAmbient({
        base: server,
        threadId,
        token,
        onFrame: (frame) => void store.controller.ambient(frame),
        onStatus: (status, detail) => setAmbientStatus(detail ? `${status} (${detail})` : status),
      });
    } catch (error) {
      setAmbientStatus(`refused (${error instanceof Error ? error.message : String(error)})`);
    }
    void store.controller.refresh();
    return () => handle?.close();
  }, [store, server, threadId, token]);
  const status = note ? `${ambientStatus}; ${note}` : ambientStatus;
  return <App store={store} server={server} ambientStatus={status} />;
}
