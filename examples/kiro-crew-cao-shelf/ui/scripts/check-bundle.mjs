// Guards for the committed bundle (ui/bundle/index.mjs). Run after `npm run build`.
import { readFileSync, statSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const file = resolve(dirname(fileURLToPath(import.meta.url)), '../bundle/index.mjs')
const MAX = 120 * 1024
const src = readFileSync(file, 'utf8')
const size = statSync(file).size
const problems = []

if (size >= MAX) problems.push(`bundle is ${size} bytes, limit ${MAX}`)
if (/dangerouslySetInnerHTML|\.innerHTML\s*=|insertAdjacentHTML|document\.write/.test(src)) problems.push('bundle contains an HTML injection sink')
if (/\bfetch\(\s*[`'"]https?:/.test(src) || /\b(XMLHttpRequest|WebSocket|EventSource|sendBeacon)\b/.test(src)) problems.push('bundle makes a network call that is not through the SDK')
if (/\bfetch\(/.test(src)) problems.push('bundle calls fetch directly; use useAppApi')
// "running" as a status value or label: only the sentence "Idle is not running." may contain it.
const stripped = src.replace(/Idle is not running\./g, '')
if (/running/i.test(stripped)) problems.push('bundle contains the word "running" outside the honest sentence')
for (const m of src.matchAll(/https?:\/\/[^\s"'`)<>]+/g)) {
  if (!/^https?:\/\/(127\.0\.0\.1|localhost|www\.w3\.org|reactjs\.org|react\.dev)/.test(m[0])) problems.push(`unexpected absolute URL: ${m[0]}`)
}
if (!/\/apps\/cao-shelf\/api/.test(src)) problems.push('bundle does not reference the app API base path')
// A named import of a host-kit export the host lacks fails the whole bundle at load, so only names in
// the host's base list at the declared minimum may be imported by name (ui/test/kit-names.ts holds the
// same list); Select and guideCaution are read through a namespace import (src/components/kit.tsx).
const KIT_NAMED = new Set(['Badge', 'Btn', 'Card', 'CardTitle', 'EmptyState', 'ErrorNotice', 'Input', 'Modal', 'PageHeader', 'Skeleton', 'Toggle'])
for (const m of src.matchAll(/import\s*\{([^}]*)\}\s*from\s*["']@kirocrew\/app-sdk\/ui["']/g)) {
  for (const part of m[1].split(',')) {
    const name = part.trim().split(/\s+as\s+/)[0]
    if (name && !KIT_NAMED.has(name)) problems.push(`named import of ${name} from @kirocrew/app-sdk/ui is not in the allowlist`)
  }
}
if (/import\s+\w+\s*(?:,\s*\{[^}]*\})?\s*from\s*["']@kirocrew\/app-sdk\/ui["']/.test(src)) problems.push('default import from @kirocrew/app-sdk/ui')

if (problems.length) {
  console.error('check-bundle FAILED:\n- ' + problems.join('\n- '))
  process.exit(1)
}
console.log(`check-bundle ok (${size} bytes)`)
