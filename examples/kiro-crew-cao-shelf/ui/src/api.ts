import { useAppApi } from '@kirocrew/app-sdk'
import { useMemo } from 'react'
import type {
  AnswerRequest,
  AnswerResponse,
  ApiError,
  AskRequest,
  AskResponse,
  AllHistory,
  Config,
  ConfigPatch,
  HistoryResponse,
  HistoryRows,
  Profiles,
  PromptResponse,
  RelayCreateRequest,
  RelayEndRequest,
  RelayEndResponse,
  RelayList,
  RelaySummary,
  RelayTakeoverRequest,
  ShelfResponse,
  StartRequest,
  StartResponse,
  StopExecuteRequest,
  StopExecuteResponse,
  StopPlan,
  StopPlanRequest,
  Unchanged,
} from './types'

export const BASE = '/apps/cao-shelf/api'

/** A failure from the app backend, normalised from the SDK's `API <status>: <body>` Error. */
export interface ShelfError {
  status: number | null
  code: ApiError['code'] | 'backend_unreachable'
  message: string
  retryable: boolean
  detail?: Record<string, unknown>
}

export function toShelfError(e: unknown): ShelfError {
  const err = e as { status?: number; body?: string; name?: string; message?: string }
  if (typeof err?.status === 'number') {
    try {
      const parsed = JSON.parse(err.body ?? '') as { error?: ApiError }
      if (parsed.error && typeof parsed.error.code === 'string') {
        return {
          status: err.status,
          code: parsed.error.code,
          message: String(parsed.error.message ?? ''),
          retryable: !!parsed.error.retryable,
          detail: parsed.error.detail,
        }
      }
    } catch {
      /* body was not our envelope */
    }
    return {
      status: err.status,
      code: 'backend_unreachable',
      message: `The CAO Shelf backend answered ${err.status}.`,
      retryable: true,
    }
  }
  if (err?.name === 'AbortError') return { status: null, code: 'backend_unreachable', message: 'Request cancelled.', retryable: true }
  return {
    status: null,
    code: 'backend_unreachable',
    message: 'Cannot reach the CAO Shelf backend.',
    retryable: true,
  }
}

const enc = encodeURIComponent

export function makeApi(api: ReturnType<typeof useAppApi>) {
  return {
    shelf: (since: number | null, signal?: AbortSignal) =>
      api.get<ShelfResponse | Unchanged>(`${BASE}/shelf${since === null ? '' : `?since=${since}`}`, { signal }),
    history: (name: string) => api.get<HistoryResponse>(`${BASE}/sessions/${enc(name)}/history`),
    historyRows: (name: string, terminalId: string) =>
      api.get<HistoryRows>(`${BASE}/sessions/${enc(name)}/history/${enc(terminalId)}/rows`),
    allHistory: () => api.get<AllHistory>(`${BASE}/history`),
    prompt: (id: string) => api.get<PromptResponse>(`${BASE}/terminals/${enc(id)}/prompt`),
    ask: (name: string, body: AskRequest) => api.post<AskResponse>(`${BASE}/sessions/${enc(name)}/ask`, body),
    answer: (id: string, body: AnswerRequest) => api.post<AnswerResponse>(`${BASE}/terminals/${enc(id)}/answer`, body),
    profiles: () => api.get<Profiles>(`${BASE}/profiles`),
    start: (body: StartRequest) => api.post<StartResponse>(`${BASE}/sessions`, body),
    stopPlan: (body: StopPlanRequest) => api.post<StopPlan>(`${BASE}/stop/plan`, body),
    stopExecute: (body: StopExecuteRequest) => api.post<StopExecuteResponse>(`${BASE}/stop/execute`, body),
    getConfig: () => api.get<Config>(`${BASE}/config`),
    putConfig: (body: ConfigPatch) => api.put<Config>(`${BASE}/config`, body),
    relays: () => api.get<RelayList>(`${BASE}/relays`),
    relayCreate: (body: RelayCreateRequest) => api.post<RelaySummary>(`${BASE}/relays`, body),
    relayEnd: (id: string, body: RelayEndRequest) => api.post<RelayEndResponse>(`${BASE}/relays/${enc(id)}/end`, body),
    relayTakeover: (id: string, body: RelayTakeoverRequest) => api.post<RelaySummary>(`${BASE}/relays/${enc(id)}/takeover`, body),
  }
}

export type ShelfApi = ReturnType<typeof makeApi>

/** Stable for the life of the component. */
export function useShelfApi(): ShelfApi {
  const api = useAppApi()
  // The SDK client is memoised by the host; keep our wrapper stable regardless.
  return useMemo(() => makeApi(api), [api])
}
