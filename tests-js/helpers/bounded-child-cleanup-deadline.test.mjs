import { afterEach, expect, test, vi } from 'vitest'

const state = vi.hoisted(() => ({ child: null }))

vi.mock('node:child_process', async () => {
  const { EventEmitter } = await import('node:events')
  const makeStream = () => {
    const stream = new EventEmitter()
    stream.destroy = vi.fn()
    stream.unpipe = vi.fn()
    return stream
  }
  return {
    execFileSync: vi.fn(),
    spawn: vi.fn(() => {
      const child = new EventEmitter()
      child.pid = 4242
      child.stdout = makeStream()
      child.stderr = makeStream()
      state.child = child
      return child
    }),
  }
})

import { runBoundedChild } from './bounded-child.mjs'

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
  state.child = null
})

test('cleanup deadline rejects and releases resources when group absence remains unverifiable', async () => {
  vi.useFakeTimers()
  vi.spyOn(process, 'kill').mockImplementation(() => undefined)

  const result = runBoundedChild('ignored', [], {
    timeoutMs: 10,
    graceMs: 5,
    cleanupTimeoutMs: 25,
  })
  const rejection = expect(result).rejects.toThrow('Cleanup unverified: process group 4242 remained after 25ms')
  await vi.advanceTimersByTimeAsync(10)
  state.child.emit('close', 0, null)
  await vi.advanceTimersByTimeAsync(25)

  await rejection
  expect(state.child.stdout.destroy).toHaveBeenCalledOnce()
  expect(state.child.stderr.destroy).toHaveBeenCalledOnce()
  expect(state.child.stdout.unpipe).toHaveBeenCalledOnce()
  expect(state.child.stderr.unpipe).toHaveBeenCalledOnce()
  expect(state.child.listenerCount('close')).toBe(0)
  expect(state.child.listenerCount('error')).toBe(0)
  expect(vi.getTimerCount()).toBe(0)
})
