import { execFileSync, spawn } from 'node:child_process'

const DEFAULT_OUTPUT_BYTES = 8_388_608
const DEFAULT_GRACE_MS = 250
const CLEANUP_TIMEOUT_MS = 5_000

function groupExists(pgid) {
  try {
    process.kill(-pgid, 0)
    return true
  } catch (error) {
    // Only ESRCH proves absence. Treat permission and probe errors as a live,
    // unverified group so the cleanup deadline retains ownership of settlement.
    return error.code !== 'ESRCH'
  }
}

function signalGroup(pgid, signal) {
  if (!groupExists(pgid)) return false
  process.kill(-pgid, signal)
  return true
}

function clippedText(chunks) {
  return Buffer.concat(chunks).toString('utf8')
}

function failure(message, details) {
  const error = new Error(message)
  Object.assign(error, details)
  return error
}

/**
 * Runs a test subprocess with a deadline, output ceilings, and tree cleanup.
 * On POSIX it owns a new process group; after TERM it still sends KILL while
 * that group exists, even if the spawned leader has already exited. On Windows
 * taskkill /T /F is the supported tree termination mechanism.
 */
export function runBoundedChild(file, args, {
  cwd,
  env,
  timeoutMs = 5_000,
  graceMs = DEFAULT_GRACE_MS,
  maxStdoutBytes = DEFAULT_OUTPUT_BYTES,
  maxStderrBytes = DEFAULT_OUTPUT_BYTES,
  maxOutputBytes = DEFAULT_OUTPUT_BYTES * 2,
  cleanupTimeoutMs = CLEANUP_TIMEOUT_MS,
} = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(file, args, {
      cwd,
      env,
      detached: process.platform !== 'win32',
      stdio: ['ignore', 'pipe', 'pipe'],
    })
    const pgid = child.pid
    const output = { stdout: [], stderr: [], stdoutBytes: 0, stderrBytes: 0, totalBytes: 0 }
    let reason = null
    let outputTruncated = false
    let cleanupFailure = null
    let closed = false
    let settled = false
    let forceKill
    let cleanupDeadline
    let cleanupPoll
    let onStdout
    let onStderr
    let onError
    let onClose
    let cleanedUp = false

    const details = () => ({
      stdout: clippedText(output.stdout),
      stderr: clippedText(output.stderr),
      outputTruncated,
    })
    const clearTimers = () => {
      clearTimeout(deadline)
      clearTimeout(forceKill)
      clearTimeout(cleanupDeadline)
      clearTimeout(cleanupPoll)
    }
    const safely = callback => {
      try { callback?.() } catch { /* teardown must not replace the original outcome */ }
    }
    const cleanup = () => {
      if (cleanedUp) return
      cleanedUp = true
      clearTimers()
      for (const [stream, listener] of [[child.stdout, onStdout], [child.stderr, onStderr]]) {
        if (!stream) continue
        safely(() => listener && stream.removeListener('data', listener))
        safely(() => stream.unpipe?.())
        safely(() => stream.destroy?.())
      }
      safely(() => onError && child.removeListener('error', onError))
      safely(() => onClose && child.removeListener('close', onClose))
    }
    const rejectOnce = error => {
      if (settled) return
      settled = true
      cleanup()
      reject(error)
    }
    const resolveOnce = value => {
      if (settled) return
      settled = true
      cleanup()
      resolve(value)
    }
    const finishFailure = extra => {
      const error = failure(`${reason}: ${file} ${args.join(' ')}\n${details().stderr}${extra || ''}`, details())
      rejectOnce(error)
    }
    const settleAfterCleanup = () => {
      if (settled || !closed || !reason) return
      if (process.platform === 'win32' || !groupExists(pgid)) {
        finishFailure(cleanupFailure ? `\nCleanup failure: ${cleanupFailure}` : '')
      }
    }
    const awaitGroupAbsence = () => {
      if (settled || !groupExists(pgid)) return settleAfterCleanup()
      cleanupPoll = setTimeout(awaitGroupAbsence, 25)
      cleanupPoll.unref()
    }
    const forceTerminate = () => {
      if (process.platform === 'win32') return
      try {
        // Do not use child.exitCode as the decision: descendants can remain.
        signalGroup(pgid, 'SIGKILL')
      } catch (error) {
        cleanupFailure = `SIGKILL process group failed: ${error.message}`
      }
      awaitGroupAbsence()
    }
    const terminateWindowsTree = () => {
      try {
        execFileSync('taskkill.exe', ['/PID', String(child.pid), '/T', '/F'], {
          timeout: CLEANUP_TIMEOUT_MS,
          stdio: 'ignore',
          windowsHide: true,
        })
      } catch (error) {
        cleanupFailure = `taskkill /T /F failed: ${error.message}`
        try { child.kill('SIGKILL') } catch { /* report unverified tree cleanup below */ }
      }
      settleAfterCleanup()
    }
    const beginTermination = why => {
      if (reason) return
      reason = why
      if (process.platform === 'win32') {
        terminateWindowsTree()
        return
      }
      try {
        // A live process group cannot have reused this PGID: its members retain it.
        signalGroup(pgid, 'SIGTERM')
      } catch (error) {
        cleanupFailure = `SIGTERM process group failed: ${error.message}`
      }
      forceKill = setTimeout(forceTerminate, graceMs)
      forceKill.unref()
      cleanupDeadline = setTimeout(() => {
        const cleanupUnverified = `process group ${pgid} remained after ${cleanupTimeoutMs}ms`
        cleanupFailure ||= cleanupUnverified
        finishFailure(`\nCleanup unverified: ${cleanupUnverified}`)
      }, cleanupTimeoutMs)
      cleanupDeadline.unref()
    }
    const deadline = setTimeout(() => beginTermination(`Child exceeded ${timeoutMs}ms`), timeoutMs)
    deadline.unref()

    const capture = (stream, chunk, streamLimit) => {
      const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk)
      const available = Math.max(0, Math.min(streamLimit - output[`${stream}Bytes`], maxOutputBytes - output.totalBytes))
      const captured = Math.min(available, bytes.length)
      if (captured < bytes.length) {
        outputTruncated = true
        beginTermination(`Child exceeded output limit (${stream})`)
      }
      if (captured > 0) {
        output[stream].push(bytes.subarray(0, captured))
        output[`${stream}Bytes`] += captured
        output.totalBytes += captured
      }
    }
    onStdout = chunk => capture('stdout', chunk, maxStdoutBytes)
    onStderr = chunk => capture('stderr', chunk, maxStderrBytes)
    child.stdout.on('data', onStdout)
    child.stderr.on('data', onStderr)
    onError = error => {
      if (!reason) rejectOnce(error)
    }
    child.once('error', onError)
    onClose = (code, signal) => {
      closed = true
      if (!reason) {
        resolveOnce({ code, signal, ...details() })
        return
      }
      settleAfterCleanup()
    }
    child.once('close', onClose)
  })
}
