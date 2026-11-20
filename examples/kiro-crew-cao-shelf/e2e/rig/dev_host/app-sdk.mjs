// A small stand-in for '@kirocrew/app-sdk' so the page can be looked at without Kiro Crew.
// It mirrors only what the CAO Shelf imports: useAppApi, useAppInfo, useNavBadge.
import { useEffect, useState } from "react";

const DECLARED = ["/apps/cao-shelf/api"];

async function call(method, path, body, init) {
  if (!DECLARED.some((d) => path === d || path.startsWith(d + "/") || path.startsWith(d + "?"))) {
    throw new Error("AppApiPermissionError: " + path + " is not declared");
  }
  const res = await fetch(path, {
    ...init,
    method,
    headers: { "Content-Type": "application/json", ...((init && init.headers) || {}) },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await res.text();
  if (!res.ok) {
    const e = new Error("API " + res.status + ": " + text);
    e.name = "AppApiError";
    e.status = res.status;
    e.body = text;
    throw e;
  }
  return text ? JSON.parse(text) : undefined;
}

const client = {
  get: (p, i) => call("GET", p, undefined, i),
  post: (p, b, i) => call("POST", p, b, i),
  put: (p, b, i) => call("PUT", p, b, i),
  del: (p, i) => call("DELETE", p, undefined, i),
};

export function useAppApi() {
  return client;
}

export function useAppInfo() {
  const [active, setActive] = useState(!document.hidden);
  useEffect(() => {
    const on = () => setActive(!document.hidden);
    document.addEventListener("visibilitychange", on);
    return () => document.removeEventListener("visibilitychange", on);
  }, []);
  return { name: "cao-shelf", version: "dev", active };
}

export function useNavBadge() {
  return (n) => {
    document.title = (n > 0 ? "(" + n + ") " : "") + "CAO Shelf (dev host)";
  };
}
