"""Removed Token budget gate.

Import compatibility only; ModelCallLedger is observe-only and never refuses,
cancels, or changes terminal state based on Token usage.
"""

from plow_whip_web.runtime.model_call_ledger import ModelCallLedger

BudgetManager = ModelCallLedger

__all__ = ["BudgetManager"]
