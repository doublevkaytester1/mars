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

import asyncio
import logging
from typing import List, Dict, Any, Set
from .....core import ChunkGraph, Chunk, TileContext
from .....core.operand import (
    Fuse,
    VirtualOperand,
    MapReduceOperand,
    execute,
)
from .....lib.aio import alru_cache
from .....optimization.physical import optimize
from .....resource import Resource
from .....serialization import serialize, deserialize
from .....typing import BandType
from .....utils import (
    lazy_import,
    get_chunk_params,
    get_chunk_key_to_data_keys,
    ensure_coverage,
)
from ....cluster.api import ClusterAPI
from ....lifecycle.api import LifecycleAPI
from ....meta.api import MetaAPI
from ....subtask import Subtask, SubtaskGraph
from ....subtask.utils import iter_input_data_keys, iter_output_data
from ..api import TaskExecutor, ExecutionChunkResult, register_executor_cls
from .context import RayExecutionContext

ray = lazy_import("ray")
logger = logging.getLogger(__name__)


def execute_subtask(
    subtask_id: str,
    subtask_chunk_graph: ChunkGraph,
    output_meta_keys: Set[str],
    input_keys: List[str],
    *inputs
):
    logger.info("Begin to execute subtask: %s", subtask_id)
    ensure_coverage()
    subtask_chunk_graph = deserialize(*subtask_chunk_graph)
    # inputs = [i[1] for i in inputs]
    context = RayExecutionContext(zip(input_keys, inputs))
    # optimize chunk graph.
    subtask_chunk_graph = optimize(subtask_chunk_graph)
    # from data_key to results
    for chunk in subtask_chunk_graph.topological_iter():
        if chunk.key not in context:
            execute(context, chunk.op)

    output = {
        key: data for key, data, _ in iter_output_data(subtask_chunk_graph, context)
    }
    output_values = []
    if output_meta_keys:
        output_meta = {}
        for chunk in subtask_chunk_graph.result_chunks:
            if chunk.key in output_meta_keys:
                if isinstance(chunk.op, Fuse):
                    # fuse op
                    chunk = chunk.chunk
                output_meta[chunk.key] = get_chunk_params(chunk)
        assert len(output_meta_keys) == len(output_meta)
        output_values.append(output_meta)
    output_values.extend(output.values())

    logger.info("Finish executing subtask: %s", subtask_id)
    return output_values[0] if len(output_values) == 1 else output_values


@register_executor_cls
class RayTaskExecutor(TaskExecutor):
    name = "ray"

    def __init__(
        self,
        config,
        task,
        tile_context,
        ray_executor,
        cluster_api,
        lifecycle_api,
        meta_api,
    ):
        self._config = config
        self._task = task
        self._tile_context = tile_context
        self._ray_executor = ray_executor

        # api
        self._cluster_api = cluster_api
        self._lifecycle_api = lifecycle_api
        self._meta_api = meta_api

        self._task_context = {}

    @classmethod
    async def create(
        cls,
        config: Dict,
        *,
        session_id: str,
        address: str,
        task,
        tile_context: TileContext,
        **kwargs
    ) -> "TaskExecutor":
        ray_executor = ray.remote(execute_subtask)
        cluster_api, lifecycle_api, meta_api = await cls._get_apis(session_id, address)
        return cls(
            config,
            task,
            tile_context,
            ray_executor,
            cluster_api,
            lifecycle_api,
            meta_api,
        )

    @classmethod
    @alru_cache(cache_exceptions=False)
    async def _get_apis(cls, session_id: str, address: str):
        # TODO(fyrestone): Remove ClusterAPI usage.
        return await asyncio.gather(
            ClusterAPI.create(address),
            LifecycleAPI.create(session_id, address),
            MetaAPI.create(session_id, address),
        )

    async def execute_subtask_graph(
        self,
        stage_id: str,
        subtask_graph: SubtaskGraph,
        chunk_graph: ChunkGraph,
        tile_context: TileContext,
        context: Any = None,
    ) -> Dict[Chunk, ExecutionChunkResult]:
        logger.info("Stage %s start.", stage_id)
        context = self._task_context  # TODO(fyrestone): Load context from meta service.
        output_meta_object_refs = []

        logger.info("Submitting %s subtasks of stage %s.", len(subtask_graph), stage_id)
        # TODO(fyrestone): Filter out the Fetch chunk.
        result_keys = {chunk.key for chunk in chunk_graph.result_chunks}
        for subtask in subtask_graph.topological_iter():
            subtask_chunk_graph = subtask.chunk_graph
            key_to_input = await self._load_subtask_inputs(
                stage_id, subtask, subtask_chunk_graph, context
            )
            output_keys = self._get_subtask_output_keys(subtask_chunk_graph)
            output_meta_keys = result_keys & output_keys
            output_count = len(output_keys) + bool(output_meta_keys)
            output_object_refs = self._ray_executor.options(
                num_returns=output_count
            ).remote(
                subtask.subtask_id,
                serialize(subtask_chunk_graph),
                output_meta_keys,
                list(key_to_input.keys()),
                *key_to_input.values(),
            )
            if output_count == 0:
                continue
            elif output_count == 1:
                output_object_refs = [output_object_refs]
            if output_meta_keys:
                meta_object_ref, *output_object_refs = output_object_refs
                # TODO(fyrestone): Fetch(not get) meta object here.
                output_meta_object_refs.append(meta_object_ref)
            context.update(zip(output_keys, output_object_refs))
        logger.info("Submitted %s subtasks of stage %s.", len(subtask_graph), stage_id)

        assert len(output_meta_object_refs) > 0
        key_to_meta = {}
        meta_list = await asyncio.gather(*output_meta_object_refs)
        for meta in meta_list:
            key_to_meta.update(meta)
        assert len(key_to_meta) == len(chunk_graph.result_chunks)
        logger.info("Got %s metas of stage %s.", len(output_meta_object_refs), stage_id)

        chunk_to_result = {}
        output_object_refs = []
        for chunk in chunk_graph.result_chunks:
            chunk_key = chunk.key
            object_ref = context[chunk_key]
            output_object_refs.append(object_ref)
            chunk_to_result[chunk] = ExecutionChunkResult(
                key_to_meta[chunk_key], object_ref
            )

        logger.info("Waiting for stage %s complete.", stage_id)
        ray.wait(output_object_refs, fetch_local=False)
        logger.info("Stage %s is complete.", stage_id)
        return chunk_to_result

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            tileable_keys = []
            update_metas = []
            update_lifecycles = []
            for tileable in self._task.tileable_graph.result_tileables:
                tileable_keys.append(tileable.key)
                tileable = tileable.data if hasattr(tileable, "data") else tileable
                chunk_keys = []
                for chunk in self._tile_context[tileable].chunks:
                    chunk_keys.append(chunk.key)
                    object_ref = self._task_context[chunk.key]
                    update_metas.append(
                        self._meta_api.set_chunk_meta.delay(
                            chunk,
                            bands=[],
                            object_ref=object_ref,
                        )
                    )
                    update_lifecycles.append(
                        self._lifecycle_api.track.delay(tileable.key, chunk_keys)
                    )
            await self._meta_api.set_chunk_meta.batch(*update_metas)
            await self._lifecycle_api.track.batch(*update_lifecycles)
            await self._lifecycle_api.incref_tileables(tileable_keys)

    async def get_available_band_resources(self) -> Dict[BandType, Resource]:
        async for bands in self._cluster_api.watch_all_bands():
            if bands:
                return bands

    async def get_progress(self) -> float:
        """Get the execution progress."""
        return 1

    async def cancel(self):
        """Cancel execution."""

    async def _load_subtask_inputs(
        self, stage_id: str, subtask: Subtask, chunk_graph: ChunkGraph, context: Dict
    ):
        """
        Load a dict of input key to object ref of subtask from context.

        It updates the context if the input object refs are fetched from
        the meta service.
        """
        key_to_input = {}
        key_to_get_meta = {}
        chunk_key_to_data_keys = get_chunk_key_to_data_keys(chunk_graph)
        for key, _ in iter_input_data_keys(
            subtask, chunk_graph, chunk_key_to_data_keys
        ):
            if key in context:
                key_to_input[key] = context[key]
            else:
                key_to_get_meta[key] = self._meta_api.get_chunk_meta.delay(
                    key, fields=["object_refs"]
                )
        if key_to_get_meta:
            logger.info(
                "Fetch %s metas and update context of stage %s.",
                len(key_to_get_meta),
                stage_id,
            )
            meta_list = await self._meta_api.get_chunk_meta.batch(
                *key_to_get_meta.values()
            )
            for key, meta in zip(key_to_get_meta.keys(), meta_list):
                object_ref = meta["object_refs"][0]
                key_to_input[key] = object_ref
                context[key] = object_ref
        return key_to_input

    @staticmethod
    def _get_subtask_output_keys(chunk_graph: ChunkGraph):
        output_keys = {}
        for chunk in chunk_graph.results:
            if isinstance(chunk.op, VirtualOperand):
                continue
            elif isinstance(chunk.op, MapReduceOperand):
                # TODO(fyrestone): Handle shuffle operands.
                raise NotImplementedError(
                    "The shuffle operands are not supported by the ray executor."
                )
            else:
                output_keys[chunk.key] = 1
        return output_keys.keys()
