"""The access log without what works as a credential: a presigned URL carries its signature in
the query, and a share link is its token, so neither is written to the log."""
import copy
import logging
import re

_SHARE = re.compile(r"^/s/[^/?]+")


def redact(target: str) -> str:
    path, sep, _ = target.partition("?")
    return _SHARE.sub("/s/-", path) + ("?-" if sep else "")


class Redact(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        a = record.args
        # uvicorn's access record: (client, method, path with query, http version, status)
        if isinstance(a, tuple) and len(a) >= 3 and isinstance(a[2], str):
            record.args = a[:2] + (redact(a[2]),) + a[3:]
        return True


def log_config() -> dict:
    """uvicorn's logging config with the access log redacted."""
    import uvicorn.config
    cfg = copy.deepcopy(uvicorn.config.LOGGING_CONFIG)
    cfg.setdefault("filters", {})["redact"] = {"()": Redact}
    cfg["handlers"]["access"]["filters"] = ["redact"]
    return cfg
