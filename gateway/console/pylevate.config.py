"""PyLevate project configuration for the rixi gateway console."""

from pylevate.config import Config

config = Config(
    mode="app",
    entry="main.py",
    out_dir="dist/",
    dev_port=3000,
    hmr_port=3001,
)
