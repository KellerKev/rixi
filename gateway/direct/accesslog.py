"""The access log without what works as a credential: a presigned URL carries its signature in
the query, and a share link is its token, so neither is written to the log."""
import copy
import logging
import re

_SHARE = re.compile(r"^/s/[^/?]+")


def redact(target: str, queries: bool = True) -> str:
    """A share link's token always goes; the query goes where it carries signatures (`queries`)."""
    path, sep, query = target.partition("?")
    path = _SHARE.sub("/s/-", path)
    if not sep:
        return path
    return path + ("?-" if queries else "?" + query)


class Redact(logging.Filter):
    def __init__(self, queries: bool = True) -> None:
        super().__init__()
        self.queries = queries

    def filter(self, record: logging.LogRecord) -> bool:
        a = record.args
        # uvicorn's access record: (client, method, path with query, http version, status)
        if isinstance(a, tuple) and len(a) >= 3 and isinstance(a[2], str):
            record.args = a[:2] + (redact(a[2], self.queries),) + a[3:]
        return True


def log_config(queries: bool = True) -> dict:
    """uvicorn's logging config with the access log redacted. The storage proxy drops every
    query (presigned URLs sign there); the gateway's API keeps its queries, which hold nothing
    secret and are what an operator reads the log for."""
    import uvicorn.config
    cfg = copy.deepcopy(uvicorn.config.LOGGING_CONFIG)
    cfg.setdefault("filters", {})["redact"] = {"()": Redact, "queries": queries}
    cfg["handlers"]["access"]["filters"] = ["redact"]
    return cfg
