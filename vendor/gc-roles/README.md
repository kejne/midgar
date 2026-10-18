# Vendored Gas City roles

These role agents and the shared worker prompt are copied from
`gastownhall/gascity-packs` at commit
`520e95cb22992a8a1017f7a60da267f203497c7e` (`gascity/roles` and
`gascity/template-fragments/gc-role-worker.template.md`).

The upstream roles pack imports its parent `gascity` pack to obtain the shared
worker prompt. That parent also contains formulas and unrelated assets, so this
vendored pack carries only the role agents and the one shared prompt fragment;
its manifest deliberately has no parent import. Midgar imports it with this
local-pack entry, which exposes the roles to all of Midgar's rigs:

```toml
[imports."gc-roles"]
source = "./vendor/gc-roles"
```
