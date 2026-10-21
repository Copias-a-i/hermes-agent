import { spawnSync } from 'node:child_process'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { expect, test } from 'vitest'

const script = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../scripts/sandbox/generate-e2e-matrix.mjs')

function renderResults(tags: string) {
  return spawnSync(process.execPath, [script, '--format', 'results', '--tags', tags], {
    encoding: 'utf8',
    input: '',
  })
}

test.each(['', '{bad json', '{}'])('result rendering rejects malformed picker output before JSON parsing: %j', tags => {
  const result = renderResults(tags)

  expect(result.status).not.toBe(0)
  expect(result.stderr).toContain('Result chart requires a non-empty JSON array of release-tag annotations')
  expect(result.stderr).not.toContain('Unexpected end of JSON input')
  expect(result.stderr).not.toContain('Unexpected token')
})

test('result rendering accepts the picker annotation array', () => {
  const result = renderResults(JSON.stringify([{ ref: 'v2026.6.19', desktop: true }]))

  expect(result.status).toBe(0)
  expect(result.stdout).toContain('(no legs found in this run)')
})
