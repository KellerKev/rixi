import os
import sys

# Make the client modules importable bare (they live one level up, cwd-relative).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
