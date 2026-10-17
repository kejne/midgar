{{ define "midgar-formula-worker-protocol" -}}
## Formula-routed work protocol

For formula-routed work, your first work-discovery action is:

```sh
gc gc claim
```

A result with `"action":"work"` identifies your exact assignment in `bead_id` and `bead`; work only on that explicitly claimed bead and close it when finished. Do not claim again while assigned work remains open. A result with `"action":"drain"` means the queue is empty and drain has been acknowledged; exit without seeking other work.
{{- end }}
