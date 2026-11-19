"""Per-turn timing built from recorded SDK and host events.

Every value here comes from a stored event, approval row or the SDK's own session
log. Where a time is not recorded the field is ``None`` and the browser says so;
nothing is estimated except the placement of a model call whose usage report has
no timestamp (older rows), which is anchored to its recorded response.
"""

from __future__ import annotations

import json
from datetime import datetime

# Event types the timing view reads. Streaming deltas are excluded: they are numerous
# and add nothing the usage report does not already summarise.
TYPES = (
    "user.message",
    "assistant.turn_start",
    "assistant.turn_end",
    "assistant.message",
    "assistant.usage",
    "tool.execution_start",
    "tool.execution_complete",
    "skill.invoked",
    "session.compaction_start",
    "session.compaction_complete",
    "app.turn_queued",
    "app.turn_started",
    "app.turn_settled",
)

TEXT_LIMIT = 600


def parse_time(value):
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def transcript_times(content):
    """Map SDK event id to timestamp from the native ``events.jsonl`` log."""
    times = {}
    if not content:
        return times
    for line in bytes(content).decode("utf-8", "replace").splitlines():
        # Cheap pre-filter: only lines that can carry an id and a timestamp.
        if '"timestamp"' not in line:
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict) and item.get("id") and (t := parse_time(item.get("timestamp"))):
            times[str(item["id"])] = t
    return times


def clip(value, limit=TEXT_LIMIT):
    if value is None:
        return None
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def usage_details(u):
    tokens = {
        "input": number(u.get("inputTokens")),
        "output": number(u.get("outputTokens")),
        "cache_read": number(u.get("cacheReadTokens")),
        "cache_write": number(u.get("cacheWriteTokens")),
        "reasoning": number(u.get("reasoningTokens")),
        "tools": number(u.get("toolTokenCount")),
    }
    provider = u.get("provider") if isinstance(u.get("provider"), dict) else {}
    copilot = u.get("copilotUsage") if isinstance(u.get("copilotUsage"), dict) else {}
    nano = number(copilot.get("totalNanoAiu"))
    return {
        "model": u.get("model"),
        "model_name": u.get("modelDisplayName") or u.get("model"),
        "response_model": u.get("responseModel"),
        "provider": provider.get("label") or provider.get("id"),
        "endpoint": u.get("apiEndpoint"),
        "transport": u.get("transport"),
        "initiator": u.get("initiator"),
        "finish": u.get("finishReason"),
        "effort": u.get("reasoningEffort"),
        "duration_ms": number(u.get("duration")),
        "ttft_ms": number(u.get("timeToFirstTokenMs")),
        "inter_token_ms": number(u.get("interTokenLatencyMs")),
        "tokens": {k: v for k, v in tokens.items() if v is not None},
        "max_prompt_tokens": number(u.get("maxPromptTokens")),
        "request_bytes": number(u.get("requestBodyBytes")),
        "request_wire_bytes": number(u.get("requestBodyWireBytes")),
        "request_encoding": u.get("requestBodyEncoding"),
        "available_tools": number(u.get("availableToolCount")),
        "premium_requests": number(u.get("cost")),
        "ai_units": nano / 1e9 if nano is not None else None,
        "content_filtered": u.get("contentFilterTriggered") is True,
    }


def build_turn(turn, events, approvals, times):
    """Return the timing record for one turn.

    ``events`` are stored rows (``type``, ``data``, ``sdk_id``, ``ts``) in sequence order.
    ``times`` maps SDK ids to transcript timestamps for rows stored before ``ts`` existed.
    """

    def when(row):
        return row.get("ts") if row.get("ts") is not None else times.get(row.get("sdk_id") or "")

    calls, tools, compactions, notes = [], [], [], []
    by_tool, by_api = {}, {}
    call = None
    user = started = None
    host = {}
    for row in events:
        kind, data, t = row["type"], row["data"] or {}, when(row)
        if kind.startswith("app."):
            host[kind] = t
            continue
        if kind == "user.message" and user is None:
            user = t
        elif kind == "assistant.turn_start":
            call = {"index": len(calls) + 1, "start": t, "end": None, "response": None}
            calls.append(call)
        elif kind == "assistant.turn_end" and call is not None:
            call["end"] = t
        elif kind == "assistant.usage":
            details = usage_details(data)
            target = call
            if target is None or target.get("usage"):
                continue
            target["usage"] = details
            target["usage_at"] = t
            if data.get("apiCallId"):
                by_api[data["apiCallId"]] = target
        elif kind == "assistant.message":
            target = by_api.get(data.get("apiCallId")) or call
            if target is None:
                continue
            if target["response"] is None:
                target["response"] = t
            requests = data.get("toolRequests") or []
            target["tool_requests"] = target.get("tool_requests", 0) + len(requests)
            for item in requests:
                if isinstance(item, dict) and item.get("toolCallId"):
                    by_tool[item["toolCallId"]] = (target["index"], t)
        elif kind == "tool.execution_start":
            tid = data.get("toolCallId")
            origin = by_tool.get(tid)
            tools.append(
                {
                    "key": tid,
                    "name": data.get("toolName") or data.get("mcpToolName") or "tool",
                    "server": data.get("mcpServerName"),
                    "call": origin[0] if origin else (call["index"] if call else None),
                    "requested": origin[1] if origin else None,
                    "start": t,
                    "end": None,
                }
            )
        elif kind == "tool.execution_complete":
            item = next((x for x in reversed(tools) if x["key"] == data.get("toolCallId")), None)
            if item is None:
                continue
            item["end"] = t
            item["success"] = data.get("success")
            result = data.get("result") if isinstance(data.get("result"), dict) else {}
            content = result.get("content")
            if isinstance(content, str):
                item["result_chars"] = len(content)
            # Arguments, result text and error messages stay out: they can carry user data.
            metrics = (data.get("toolTelemetry") or {}).get("metrics") or {}
            if number(metrics.get("result_token_count")) is not None:
                item["result_tokens"] = metrics["result_token_count"]
        elif kind == "skill.invoked":
            item = next((x for x in reversed(tools) if x["name"] == "skill"), None)
            if item is not None and data.get("name"):
                item["skill"] = data.get("name")
        elif kind == "session.compaction_start":
            compactions.append({"start": t, "end": None})
        elif kind == "session.compaction_complete" and compactions:
            metrics = data.get("attemptMetrics") or {}
            compactions[-1].update(
                end=t, success=data.get("success"), duration_ms=number(metrics.get("durationMs"))
            )

    for call in calls:
        usage = call.get("usage") or {}
        duration, ttft = usage.get("duration_ms"), usage.get("ttft_ms")
        # The request ends when the usage report was emitted; older rows lack that
        # timestamp, so the recorded response time is used and marked as placed.
        end = call.get("usage_at") or call.get("response")
        call["request"] = None
        if duration is not None and end is not None:
            start = end - duration / 1000
            if call["start"] is not None:
                start = max(start, call["start"])
            call["request"] = {
                "start": start,
                "first_token": start + ttft / 1000 if ttft is not None else None,
                "end": end,
                "placed": call.get("usage_at") is None,
            }
        call.pop("usage_at", None)

    # Results go back to the next model call that starts after the tool finishes.
    for item in tools:
        if item["end"] is None:
            continue
        nxt = next(
            (
                c
                for c in calls
                if c["index"] > (item["call"] or 0) and (c["start"] or 0) >= item["end"]
            ),
            None,
        )
        item["returns_to"] = nxt["index"] if nxt else None

    gates = []
    for index, a in enumerate(approvals):
        try:
            request = json.loads(a["request"]) if isinstance(a["request"], str) else a["request"]
        except ValueError:
            request = {}
        tool_call = request.get("toolCallId")
        gate = {
            "requested": a["created_at"],
            "resolved": a.get("resolved_at"),
            "status": a["status"],
            "tool": request.get("toolName") or request.get("kind"),
            "kind": request.get("kind"),
            # Position in ``approvals``; JSON loses object identity, so tools refer to it.
            "index": index,
        }
        gates.append(gate)
        owner = next((x for x in tools if tool_call and x["key"] == tool_call), None)
        if owner is None and not tool_call:
            # Application approvals (such as file sharing) carry no tool call id; they are
            # raised by whichever tool was running when the request was made.
            running = [
                x
                for x in tools
                if x["start"] is not None
                and x["start"] <= gate["requested"]
                and (x["end"] is None or x["end"] >= gate["requested"])
            ]
            owner = max(running, key=lambda x: x["start"], default=None)
        if owner is not None:
            # A tool can ask more than once (for example, a fetch that is redirected).
            owner["approval"] = gate
            owner.setdefault("approvals", []).append(index)
    if any(g["resolved"] is None and g["status"] != "pending" for g in gates):
        notes.append("Approval answer times were not recorded for this turn.")
    if any(c["request"] and c["request"]["placed"] for c in calls):
        notes.append("Model calls are placed to end at their recorded response.")
    for item in tools:
        item.pop("key", None)

    marks = [t for t in [user, *[c["start"] for c in calls], *[x["start"] for x in tools]] if t]
    ends = [
        t
        for t in [
            *[c["end"] for c in calls],
            *[x["end"] for x in tools],
            *[c["end"] for c in compactions],
        ]
        if t
    ]
    started = host.get("app.turn_started")
    return {
        "id": turn["id"],
        "kind": turn.get("kind") or "prompt",
        "status": turn["status"],
        "number": turn.get("number"),
        # A short first-line excerpt identifies the turn; the full prompt is in the chat.
        "prompt": clip(" ".join((turn.get("prompt") or "").split()), 80),
        "error": clip(turn.get("error"), 300),
        "received": turn.get("created_at"),
        "started": started,
        "sdk_received": user,
        "finished": turn.get("finished_at"),
        "start": min(marks) if marks else None,
        "end": max(ends) if ends else None,
        "calls": calls,
        "tools": tools,
        "approvals": gates,
        "compactions": compactions,
        "notes": notes,
    }
