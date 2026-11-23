export type PythonTokenType =
  | 'keyword'
  | 'builtin'
  | 'string'
  | 'comment'
  | 'number'
  | 'decorator'
  | 'definition'
  | 'operator'
  | 'plain'

export interface PythonToken {
  type: PythonTokenType
  text: string
}

export const PYTHON_HIGHLIGHT_SIZE_CAP = 500 * 1024

const KEYWORDS = new Set([
  'False', 'None', 'True', 'and', 'as', 'assert', 'async', 'await', 'break',
  'class', 'continue', 'def', 'del', 'elif', 'else', 'except',
  'finally', 'for', 'from', 'global', 'if', 'import', 'in', 'is', 'lambda',
  'nonlocal', 'not', 'or', 'pass', 'raise', 'return', 'try', 'while',
  'with', 'yield',
])

const BUILTINS = new Set([
  'abs', 'all', 'any', 'bin', 'bool', 'breakpoint', 'bytearray', 'bytes',
  'callable', 'chr', 'classmethod', 'compile', 'complex', 'delattr', 'dict',
  'dir', 'divmod', 'enumerate', 'eval', 'exec', 'filter', 'float', 'format',
  'frozenset', 'getattr', 'globals', 'hasattr', 'hash', 'help', 'hex', 'id',
  'input', 'int', 'isinstance', 'issubclass', 'iter', 'len', 'list', 'locals',
  'map', 'max', 'memoryview', 'min', 'next', 'object', 'oct', 'open', 'ord',
  'pow', 'print', 'property', 'range', 'repr', 'reversed', 'round', 'set',
  'setattr', 'slice', 'sorted', 'staticmethod', 'str', 'sum', 'super', 'tuple',
  'type', 'vars', 'zip', '__import__',
])

const isIdentifierStart = (c: string | undefined) => !!c && /[A-Za-z_]/.test(c)
const isIdentifierPart = (c: string | undefined) => !!c && /[A-Za-z0-9_]/.test(c)
const isDigit = (c: string | undefined) => !!c && c >= '0' && c <= '9'
const isHexDigit = (c: string | undefined) => !!c && /[0-9a-fA-F]/.test(c)
const isPrefixLetter = (c: string | undefined) => !!c && /[rRbBfFuU]/.test(c)
const isOperator = (c: string | undefined) => !!c && '+-*/%@<>=!&|^~:;,.()[]{}'.includes(c)

const softKeywordEndsPattern = (source: string, afterWord: number) => {
  let index = afterWord
  while (source[index] === ' ' || source[index] === '\t') index++
  if ('=. ,'.includes(source[index] ?? '')) return false

  let lastCodeCharacter = ''
  while (index < source.length && source[index] !== '\n' && source[index] !== '#') {
    const character = source[index]
    if (character !== ' ' && character !== '\t' && character !== '\r') lastCodeCharacter = character
    index++
  }
  return lastCodeCharacter === ':'
}

/**
 * A deliberately small, recovery-friendly Python lexer for display only.
 * Every loop consumes at least one character and every character is visited a
 * bounded number of times, so malformed input remains linear and never throws.
 */
export function tokenizePython(source: string): PythonToken[] {
  if (!source || source.length > PYTHON_HIGHLIGHT_SIZE_CAP) {
    return source ? [{ type: 'plain', text: source }] : []
  }

  const tokens: PythonToken[] = []
  let i = 0
  let expectDefinition = false
  let atLogicalLineStart = true
  let bracketDepth = 0
  const push = (type: PythonTokenType, start: number, end: number) => {
    tokens.push({ type, text: source.slice(start, end) })
    for (let index = start; index < end; index++) {
      const character = source[index]
      if (type !== 'string' && type !== 'comment') {
        if ('([{'.includes(character)) bracketDepth++
        else if (')]}'.includes(character)) bracketDepth = Math.max(0, bracketDepth - 1)
      }
      if (character === '\n') atLogicalLineStart = bracketDepth === 0
      else if (source[index] !== ' ' && source[index] !== '\t' && source[index] !== '\r') atLogicalLineStart = false
    }
  }

  while (i < source.length) {
    const start = i
    const c = source[i]

    if (c === '#') {
      while (i < source.length && source[i] !== '\n') i++
      push('comment', start, i)
      continue
    }

    // A string can begin at a quote, or after a case-insensitive combination
    // of Python's r/b/f/u prefix letters.
    let quoteAt = i
    if (isPrefixLetter(c)) {
      while (quoteAt < source.length && isPrefixLetter(source[quoteAt]) && quoteAt - i < 4) quoteAt++
    }
    if (source[quoteAt] === "'" || source[quoteAt] === '"') {
      const quote = source[quoteAt]
      const triple = source.slice(quoteAt, quoteAt + 3) === quote.repeat(3)
      i = quoteAt + (triple ? 3 : 1)
      while (i < source.length) {
        if (!triple && source[i] === '\n') break
        if (source[i] === '\\') {
          i += Math.min(2, source.length - i)
          continue
        }
        if (triple ? source.slice(i, i + 3) === quote.repeat(3) : source[i] === quote) {
          i += triple ? 3 : 1
          break
        }
        i++
      }
      push('string', start, i)
      continue
    }

    if (c === '@' && atLogicalLineStart && isIdentifierStart(source[i + 1])) {
      i += 2
      while (isIdentifierPart(source[i]) || source[i] === '.') i++
      push('decorator', start, i)
      continue
    }

    if (isDigit(c) || (c === '.' && isDigit(source[i + 1]))) {
      if (c === '0' && /[xX]/.test(source[i + 1] ?? '')) {
        i += 2
        while (isHexDigit(source[i]) || source[i] === '_') i++
      } else if (c === '0' && /[bB]/.test(source[i + 1] ?? '')) {
        i += 2
        while (source[i] === '0' || source[i] === '1' || source[i] === '_') i++
      } else if (c === '0' && /[oO]/.test(source[i + 1] ?? '')) {
        i += 2
        while ((source[i] >= '0' && source[i] <= '7') || source[i] === '_') i++
      } else {
        if (c === '.') i++
        while (isDigit(source[i]) || source[i] === '_') i++
        if (source[i] === '.') {
          i++
          while (isDigit(source[i]) || source[i] === '_') i++
        }
        if (source[i] === 'e' || source[i] === 'E') {
          const exponent = i++
          if (source[i] === '+' || source[i] === '-') i++
          const digits = i
          while (isDigit(source[i]) || source[i] === '_') i++
          if (digits === i) i = exponent
        }
        if (source[i] === 'j' || source[i] === 'J') i++
      }
      push('number', start, i)
      continue
    }

    if (isIdentifierStart(c)) {
      const startsLogicalLine = atLogicalLineStart
      i++
      while (isIdentifierPart(source[i])) i++
      const word = source.slice(start, i)
      const type = expectDefinition
        ? 'definition'
        : (word === 'match' || word === 'case') && startsLogicalLine && softKeywordEndsPattern(source, i)
          ? 'keyword'
          : KEYWORDS.has(word)
          ? 'keyword'
          : BUILTINS.has(word)
            ? 'builtin'
            : 'plain'
      push(type, start, i)
      expectDefinition = word === 'def' || word === 'class'
      continue
    }

    if (isOperator(c)) {
      i++
      while (isOperator(source[i])) i++
      push('operator', start, i)
      continue
    }

    i++
    while (
      i < source.length
      && source[i] !== '#'
      && !isPrefixLetter(source[i])
      && !isIdentifierStart(source[i])
      && !isDigit(source[i])
      && !(source[i] === '.' && isDigit(source[i + 1]))
      && !isOperator(source[i])
      && source[i] !== "'"
      && source[i] !== '"'
    ) i++
    push('plain', start, i)
  }

  return tokens
}
