// @vitest-environment node
// The demo recorder must never default to, or record beside, a live cao-server.
// No test here touches port 9889: a local listener stands in for the server and
// `caoPort` points the guard at it.

import net from "node:net";
import { afterEach, describe, expect, it } from "vitest";
import {
  CAO_SERVER_PORT,
  pickFreePort,
  portAnswers,
  resolveDemoPort,
} from "../../scripts/demo-port.mjs";

const open: net.Server[] = [];

function listen(): Promise<number> {
  return new Promise((resolve) => {
    const server = net.createServer();
    open.push(server);
    server.listen(0, "127.0.0.1", () =>
      resolve((server.address() as net.AddressInfo).port),
    );
  });
}

afterEach(async () => {
  await Promise.all(
    open.splice(0).map((s) => new Promise((r) => s.close(() => r(null)))),
  );
});

describe("demo port selection", () => {
  it("knows the cao-server port is 9889", () => {
    expect(CAO_SERVER_PORT).toBe(9889);
  });

  it("picks a free port that is not 9889 by default", async () => {
    const idle = await pickFreePort();
    const port = await resolveDemoPort({}, idle);
    expect(port).not.toBe(9889);
    expect(port).not.toBe(idle);
    expect(port).toBeGreaterThan(0);
  });

  it("never returns the port it was told to avoid", async () => {
    const avoid = await listen();
    for (let i = 0; i < 5; i += 1) {
      expect(await pickFreePort(avoid)).not.toBe(avoid);
    }
  });

  it("honours an explicit DEMO_PORT that is not the cao-server port", async () => {
    const idle = await pickFreePort();
    const wanted = await pickFreePort(idle);
    expect(await resolveDemoPort({ DEMO_PORT: String(wanted) }, idle)).toBe(
      wanted,
    );
  });

  it("refuses DEMO_PORT set to the cao-server port", async () => {
    const idle = await pickFreePort();
    await expect(
      resolveDemoPort({ DEMO_PORT: String(idle) }, idle),
    ).rejects.toThrow(/refusing to record on port/);
  });

  it("refuses to start while something answers on the cao-server port", async () => {
    const standIn = await listen();
    expect(await portAnswers(standIn)).toBe(true);
    await expect(resolveDemoPort({}, standIn)).rejects.toThrow(
      /something is listening/,
    );
    const other = await pickFreePort(standIn);
    await expect(
      resolveDemoPort({ DEMO_PORT: String(other) }, standIn),
    ).rejects.toThrow(/something is listening/);
  });

  it("rejects a DEMO_PORT that is not a port number", async () => {
    const idle = await pickFreePort();
    await expect(
      resolveDemoPort({ DEMO_PORT: "http://x" }, idle),
    ).rejects.toThrow(/must be a port number/);
  });
});
