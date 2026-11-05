// Keeps recently viewed terminals alive so switching agent tabs is instant and
// keeps scrollback.
//
// Every live session is a WebSocket, and on the server every WebSocket is its
// own `tmux attach` client, so the pool is capped. Past the cap the least
// recently used session that is not on screen is disposed; reopening it
// reconnects and tmux repaints the current screen.
//
// A session that leaves the screen is parked in an off-screen holder marked
// PARKED_ATTR, which stops it fitting: by the time a pane's cleanup runs, React
// has already removed the pane from the document, so the element measures 0x0,
// and a fit at that size would be sent to tmux and shrink the agent's window.
// The next attach to a real pane refits it.
import { PARKED_ATTR, type XtermSession } from '../components/terminal/xtermSession'

export const DEFAULT_POOL_SIZE = 6

type Factory = (terminalId: string, fontSize?: number) => XtermSession

export class TerminalPool {
  private sessions = new Map<string, XtermSession>()
  /** Terminal id -> the container currently showing it. */
  private mounted = new Map<string, HTMLElement>()
  /** Least recently used first. */
  private order: string[] = []
  private holder: HTMLDivElement | null = null
  private fontSize: number | undefined

  constructor(private factory: Factory, private max = DEFAULT_POOL_SIZE) {}

  /** Show `terminalId` inside `container`, creating the session if needed. */
  attach(terminalId: string, container: HTMLElement): XtermSession {
    let session = this.sessions.get(terminalId)
    if (!session) {
      session = this.factory(terminalId, this.fontSize)
      this.sessions.set(terminalId, session)
    }
    if (session.element.parentElement !== container) {
      // Re-parenting moves the element out of any other container, so whatever
      // showed it before is no longer showing it.
      container.appendChild(session.element)
    }
    this.mounted.set(terminalId, container)
    this.touch(terminalId)
    this.evict()
    return session
  }

  /** Take `terminalId` off screen but keep it alive (until evicted). */
  detach(terminalId: string, container?: HTMLElement): void {
    const session = this.sessions.get(terminalId)
    if (!session) return
    // A stale detach from a pane that already shows a different terminal must
    // not pull this terminal out of the pane it moved to.
    if (container && this.mounted.get(terminalId) !== container) return
    this.mounted.delete(terminalId)
    this.park(session)
    this.evict()
  }

  /** Dispose sessions whose terminal no longer exists on the server. */
  retain(liveIds: Set<string>): void {
    for (const id of [...this.sessions.keys()]) {
      if (!liveIds.has(id)) this.drop(id)
    }
  }

  /** Apply a font size to every live session and to sessions created later. */
  setFontSize(px: number): void {
    this.fontSize = px
    for (const session of this.sessions.values()) session.setFontSize(px)
  }

  has(terminalId: string): boolean {
    return this.sessions.has(terminalId)
  }

  size(): number {
    return this.sessions.size
  }

  disposeAll(): void {
    for (const id of [...this.sessions.keys()]) this.drop(id)
    this.holder?.remove()
    this.holder = null
  }

  private touch(id: string) {
    this.order = this.order.filter(x => x !== id)
    this.order.push(id)
  }

  private evict() {
    for (const id of [...this.order]) {
      if (this.sessions.size <= this.max) return
      if (!this.mounted.has(id)) this.drop(id)
    }
  }

  private drop(id: string) {
    this.sessions.get(id)?.dispose()
    this.sessions.delete(id)
    this.mounted.delete(id)
    this.order = this.order.filter(x => x !== id)
  }

  private park(session: XtermSession) {
    this.getHolder().appendChild(session.element)
  }

  private getHolder(): HTMLDivElement {
    if (!this.holder) {
      const h = document.createElement('div')
      h.setAttribute('aria-hidden', 'true')
      h.setAttribute(PARKED_ATTR, '')
      h.style.position = 'fixed'
      h.style.left = '-100000px'
      h.style.top = '0'
      h.style.visibility = 'hidden'
      h.style.pointerEvents = 'none'
      document.body.appendChild(h)
      this.holder = h
    }
    return this.holder
  }
}
