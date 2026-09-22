"""Reconstructed M1 executable grammar: explicit-ID temporal paper counts."""

from typing import Literal

from pydantic import Field, model_validator

from kdiff.core.contracts import Contract, CountRequest

OPERATORS = ("ResolveEntity", "NormalizeTime", "FilterInterval", "Expand", "PathSearch",
             "Aggregate", "CompareWindows", "ProjectProvenance", "ReadState", "UpdateState")
IMPLEMENTED = ("ResolveEntity", "NormalizeTime", "Aggregate", "ProjectProvenance")


class CountStep(Contract):
    id: str
    operator: Literal["ResolveEntity", "NormalizeTime", "Aggregate", "ProjectProvenance"]
    depends_on: list[str] = Field(default_factory=list)


class CountProgram(Contract):
    grammar: Literal["explicit-author-count-v1"] = "explicit-author-count-v1"
    release_id: str
    request: CountRequest
    steps: list[CountStep]

    @model_validator(mode="after")
    def valid(self):
        seen = set()
        for step in self.steps:
            if step.id in seen or any(dep not in seen for dep in step.depends_on):
                raise ValueError("Duplicate, missing, forward or cyclic dependency")
            seen.add(step.id)
        if [s.operator for s in self.steps] != list(IMPLEMENTED):
            raise ValueError("M1 supports only the four-step author-count grammar")
        expected = [[], [self.steps[0].id], [self.steps[0].id, self.steps[1].id], [self.steps[2].id]]
        if [s.depends_on for s in self.steps] != expected:
            raise ValueError("Count proof dependencies are incomplete")
        return self


def count_program(request: CountRequest, release_id: str) -> CountProgram:
    return CountProgram(release_id=release_id, request=request, steps=[
        CountStep(id="author", operator="ResolveEntity"),
        CountStep(id="window", operator="NormalizeTime", depends_on=["author"]),
        CountStep(id="count", operator="Aggregate", depends_on=["author", "window"]),
        CountStep(id="provenance", operator="ProjectProvenance", depends_on=["count"]),
    ])
