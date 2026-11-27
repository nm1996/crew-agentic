"""Answers agents' permission requests (``approvals.permission_prompts: ask``).

A mixin of :class:`crew.orchestrator.engine.Orchestrator`; split out of engine.py.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any

from crew.agents.runtime import AgentRuntime
from crew.config.schema import CrewConfig
from crew.domain.enums import (
    ApprovalDecision,
    ApprovalKind,
)
from crew.orchestrator.support import (
    PERMISSION_POLL_SECONDS,
    Notify,
    Project,
    atomic_write,
    read_json_list,
)
from crew.persistence.store import Store


class PermissionBroker:
    # Provided by Orchestrator (engine.py).
    p: Project
    store: Store
    config: CrewConfig
    runtime: AgentRuntime
    notify: Notify

    def _permission_dir(self, run_id: str) -> Path:
        return self.p.paths.run_dir(run_id) / "permissions"

    def _start_permission_broker(self, run_id: str) -> asyncio.Task[None] | None:
        if self.config.approvals.permission_prompts != "ask":
            return None
        return asyncio.create_task(self._permission_broker(run_id))

    async def _permission_broker(self, run_id: str) -> None:
        """Answer agents' permission requests (written by the permission server)."""
        directory = self._permission_dir(run_id)
        while True:
            for request_file in sorted(directory.glob("*.request.json")):
                response_file = request_file.with_name(
                    request_file.name.replace(".request.json", ".response.json")
                )
                if response_file.exists():
                    continue
                try:
                    request = json.loads(request_file.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                decision = await self._ask_developer(request)
                if decision == "allow_run":
                    allowed_file = directory / "allowed.json"
                    allowed = read_json_list(allowed_file)
                    allowed.append(request["key"])
                    atomic_write(allowed_file, json.dumps(allowed))
                atomic_write(response_file, json.dumps({"decision": decision}))
                approved = decision in ("allow", "allow_run")
                note = f"{request.get('role')} {request.get('key')}"
                self.store.add_approval(
                    run_id,
                    ApprovalKind.PERMISSION,
                    ApprovalDecision.APPROVED if approved else ApprovalDecision.REJECTED,
                    f"{note} ({decision})",
                )
                self.store.add_event(run_id, "permission_" + decision, note)
            await asyncio.sleep(PERMISSION_POLL_SECONDS)

    async def _ask_developer(self, request: dict[str, Any]) -> str:
        if self.ask_permission is None:
            return "deny"
        ask = self.ask_permission
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()

        def worker() -> None:
            try:
                answer = ask(request)
            except Exception:  # a broken prompt must not crash the run
                answer = "deny"
            loop.call_soon_threadsafe(lambda: future.done() or future.set_result(answer))

        # A daemon thread, so an unanswered prompt never blocks CREW from exiting.
        threading.Thread(target=worker, daemon=True).start()
        answer = await future
        return answer if answer in ("allow", "allow_run", "deny") else "deny"
