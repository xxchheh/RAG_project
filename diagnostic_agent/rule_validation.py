"""Compatibility wrappers for the shared knowledge contract."""
from rag_service.contracts import DEVICE_TYPES, DERIVED_LABELS, condition_label
from rag_service import contracts
from .common import DemoError

def normalize_condition(value):
    try:
        return contracts.normalize_condition(value)
    except ValueError as exc:
        raise DemoError(str(exc)) from None

def normalize_rule(value):
    try:
        return contracts.normalize_rule(value)
    except ValueError as exc:
        raise DemoError(str(exc)) from None
