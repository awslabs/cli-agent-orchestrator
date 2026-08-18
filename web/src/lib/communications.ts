// Presentation mapping for the bounded communications catalog (design §7,
// §10). This module owns exactly one job: turning the API's coverage and
// reason vocabulary into DISTINCT, honest UI states.
//
// The two rules that shape everything here:
//
//   * ABSENT AND UNREADABLE ARE DIFFERENT ANSWERS. A missing conductor state
//     root means no catalog is installed and the entry points hide; an
//     unreadable root is a named failure with a retry. Collapsing them would
//     either cry wolf on every catalog-free deployment or hide a real one.
//
//   * A REFUSAL STATES WHAT WAS OBSERVED. Every reason code maps to its own
//     words; an unrecognised future code falls back to a neutral
//     "unavailable (<reason>)" that still shows the raw code — never silence,
//     and never a message asserting something the API did not say.

import type {
  ApiError,
  CatalogReason,
  CommunicationsListResponse,
} from '../api'

/** The service's label for a root-level list reason. */
export const ROOT_SOURCE_LABEL = 'conductor-state-root'

// The reason vocabulary, mirrored from
// src/cli_agent_orchestrator/services/communications_catalog.py. The UI maps
// every one of these; anything not in the list takes the neutral fallback.
export const REASON = {
  MISSING: 'missing',
  UNREADABLE: 'unreadable',
  MALFORMED: 'malformed',
  OVERSIZE: 'oversize',
  NOT_REGULAR: 'not-a-regular-file',
  SYMLINK_REFUSED: 'symlink-refused',
  OUTSIDE_ROOT: 'outside-root',
  PROJECT_LIMIT: 'project-limit',
  IDENTIFIER_INVALID: 'identifier-invalid',
  CONTENT_DIGEST_MISMATCH: 'content-digest-mismatch',
  CONTENT_MISSING: 'content-missing',
  CONTENT_QUARANTINED: 'content-quarantined',
  CONTENT_UNREADABLE: 'content-unreadable',
} as const

/**
 * A defensive read of the list body. Returns null when the body is not the
 * documented shape at all: the caller shows the named unavailable state with
 * a retry rather than a truthful-looking empty list nothing vouched for.
 */
export function readCommunicationsList(body: unknown): CommunicationsListResponse | null {
  if (!body || typeof body !== 'object') return null
  const raw = body as Partial<CommunicationsListResponse>
  if (typeof raw.coverage !== 'string') return null
  if (!Array.isArray(raw.communications)) return null
  return {
    schema: typeof raw.schema === 'string' ? raw.schema : '',
    coverage: raw.coverage,
    reasons: Array.isArray(raw.reasons)
      ? raw.reasons.filter(
          (r): r is CatalogReason =>
            !!r && typeof r === 'object' &&
            typeof (r as CatalogReason).source === 'string' &&
            typeof (r as CatalogReason).reason === 'string',
        )
      : [],
    communications: raw.communications as CommunicationsListResponse['communications'],
    next_cursor: typeof raw.next_cursor === 'string' ? raw.next_cursor : null,
    total: typeof raw.total === 'number' ? raw.total : raw.communications.length,
  }
}

export type CatalogAvailability =
  /** No conductor catalog is installed: the entry points are not shown at all. */
  | 'not-installed'
  /** The catalog root exists and could not be read: a named state with retry. */
  | 'unreadable'
  | 'available'

/**
 * The distinction §2.1 of the implementation brief turns on. Only a root-level
 * `missing` reason means "not installed"; `unavailable` for any other reason
 * is a surface that exists and could not be read.
 */
export function catalogAvailability(coverage: string, reasons: CatalogReason[]): CatalogAvailability {
  if (coverage !== 'unavailable') return 'available'
  const root = reasons.find(r => r.source === ROOT_SOURCE_LABEL)
  if (root && root.reason === REASON.MISSING) return 'not-installed'
  return 'unreadable'
}

/** One line of the partial/truncated coverage banner, naming source and cause. */
export function coverageReasonText({ source, reason }: CatalogReason): string {
  switch (reason) {
    case REASON.MISSING:
      return `${source}: no catalog published`
    case REASON.UNREADABLE:
      return `${source}: could not be read`
    case REASON.MALFORMED:
      return `${source}: catalog file is malformed`
    case REASON.OVERSIZE:
      return `${source}: catalog exceeds the read budget`
    case REASON.NOT_REGULAR:
      return `${source}: catalog is not a regular file`
    case REASON.SYMLINK_REFUSED:
      return `${source}: catalog is a symlink (refused)`
    case REASON.OUTSIDE_ROOT:
      return `${source}: catalog resolves outside the state root (refused)`
    case REASON.PROJECT_LIMIT:
      return `${source}: project limit reached; not every project was read`
    default:
      return `${source}: unavailable (${reason})`
  }
}

/**
 * The body-level tombstones a 200 detail response can carry (`content: null`
 * plus a typed reason). Each has its own words; "quarantined" in particular
 * is a deliberate operator action and must never read as missing or corrupt.
 */
export function contentReasonText(reason: string): string {
  switch (reason) {
    case REASON.CONTENT_MISSING:
      return 'Content missing — the captured bytes are gone; the metadata record remains.'
    case REASON.CONTENT_QUARANTINED:
      return 'Content quarantined — removed by a deliberate operator action; the tombstone, digest, and receipt remain.'
    case REASON.OVERSIZE:
      return 'Too large to serve — the stored content exceeds the reader budget.'
    case REASON.CONTENT_UNREADABLE:
      return 'Content could not be read.'
    case REASON.SYMLINK_REFUSED:
      return 'Content refused — the stored object is a symlink.'
    case REASON.NOT_REGULAR:
      return 'Content refused — the stored object is not a regular file.'
    case REASON.OUTSIDE_ROOT:
      return 'Content refused — the stored object resolves outside the catalog root.'
    default:
      return `Content unavailable (${reason}).`
  }
}

export type DetailFailure =
  /** 404 — stable. On a record fetch: the record is not in the catalog. On
   *  the LIST route (which cannot 404 a record): this server build has no
   *  /communications route at all. */
  | { kind: 'not-found'; message: string }
  /** 400/422 — the identifier in the link is not a valid catalog identifier. */
  | { kind: 'invalid'; message: string }
  /** 503 content-digest-mismatch — integrity failure; content is NOT rendered. */
  | { kind: 'corrupt'; message: string }
  /** Anything else — network, 503, 401/403: an ordinary unavailable state. */
  | { kind: 'unavailable'; message: string }

/**
 * Maps a failed detail fetch to its state. `retryable` is carried implicitly
 * by the kind: only `unavailable` offers a retry, because the other three are
 * deterministic answers about the record or the link.
 */
export function detailFailure(error: unknown): DetailFailure {
  const err = error as ApiError
  if (err.status === 404) {
    return {
      kind: 'not-found',
      message: 'This record is not in the catalog. The link may be stale, or it was never published.',
    }
  }
  if (err.status === 400) {
    return { kind: 'invalid', message: 'The identifier in this link is not a valid catalog identifier.' }
  }
  if (err.status === 503 && err.detail === REASON.CONTENT_DIGEST_MISMATCH) {
    return {
      kind: 'corrupt',
      message: 'Content failed its integrity check (corrupt) and is not shown.',
    }
  }
  if (err.status === 503 && err.detail === REASON.CONTENT_UNREADABLE) {
    return { kind: 'unavailable', message: 'Content could not be read.' }
  }
  return {
    kind: 'unavailable',
    message: err.detail ?? (err.status ? `Request failed (${err.status}).` : 'The catalog could not be reached.'),
  }
}

/**
 * Maps a failed LIST fetch to its state — the detail kinds, with two
 * list-specific readings:
 *
 *   * 404 CANNOT be a record answer here. The route lists a task occurrence,
 *     not a record, and the real service answers root-level problems with
 *     200 + coverage 'unavailable' — so a 404 means this server build has no
 *     /communications route at all, the same condition the dashboard probe
 *     calls "not installed". It is never "record not found".
 *   * 422 (an over-long occurrence id) joins 400 as `invalid`: a
 *     deterministic verdict on the link itself. Offering Retry on it would
 *     promise an exit that cannot succeed.
 *
 * As with the detail pane, only `unavailable` retries.
 */
export function listFailure(error: unknown): DetailFailure {
  const err = error as ApiError
  if (err.status === 404) {
    return {
      kind: 'not-found',
      message: 'No communications catalog is installed on this deployment.',
    }
  }
  if (err.status === 422) {
    return { kind: 'invalid', message: 'The identifier in this link is not a valid catalog identifier.' }
  }
  return detailFailure(error)
}

/**
 * A neutral label for the open `kind` vocabulary: the conductor's string,
 * humanised, never mapped through a closed list.
 */
export function kindLabel(kind: string | null | undefined): string {
  if (!kind) return 'communication'
  return kind.replace(/_/g, ' ')
}

/**
 * The author-claimed report scope, shown as its own badge and never folded
 * into task outcome (design §4: `report_scope: final` means only that the
 * author submitted the occurrence as the final narrative report).
 */
export function reportScopeBadge(reportScope: string | null | undefined): string | null {
  if (reportScope === 'final') return 'final report'
  if (reportScope === 'intermediate') return 'intermediate report'
  return reportScope ? reportScope.replace(/_/g, ' ') : null
}
