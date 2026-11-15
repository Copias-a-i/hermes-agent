# Routed manual cron regression investigation

The pristine organization baseline fails the dashboard sibling-profile script test: the fire is accepted but no routed output file exists. First inspect the persisted execution result to determine whether routing, script policy, or interpreter environment caused the failure. Reproduce through the canonical test runner. A runtime repair will be made only for a demonstrated runtime defect; an obsolete test fixture must instead exercise the supported script execution contract. Preserve profile-secret isolation and verify that the test rejects a broken isolation mechanism.

## Diagnosis and repair

The home-I/O guard correctly rejects installation discovery outside the worktree: optional payload metadata lives beside the checkout, and bootstrap recovery reads the shared Git metadata directory. Those side effects run only because these focused tests omit installation seams. No routed cron runtime defect was reproduced. The repairs make the routing fixture an ordinary checkout, prevent ticker unit tests from activating or recovering their real installation, and make the launchd failed-refresh fixture explicitly stale. The guard remains unchanged.

## Verification

The original sibling-profile test fails with no routed output; its persisted execution result reports a forbidden manifest read. The isolated ordinary-checkout seam runs the script with `routed-secret|<unset>` and retains the A/B/A isolation assertions. All four affected files pass, 46 tests. Removing the fixture isolation restores the original guard failure. No live service, job, claim, or credential is changed.
# Script termination fixture isolation

Reproduce the four descendant-termination failures on pristine origin/main before classifying them. Inspect the script runner result when the child readiness marker is absent. If ordinary-checkout launcher discovery is the cause, isolate only that fixture seam and retain real child spawning and process-tree termination assertions. No production or live service changes.

## Diagnosis and repair

A diagnostic-only change to the pristine baseline exposed the returned error: `Script execution failed: TEST BUG: file I/O against the REAL hermes home` for the sibling worktree manifest. The four cases share that script-launch path and fail before spawning their child, so they do not demonstrate a termination defect. Select the provisioned test interpreter instead of inspecting the installed PM store, and classify this fixture as an ordinary checkout without a payload manifest. Keep actual subprocess creation, detached/stubborn descendants, cancellation/timeout, and final liveness assertions unchanged. Include the returned execution results in readiness failures so future early launch errors are visible.

## Verification

The canonical runner passes the entire file: 20 tests, including all four real descendant cancellation/timeout cases. The initial baseline failure is the negative control for the omitted fixture isolation. The home-I/O guard is unchanged; no live service or job is accessed.
