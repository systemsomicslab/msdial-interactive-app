"""MS-DIAL Interactive cross-platform application."""

import csv as _csv

__version__ = "0.5.54"

# MS-DIAL's text exports (.mdpeak, the alignment exports) carry each peak's MS/MS spectrum in one field. A ZenoTOF
# DDA spectrum can be longer than Python's default csv field limit of 131,072 characters (MB-POST MPST000048: 9 of
# 19,623 peaks of one .mdpeak, up to 270,713), and every csv reader of such an export then stops with "field larger
# than field limit". Raised once for the process, to the largest value a C long holds on Windows.
_csv.field_size_limit(2**31 - 1)
