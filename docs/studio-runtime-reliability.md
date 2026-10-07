# Studio gateway and Desktop reliability

At 18:52 PDT on 2026-10-06, a vault-ingestion Claude subagent issued
`pkill -9 -f python`. launchd attributed the gateway SIGKILL wave to pkill PID
47759. The same command killed Desktop's Python backend and Headroom. Hermes.app
PID 16896 stayed alive. This was a fleet-wide operator command, not a GUI crash.

Gateways own scheduled work independently of Desktop. Desktop only ticks stores
with positively established absence of a gateway. A held per-home runtime lock
is authoritative even when profile identity parsing misses an inline bootstrap.
The lock probe never creates or unlinks another store's ownership file.
Ownership errors defer Desktop,
and ownership is checked again after the scheduler lock is acquired. Yielding
stores receive no Desktop heartbeat or delivery drain.

Keep launchd `SuccessfulExit=false`: EX_CONFIG 78 must park a misconfigured
gateway. Translate an ordinary gateway exit 0 into restart-request 75 in the
supervisor wrapper. Intentional service stop uses launchctl bootout. Non-gateway
subprocess exit codes retain their meaning. Never kill by a broad process name.

Install the contributed Claude Bash guard on Studio. Restart a gateway through
launchctl, or stop a scratch task by its verified PID. Leave the CFO job and
local/suppressed delivery unchanged; never clear a live execution claim.

## Verification

- [x] Ownership errors defer both built-in and external Desktop schedulers.
- [x] Gateway startup during tick admission causes no dispatch or heartbeat.
- [x] Gateway exit 0 restarts; EX_CONFIG 78 parks; other commands retain exit codes.
- [x] Process-name kill guard rejects the incident command without executing it.
- [x] Normal GUI quit preserves all gateway PIDs and the active CFO claim.
- [x] Two subsequent eligible CFO runs complete with GUI absent and local delivery.
- [ ] No further broad-kill wave appears during the observation window.

Secret-free launchctl/PID/receipt snapshots live outside the checkout under
`~/Archive/hermes-runtime-reliability-2026-10-06/`. Source changes ship through a
Copias-a-i PR with independent review; the builder does not merge it.

## Deployed experiment (2026-10-06 PDT)

Normal application quit at 20:15 preserved all 31 gateway PIDs and CFO's
running execution. A second normal quit at 20:25:25 left the GUI absent.
CFO execution `87ff39aa10f2434497895c503447a177` started at 20:27:22 and
completed at 20:37:43 on gateway PID 47964, with delivery suppressed.
After completion, an idle-window SIGUSR1 restart loaded the wrapper patch:
CFO PID 95403, supervisor/process group 95398. All 31 gateways report running;
all last-exit values are 75 from controlled drain-aware restarts.

An isolated launchd canary using the real patched wrapper started child PID
55464, exited 0, restarted as PID 56874, then exited 78 and parked at two
starts. The canary was unloaded. Installed-code probes confirm a live CFO
store is deferred and a positively absent gateway permits Desktop takeover.
The probe-error negative control fails when the error-state check is removed.

Fleet-wide installed-code probes initially exposed 22 live stores whose inline
bootstrap profile could not be resolved by the shared identity parser. The
held-lock guard now defers all 31 live stores. Its real cross-process test fails
before the repair and passes after it; an unlocked stale lock permits takeover
without deleting the file. Identity-parser repair is tracked separately.

The second post-quit execution `3ae366defa65405599a46784b39fbf45` started at
20:48:17 on patched gateway PID 95403 and completed at 20:56:23, delivery
suppressed. Both eligible post-quit runs completed with the GUI absent.
No further fleet SIGKILL or pkill event appears from 18:52:20 through final
verification. Runtime/ownership regression suite: 153 tests passed, 13 files.
The separately reviewed profile-parser repair (PR #2, COP-702) was backported
without a gateway restart; strict same-home PID probes now recognize all 31
live gateways. Its focused suite passes 97 tests with six platform skips.
