import { execFileSync } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
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

// The leader prints the descendant pid, then publishes it to argv[1] once
// `whenArmed(callback)`, a source expression, proves the descendant is armed.
function leaderSource(descendantSource, whenArmed) {
  return `
    const fs = require('node:fs')
    const { spawn } = require('node:child_process')
    const readyPath = process.argv[1]
    process.on('SIGTERM', () => process.exit(0))
    const grandchild = spawn(process.execPath, ['-e', ${JSON.stringify(descendantSource)}],
      { stdio: ['ignore', 'pipe', 'ignore'] })
    ;(${whenArmed})(() => {
      process.stdout.write(String(grandchild.pid) + '\\n', () => {
        fs.writeFileSync(readyPath + '.tmp', String(grandchild.pid))
        fs.renameSync(readyPath + '.tmp', readyPath)
      })
    })
    setInterval(() => {}, 1000)
  `
}

function holdUntilPublished(file, timeoutMs) {
  const until = Date.now() + timeoutMs
  const cell = new Int32Array(new SharedArrayBuffer(4))
  while (!fs.existsSync(file)) {
    if (Date.now() > until) throw new Error(`descendant was not armed within ${timeoutMs}ms`)
    Atomics.wait(cell, 0, 0, 10)
  }
  return Number(fs.readFileSync(file, 'utf8'))
}

/**
 * runBoundedChild arms its deadline synchronously when it spawns. Holding this
 * event loop until the leader publishes an armed descendant means that deadline
 * cannot fire first, however slowly the runner starts processes: the timeout
 * path is exercised against a live, armed tree on every run.
 */
async function timeOutArmedTree(source, options) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'bounded-child-'))
  try {
    const readyPath = path.join(dir, 'ready')
    const outcome = runBoundedChild(node, ['-e', source, readyPath], options)
      .then(() => null, error => error)
    const armedPid = holdUntilPublished(readyPath, 10_000)
    return { failure: await outcome, armedPid }
  } finally {
    fs.rmSync(dir, { recursive: true, force: true })
  }
}

test.skipIf(process.platform === 'win32')('timeout kills a SIGTERM-ignoring descendant after its leader exits', async () => {
  const source = leaderSource(
    "process.on('SIGTERM', () => {}); process.stdout.write('armed\\n'); setInterval(() => {}, 1000)",
    "callback => grandchild.stdout.once('data', callback)")
  const { failure, armedPid } = await timeOutArmedTree(source, { timeoutMs: 100, graceMs: 100 })
  expect(failure).toBeInstanceOf(Error)
  expect(failure.message).toContain('Child exceeded 100ms')
  expect(armedPid).toBeGreaterThan(1)
  expect(Number(failure.stdout.trim())).toBe(armedPid)
  await waitForGone(armedPid)
}, 20_000)

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
  const source = leaderSource(
    "process.stdout.write('armed\\n'); setInterval(() => {}, 1000)",
    "callback => grandchild.stdout.once('data', callback)")
  const { failure, armedPid } = await timeOutArmedTree(source, { timeoutMs: 100, graceMs: 100 })
  expect(failure).toBeInstanceOf(Error)
  expect(failure.message).toContain('Child exceeded 100ms')
  expect(armedPid).toBeGreaterThan(1)
  expect(Number(failure.stdout.trim())).toBe(armedPid)
  await waitForGone(armedPid)
  expect(execFileSync('tasklist.exe', ['/FI', `PID eq ${armedPid}`, '/NH'], { encoding: 'utf8' })).not.toContain(String(armedPid))
}, 20_000)
