"""Compatibility import; implementation lives in uav_runtime.planning.scheduler."""
import sys
from .planning import scheduler as _implementation

sys.modules[__name__] = _implementation
