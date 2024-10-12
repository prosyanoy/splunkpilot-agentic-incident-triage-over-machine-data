"""Structured RCA report schema."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

Category = Literal["bad_deploy", "memory_leak", "dns_failure", "auth_storm", "disk_full", "unknown"]
CATEGORIES: tuple[str, ...] = ("bad_deploy", "memory_leak", "dns_failure", "auth_storm", "disk_full", "unknown")


class RootCause(BaseModel):
    category: Category = "unknown"
    service: str = ""
    host: str = ""
    description: str = ""

    @field_validator("category", mode="before")
    @classmethod
    def _norm(cls, v: object) -> str:
        s = str(v or "unknown").strip().lower().replace(" ", "_").replace("-", "_")
        return s if s in CATEGORIES else "unknown"


class TimelineEntry(BaseModel):
    time: str
    event: str


class Evidence(BaseModel):
    event_id: int
    why: str = ""
    # Filled from the event store when citations are verified (never trusted from the LLM).
    time: str = ""
    host: str = ""
    service: str = ""
    message: str = ""


class RCAReport(BaseModel):
    title: str = ""
    summary: str = ""
    root_cause: RootCause = Field(default_factory=RootCause)
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    impact: str = ""
    timeline: list[TimelineEntry] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    remediation: list[str] = Field(default_factory=list)
    dropped_citations: list[int] = Field(default_factory=list)


REPORT_SCHEMA_HINT = """{
  "title": "short incident title",
  "summary": "2-3 sentence summary",
  "root_cause": {"category": "bad_deploy|memory_leak|dns_failure|auth_storm|disk_full|unknown",
                 "service": "service at fault", "host": "host at fault or empty", "description": "mechanism"},
  "confidence": 0.0-1.0,
  "impact": "who/what was affected",
  "timeline": [{"time": "ISO-8601", "event": "what happened"}],
  "evidence": [{"event_id": 123, "why": "why this event supports the root cause"}],
  "remediation": ["action", "..."]
}"""
