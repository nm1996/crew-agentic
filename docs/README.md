# CREW documentation

**CREW is a local-first AI development orchestration CLI.** It coordinates specialised AI
agents to plan, implement, review, and independently evaluate software changes inside a
Git repository. It is an orchestrator, not an autonomous replacement for the developer:
important decisions and risky actions require explicit approval.

## Using CREW

| Document | Read it when |
|---|---|
| [INSTALLATION.md](INSTALLATION.md) | Installing CREW and setting up a project; troubleshooting. |
| [USAGE.md](USAGE.md) | Running a request end to end; command reference; exit codes; recovery. |
| [CONFIGURATION.md](CONFIGURATION.md) | Changing `.crew/config.yaml`; verification commands; agent permissions. |

## Specifications

| Document | Contents |
|---|---|
| [PRODUCT_SPEC.md](PRODUCT_SPEC.md) | Purpose, target user, functional/non-functional requirements, MVP acceptance criteria. |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Modular-monolith boundaries, package layout, data flow, design rules. |
| [WORKFLOW.md](WORKFLOW.md) | Run state machine, task model, review loop, failure, recovery, cancellation. |
| [AGENT_ROLES.md](AGENT_ROLES.md) | Director, Implementer, Reviewer, External Evaluator contracts. |
| [TECHNICAL_SPEC.md](TECHNICAL_SPEC.md) | Stack, CLI contract, configuration, provider interface, persistence, Git, artifacts, testing. |
| [SECURITY.md](SECURITY.md) | Safety requirements, command policy, prompt injection, secrets, threat model. |
| [DECISIONS.md](DECISIONS.md) | Decisions taken during implementation and known limitations. |
| [ROADMAP.md](ROADMAP.md) | Milestones M0–M6 and their status. |

## Product principles

- **Local-first:** project state and history live in the target repository under `.crew/`.
- **Human-controlled:** the developer approves plans and final changes.
- **Explicit roles:** Director, Implementer, Reviewer, and External Evaluator have distinct
  responsibilities.
- **Safe execution:** permissions are enforced by code and provider flags, not by prompts.
- **Recoverable:** workflow state survives process termination.
- **Small:** SQLite, local processes, Git worktrees, clear interfaces. No distributed
  infrastructure.

## Non-goals for the MVP

Cloud service or hosted dashboard; multi-user collaboration; Kubernetes, Redis, Kafka,
NATS or microservices; automatic push, merge, release or deployment; unrestricted shell
access; every AI provider from day one; a sophisticated terminal UI.
