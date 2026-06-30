import os
import sys

# Make rixi_server importable (it lives in the parent server/ dir).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
