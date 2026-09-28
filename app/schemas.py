"""请求模型（请求体校验交给 pydantic；响应直接给 dict，形状见 README）。"""

from __future__ import annotations

from pydantic import BaseModel, Field

PRIORITIES = ("P0", "P1", "P2", "P3")
MODES = ("debug", "ci")
AUTHS = ("unknown", "none", "login_form")
RUN_STATUS = ("queued", "running", "passed", "failed", "aborted", "unverified", "error")


class LoginIn(BaseModel):
    account: str
    password: str


class NodeIn(BaseModel):
    parent_id: int | None = None
    name: str = Field(min_length=1, max_length=128)
    sort: int = 0
    description: str = ""


class NodePatch(BaseModel):
    parent_id: int | None = None
    name: str | None = Field(default=None, min_length=1, max_length=128)
    sort: int | None = None
    description: str | None = None


class ScenarioIn(BaseModel):
    node_id: int
    name: str = Field(min_length=1, max_length=128)
    priority: str = "P2"
    owner: str = ""
    tags: list[str] = Field(default_factory=list)
    description: str = ""
    env_id: int | None = None


class ScenarioPatch(BaseModel):
    node_id: int | None = None
    name: str | None = Field(default=None, min_length=1, max_length=128)
    priority: str | None = None
    owner: str | None = None
    tags: list[str] | None = None
    description: str | None = None
    env_id: int | None = None


class CaseIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    priority: str | None = None
    owner: str = ""
    template: str = "blank"  # blank | copy
    copy_from: str | None = None


class CaseSave(BaseModel):
    yaml_text: str
    version: int
    comment: str = ""


class CaseValidate(BaseModel):
    yaml_text: str | None = None
    env_id: int | None = None


class CaseDraft(BaseModel):
    desc: str = Field(min_length=1)
    url: str = ""


class CaseImport(BaseModel):
    script_text: str = Field(min_length=1, max_length=200_000)
    keep_url: bool = False


class CaseRollback(BaseModel):
    version: int


class CaseCopy(BaseModel):
    name: str | None = None
    scenario_id: str | None = None


class EnvIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    label: str = ""
    base_url: str = ""
    auth: str = "unknown"
    secret_keys: list[str] = Field(default_factory=list)
    timeout_ms: int = 15000
    extra: dict = Field(default_factory=dict)
    note: str = ""


class EnvPatch(BaseModel):
    label: str | None = None
    base_url: str | None = None
    auth: str | None = None
    secret_keys: list[str] | None = None
    timeout_ms: int | None = None
    extra: dict | None = None
    note: str | None = None


class SecretsIn(BaseModel):
    base_url: str | None = None
    secrets: dict[str, str] = Field(default_factory=dict)


class ProbeIn(BaseModel):
    url: str = ""


class RunIn(BaseModel):
    case_id: str
    env_id: int
    mode: str = "debug"


class ProfileIn(BaseModel):
    name: str


class PasswordChangeIn(BaseModel):
    old_password: str
    new_password: str


class UserIn(BaseModel):
    account: str
    name: str = ""
    password: str


class UserPatch(BaseModel):
    name: str | None = None
    active: bool | None = None
    is_admin: bool | None = None


class PasswordResetIn(BaseModel):
    password: str
