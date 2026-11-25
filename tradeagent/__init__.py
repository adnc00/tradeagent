"""tradeagent: rule-based ICT/SMC scanner, risk engine and user-confirmed order flow.

Nothing in this package sends a real-money order without the user typing the
exact confirmation word, in the terminal or in the local panel; the only unattended path is
``watch --demo-auto``, which runs on demo/paper accounts only. See ``tradeagent.cli``.
"""

__version__ = "0.9.0"
