"""gulp-dissect package."""

__all__ = ["__version__"]

try:
	from ._version import version as __version__
except ImportError:
	__version__ = "0.0.0"
