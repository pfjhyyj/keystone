"""NewAPI AI (newapiai.com); account billing has no verified integration yet."""

from keystone.platform.model_access.adapters.billing.contracts import ResponseBilling


class Newapi(ResponseBilling):
    provider = "newapi"
    api_base = "https://newapiai.com/v1"

    def binding(self, api_base):
        if api_base != self.api_base:
            raise ValueError("Fixed NewAPI AI origin required")
        return None
