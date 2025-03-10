# Copyright 1999-2021 Alibaba Group Holding Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Dict, Any, Type

from ....core import ChunkGraph, Chunk, TileContext
from ....resource import Resource
from ....typing import BandType
from ...subtask import SubtaskGraph, SubtaskResult


@dataclass
class ExecutionChunkResult:
    meta: Dict  # The chunk meta for iterative tiling.
    context: Any  # The context info, e.g. ray.ObjectRef.


class TaskExecutor(ABC):
    name = None

    @classmethod
    @abstractmethod
    async def create(
        cls,
        config: Dict,
        *,
        session_id: str,
        address: str,
        task,
        tile_context: TileContext,
        **kwargs,
    ) -> "TaskExecutor":
        name = config.get("backend", "mars")
        backend_config = config.get(name, {})
        executor_cls = _name_to_task_executor_cls[name]
        if executor_cls.create.__func__ is TaskExecutor.create.__func__:
            raise NotImplementedError(
                f"The {executor_cls} should implement the abstract classmethod `create`."
            )
        return await executor_cls.create(
            backend_config,
            session_id=session_id,
            address=address,
            task=task,
            tile_context=tile_context,
            **kwargs,
        )

    async def __aenter__(self):
        """Called when begin to execute the task."""

    @abstractmethod
    async def execute_subtask_graph(
        self,
        stage_id: str,
        subtask_graph: SubtaskGraph,
        chunk_graph: ChunkGraph,
        tile_context: TileContext,
        context: Any = None,
    ) -> Dict[Chunk, ExecutionChunkResult]:
        """Execute a subtask graph and returns result."""

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Called when finish the task."""

    @abstractmethod
    async def get_available_band_resources(self) -> Dict[BandType, Resource]:
        """Get available band resources."""

    @abstractmethod
    async def get_progress(self) -> float:
        """Get the execution progress."""

    @abstractmethod
    async def cancel(self):
        """Cancel execution."""

    # The following APIs are for compatible with mars backend, they
    # will be removed as soon as possible.
    async def set_subtask_result(self, subtask_result: SubtaskResult):
        """Set the subtask result."""

    def get_stage_processors(self):
        """Get stage processors."""


_name_to_task_executor_cls: Dict[str, Type[TaskExecutor]] = {}


def register_executor_cls(executor_cls: Type[TaskExecutor]):
    _name_to_task_executor_cls[executor_cls.name] = executor_cls


class Fetcher:
    """The data fetcher for execution backends."""

    name = None
    required_meta_keys = ()  # The required meta keys.

    @abstractmethod
    def __init__(self, **kwargs):
        pass

    @abstractmethod
    async def append(self, chunk_key: str, chunk_meta: Dict, conditions: List = None):
        """Append chunk key and related infos."""

    @abstractmethod
    async def get(self):
        """Get all the data of appended chunk keys."""

    @classmethod
    def create(cls, backend: str, **kwargs) -> "Fetcher":
        fetcher_cls = _name_to_fetcher_cls[backend]
        return fetcher_cls(**kwargs)


_name_to_fetcher_cls: Dict[str, Type[Fetcher]] = {}


def register_fetcher_cls(fetcher_cls: Type[Fetcher]):
    _name_to_fetcher_cls[fetcher_cls.name] = fetcher_cls
    return fetcher_cls
