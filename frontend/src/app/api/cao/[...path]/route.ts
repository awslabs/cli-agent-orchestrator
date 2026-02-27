/**
 * Middleware proxy layer: forwards all /api/cao/* requests to the cao-control-panel.
 * This decouples the frontend from the backend and allows the Next.js app to
 * act as a middle layer between the browser and the CAO Control Panel API.
 */

import { NextRequest, NextResponse } from "next/server";

const CAO_SERVER_URL =
  process.env.CAO_SERVER_URL || "http://localhost:8000";

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

  const headers: HeadersInit = {
    "Content-Type": "application/json",
  };

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
    });

    const responseText = await upstream.text();
    let responseData: unknown;
    try {
      responseData = JSON.parse(responseText);
    } catch {
      responseData = responseText;
    }

    return NextResponse.json(responseData, { status: upstream.status });
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
