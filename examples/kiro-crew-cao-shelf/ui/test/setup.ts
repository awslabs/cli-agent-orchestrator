import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach, expect } from 'vitest'
import { toHaveNoViolations } from 'jest-axe'

// Node 26 ships its own (file-backed, unusable here) localStorage that shadows jsdom's.
class MemoryStorage {
  private m = new Map<string, string>()
  get length() { return this.m.size }
  key(i: number) { return [...this.m.keys()][i] ?? null }
  getItem(k: string) { return this.m.has(k) ? (this.m.get(k) as string) : null }
  setItem(k: string, v: string) { this.m.set(k, String(v)) }
  removeItem(k: string) { this.m.delete(k) }
  clear() { this.m.clear() }
}
Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: new MemoryStorage() })
Object.defineProperty(window, 'localStorage', { configurable: true, value: (globalThis as any).localStorage })

expect.extend(toHaveNoViolations)
afterEach(() => {
  cleanup()
  localStorage.clear()
})
