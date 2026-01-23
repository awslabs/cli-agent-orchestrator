import '@testing-library/jest-dom'
import { vi } from 'vitest'

// Mock WebSocket
class MockWebSocket {
  onmessage: ((e: { data: string }) => void) | null = null
  close() {}
}
global.WebSocket = MockWebSocket as any

// Mock fetch
global.fetch = vi.fn(() => Promise.resolve({ json: () => Promise.resolve([]) })) as any

// Mock ResizeObserver
global.ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
}
