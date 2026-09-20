"""Excel package exports."""

__all__ = ["RbwsExcelWriter"]


def __getattr__(name: str):
    if name == "RbwsExcelWriter":
        from rbws_extractor.excel.writer import RbwsExcelWriter

        return RbwsExcelWriter
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")