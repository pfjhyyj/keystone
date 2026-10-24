"""Dependencies assembled per process; no service rules or implicit registrations."""

from collections.abc import Callable
from dataclasses import dataclass

from keystone.platform.model_access.adapters.billing.registry import BillingClient
from keystone.platform.model_access.adapters.coordination import QueryCoordination
from keystone.platform.model_access.adapters.credentials import ChannelCredentials
from keystone.platform.model_access.adapters.http import HttpClient
from keystone.platform.model_access.adapters.inference.client import InferenceClient


@dataclass(frozen=True)
class ModelClients:
    http: HttpClient
    inference: InferenceClient
    billing: BillingClient
    credentials: ChannelCredentials
    coordination: QueryCoordination
    model_referenced: Callable
