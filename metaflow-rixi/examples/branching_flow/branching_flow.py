"""A minimal Metaflow flow with one step running on RIXI compute.

`start` and `end` run locally; `compute` runs on a rixi box (the @rixi step). Because the
datastore is S3, Metaflow moves the artifacts — the value produced remotely by `compute` is read
by the local `end` step. See ../../README.md for how to run it.
"""
from metaflow import FlowSpec, rixi, step


class BranchingFlow(FlowSpec):
    @step
    def start(self):
        self.n = 21
        self.next(self.compute)

    @rixi(server="http://127.0.0.1:9002")  # or @rixi(resource="hetzner-cpu") via the gateway
    @step
    def compute(self):
        self.result = self.n * 2       # produced on the rixi box, stored in the S3 datastore
        print("computed on the rixi box: result =", self.result)
        self.next(self.end)

    @step
    def end(self):
        print("end (local) sees result =", self.result)
        assert self.result == 42, self.result


if __name__ == "__main__":
    BranchingFlow()
