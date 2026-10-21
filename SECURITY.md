# Security policy

BreakMyBot is a local developer tool. The current dashboard and API have no user authentication; keep the API bound to loopback and do not expose it to an untrusted network.

Report a vulnerability privately through GitHub's repository security advisory feature. Include affected revision, reproduction steps, and impact. Do not include live credentials or personal data in a report.

The target runner allows loopback by default, disables redirects, disables ambient proxy use, caps recorded response bodies, and supports explicit test-network allowlists. These are risk-reduction measures, not a complete SSRF or hostile-code sandbox. Do not test production targets or real financial operations. Docker is not a complete security boundary for arbitrary hostile applications.
