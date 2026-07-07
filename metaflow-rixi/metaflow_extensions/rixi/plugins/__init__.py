"""RIXI plugin registration for Metaflow.

Metaflow merges these DESC lists with its own (metaflow/plugins/__init__.py). We add:
  • the `@rixi` step decorator, and
  • a trampoline `rixi` CLI whose `step` subcommand executes a single step on a rixi box.
The paths are relative to this package.
"""

STEP_DECORATORS_DESC = [
    ("rixi", ".rixi_decorator.RixiDecorator"),
]

# Trampoline CLIs are the ones invoked as `python flow.py <name> step ...` (same slot @kubernetes
# uses). runtime_step_cli reroutes a decorated step to `["rixi", "step"]`.
TRAMPOLINE_CLIS_DESC = [
    ("rixi", ".rixi_cli.cli"),
]
