from typing import Annotated

from fastapi import Depends, HTTPException, Request

from app.agent.harness import AnswerHarness


def get_answer_harness(request: Request) -> AnswerHarness:
    harness = getattr(request.app.state, "answer_harness", None)
    if not isinstance(harness, AnswerHarness):
        raise HTTPException(
            status_code=503, detail="The answer harness is unavailable"
        )
    return harness


AnswerHarnessDependency = Annotated[
    AnswerHarness, Depends(get_answer_harness)
]
