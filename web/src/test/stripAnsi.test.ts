import { describe, it, expect } from 'vitest'
import { stripAnsi } from '../runs/OutputViewer'

describe('stripAnsi', () => {
  it('strips cursor show/hide private-mode sequences (the ␛[?25h leak)', () => {
    // Exact shape from the user's Full Output screenshot.
    expect(stripAnsi('\x1b[?25h\x1b[?25l2')).toBe('2')
    expect(stripAnsi('\x1b[?25h\x1b[?25l·')).toBe('·')
  })

  it('strips ST-terminated OSC-8 hyperlinks (the file:// link leak)', () => {
    // Exact shape from the user's Last Response screenshot.
    const input =
      'Write(\x1b]8;id=1lrgzhh;file:///mnt/c/Users/rkram/app.js\x1b\\app.js\x1b]8;;\x1b\\)'
    expect(stripAnsi(input)).toBe('Write(app.js)')
  })

  it('strips BEL-terminated OSC (terminal titles)', () => {
    expect(stripAnsi('\x1b]0;my title\x07hello')).toBe('hello')
  })

  it('still strips colors and cursor movement', () => {
    expect(stripAnsi('\x1b[31mred\x1b[0m \x1b[2Aup')).toBe('red up')
  })

  it('drops stray ESC bytes instead of rendering ␛', () => {
    expect(stripAnsi('a\x1bz')).toBe('az')
  })

  it('normalizes carriage returns and removes other control chars', () => {
    expect(stripAnsi('line1\r\nline2\rline3\x08\x07')).toBe('line1\nline2\nline3')
  })

  it('keeps tabs, newlines, and unicode text', () => {
    expect(stripAnsi('✻ Orchestrating…\n\tdone')).toBe('✻ Orchestrating…\n\tdone')
  })
})
