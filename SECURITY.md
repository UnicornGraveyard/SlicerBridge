# Security

SlicerBridge reads `.3mf` files you give it, runs the slicers installed on your machine, and writes files to the
folder you name. Its MCP server runs locally over stdio and never listens on a network port.

## Reporting a vulnerability

Please report security problems privately, not in a public issue: use GitHub's
[private vulnerability reporting](https://github.com/ianmacdowell13-byte/SlicerBridge/security/advisories/new),
or the contact form at [unicorngraveyard.com/press](https://unicorngraveyard.com/press) (say it's about
SlicerBridge). You'll get an answer within a week.

Things worth reporting: a crafted `.3mf` that makes SlicerBridge write outside the output folder or run
something, and anything in the MCP server that reaches beyond the paths it's given.
