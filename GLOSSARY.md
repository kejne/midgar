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

**Spec snapshot**:
A temporary, fixed copy of a request's requirements that serves as the common reference for a factory run. It is not edited during normal execution.
_Avoid_: Execution plan when referring to requirements.

**Execution plan**:
The decomposition of a spec snapshot into implementation tasks and their blocking relationships. A new plan revision may refine the decomposition without changing the requirements.
_Avoid_: Spec snapshot when referring to task decomposition.

**Implementation task**:
A bounded, independently verifiable slice of a spec snapshot, with explicit acceptance criteria and prerequisites.
_Avoid_: GitHub issue when referring to delegated implementation work.

**Factory run**:
A resumable effort to deliver one specified request in a target rig as a reviewed pull request. A request has at most one active run.
_Avoid_: Formula graph when referring to a particular execution.
