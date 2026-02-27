export type HttpMethod = "GET" | "POST" | "DELETE" | "PUT" | "PATCH";

export interface ApiResponse<T> {
  ok: boolean;
  status: number;
  data: T;
}

async function parseResponseBody(response: Response): Promise<unknown> {
  const contentType = response.headers.get("content-type") ?? "";
  if (contentType.includes("application/json")) {
    return response.json();
  }
  return response.text();
}

export async function caoRequest<T = unknown>(
  method: HttpMethod,
  path: string,
  options?: {
    query?: Record<string, string | number | undefined | null>;
    body?: unknown;
  }
): Promise<ApiResponse<T>> {
  const url = new URL(`/api/cao${path}`, window.location.origin);

  if (options?.query) {
    Object.entries(options.query).forEach(([key, value]) => {
      if (value !== undefined && value !== null && value !== "") {
        url.searchParams.set(key, String(value));
      }
    });
  }

  const response = await fetch(url.toString(), {
    method,
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
    },
    body: options?.body ? JSON.stringify(options.body) : undefined,
    cache: "no-store",
  });

  const data = (await parseResponseBody(response)) as T;
  return {
    ok: response.ok,
    status: response.status,
    data,
  };
}

export interface ConsoleAgent {
  id: string;
  name?: string;
  provider?: string;
  session_name?: string;
  agent_profile?: string;
  status?: string;
  is_main?: boolean;
  last_active?: string;
}

export interface ConsoleOverview {
  uptime_seconds: number;
  agents_total: number;
  main_agents_total: number;
  worker_agents_total: number;
  provider_counts: Record<string, number>;
  status_counts: Record<string, number>;
  profile_counts: Record<string, number>;
  main_agents: ConsoleAgent[];
}

export interface ConsoleLeaderGroup {
  leader: ConsoleAgent;
  members: ConsoleAgent[];
}

export interface ConsoleOrganization {
  leaders_total: number;
  workers_total: number;
  leaders: ConsoleAgent[];
  workers: ConsoleAgent[];
  leader_groups: ConsoleLeaderGroup[];
  unassigned_workers: ConsoleAgent[];
}

export interface ConsoleAgentProfilesResponse {
  profiles: string[];
}

export interface CreateAgentProfileRequest {
  name: string;
  description: string;
  system_prompt: string;
  provider?: string;
}

export interface CreateAgentProfileResponse {
  ok: boolean;
  profile: string;
  file_path: string;
}

export interface AgentProfileFileResponse {
  profile: string;
  file_path: string;
  content: string;
}

export interface UpdateAgentProfileResponse {
  ok: boolean;
  profile: string;
  file_path: string;
}

export interface InstallAgentProfileResponse {
  ok: boolean;
  profile: string;
  command: string;
  return_code: number;
  stdout: string;
  stderr: string;
}
