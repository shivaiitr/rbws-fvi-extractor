"""Pipeline package.

Import submodules directly, e.g.:
`from rbws_extractor.pipeline.orchestrator import RbwsPipeline`
"""

__all__ = ["RbwsPipeline"]


def __getattr__(name: str):
    if name == "RbwsPipeline":
        from rbws_extractor.pipeline.orchestrator import RbwsPipeline

        return RbwsPipeline
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")