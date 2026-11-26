# Reference run: Spring Boot CRUD service

A measured, real CREW run against the Claude Code CLI. It answers three questions: does
the workflow produce a working service, what does it cost, and where does the money go?

| | |
|---|---|
| Date | 2026-11-26 |
| CREW | 0.3.0 + `16d4328` (usage tracking) |
| Claude Code CLI | 2.1.319, CLI default model (`claude-opus-5-5`; Claude Code also used `claude-haiku-5-5` internally) |
| Machine | Linux, OpenJDK 25, Maven 3.10, empty Maven cache |
| Result | **COMPLETED**, accepted after independent verification |
| Total cost | **$3.34** (budget $25) |
| Time | 44 s planning + 13 min implementation, review and evaluation |

## Setup

An empty Git repository containing only a README, plus:

```bash
crew init --name shop-crud
crew configure \
  'execution.verification_commands=[["mvn","-q","-B","verify"]]' \
  'execution.command_timeout_seconds=1200' \
  'execution.max_run_budget_usd=25' \
  'execution.sandbox=bwrap' 'execution.sandbox_network=true' \
  'execution.sandbox_writable_paths=["~/.m2"]' \
  'provider.claude_code.implementer_bash_rules=["Bash(mvn -q -B verify:*)","Bash(mvn -q -B test:*)","Bash(mvn verify:*)","Bash(mvn test:*)","Bash(mvn -q -B compile:*)"]' \
  'provider.claude_code.reviewer_bash_rules=["Bash(mvn -q -B verify:*)","Bash(mvn -q -B test:*)"]'
```

`.crew/context/project.md` told the agents about the environment (JDK 25, Maven, empty
cache), the package name, the Java 21 target, how to verify, and not to start the server.
Implementers could run only those Maven goals; `mvn spring-boot:run` was not allowed.
The verification sandbox had network access so that Maven could download dependencies,
and write access only to the worktree and `~/.m2`.

Request:

> Build a small Spring Boot REST service with an H2 in-memory database and full CRUD for
> four resources: User, Product, Order and Cart. A cart belongs to a user and contains
> cart items (product + quantity); an order belongs to a user and contains order items
> (product, quantity, unit price). Use Maven, Spring Web, Spring Data JPA, Bean Validation
> and H2. Expose /api/users, /api/products, /api/orders and /api/carts, each with list, get
> by id, create, update and delete; return 404 for unknown ids and 400 for invalid input.
> Add integration tests for every resource. `mvn -q -B verify` must pass and
> `mvn spring-boot:run` must start the service on port 8080.

## What happened

1. **Plan (44 s, $0.10).** The Director proposed three tasks in a chain:
   `scaffold-project` (pom, error handling, User + Product) → `cart-crud` → `order-crud`.
   It deliberately did **not** parallelise: all tasks touch shared files (pom, exception
   handler), and parallel tasks would have conflicted at integration.
2. **Implementation (684 s of agent time, $2.15).** Every task succeeded on its first
   attempt. Implementers ran Maven themselves through the scoped rules.
3. **Integration and verification.** CREW merged the three branches and ran
   `mvn -q -B verify` in the bwrap sandbox: exit 0 in 16.7 s.
4. **Review ($0.56).** 5 findings, none blocking (see below), so no correction cycle.
5. **External evaluation ($0.52).** ACCEPT. It marked `spring-boot:run` on port 8080 as
   **unverifiable**, because no agent was allowed to start the server. That is the honest
   answer, and it is why the independent check below exists.

Output: 42 files, 2,236 lines (39 Java files: entity, repository, request/response
records, service and controller per resource, a shared `GlobalExceptionHandler`, four
`*ControllerIT` test classes). Spring Boot 3.5.7, Java 21 target.

## Independent verification

Done outside CREW, on a fresh clone of `crew/<run>/integration`:

| Check | Result |
|---|---|
| `mvn -q -B verify` | passes in 17 s; **69 integration tests**, 0 failures (User 16, Product 16, Cart 17, Order 20) |
| `mvn spring-boot:run` | started in 3.6 s on port 8080 |
| Black-box API script (curl) | **25/25** passed: create/read/update/delete for all four resources, 404 for unknown ids, 400 for invalid email, negative price, zero quantity, unknown product and empty order; order item price defaults to the product price |

## Cost breakdown

| Role | Executions | Cost (USD) | Share | Agent time (s) | Turns | Input tok¹ | Output tok | Cache-read tok |
|---|---|---|---|---|---|---|---|---|
| Director | 1 | 0.10 | 3 % | 40 | 3 | 2,286 | 4,114 | 13,393 |
| Implementer | 3 | 2.15 | 64 % | 684 | 99 | 101,807 | 56,587 | 1,004,684 |
| Reviewer | 1 | 0.56 | 17 % | 43 | 6 | 57,451 | 3,893 | 109,991 |
| Evaluator | 1 | 0.52 | 16 % | 34 | 4 | 55,351 | 2,964 | 64,545 |
| **Total** | 6 | **3.34** | | 801 | 112 | 216,895 | 67,558 | 1,192,613 |

¹ Uncached input plus cache-creation tokens.

Per implementation task:

| Task | Files | Cost | Agent time | Turns |
|---|---|---|---|---|
| scaffold-project | 22 | $0.82 | 205 s | 35 |
| cart-crud | 10 | $0.69 | 325 s | 35 |
| order-crud | 10 | $0.64 | 154 s | 29 |

That is roughly **$0.08 per generated file** or **$1.50 per 1,000 lines** including
tests, review and evaluation.

## Where the money goes, and what to tune

- **Implementers are 64 % of the cost** and most of their tokens are cache reads (cheap),
  from many short build-and-fix turns. Letting them run the tests themselves is what made
  every task pass first time; removing the Maven rules would save turns but would likely
  cost correction cycles instead.
- **Review and evaluation are a third of the cost for one-tenth of the time.** Both get
  the whole integrated diff (here about 55 k uncached input tokens each) in their prompt.
  Options, in order of impact:
  1. use a cheaper model for the Evaluator (and possibly the Reviewer) — needs a per-role
     model setting, see below;
  2. pass a diff summary (file list and stats) plus the paths, and let them read the
     files they need with their Read tool;
  3. skip the Evaluator on small changes.
- **Planning is cheap** (3 %). Detailed task descriptions and acceptance criteria paid
  off: no task needed a retry.
- **Wall-clock time** (13 min) was limited by the sequential chain. Parallel
  Implementers only help when the Director can split work into tasks that touch
  different files; for a new project, shared scaffolding forces a chain.
- **The budget was never close** ($3.34 of $25). For runs like this one,
  `max_run_budget_usd: 8` leaves room for a correction cycle.

### Optimisation levers in CREW today

| Lever | Setting | Effect here |
|---|---|---|
| Model for all agents | `provider.claude_code.model` | one model for every role |
| Spending cap | `execution.max_run_budget_usd`, `provider.claude_code.max_budget_usd` | hard stop |
| Corrections | `execution.max_correction_cycles` | none were needed |
| Agent tools | `*_bash_rules` | fewer rules, fewer turns, but more risk of failed tasks |

Missing and now on the roadmap: **per-role models** (e.g. a cheaper Evaluator) and a
**compact review payload** (diff summary instead of the full diff).

## Review findings (all non-blocking)

| Severity | Finding |
|---|---|
| MEDIUM | Order `unitPrice` has no `@Digits` limit, unlike product price |
| LOW | Strings longer than 255 characters return 409 instead of 400 |
| LOW | README API table omits carts and orders |
| LOW | No test for 409 when deleting a user or product still referenced |
| LOW | List endpoints trigger N+1 lazy loads |

These are the kind of follow-ups a developer would file. CREW reports them; with
`max_correction_cycles` only *blocking* findings start a correction cycle.

## Reproduce

```bash
mkdir shop-crud && cd shop-crud && git init -b main
printf '# Shop CRUD\n' > README.md && printf 'target/\n' > .gitignore
git add . && git commit -m "Initial commit"
crew init --name shop-crud
# crew configure … (see Setup) and write .crew/context/project.md
crew run "<request above>"
crew plan <run-id> && crew approve-plan <run-id>
crew report <run-id>          # the "Agent usage" section has the cost table
crew accept <run-id>
```

Costs vary between runs and models; treat these numbers as an order of magnitude.
