"""Dependencies assembled per process; no service rules or implicit registrations."""

from dataclasses import dataclass

from .billing.registry import BillingClient
from .coordination import QueryCoordination
from .credentials import ChannelCredentials
from .http import HttpClient
from .inference.client import InferenceClient


@dataclass(frozen=True)
class ModelClients:
    http: HttpClient
    inference: InferenceClient
    billing: BillingClient
    credentials: ChannelCredentials
    coordination: QueryCoordination
