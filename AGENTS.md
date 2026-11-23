# SlicerBridge contributor instructions

## Repository quick reference

Retarget multicolor slicer projects to supported printers and verify the result. Python CLI and MCP server.

- **Layout.** slicerbridge/ owns engines and checks, tests/ builds synthetic projects, plugin/ contains client integration.
- **Local setup and validation.** `python3 -m venv .venv` then `.venv/bin/pip install -e ".[dev,mcp]"`. Run `.venv/bin/pytest` and `.venv/bin/ruff check .`. For engines, checks or printer changes run `SLICERBRIDGE_SLICER_TESTS=1 .venv/bin/pytest -m slicer` per CONTRIBUTING.md. Confirm any additional checks for the affected path in existing instructions and CI.
- **Delivery.** GitHub release workflow. CLI and local MCP server.
- **Jira.** Project SB.
- **Notion.** Canonical project page unconfirmed. Follow the issue’s context link or resolve it with the owner.

## Portfolio engineering workflow

1. **Pick up the issue.** For substantive changes, read the Jira issue, its acceptance criteria, dependencies and linked Notion context. Read this file, applicable directory instructions, contribution notes and the affected implementation before editing. Confirm the intended outcome with the owner if requirements are missing. Keep product strategy, research and durable decisions in Notion, execution status in Jira and implementation history in GitHub.
2. **Claim a single line of work.** Check Jira status, open PRs, remote branches, `git status --short --branch` and `git worktree list`. Resume the existing issue branch when one exists. Coordinate a single writer per branch. For independent tasks, use separate branches and worktrees. Preserve other contributors’ uncommitted changes.
3. **Name and scope changes.** Branch from the intended base using `type/PROJECT-123-short-description`. Use `feature`, `fix`, `chore`, `docs`, `refactor` or `test`. Include the real Jira key in each commit message and PR title. Work through PRs. Follow local code style, scoped instructions and existing CI, release and merge policies.
4. **Verify and review.** Compare the diff with each acceptance criterion. Run the relevant repository checks and record exact commands, outcomes, skipped checks and reasons. In the PR link Jira and describe implementation, validation, residual risk and deployment or rollback considerations. Keep incomplete work in draft. Seek review and resolve failures before merge. Production actions require separate authorization.
5. **Protect sensitive data.** Use approved local environment or secret-store interfaces. Keep credentials, personal data and production payloads out of commits, logs, agent prompts, fixtures and handoffs. Use synthetic inputs. Keep private Notion content in its access-controlled workspace.
6. **Hand off explicitly.** Checkpoint and push the issue branch when authorized. Record owner, base, branch, head SHA, PR, completed criteria, verification, blockers and the next action in Jira. Release ownership before the next agent writes. The next agent fetches the recorded branch, verifies its SHA and checks the live diff, PR and CI before resuming.
7. **Close against evidence.** Update Jira when work starts, enters review or is blocked, using available project statuses. Mark Done only when the accepted scope has been implemented, verified and delivered under the repository's review/merge policy. Link the final PR and evidence. For a durable decision, record rationale, alternatives, owner and date in the relevant Notion page and link it from Jira.
