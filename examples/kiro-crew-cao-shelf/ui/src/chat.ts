import * as sdk from '@kirocrew/app-sdk'
import type { ShelfError } from './api'

/** Opens a Kiro Crew chat by its slot key, or null when the host has no chat launcher. The hook is
 *  read through the namespace: a host without it gives undefined instead of failing the bundle. */
export function useOpenChat(): ((slotKey: string) => void) | null {
  const hook = sdk.useChatLauncher
  const launcher = hook ? hook() : null
  return launcher ? (slotKey: string) => launcher.openChat({ slotKey }) : null
}

/** What remote control means, shown near the top of the page whatever its state (api-contract.md,
 *  "explain:relay", word for word): the one sentence the page always shows. */
export const RELAY_EXPLAIN =
  'Remote control ties a Kiro Crew chat to one CAO session, so you can talk to that session from the chat.'

/** The summary of the disclosure under that sentence. */
export const RELAY_EXPLAIN_SUMMARY = 'What is remote control?'

/** The text behind "What is remote control?" (api-contract.md, "explain:relay-detail", one entry per
 *  paragraph). */
export const RELAY_EXPLAIN_DETAIL: readonly string[] = [
  "Your messages go to the session's conductor tagged [REMOTE-CONTROL], and its replies come back to the chat. A message that starts with / goes to the session untagged.",
  "The chat's relay agent reads the conductor's replies to judge progress. Idle does not mean done: the conductor can be idle while its helpers are still working. So the agent checks back with pauses between checks, and reports what the conductor said and which helpers are still working.",
  "You can keep typing to the session directly. Your next message from the chat then carries the tag [REMOTE-CONTROL catch-up], and the session opens its reply with a brief of what happened in between.",
  'End relay stops the chat, never the CAO session: the session keeps working.',
  "To start, press Remote control on a session's card, or tick and remote control it when you start a session.",
]

/** The risk the owner accepts before CAO Shelf makes a relay chat (api-contract.md, "risk:relay", word
 *  for word): the short text the dialog shows first. */
export const RELAY_RISK =
  "A Kiro Crew chat sends your messages to the CAO session and shows its replies. Text from the session reaches the chat's model, so an instruction hidden in it could make the model call a tool the chat has, and an allowed tool runs without asking you. On the claude backend the chat also has your own Claude Code tools and plugins, and on any backend a host plugin can add servers to every agent, for example a credentials server."

/** The detail behind "What the chat can reach" (api-contract.md, "risk:relay-detail", one entry per
 *  paragraph): what reaches the chat on the kas backend, on the claude backend, and on both. */
export const RELAY_RISK_DETAIL: readonly string[] = [
  "On the kas backend the chat is offered only its four CAO tools (send_session_message, get_terminal_status, read_session_output and get_session_info) and Kiro Crew's wait tool, which only pauses the chat. They run without asking you, and a CAO tool runs for any terminal the model names. No Kiro built-in tool (shell, files, web or sub-agents), no Claude Code setting or plugin and no server from your mcp.json reaches it, except servers that Kiro Crew or a host plugin adds to every agent.",
  "On the claude backend the chat runs with your own Claude Code settings. Besides its four CAO tools and Kiro Crew's wait tool (which only pauses the chat), it can reach your Claude Code plugins, your user-level MCP servers and Claude Code's other built-in tools (for example Read, WebFetch, sub-agents, scheduled tasks and worktrees). Kiro Crew refuses the shell and Claude Code's file-edit tools here; if you enable the Claude Code sandbox, also set sandbox.autoAllowBashIfSandboxed to false, or sandboxed shell commands run without that refusal. A call runs without asking you when your Claude Code settings allow that tool or use auto mode. Sub-agents start without asking and can go on after End relay or Take over. Read plus WebFetch, or any plugin that posts or sends, is enough to send out any file you can read.",
  "On both backends a host plugin can add an MCP server to every agent, this one included: on some installs a credentials server whose tool hands out cloud credentials. Such a tool asks you first unless an allow rule, auto mode or a YOLO grant covers it. In a relay chat, refuse any request for a tool other than the four CAO tools and wait.",
]

/** Fixed sentences for relay failures. The backend's own text is never shown for these codes. */
const SENTENCES: Partial<Record<ShelfError['code'], string>> = {
  no_conductor: 'This session has no conductor, so there is nothing to remote control.',
  relay_exists: 'This session already has an active relay.',
  risk_not_acknowledged: 'Remote control was not set up: accept the stated risk first.',
  relay_cao_mismatch: "The remote-control agent was installed for another CAO address. Run install.sh again with --cao-url set to this page's CAO address.",
  gateway_unavailable: 'CAO Shelf could not reach Kiro Crew to set up the relay chat. Try again in a moment.',
  gateway_refused: 'Kiro Crew refused to create the relay chat.',
  not_found: 'That session or relay is gone. Wait for the next refresh.',
  conflict_changed: 'That relay is no longer active. Press Remote control to start a new one.',
  cao_unreachable: 'CAO is not reachable, so the session could not be checked.',
  cao_timeout: 'CAO did not answer in time. Try again.',
  invalid_request: 'The request was refused as invalid.',
  backend_unreachable: 'Cannot reach the CAO Shelf backend.',
}

export function relayErrorText(e: ShelfError): string {
  const gw = e.detail?.gateway_code
  const code = typeof gw === 'string' && /^[a-z0-9_]{1,64}$/.test(gw) ? `${e.code} (${gw})` : e.code
  const sentence = SENTENCES[e.code]
  if (sentence) return `${code}: ${sentence}`
  // other codes come from the Start path, whose messages the backend writes itself (never CAO text)
  return `${code}: ${e.message || 'The relay was not created.'}`
}
