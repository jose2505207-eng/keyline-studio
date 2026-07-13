import os
import sys

# Tests must run offline: force the pure-Python hydrology engine so no
# WhiteboxTools binary download is attempted.
os.environ.setdefault("KEYLINE_HYDRO_ENGINE", "pysheds")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
