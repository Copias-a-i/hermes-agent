# Routed manual cron regression investigation

The pristine organization baseline fails the dashboard sibling-profile script test: the fire is accepted but no routed output file exists. First inspect the persisted execution result to determine whether routing, script policy, or interpreter environment caused the failure. Reproduce through the canonical test runner. A runtime repair will be made only for a demonstrated runtime defect; an obsolete test fixture must instead exercise the supported script execution contract. Preserve profile-secret isolation and verify that the test rejects a broken isolation mechanism.

## Diagnosis and repair

The home-I/O guard correctly rejects installation discovery outside the worktree: optional payload metadata lives beside the checkout, and bootstrap recovery reads the shared Git metadata directory. Those side effects run only because these focused tests omit installation seams. No routed cron runtime defect was reproduced. The repairs make the routing fixture an ordinary checkout, prevent ticker unit tests from activating or recovering their real installation, and make the launchd failed-refresh fixture explicitly stale. The guard remains unchanged.

## Verification

The original sibling-profile test fails with no routed output; its persisted execution result reports a forbidden manifest read. The isolated ordinary-checkout seam runs the script with `routed-secret|<unset>` and retains the A/B/A isolation assertions. All four affected files pass, 46 tests. Removing the fixture isolation restores the original guard failure. No live service, job, claim, or credential is changed.
