import { describe, expect, it, vi } from "vitest";
import { chooseToken, DEFAULT_SERVER, isLoopback, parseServer } from "../token.ts";

const storage = (token?: string) => ({ getItem: (key: string) => (key === "caoToken" ? (token ?? null) : null) });

describe("isLoopback", () => {
  it.each([
    ["http://127.0.0.1:9889", true],
    ["http://localhost:9889", true],
    ["http://[::1]:9889", true],
    ["https://localhost", true],
    ["http://127.0.0.1.evil.example", false],
    ["http://localhost.evil.example:9889", false],
    // userinfo: the host is evil.example, not localhost
    ["http://localhost@evil.example", false],
    ["http://localhost:9889@evil.example:9889", false],
    ["http://10.0.0.5:9889", false],
    ["file:///etc/passwd", false],
    ["not a url", false],
  ])("%s → %s", (url, expected) => {
    expect(isLoopback(url)).toBe(expected);
  });
});

describe("parseServer", () => {
  it.each(["javascript:alert(1)", "file:///etc/passwd", "ftp://h/x", "data:text/html,x", "nonsense", ""])(
    "refuses %j",
    (server) => {
      expect(parseServer(server)).toBeNull();
    },
  );

  it("accepts http and https", () => {
    expect(parseServer("http://h:1")?.origin).toBe("http://h:1");
    expect(parseServer("https://h")?.origin).toBe("https://h");
  });
});

describe("chooseToken", () => {
  it("defaults the server and sends no token when none is stored", () => {
    const confirm = vi.fn(() => true);
    expect(chooseToken("", storage(), confirm)).toEqual({ ok: true, server: DEFAULT_SERVER, note: "" });
    expect(confirm).not.toHaveBeenCalled();
  });

  it("attaches the stored token to a loopback server without asking", () => {
    const confirm = vi.fn(() => false);
    expect(chooseToken("?server=http://localhost:9900", storage("t"), confirm)).toEqual({
      ok: true,
      server: "http://localhost:9900",
      token: "t",
      note: "",
    });
    expect(confirm).not.toHaveBeenCalled();
  });

  it("asks before sending the token to any other server, naming its origin", () => {
    const confirm = vi.fn(() => false);
    const choice = chooseToken("?server=https://cao.example.com:9889/path", storage("t"), confirm);
    expect(confirm).toHaveBeenCalledWith("Send your stored CAO token to https://cao.example.com:9889? It is not on this machine.");
    expect(choice).toEqual({ ok: true, server: "https://cao.example.com:9889/path", note: "token not sent to https://cao.example.com:9889" });
    expect(chooseToken("?server=https://cao.example.com", storage("t"), () => true)).toMatchObject({ ok: true, token: "t" });
  });

  it("a userinfo URL that only looks local is asked about, naming the real origin", () => {
    const confirm = vi.fn(() => false);
    chooseToken("?server=http://localhost@evil.example", storage("t"), confirm);
    expect(confirm).toHaveBeenCalledWith("Send your stored CAO token to http://evil.example? It is not on this machine.");
  });

  it.each(["nonsense", "javascript:alert(1)", "file:///etc/passwd"])(
    "refuses a server %j outright: no confirm, no token, a note to show",
    (server) => {
      const confirm = vi.fn(() => true);
      expect(chooseToken(`?server=${encodeURIComponent(server)}`, storage("t"), confirm)).toEqual({
        ok: false,
        server,
        note: "the server URL is not valid: it must be an http or https URL",
      });
      expect(confirm).not.toHaveBeenCalled();
    },
  );
});
