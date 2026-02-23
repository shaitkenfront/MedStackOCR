from __future__ import annotations

from enum import Enum


class DocumentType(str, Enum):
    PHARMACY = "pharmacy"
    CLINIC_OR_HOSPITAL = "clinic_or_hospital"
    UNKNOWN = "unknown"


class DecisionStatus(str, Enum):
    AUTO_ACCEPT = "AUTO_ACCEPT"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    REJECTED = "REJECTED"


class FieldName:
    PAYER_FACILITY_NAME = "payer_facility_name"
    PRESCRIBING_FACILITY_NAME = "prescribing_facility_name"
    PAYMENT_DATE = "payment_date"
    PAYMENT_AMOUNT = "payment_amount"

    REQUIRED_FIELDS = (
        PAYER_FACILITY_NAME,
        PAYMENT_DATE,
        PAYMENT_AMOUNT,
    )

