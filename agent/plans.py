"""Session plan inputs and ordered, atomic checklist edits."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Status = Literal["pending", "in_progress", "completed"]
Step = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=400)]


class PlanItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    step: Step
    status: Status = "pending"


class PlanPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: Literal["add", "update", "remove"]
    index: int | None = Field(default=None, ge=1, le=51)
    step: Step | None = None
    status: Status | None = None


class PlanInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["read", "update", "patch"]
    plan: list[PlanItem] | None = Field(default=None, max_length=50)
    patches: list[PlanPatch] | None = Field(default=None, min_length=1, max_length=50)
    explanation: str | None = Field(default=None, max_length=400)

    def apply(self, previous):
        if self.action == "update" and self.plan is not None and self.patches is None:
            items = self.plan
        elif self.action == "patch" and self.patches and self.plan is None:
            items = [PlanItem.model_validate(item) for item in previous["items"]]
            for patch in self.patches:
                offset = patch.index - 1 if patch.index is not None else len(items)
                if patch.operation == "add":
                    if offset > len(items) or patch.step is None:
                        raise ValueError("An add needs step text and a valid insertion index")
                    items.insert(
                        offset, PlanItem(step=patch.step, status=patch.status or "pending")
                    )
                elif patch.index is None or offset >= len(items):
                    raise ValueError("Update and remove require an existing 1-based index")
                elif patch.operation == "remove":
                    if patch.step is not None or patch.status is not None:
                        raise ValueError("Remove only takes an index")
                    items.pop(offset)
                elif patch.step is not None or patch.status is not None:
                    old = items[offset]
                    items[offset] = PlanItem(
                        step=patch.step or old.step, status=patch.status or old.status
                    )
                else:
                    raise ValueError("Update requires step text, status, or both")
        else:
            raise ValueError("Use plan for update or patches for patch")
        if len(items) > 50 or sum(item.status == "in_progress" for item in items) > 1:
            raise ValueError("A plan supports 50 items and at most one in-progress item")
        explanation = self.explanation if self.explanation is not None else previous["explanation"]
        return {
            "revision": previous["revision"] + 1,
            "explanation": explanation.strip(),
            "items": [item.model_dump() for item in items],
        }
