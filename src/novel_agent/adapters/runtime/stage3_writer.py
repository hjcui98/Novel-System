"""Real adapter to the public Stage 3 Writer Context Loop boundary."""

from __future__ import annotations

from collections.abc import Callable

from novel_agent.domain.generation import WritingLoopRequest
from novel_agent.domain.model_calls import ModelRequest
from novel_agent.domain.writing_loop import WritingLoopResult
from novel_agent.services.writer_context_loop import WriterContextLoopService
from novel_agent.services.writer_reactive_memory import ReactiveMemoryInputs


class Stage3WritingLeafAdapter:
    """Bind Stage 5 only to Stage 3 request/result and immutable evidence lineage."""

    is_fixture = False

    def __init__(
        self,
        loop: WriterContextLoopService,
        model_request_factory: Callable[[WritingLoopRequest], ModelRequest],
        reactive_inputs_factory: Callable[[WritingLoopRequest], ReactiveMemoryInputs],
    ) -> None:
        self._loop = loop
        self._model_request_factory = model_request_factory
        self._reactive_inputs_factory = reactive_inputs_factory

    async def run(self, request: WritingLoopRequest) -> WritingLoopResult:
        result = await self._loop.execute(
            request,
            self._model_request_factory(request),
            self._reactive_inputs_factory(request),
        )
        if result.run_id != request.run_id or result.task_id != request.task_id:
            raise RuntimeError("Stage 3 Writer returned cross-task lineage")
        return result


__all__ = ["Stage3WritingLeafAdapter"]
