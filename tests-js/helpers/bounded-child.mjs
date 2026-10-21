import { spawn } from 'node:child_process'

function terminate(child) {
  if (child.exitCode !== null || child.signalCode !== null) return
  if (process.platform !== 'win32' && child.pid) {
    try { process.kill(-child.pid, 'SIGTERM') } catch { /* process group already exited */ }
  }
  try { child.kill('SIGTERM') } catch { /* child already exited */ }
}

/**
 * Runs a test subprocess with a deadline and closes its process group on timeout.
 * The result intentionally includes non-zero exits so callers can assert refusal
 * paths without losing stdout/stderr diagnostics.
 */
export function runBoundedChild(file, args, { cwd, env, timeoutMs = 5_000 } = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(file, args, {
      cwd,
      env,
      detached: process.platform !== 'win32',
      stdio: ['ignore', 'pipe', 'pipe'],
    })
    let stdout = ''
    let stderr = ''
    let timedOut = false
    let forceKill
    const deadline = setTimeout(() => {
      timedOut = true
      terminate(child)
      forceKill = setTimeout(() => {
        if (child.exitCode === null && child.signalCode === null) {
          if (process.platform !== 'win32' && child.pid) {
            try { process.kill(-child.pid, 'SIGKILL') } catch { /* process group already exited */ }
          }
          try { child.kill('SIGKILL') } catch { /* child already exited */ }
        }
      }, 250)
      forceKill.unref()
    }, timeoutMs)
    deadline.unref()
    child.stdout.setEncoding('utf8')
    child.stderr.setEncoding('utf8')
    child.stdout.on('data', chunk => { stdout += chunk })
    child.stderr.on('data', chunk => { stderr += chunk })
    child.once('error', error => {
      clearTimeout(deadline)
      clearTimeout(forceKill)
      reject(error)
    })
    child.once('close', (code, signal) => {
      clearTimeout(deadline)
      clearTimeout(forceKill)
      if (timedOut) {
        reject(new Error(`Child exceeded ${timeoutMs}ms: ${file} ${args.join(' ')}\n${stderr}`))
        return
      }
      resolve({ code, signal, stdout, stderr })
    })
  })
}
