"""SRE / Ops agent (mid tier).

Triggered by Conductor fault events (delivered as tick directives, mirrored on agent.events) and by
optional alert rules on span metrics. Investigates through observability-mcp, opens incidents and
proposes flag changes, which always need human approval.
Memory: working, episodic (incidents), procedural (runbooks).
"""
from __future__ import annotations

import json
from typing import Any

from svc_sim_common.agent_base import BaseAgent, RunState
from svc_sim_common.config import env
from svc_sim_common.mcp_client import MCPTools, ToolError

from .tooling import assistant_tool_message, summarise, tool_catalogue, tool_result_message

OBS_TOOLS = {"query_traces", "query_metrics", "search_logs", "read_flags", "open_incident", "propose_flag_change"}
ALERT_EVERY_TICKS = 10
ALERT_ERROR_RATE = 0.2


class SreOps(BaseAgent):
    agent_type = "sre"
    tier = "mid"

    def __init__(self) -> None:
        super().__init__("sre-ops", "SRE / Ops")
        self.obs = MCPTools("observability-mcp", env("OBSERVABILITY_MCP_URL"))
        self._tools: list[dict[str, Any]] | None = None

    async def tools(self) -> list[dict[str, Any]]:
        if self._tools is None:
            self._tools = await tool_catalogue(self.obs, OBS_TOOLS)
        return self._tools

    async def on_start(self, run: RunState) -> None:
        run.data.update(runbook=await self.policies.get_text("runbooks/fault-flags.md"), investigated=set(),
                        alert_rules=bool(run.config.get("sre_alert_rules", True)))

    async def on_finish(self, run: RunState) -> dict[str, Any]:
        return {"investigations": len(run.data.get("investigated", set()))}

    async def on_tick(self, run: RunState) -> dict[str, Any]:
        triggers = [d for d in run.directives if d.get("type") == "fault_alert"]
        if not triggers and run.data["alert_rules"] and run.tick > 0 and run.tick % ALERT_EVERY_TICKS == 0:
            triggers = await self._alert_rules(run)
        done = 0
        for t in triggers:
            key = t.get("flag") or t.get("service")
            if key in run.data["investigated"]:
                continue
            run.data["investigated"].add(key)
            await self.emit(run, "thought", {"text": f"Alert received: {key}. Starting investigation."}, location="ops-room")
            await self._investigate(run, t)
            done += 1
        return {"investigations": done}

    async def _alert_rules(self, run: RunState) -> list[dict[str, Any]]:
        try:
            series = await self.obs.call("query_metrics", {"preset": "error_rate_by_service"})
        except ToolError:
            return []
        out = []
        for s in series.get("results", []) if isinstance(series, dict) else []:
            svc = s.get("labels", {}).get("service_name")
            if svc and float(s.get("value", 0) or 0) >= ALERT_ERROR_RATE and not svc.startswith("svc-sim"):
                out.append({"type": "alert_rule", "service": svc, "error_rate": s.get("value")})
        return sorted(out, key=lambda x: x["service"])[:1]

    async def _investigate(self, run: RunState, trigger: dict[str, Any]) -> None:
        state: dict[str, Any] = {"trigger": trigger, "done_tools": [], "results": {}}
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": "You are the SRE / Ops agent. Follow the runbook. Never change flags directly.\n" + run.data["runbook"]},
            {"role": "user", "content": json.dumps({"trigger": trigger})},
        ]
        for step in range(8):
            res = await self.llm.chat(messages, tools=await self.tools(), purpose="investigate", state=state,
                                      seed=f"{run.seed}/{trigger.get('flag') or trigger.get('service')}/{step}")
            if not res.tool_calls:
                if res.content:
                    await self.emit(run, "thought", {"text": res.content[:200]}, location="ops-room")
                break
            tc = res.tool_calls[0]
            args = dict(tc.arguments)
            if tc.name == "open_incident":
                args.update(run_id=run.run_id, opened_by=self.agent_id,
                            evidence={**(args.get("evidence") or {}), "sim_time": run.sim_time, "trigger": trigger})
            if tc.name == "propose_flag_change":
                args.update(run_id=run.run_id, requested_by=self.agent_id)
            messages.append(assistant_tool_message(res.tool_calls, res.content))
            await self.emit(run, "tool.call", {"tool": tc.name, "server": "observability-mcp",
                                               "args": {k: v for k, v in args.items() if k != "evidence"}}, location="ops-room")
            try:
                result = await self.obs.call(tc.name, args, tc.id)
                ok = not (isinstance(result, dict) and result.get("ok") is False)
            except ToolError as exc:
                result, ok = {"error": str(exc)}, False
            await self.emit(run, "tool.result", {"tool": tc.name, "ok": ok, "summary": summarise(result)},
                            location="approval-desk" if tc.name == "propose_flag_change" else "ops-room")
            state["done_tools"].append(tc.name)
            state["results"][tc.name] = result
            messages.append(tool_result_message(tc.id, result))
        await self.remember(run, f"Investigated {trigger.get('flag') or trigger.get('service')}: tools {', '.join(state['done_tools'])}.",
                            importance=8, kind="incident")


agent = SreOps()
app = agent.app()
