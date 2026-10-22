import { execFileSync } from 'node:child_process'
import { expect, test } from 'vitest'
import { runBoundedChild } from './bounded-child.mjs'

const node = process.execPath

function isRunning(pid) {
  try {
    process.kill(pid, 0)
    return true
  } catch (error) {
    if (error.code === 'ESRCH') return false
    throw error
  }
}

async function waitForGone(pid, timeoutMs = 2_000) {
  const until = Date.now() + timeoutMs
  while (Date.now() < until) {
    if (!isRunning(pid)) return
    await new Promise(resolve => setTimeout(resolve, 25))
  }
  expect(isRunning(pid), `child ${pid} still running`).toBe(false)
}

test.skipIf(process.platform === 'win32')('timeout kills a SIGTERM-ignoring descendant after its leader exits', async () => {
  const source = `
    const { spawn } = require('node:child_process')
    const grandchild = spawn(process.execPath, ['-e', \
      "process.on('SIGTERM', () => {}); setInterval(() => {}, 1000)"], { stdio: 'ignore' })
    process.stdout.write(String(grandchild.pid) + '\\n')
    process.on('SIGTERM', () => process.exit(0))
    setInterval(() => {}, 1000)
  `
  let failure
  try {
    await runBoundedChild(node, ['-e', source], { timeoutMs: 100, graceMs: 100 })
  } catch (error) {
    failure = error
  }
  expect(failure).toBeInstanceOf(Error)
  const pid = Number(failure.stdout.trim())
  expect(pid).toBeGreaterThan(1)
  await waitForGone(pid)
})

test('output limits cap stdout, stderr, and their combined size without retaining unbounded output', async () => {
  const source = 'process.stdout.write("a".repeat(8192)); process.stderr.write("b".repeat(8192)); setInterval(() => {}, 1000)'
  let failure
  try {
    await runBoundedChild(node, ['-e', source], {
      timeoutMs: 2_000,
      maxStdoutBytes: 512,
      maxStderrBytes: 512,
      maxOutputBytes: 768,
    })
  } catch (error) {
    failure = error
  }
  expect(failure).toBeInstanceOf(Error)
  expect(failure.outputTruncated).toBe(true)
  expect(Buffer.byteLength(failure.stdout)).toBeLessThanOrEqual(512)
  expect(Buffer.byteLength(failure.stderr)).toBeLessThanOrEqual(512)
  expect(Buffer.byteLength(failure.stdout) + Buffer.byteLength(failure.stderr)).toBeLessThanOrEqual(768)
  expect(failure.message).toContain('output limit')
})

test('stderr and combined-output ceilings terminate independently bounded streams', async () => {
  for (const [source, limits] of [
    ['process.stderr.write("b".repeat(8192)); setInterval(() => {}, 1000)', { maxStdoutBytes: 512, maxStderrBytes: 512, maxOutputBytes: 1_024 }],
    ['process.stdout.write("a".repeat(400)); process.stderr.write("b".repeat(400)); setInterval(() => {}, 1000)', { maxStdoutBytes: 512, maxStderrBytes: 512, maxOutputBytes: 768 }],
  ]) {
    let failure
    try {
      await runBoundedChild(node, ['-e', source], { timeoutMs: 2_000, ...limits })
    } catch (error) {
      failure = error
    }
    expect(failure).toBeInstanceOf(Error)
    expect(failure.outputTruncated).toBe(true)
    expect(Buffer.byteLength(failure.stdout)).toBeLessThanOrEqual(limits.maxStdoutBytes)
    expect(Buffer.byteLength(failure.stderr)).toBeLessThanOrEqual(limits.maxStderrBytes)
    expect(Buffer.byteLength(failure.stdout) + Buffer.byteLength(failure.stderr)).toBeLessThanOrEqual(limits.maxOutputBytes)
  }
})

test.runIf(process.platform === 'win32')('timeout uses taskkill to remove a Windows descendant tree', async () => {
  const source = `
    const { spawn } = require('node:child_process')
    const grandchild = spawn(process.execPath, ['-e', 'setInterval(() => {}, 1000)'], { stdio: 'ignore' })
    process.stdout.write(String(grandchild.pid) + '\\n')
    setInterval(() => {}, 1000)
  `
  let failure
  try {
    await runBoundedChild(node, ['-e', source], { timeoutMs: 100, graceMs: 100 })
  } catch (error) {
    failure = error
  }
  expect(failure).toBeInstanceOf(Error)
  const pid = Number(failure.stdout.trim())
  expect(pid).toBeGreaterThan(1)
  await waitForGone(pid)
  expect(execFileSync('tasklist.exe', ['/FI', `PID eq ${pid}`, '/NH'], { encoding: 'utf8' })).not.toContain(String(pid))
})
