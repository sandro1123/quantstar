"""Pipeline Hook — before/after/on_error 生命周期钩子"""
from agent_layer.core.models import AgentRequest, AgentResponse
from common.logging import get_logger

logger = get_logger("agent.hooks")


class PipelineHook:
    def before_execute(self, request: AgentRequest):
        logger.debug(f"Hook before: {request.task}/{request.depth}")

    def after_execute(self, request: AgentRequest, response: AgentResponse):
        logger.debug(f"Hook after: {request.task} → {response.verdict}")

    def on_error(self, request: AgentRequest, error: Exception):
        logger.warning(f"Hook error: {request.task}: {error}")


class LoggingHook(PipelineHook):
    def after_execute(self, request: AgentRequest, response: AgentResponse):
        logger.info(f"[{request.consumer}] {request.task}/{request.depth} → {response.verdict} ({response.confidence:.2f})")
