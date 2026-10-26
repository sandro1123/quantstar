"""agent_layer/llm/discoverers/nvidia_nim.py — NvidiaNIMDiscoverer"""


class NvidiaNIMDiscoverer:
    API_URL = "https://integrate.api.nvidia.com/v1"

    def discover(self) -> list[dict]:
        return [
            {"name": "nvidia-nemotron-super", "provider": "nvidia_nim", "free": True, "context": 32768},
            {"name": "nvidia-llama-70b", "provider": "nvidia_nim", "free": True, "context": 32768},
            {"name": "nvidia-deepseek-pro", "provider": "nvidia_nim", "free": True, "context": 32768},
            {"name": "nvidia-llama-8b", "provider": "nvidia_nim", "free": True, "context": 32768},
        ]
