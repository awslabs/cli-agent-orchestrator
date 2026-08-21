# Portfolio Lifecycle

The portfolio utility owns this forward-only parent lifecycle:

`Bootstrap -> Discover -> Confirm -> Plan -> Dispatch -> Integrate -> Learn`

It does not replace or inspect child AI-DLC stage routing.

## Transitions

- **Bootstrap -> Discover** requires at least one registered project and intent.
- **Discover -> Confirm** requires a current human discovery decision.
- **Confirm -> Plan** requires that discovery decision to remain current.
- **Plan -> Dispatch** requires current discovery, clean harness verification,
  and `--accepted-by <human>`. Acceptance is bound to the catalog revision.
- **Dispatch -> Integrate** requires every child session to be completed.
- **Integrate -> Learn** requires completed sessions, no unanswered questions,
  no pending shared-memory proposals, no unresolved contract dependencies, and
  current discovery confirmation. Every child must also have a structured
  result, and all convergence risks must be satisfied or explicitly decided.
- **Learn -> completed** rechecks the integration conditions.

Repeating the current phase or completing an already completed lifecycle is
idempotent. Skipping or reversing a phase is rejected.

## Recovery

`lifecycle status` reports the persisted phase, blockers, transition history,
and exact next command. CAO terminals may restart without changing this state.

State schemaVersion 1 workspaces fail closed with a migration instruction. Run
`lifecycle migrate` once. Migration is idempotent and infers a conservative
phase from durable session and discovery state:

- active, waiting, blocked, or failed sessions -> Dispatch;
- all sessions completed -> Integrate;
- discovery recorded -> Confirm;
- registered catalog documents -> Discover;
- otherwise -> Bootstrap.

Migration records its decision in lifecycle history and never fabricates plan
acceptance.
