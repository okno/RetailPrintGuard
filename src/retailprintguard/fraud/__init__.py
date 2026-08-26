"""Explainable deterministic fraud rules."""

from retailprintguard.fraud.engine import (
    DEFAULT_RULES,
    AuthorizedEmployeeDiscount,
    FraudContext,
    FraudEngine,
    FraudEvaluation,
    OperatorPatternStats,
    RuleDefinition,
    SuppressedFinding,
    WhitelistEntry,
    WhitelistScope,
    authorized_employee_discount,
    finding_chain_record,
    finding_fingerprint,
    whitelist_entry_key,
)

__all__ = [
    "AuthorizedEmployeeDiscount",
    "DEFAULT_RULES",
    "FraudContext",
    "FraudEngine",
    "FraudEvaluation",
    "OperatorPatternStats",
    "RuleDefinition",
    "SuppressedFinding",
    "WhitelistEntry",
    "WhitelistScope",
    "authorized_employee_discount",
    "finding_chain_record",
    "finding_fingerprint",
    "whitelist_entry_key",
]
