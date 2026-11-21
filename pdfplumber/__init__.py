__all__ = [
    "__version__",
    "utils",
    "pdfminer",
    "open",
    "repair",
    "set_debug",
    "ledger",
]

import pdfminer
import pdfminer.pdftypes

from . import utils
from ._version import __version__
from .pdf import PDF
from .repair import repair

open = PDF.open


def __getattr__(name):
    # Lazily expose the ledger-sync submodule so that `import pdfplumber`
    # does not pay for its stdlib dependencies (sqlite3 etc.).
    if name == "ledger":
        from . import ledger

        return ledger
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
