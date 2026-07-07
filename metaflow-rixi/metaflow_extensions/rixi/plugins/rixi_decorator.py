"""The `@rixi` step decorator — a Metaflow compute backend that runs a step on a rixi box.

Mirrors the mechanism of `@kubernetes`/`@batch`: `runtime_step_cli` reroutes the step's launch to
the trampoline `rixi step` CLI command, which ships the flow to a rixi box (a rixi server directly,
or one provisioned on demand via the gateway), runs the step there against a shared S3 datastore,
streams logs back, and tears the box down. Because the datastore is S3, Metaflow moves the
artifacts itself — rixi only ships code + runs the step command.

    from metaflow import FlowSpec, step
    from metaflow_extensions.rixi.plugins.rixi_decorator import ...  # (auto-registered as @rixi)

    class MyFlow(FlowSpec):
        @rixi(server="http://127.0.0.1:9000")      # or resource="hetzner-cpu" (via the gateway)
        @step
        def train(self): ...
"""
import sys

from metaflow.decorators import StepDecorator


class RixiDecorator(StepDecorator):
    """Run this step on RIXI compute.

    Attributes
    ----------
    server:    a rixi server URL to run the step on directly (skips the gateway).
    resource:  a gateway resource name to provision/reuse (needs `gateway`+`secret`).
    gateway:   gateway ws URL (with `resource`).
    secret:    gateway shared secret (env RIXI_GATEWAY_SECRET).
    provider:  gateway provider for an ad-hoc box when no `resource` is given.
    token:     JWT bearer token for the rixi server.
    aes_key:   base64 AES key (matches a server started with --aes-key).
    task:      the pixi task in the flow project that runs the step (default "rixi-step").
    teardown:  tear the box down after the step (gateway only; default True).
    """

    name = "rixi"
    defaults = {
        "server": None,
        "resource": None,
        "gateway": None,
        "secret": None,
        "provider": None,
        "token": None,
        "aes_key": None,
        "task": "rixi-step",
        "teardown": True,
    }

    # Forwarded to the `rixi step` CLI as options (kept separate from Metaflow's own step options).
    _OPTION_KEYS = ("server", "resource", "gateway", "secret", "provider", "token", "aes_key",
                    "task", "teardown")

    def runtime_step_cli(self, cli_args, retry_count, max_user_code_retries, ubf_context):
        # Only redirect the real attempts; once user-code retries are exhausted, any fallback runs
        # locally (matches @kubernetes/@batch).
        if retry_count <= max_user_code_retries:
            cli_args.commands = ["rixi", "step"]
            for k in self._OPTION_KEYS:
                v = self.attributes.get(k)
                if v is None:
                    continue
                # click flags want a bool; everything else a string.
                cli_args.command_options[k] = v
            cli_args.entrypoint[0] = sys.executable
