# Releasing

A release is a git tag. Pushing `v<version>` runs `.github/workflows/release.yml`, which:

1. checks that the tag, the package, the plugin's manifest, the plugin's pinned launch command, `server.json`
   and `CHANGELOG.md` all name the same version (`scripts/release.py check`), and runs lint and the tests;
2. builds the package and publishes it to [PyPI](https://pypi.org/project/slicerbridge/) with trusted
   publishing, so no token is stored anywhere;
3. publishes `server.json` to the [MCP Registry](https://registry.modelcontextprotocol.io), logged in with the
   workflow's GitHub OIDC identity, which proves the `io.github.UnicornGraveyard` namespace;
4. creates the GitHub release with the built files and the version's CHANGELOG notes.

## Each release

1. Set the new version in `slicerbridge/__init__.py`, `server.json` (twice), `plugin/.claude-plugin/plugin.json`
   and the pin in `plugin/.mcp.json`, and add its `CHANGELOG.md` section.
   `python scripts/release.py check <version>` says what's left; the tests check it too.
2. Run the slicer tests on a Mac with the slicers installed: `SLICERBRIDGE_SLICER_TESTS=1 pytest -m slicer`.
3. Commit, push, wait for CI, then tag and push the tag:
   ```bash
   git tag v0.2.0 && git push origin v0.2.0
   ```

A version number can be used on PyPI once only, even if that release is deleted.

## One-time setup (done once, by the account owner)

- **PyPI:** a PyPI account with two-factor authentication, and a *pending trusted publisher* for the project
  (Account settings, Publishing, Add a new pending publisher, GitHub): project `slicerbridge`, owner
  `UnicornGraveyard`, repository `SlicerBridge`, workflow `release.yml`, environment `pypi`. The first
  release then creates the project.
- **GitHub (optional):** the `pypi` environment is created by the first release. To have every PyPI publish
  wait for approval, add yourself as a required reviewer under Settings, Environments, `pypi`.
- **MCP Registry:** nothing. The workflow's OIDC login is enough.
- **Anthropic's directory:** submit the plugin once from a paid claude.ai account at
  [claude.ai/directory/manage](https://claude.ai/directory/manage): Submit new, Plugin bundle, repository
  `UnicornGraveyard/SlicerBridge`, folder `plugin`, branch `main`. Validate, then submit for review. Later
  versions are picked up from `main` on their own.
