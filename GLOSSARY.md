# Midgar

This context describes Midgar's Gas City configuration and how city-defined formulas and tooling are used by agents running in its rigs.

## Language

**City**:
The Gas City workspace that centrally defines and configures packs, formulas, commands, and agent guidance for its rigs.
_Avoid_: Instance when referring specifically to the configured Gas City workspace.

**Rig**:
A repository-scoped execution context within a city where agents run work routed by city-defined formulas.
_Avoid_: Treating a rig as the owner of formula or command definitions merely because its agents use them.

**Formula graph**:
A Gas City workflow definition whose steps and routing are evaluated by the city runtime.
_Avoid_: Script when referring to the workflow as a whole.

**Claim command**:
The city-provided `gc gc claim` entry point an agent uses to atomically obtain routed work or receive a drain result.
_Avoid_: Formula graph; the claim command connects an agent to work but does not define the graph.

**Formula-routed work**:
Work assigned by a formula graph to an agent executing in a target rig.
_Avoid_: General city work when referring specifically to this agent workflow.
