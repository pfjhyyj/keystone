from pydantic import Field, SecretStr

from keystone.core.schemas import Input

from .types import Username
from .users import PasswordReset


class Login(Input):
    username: Username
    password: SecretStr = Field(min_length=1, max_length=128)


class PasswordChange(PasswordReset):
    current_password: SecretStr = Field(min_length=1, max_length=128)
