"""QuantPilot — a local market terminal.

Finviz-class screening over the whole US universe, Bloomberg-style chrome,
no API keys and no vendor. The data layer (net/store/universe/providers)
is deliberately separable from the terminal (screen/server/web) so the
sources can be swapped without touching the UI.
"""

__version__ = "0.1.0"
