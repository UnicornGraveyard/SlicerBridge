# Panda2Prusa (vendored)

From https://github.com/liutang/Panda2Prusa at commit
`b6cd13143fb5ea29b9111a11bd329c7e25f25b8d` (2026-10-20), MIT License (LICENSE here).

Only the library modules are copied, unmodified: `__init__`, `ns`, `paint`,
`transform`, `reader`, `convert`, `writer`, `settings`, `autobrim`, `prusa`.
The web app, GUI and CLI are left out.

Vendored rather than installed because that commit's `pyproject.toml` can't be
built by pip (`keywords` and `classifiers` sit under
`[project.optional-dependencies]`). To update: copy the same modules from a
newer commit, update the hash above, and run the full test suite.
