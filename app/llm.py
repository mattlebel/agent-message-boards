"""One place for every Claude call: model choice, cost tracking, daily budget, fake mode."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, time, timezone
from typing import Any, Callable, Optional, Type

from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select

from app import config, settings_store
from app.db import SessionLocal
from app.models import LlmCall

log = logging.getLogger(__name__)

# $ per million tokens: (input, output, cache read). Cache writes bill at 1.25x input.
PRICES = {
    "claude-fable-5-1": (10.0, 50.0, 0.25),
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-sonnet-5-5": (2.0, 10.0, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
}
WEB_SEARCH_USD = 0.01  # per search
# Models that accept server-side refusal fallbacks ("default" routing).
FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-sonnet-5-5"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class BudgetExceeded(Exception):
    pass


class LlmError(Exception):
    pass


@dataclass
class LlmResult:
    text: str
    parsed: Any = None
    model: str = ""
    cost_usd: float = 0.0
    raw: Any = field(default=None, repr=False)


_client = None


def client():
    global _client
    if _client is None:
        import anthropic

        _client = anthropic.Anthropic(
            api_key=config.ANTHROPIC_API_KEY or None, max_retries=3, timeout=300.0
        )
    return _client


def et_day_start_utc(now: Optional[datetime] = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(config.TZ)
    start_local = datetime.combine(local.date(), time.min, tzinfo=config.TZ)
    return start_local.astimezone(timezone.utc).replace(tzinfo=None)


def spent_today() -> float:
    with SessionLocal() as s:
        total = s.execute(
            select(func.coalesce(func.sum(LlmCall.cost_usd), 0.0)).where(
                LlmCall.ts >= et_day_start_utc()
            )
        ).scalar()
        return float(total or 0.0)


def daily_budget() -> float:
    with SessionLocal() as s:
        return float(settings_store.get(s, "daily_budget_usd") or 0)


def cost_of(model: str, usage: Any, searches: int = 0) -> float:
    p_in, p_out, p_cache = PRICES.get(model, PRICES["claude-opus-5-5"])
    inp = getattr(usage, "input_tokens", 0) or 0
    out = getattr(usage, "output_tokens", 0) or 0
    c_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    c_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    return (
        inp * p_in + out * p_out + c_read * p_cache + c_write * p_in * 1.25
    ) / 1_000_000 + searches * WEB_SEARCH_USD


def _record(purpose: str, model: str, usage: Any, searches: int, cost: float, ok=True, error=""):
    if config.LLM_FAKE and config.DATABASE_URL.startswith("sqlite"):
        return  # SQLite allows one writer; fake calls cost nothing anyway
    try:
        with SessionLocal() as s:
            s.add(
                LlmCall(
                    purpose=purpose,
                    model=model,
                    input_tokens=getattr(usage, "input_tokens", 0) or 0,
                    output_tokens=getattr(usage, "output_tokens", 0) or 0,
                    cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
                    cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
                    web_searches=searches,
                    cost_usd=cost,
                    ok=ok,
                    error=error[:2000],
                )
            )
            s.commit()
    except Exception:  # never let bookkeeping break a run
        log.exception("failed to record llm call")


def extract_json(text: str) -> Any:
    """Pull the last JSON object/array out of free text (used with web search)."""
    fenced = re.findall(r"```(?:json)?\s*(.*?)```", text, re.S)
    candidates = list(reversed(fenced)) + [text]
    for cand in candidates:
        cand = cand.strip()
        for opener, closer in (("{", "}"), ("[", "]")):
            start, end = cand.find(opener), cand.rfind(closer)
            if start != -1 and end > start:
                try:
                    return json.loads(cand[start : end + 1])
                except json.JSONDecodeError:
                    continue
    raise LlmError("no JSON found in model output")


def generate(
    *,
    purpose: str,
    role: str,
    system: str,
    user: str,
    max_tokens: int = 4000,
    effort: Optional[str] = None,
    schema: Optional[Type[BaseModel]] = None,
    web_search: bool = False,
    max_searches: int = 3,
    critical: bool = False,
    fake: Optional[Callable[[], Any]] = None,
) -> LlmResult:
    """Call Claude.

    role picks the model from Settings (posts, newsletter, fast, ...).
    critical=True ignores the daily budget (alerts and the newsletter must go out).
    schema returns a validated pydantic object in .parsed.
    """
    with SessionLocal() as s:
        model = settings_store.model_for(s, role)

    if config.LLM_FAKE:
        value = fake() if fake else "(fake model output)"
        _record(purpose, f"fake:{model}", None, 0, 0.0)
        if isinstance(value, BaseModel):
            return LlmResult(text=value.model_dump_json(), parsed=value, model="fake")
        return LlmResult(text=str(value), model="fake")

    if not critical:
        budget = daily_budget()
        if budget and spent_today() >= budget:
            raise BudgetExceeded(f"daily budget ${budget:.2f} reached")

    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": user}],
    }
    if effort:
        kwargs["output_config"] = {"effort": effort}
    if model in FALLBACK_MODELS:
        kwargs["betas"] = [FALLBACK_BETA]
        kwargs["fallbacks"] = "default"
    if web_search:
        tool_type = "web_search_20250305" if "haiku" in model else "web_search_20260209"
        kwargs["tools"] = [{"type": tool_type, "name": "web_search", "max_uses": max_searches}]

    messages_api = client().beta.messages
    try:
        if schema is not None and not web_search:
            resp = messages_api.parse(output_format=schema, **kwargs)
            total_cost = cost_of(model, resp.usage)
            _record(purpose, model, resp.usage, 0, total_cost)
        else:
            resp, total_cost = _create_with_pause_handling(messages_api, kwargs, purpose, model)
    except Exception as e:
        _record(purpose, model, None, 0, 0.0, ok=False, error=repr(e))
        raise

    if resp.stop_reason == "refusal":
        raise LlmError(f"model declined ({purpose})")

    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    parsed = None
    if schema is not None:
        parsed = getattr(resp, "parsed_output", None)
        if parsed is None:
            try:
                parsed = schema.model_validate(extract_json(text))
            except (ValidationError, LlmError) as e:
                raise LlmError(f"could not parse {purpose} output: {e}") from e
    return LlmResult(text=text, parsed=parsed, model=model, cost_usd=total_cost, raw=resp)


def _create_with_pause_handling(messages_api, kwargs, purpose, model):
    """Web search turns can pause; resend the partial turn to let it continue.

    Records every request's usage. Returns (final response, total cost).
    """
    messages = list(kwargs["messages"])
    total = 0.0
    resp = None
    for _ in range(4):
        resp = messages_api.create(**{**kwargs, "messages": messages})
        stu = getattr(resp.usage, "server_tool_use", None)
        searches = (getattr(stu, "web_search_requests", 0) or 0) if stu else 0
        cost = cost_of(model, resp.usage, searches)
        _record(purpose, model, resp.usage, searches, cost)
        total += cost
        if resp.stop_reason != "pause_turn":
            break
        messages = messages + [{"role": "assistant", "content": resp.content}]
    return resp, total
