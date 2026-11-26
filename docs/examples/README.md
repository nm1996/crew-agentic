# Examples

Real, measured CREW runs against the Claude Code CLI. Each one records the setup, what
the agents did, an independent check of the result, and the cost per role, so you can
estimate what a run of similar size will cost and which settings to tune.

| Example | What | Result | Cost | Time |
|---|---|---|---|---|
| [Spring Boot CRUD](spring-boot-crud.md) | New Maven project: User, Product, Order, Cart on H2, with 69 integration tests | COMPLETED; 25/25 black-box API checks | $3.34 | 14 min |

To add one: run it with usage tracking (CREW ≥ 0.3.1), copy the "Agent usage" table
from `crew report <run-id>`, and verify the result outside CREW before writing it up.
