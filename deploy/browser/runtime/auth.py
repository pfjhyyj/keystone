import hmac
import os

from websockify.auth_plugins import AuthenticationError


class BrowserRuntimeAuth:
    def __init__(self, src=None):
        self.secret = os.environ["KEYSTONE_BROWSER_CONTROL"].encode()

    def authenticate(self, headers, target_host, target_port):
        supplied = headers.get("X-Keystone-Browser-Control", "").encode()
        if not hmac.compare_digest(supplied, self.secret):
            raise AuthenticationError(response_code=403)
