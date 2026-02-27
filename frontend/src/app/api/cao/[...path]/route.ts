/**
 * Middleware proxy layer: forwards all /api/cao/* requests to the cao-control-panel.
 * This decouples the frontend from the backend and allows the Next.js app to
 * act as a middle layer between the browser and the CAO Control Panel API.
 */

import { NextRequest, NextResponse } from "next/server";

const CAO_SERVER_URL = process.env.CAO_CONTROL_PANEL_URL || "http://localhost:8000";

type RouteContext = { params: Promise<{ path: string[] }> };

async function proxyToCao(
  request: NextRequest,
  context: RouteContext
): Promise<NextResponse> {
  const { path } = await context.params;
  const upstreamPath = "/" + path.join("/");

  // Forward query parameters as-is
  const searchParams = request.nextUrl.searchParams.toString();
  const upstreamUrl = `${CAO_SERVER_URL}${upstreamPath}${
    searchParams ? "?" + searchParams : ""
  }`;

  const requestId = request.headers.get("x-request-id") ?? crypto.randomUUID();
  const headers = new Headers();
  headers.set("x-request-id", requestId);

  const contentType = request.headers.get("content-type");
  if (contentType) {
    headers.set("content-type", contentType);
  }

  const authorization = request.headers.get("authorization");
  if (authorization) {
    headers.set("authorization", authorization);
  }

  const cookie = request.headers.get("cookie");
  if (cookie) {
    headers.set("cookie", cookie);
  }

  let body: string | undefined;
  if (request.method !== "GET" && request.method !== "HEAD") {
    try {
      const text = await request.text();
      if (text) body = text;
    } catch {
      // no body
    }
  }

  try {
    const upstream = await fetch(upstreamUrl, {
      method: request.method,
      headers,
      body,
      cache: "no-store",
    });

    const responseHeaders = new Headers();
    responseHeaders.set("x-request-id", upstream.headers.get("x-request-id") ?? requestId);
    const upstreamContentType = upstream.headers.get("content-type");
    if (upstreamContentType) {
      responseHeaders.set("content-type", upstreamContentType);
    }

    const setCookie = upstream.headers.get("set-cookie");
    if (setCookie) {
      responseHeaders.set("set-cookie", setCookie);
    }

    if (upstreamContentType?.includes("text/event-stream")) {
      return new NextResponse(upstream.body, {
        status: upstream.status,
        headers: responseHeaders,
      });
    }

    const responseText = await upstream.text();
    const response = new NextResponse(responseText, {
      status: upstream.status,
      headers: responseHeaders,
    });

    return response;
  } catch (err) {
    return NextResponse.json(
      { error: "Failed to reach cao-control-panel", detail: String(err) },
      { status: 502 }
    );
  }
}

export const GET = proxyToCao;
export const POST = proxyToCao;
export const DELETE = proxyToCao;
export const PUT = proxyToCao;
export const PATCH = proxyToCao;
