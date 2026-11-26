"""Adapters to systems around the code: issue trackers, code hosts, documentation.

Every adapter is small, configured in ``.crew/config.yaml`` (``integrations``), reads
credentials from environment variables only, and is invoked only by explicit
developer commands (``crew run --issue/--doc``, ``crew publish``). Outbound calls are
recorded as workflow events. See docs/INTEGRATIONS.md.
"""
