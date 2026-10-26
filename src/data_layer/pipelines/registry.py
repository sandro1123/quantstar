"""pipelines/registry.py — 管道注册表"""
from data_layer.pipelines.base import BasePipeline


class PipelineRegistry:
    def __init__(self):
        self._pipelines: dict[str, BasePipeline] = {}

    def register(self, pipeline: BasePipeline):
        self._pipelines[pipeline.name] = pipeline

    def get(self, name: str) -> BasePipeline | None:
        return self._pipelines.get(name)

    def get_all(self) -> list[BasePipeline]:
        return list(self._pipelines.values())

    def run(self, name: str, input) -> object:
        pipe = self.get(name)
        if pipe is None:
            raise ValueError(f"Pipeline '{name}' 未注册")
        return pipe.execute(input)
