import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { PYTHON_HIGHLIGHT_SIZE_CAP, tokenizePython } from '../components/workflow/pythonHighlight'

const text = (source: string, type: string) =>
  tokenizePython(source).filter(token => token.type === type).map(token => token.text)
const joined = (source: string) => tokenizePython(source).map(token => token.text).join('')

describe('tokenizePython', () => {
  it('colours keywords, builtins, decorators, definitions, numbers and operators', () => {
    const source = '@decorators.task\nasync def build(value=0xCA_FE, ratio=1.25e-3):\n    return print(value)\nclass Report:\n    pass\n'
    expect(text(source, 'decorator')).toContain('@decorators.task')
    expect(text(source, 'keyword')).toEqual(expect.arrayContaining(['async', 'def', 'return', 'class', 'pass']))
    expect(text(source, 'definition')).toEqual(['build', 'Report'])
    expect(text(source, 'builtin')).toEqual(['print'])
    expect(text(source, 'number')).toEqual(expect.arrayContaining(['0xCA_FE', '1.25e-3']))
    expect(text(source, 'operator').join('')).toContain('():')
  })

  it('keeps triple docstrings, f-strings and mixed-case prefixed strings intact', () => {
    const source = '"""one\n# still text\ntwo"""\nRF"value={value}"\nbrU\'bytes-ish\''
    expect(text(source, 'string')).toEqual([
      '"""one\n# still text\ntwo"""',
      'RF"value={value}"',
      "brU'bytes-ish'",
    ])
    expect(text(source, 'comment')).toEqual([])
  })

  it('does not treat a hash in a string as a comment or a quote in a comment as a string', () => {
    const source = 'value = "not # a comment" # comment with "\n'
    expect(text(source, 'string')).toEqual(['"not # a comment"'])
    expect(text(source, 'comment')).toEqual(['# comment with "'])
  })

  it('recovers from unterminated strings and empty input without throwing', () => {
    expect(text("'unfinished\nnext", 'string')).toEqual(["'unfinished"])
    expect(() => tokenizePython('items = [1, 2')).not.toThrow()
    expect(tokenizePython('')).toEqual([])
  })

  it('preserves every source character in representative workflow scripts', () => {
    for (const path of [
      '../examples/workflow/workflow.py',
      '../examples/workflows/pr-health/pr_health.py',
      '../examples/workflows/pr-health/pr_health_biweekly_guard.py',
    ]) {
      const source = readFileSync(join(process.cwd(), path), 'utf-8')
      expect(joined(source)).toBe(source)
    }
  })

  it('falls back above the cap and handles a large input in bounded time', () => {
    const belowCap = 'result = print(123_456)\n'.repeat(15_000)
    const started = performance.now()
    expect(joined(belowCap)).toBe(belowCap)
    expect(performance.now() - started).toBeLessThan(1_000)

    const aboveCap = 'x'.repeat(PYTHON_HIGHLIGHT_SIZE_CAP + 1)
    expect(tokenizePython(aboveCap)).toEqual([{ type: 'plain', text: aboveCap }])
  })
})
