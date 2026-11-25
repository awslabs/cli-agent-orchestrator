// Port selection for the demo recorder (scripts/record-demo.mjs).
//
// The recorder boots its own harness server and drives a browser at it. A
// harness that shares a port with a real cao-server records that server's
// sessions, so the demo never defaults to the CAO port and refuses to run while
// anything answers there.

import net from "node:net";

/** The port a real cao-server listens on by default. */
export const CAO_SERVER_PORT = 9889;

/** True when something accepts a TCP connection on 127.0.0.1:`port`. */
export function portAnswers(port, timeoutMs = 500) {
  return new Promise((resolve) => {
    const socket = net.connect({ port, host: "127.0.0.1" });
    const done = (answered) => {
      socket.destroy();
      resolve(answered);
    };
    socket.setTimeout(timeoutMs, () => done(false));
    socket.once("connect", () => done(true));
    socket.once("error", () => done(false));
  });
}

/** Ask the OS for a free loopback port. Never returns `avoid`. */
export async function pickFreePort(avoid = CAO_SERVER_PORT) {
  for (let attempt = 0; attempt < 10; attempt += 1) {
    const port = await new Promise((resolve, reject) => {
      const server = net.createServer();
      server.once("error", reject);
      server.listen(0, "127.0.0.1", () => {
        const { port: assigned } = server.address();
        server.close(() => resolve(assigned));
      });
    });
    if (port !== avoid) return port;
  }
  throw new Error("could not find a free loopback port");
}

/**
 * Choose the harness port and check it is safe to record on.
 *
 * - `DEMO_PORT` unset: a free port picked by the OS.
 * - `DEMO_PORT` set to the CAO port: refused.
 * - Anything answering on the CAO port: refused, whatever port was chosen,
 *   because a live cao-server is exactly what the recording must not capture.
 *
 * `caoPort` is a parameter so tests can stand in a local listener instead of
 * connecting to a real server.
 */
export async function resolveDemoPort(
  env = process.env,
  caoPort = CAO_SERVER_PORT,
) {
  const requested = env.DEMO_PORT;
  const port = requested ? Number(requested) : await pickFreePort(caoPort);
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    throw new Error(`DEMO_PORT must be a port number, got "${requested}"`);
  }
  if (port === caoPort) {
    throw new Error(
      `refusing to record on port ${caoPort}: that is the cao-server port. ` +
        "Unset DEMO_PORT to use a free port.",
    );
  }
  if (await portAnswers(caoPort)) {
    throw new Error(
      `refusing to record: something is listening on port ${caoPort}, which is ` +
        "where a live cao-server would be. Stop it first so the demo cannot " +
        "capture real sessions.",
    );
  }
  return port;
}
