"""Strict, shared configuration for every RetailPrintGuard service."""

from __future__ import annotations

import os
import re
from enum import StrEnum
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path, PurePosixPath
from typing import Annotated, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

DEVICE_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")
MAC_ADDRESS_PATTERNS = (
    re.compile(r"^[0-9A-Fa-f]{12}$"),
    re.compile(r"^(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$"),
    re.compile(r"^(?:[0-9A-Fa-f]{2}-){5}[0-9A-Fa-f]{2}$"),
)


class DeviceType(StrEnum):
    POS = "pos"
    RCH = "rch"


class ParserKind(StrEnum):
    ESCPOS = "escpos"
    RCH_OBSERVED = "rch_observed"


class DeviceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    name: Annotated[str, Field(min_length=1, max_length=120)]
    mac_address: Annotated[str | None, Field(min_length=17, max_length=17)] = None
    department: Annotated[str | None, Field(min_length=1, max_length=128)] = None
    role: Annotated[str | None, Field(min_length=1, max_length=64)] = None
    type: DeviceType
    listen_ip: IPv4Address
    listen_port: Annotated[int, Field(ge=1, le=65535)]
    target_ip: IPv4Address
    target_port: Annotated[int, Field(ge=1, le=65535)]
    parser: ParserKind
    bidirectional: bool = True
    enabled: bool = True
    allowed_clients: tuple[IPv4Address, ...] = ()
    allowed_networks: tuple[IPv4Network, ...] = ()

    @field_validator("id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        if not DEVICE_ID_RE.fullmatch(value):
            raise ValueError("device id must match [a-z][a-z0-9_-]{1,63}")
        return value

    @field_validator("mac_address", mode="before")
    @classmethod
    def canonical_mac_address(cls, value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("device MAC address must be a string")
        candidate = value.strip()
        if not any(pattern.fullmatch(candidate) for pattern in MAC_ADDRESS_PATTERNS):
            raise ValueError(
                "device MAC address must contain 12 hexadecimal digits, optionally "
                "separated consistently by ':' or '-'"
            )
        compact = candidate.replace(":", "").replace("-", "").upper()
        return ":".join(compact[index : index + 2] for index in range(0, 12, 2))

    @field_validator("department", "role", mode="before")
    @classmethod
    def strip_optional_device_label(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def coherent_type_and_parser(self) -> DeviceConfig:
        if self.type is DeviceType.POS and self.parser is not ParserKind.ESCPOS:
            raise ValueError("POS devices require parser=escpos")
        if self.type is DeviceType.RCH and self.parser is not ParserKind.RCH_OBSERVED:
            raise ValueError("RCH devices require parser=rch_observed")
        if self.listen_ip == self.target_ip and self.listen_port == self.target_port:
            raise ValueError("listen and target endpoint must differ")
        if not self.bidirectional:
            raise ValueError("all configured transparent relays must be bidirectional")
        if not self.allowed_clients and not self.allowed_networks:
            raise ValueError("at least one allowed client or network is required")
        return self


class IngestionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scan_interval_seconds: Annotated[float, Field(gt=0, le=3600)] = 3
    retry_initial_seconds: Annotated[float, Field(gt=0, le=3600)] = 2
    retry_max_seconds: Annotated[float, Field(gt=0, le=86400)] = 300
    max_batch_jobs: Annotated[int, Field(ge=1, le=10_000)] = 100
    spool_warning_bytes: Annotated[int, Field(ge=1_048_576, le=10**15)] = 1_073_741_824

    @model_validator(mode="after")
    def retry_order(self) -> IngestionConfig:
        if self.retry_max_seconds < self.retry_initial_seconds:
            raise ValueError("retry_max_seconds must be >= retry_initial_seconds")
        return self


class CorrelationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    time_window_seconds: Annotated[int, Field(ge=1, le=604_800)] = 7200
    minimum_score: Annotated[int, Field(ge=0, le=100)] = 60


class FraudConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    default_amount_drop_percent: Annotated[int, Field(ge=0, le=100)] = 20
    order_without_fiscal_close_minutes: Annotated[int, Field(ge=1, le=10_080)] = 120
    extreme_price_change_percent: Annotated[int, Field(ge=0, le=100)] = 70


class ApiConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    bind_host: IPv4Address = IPv4Address("127.0.0.1")
    bind_port: Annotated[int, Field(ge=1024, le=65535)] = 8080
    access_token_minutes: Annotated[int, Field(ge=5, le=1440)] = 30
    failed_login_limit: Annotated[int, Field(ge=1, le=100)] = 5
    failed_login_delay_seconds: Annotated[float, Field(ge=0, le=60)] = 2
    allowed_origins: tuple[str, ...] = ()


class RetentionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    database_days: Annotated[int, Field(ge=0, le=36_500)] = 0
    spool_days: Annotated[int, Field(ge=0, le=36_500)] = 0


class ProxyConfig(BaseModel):
    """Protocol-neutral data-plane limits.

    Capture is deliberately asynchronous: a full or failed capture queue is
    handled according to ``storage_failure_policy`` and never grows without a
    bound.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    connect_timeout_seconds: Annotated[float, Field(gt=0, le=3600)] = 30
    forward_timeout_seconds: Annotated[float, Field(gt=0, le=3600)] = 30
    response_tail_timeout_seconds: Annotated[float, Field(gt=0, le=3600)] = 10
    session_idle_timeout_seconds: Annotated[float, Field(gt=0, le=86_400)] = 300
    shutdown_grace_seconds: Annotated[float, Field(gt=0, le=3600)] = 15
    read_chunk_bytes: Annotated[int, Field(ge=512, le=1_048_576)] = 65_536
    capture_queue_max_events: Annotated[int, Field(ge=1, le=100_000)] = 4096
    max_connections: Annotated[int, Field(ge=1, le=4096)] = 128
    fsync_each_event: bool = True
    storage_failure_policy: str = "continue"

    @field_validator("storage_failure_policy")
    @classmethod
    def valid_storage_failure_policy(cls, value: str) -> str:
        normalized = value.lower()
        if normalized not in {"continue", "abort"}:
            raise ValueError("storage_failure_policy must be continue or abort")
        return normalized


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Annotated[int, Field(ge=1, le=1)] = 1
    timezone: str = "Europe/Rome"
    spool_root: Path
    archive_root: Path
    log_root: Path
    database_url_env: str = "RPG_DATABASE_URL"
    devices: tuple[DeviceConfig, ...]
    ingestion: IngestionConfig = IngestionConfig()
    correlation: CorrelationConfig = CorrelationConfig()
    fraud: FraudConfig = FraudConfig()
    api: ApiConfig = ApiConfig()
    retention: RetentionConfig = RetentionConfig()
    proxy: ProxyConfig = ProxyConfig()

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown timezone: {value}") from exc
        return value

    @field_validator("spool_root", "archive_root", "log_root", mode="before")
    @classmethod
    def safe_absolute_path(cls, value: object) -> Path:
        if not isinstance(value, (str, os.PathLike)):
            raise ValueError("path must be a string")
        raw = os.fspath(value)
        if "\x00" in raw or not raw.startswith("/"):
            raise ValueError("path must be absolute")
        normalized = PurePosixPath(raw)
        if ".." in normalized.parts:
            raise ValueError("path traversal is forbidden")
        if normalized in {
            PurePosixPath("/"),
            PurePosixPath("/etc"),
            PurePosixPath("/usr"),
            PurePosixPath("/var"),
            PurePosixPath("/home"),
        }:
            raise ValueError("path is an unsafe system root")
        return Path(str(normalized))

    @field_validator("database_url_env")
    @classmethod
    def safe_env_name(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,127}", value):
            raise ValueError("database_url_env must be an uppercase environment variable name")
        return value

    @model_validator(mode="after")
    def unique_devices_and_endpoints(self) -> Settings:
        if not self.devices:
            raise ValueError("at least one device is required")
        ids = [device.id for device in self.devices]
        endpoints = [
            (device.listen_ip, device.listen_port) for device in self.devices if device.enabled
        ]
        targets = [
            (device.target_ip, device.target_port) for device in self.devices if device.enabled
        ]
        if len(ids) != len(set(ids)):
            raise ValueError("device ids must be unique")
        if len(endpoints) != len(set(endpoints)):
            raise ValueError("enabled listener endpoints must be unique")
        if len(targets) != len(set(targets)):
            raise ValueError("enabled target endpoints must be unique")
        if len({self.spool_root, self.archive_root, self.log_root}) != 3:
            raise ValueError("spool, archive and log roots must differ")
        if any(endpoint in endpoints for endpoint in targets):
            raise ValueError("a target endpoint cannot also be an enabled listener endpoint")
        return self

    def database_url(self) -> SecretStr:
        value = os.environ.get(self.database_url_env)
        if not value:
            raise RuntimeError(
                f"required database URL environment variable is unset: {self.database_url_env}"
            )
        return SecretStr(value)


def load_settings(path: str | os.PathLike[str]) -> Settings:
    config_path = Path(path)
    if config_path.is_symlink() or not config_path.is_file():
        raise ValueError(f"configuration must be a regular non-symlink file: {config_path}")
    if config_path.stat().st_size > 1_048_576:
        raise ValueError("configuration exceeds 1 MiB")
    try:
        loaded: Any = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError(f"cannot load configuration: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ValueError("configuration root must be a mapping")
    return Settings.model_validate(loaded)
