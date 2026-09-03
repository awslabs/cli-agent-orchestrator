import { describe, it, expect } from 'vitest'
import { renderHook } from '@testing-library/react'
import { useGeneration } from '../hooks/useGeneration'

describe('useGeneration', () => {
  it('a token is current until a newer begin() supersedes it', () => {
    const { result } = renderHook(() => useGeneration())
    const first = result.current.begin()
    expect(first.isCurrent()).toBe(true)
    const second = result.current.begin()
    expect(first.isCurrent()).toBe(false)
    expect(second.isCurrent()).toBe(true)
  })

  it('invalidate() stales every outstanding token without starting new work', () => {
    const { result } = renderHook(() => useGeneration())
    const token = result.current.begin()
    result.current.invalidate()
    expect(token.isCurrent()).toBe(false)
    // and work begun AFTER the invalidation is current
    expect(result.current.begin().isCurrent()).toBe(true)
  })

  it('the returned object is referentially stable across renders', () => {
    const { result, rerender } = renderHook(() => useGeneration())
    const before = result.current
    const token = before.begin()
    rerender()
    expect(result.current).toBe(before)
    // tokens issued before a render still bind to the same counter
    expect(token.isCurrent()).toBe(true)
    result.current.invalidate()
    expect(token.isCurrent()).toBe(false)
  })

  it('two instances are independent counters', () => {
    const { result: a } = renderHook(() => useGeneration())
    const { result: b } = renderHook(() => useGeneration())
    const tokenA = a.current.begin()
    b.current.invalidate()
    expect(tokenA.isCurrent()).toBe(true)
  })
})
