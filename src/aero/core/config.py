"""Aero workspace configuration."""

import os
import re
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field


class CDSCredentials(BaseModel):
    url: str = "https://cds.climate.copernicus.eu/api"
    key: str = ""


class ADSCredentials(BaseModel):
    url: str = "https://ads.atmosphere.copernicus.eu/api"
    key: str = ""


class EarthdataCredentials(BaseModel):
    token: str = ""


class Credentials(BaseModel):
    cds: CDSCredentials = CDSCredentials()
    ads: ADSCredentials = ADSCredentials()
    earthdata: EarthdataCredentials = EarthdataCredentials()


class LLMConfig(BaseModel):
    provider: str = "deepseek"
    model: str = "deepseek-chat"
    reasoning_effort: str = ""
    base_url: str = ""
    providers: dict[str, "LLMProviderConfig"] = Field(default_factory=dict)

    def active_api_key(self) -> str:
        provider_config = self.providers.get(self.provider)
        return provider_config.api_key if provider_config else ""

    def set_active_api_key(self, api_key: str) -> None:
        provider_config = self.provider_config(self.provider)
        provider_config.api_key = api_key

    def provider_config(self, provider: str | None = None) -> "LLMProviderConfig":
        provider_id = provider or self.provider
        if provider_id not in self.providers:
            self.providers[provider_id] = LLMProviderConfig()
        return self.providers[provider_id]

    def apply_active_provider_defaults(self) -> None:
        provider_config = self.provider_config(self.provider)
        if self.model:
            provider_config.model = self.model
        if self.base_url:
            provider_config.base_url = self.base_url

    def use_provider_settings(self) -> None:
        provider_config = self.providers.get(self.provider)
        if provider_config is None:
            return
        if provider_config.model:
            self.model = provider_config.model
        if provider_config.base_url:
            self.base_url = provider_config.base_url

    @property
    def api_key(self) -> str:
        return self.active_api_key()

    @api_key.setter
    def api_key(self, value: str) -> None:
        self.set_active_api_key(value)

    def switch_provider(self, provider: str) -> None:
        self.provider = provider
        self.use_provider_settings()


class LLMProviderConfig(BaseModel):
    api_key: str = ""
    model: str = ""
    base_url: str = ""


class VisionConfig(BaseModel):
    provider: str = "bailian"
    model: str = "qwen3.7-plus"
    api_key: str = ""
    base_url: str = ""
    cache_ttl_hours: int = 3


class EmailConfig(BaseModel):
    enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_tls: bool = True
    smtp_user: str = ""
    smtp_password: str = ""
    from_name: str = "Aero"
    default_to: str = ""


class OutputConfig(BaseModel):
    data_dir: str = "data"


class AeroConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    llm: LLMConfig = LLMConfig()
    credentials: Credentials = Credentials()
    output: OutputConfig = OutputConfig()
    vision: VisionConfig = VisionConfig()
    email: EmailConfig = EmailConfig()
    language: str = "zh"
    mode: str = "execute"  # plan | qa | execute
    max_tool_rounds: int = 999

    @classmethod
    def load(cls, path: Path | str) -> "AeroConfig":
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")

        raw = yaml.safe_load(path.read_text()) or {}

        raw = cls._resolve_env(raw)
        _remove_secrets_from_config_data(raw)

        config = cls.model_validate(raw)
        config.llm.use_provider_settings()
        return apply_user_secrets(config)

    @classmethod
    def create_default(cls) -> "AeroConfig":
        return apply_user_secrets(cls())

    def save(self, path: Path | str) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.llm.apply_active_provider_defaults()
        data = self.model_dump()
        _remove_secrets_from_config_data(data)
        path.write_text(yaml.dump(data, default_flow_style=False, allow_unicode=True))

    @staticmethod
    def _resolve_env(data: dict) -> dict:
        """Recursively resolve ${ENV_VAR} patterns in string values."""

        def resolve_value(value):
            if isinstance(value, str):
                return re.sub(
                    r"\$\{(\w+)\}",
                    lambda m: os.environ.get(m.group(1), m.group(0)),
                    value,
                )
            elif isinstance(value, dict):
                return {k: resolve_value(v) for k, v in value.items()}
            elif isinstance(value, list):
                return [resolve_value(v) for v in value]
            return value

        return resolve_value(data)


def user_secrets_path() -> Path:
    override = os.environ.get("AERO_SECRETS_PATH")
    if override:
        return Path(override)
    return Path.home() / ".aero" / "secrets.yaml"


def load_user_secrets() -> dict:
    path = user_secrets_path()
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except Exception:
        return {}
    data = AeroConfig._resolve_env(data)
    return data if isinstance(data, dict) else {}


def save_user_secrets(data: dict) -> None:
    path = user_secrets_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(data, default_flow_style=False, allow_unicode=True))


def apply_user_secrets(config: AeroConfig) -> AeroConfig:
    secrets = load_user_secrets()
    llm = secrets.get("llm")
    if isinstance(llm, dict):
        providers = llm.get("providers")
        if isinstance(providers, dict):
            for provider, values in providers.items():
                if not isinstance(values, dict):
                    continue
                api_key = str(values.get("api_key") or "")
                if api_key:
                    config.llm.provider_config(str(provider)).api_key = api_key
                model = str(values.get("model") or "")
                if model:
                    config.llm.provider_config(str(provider)).model = model
                base_url = str(values.get("base_url") or "")
                if base_url:
                    config.llm.provider_config(str(provider)).base_url = base_url
        active_provider = str(llm.get("active_provider") or "")
        if active_provider and active_provider in config.llm.providers:
            config.llm.switch_provider(active_provider)
        elif not config.llm.active_api_key():
            for provider, values in config.llm.providers.items():
                if values.api_key:
                    config.llm.switch_provider(provider)
                    break

    credentials = secrets.get("credentials")
    if isinstance(credentials, dict):
        cds = credentials.get("cds")
        if isinstance(cds, dict):
            if cds.get("url"):
                config.credentials.cds.url = str(cds["url"])
            if cds.get("key"):
                config.credentials.cds.key = str(cds["key"])
        ads = credentials.get("ads")
        if isinstance(ads, dict):
            if ads.get("url"):
                config.credentials.ads.url = str(ads["url"])
            if ads.get("key"):
                config.credentials.ads.key = str(ads["key"])
        earthdata = credentials.get("earthdata")
        if isinstance(earthdata, dict) and earthdata.get("token"):
            config.credentials.earthdata.token = str(earthdata["token"])

    vision = secrets.get("vision")
    if isinstance(vision, dict):
        if vision.get("api_key"):
            config.vision.api_key = str(vision["api_key"])
        if vision.get("base_url"):
            config.vision.base_url = str(vision["base_url"])

    email = secrets.get("email")
    if isinstance(email, dict):
        if email.get("smtp_password"):
            config.email.smtp_password = str(email["smtp_password"])

    return config


def save_llm_api_key(provider: str, api_key: str) -> None:
    secrets = load_user_secrets()
    llm = secrets.setdefault("llm", {})
    providers = llm.setdefault("providers", {})
    provider_data = providers.setdefault(provider, {})
    provider_data["api_key"] = api_key
    save_user_secrets(secrets)


def save_llm_profile(provider: str, api_key: str, model: str, base_url: str) -> None:
    secrets = load_user_secrets()
    llm = secrets.setdefault("llm", {})
    llm["active_provider"] = provider
    providers = llm.setdefault("providers", {})
    provider_data = providers.setdefault(provider, {})
    provider_data["api_key"] = api_key
    provider_data["model"] = model
    provider_data["base_url"] = base_url
    save_user_secrets(secrets)


def clear_llm_api_key(provider: str) -> None:
    save_llm_api_key(provider, "")


def save_cds_credentials(url: str, key: str) -> None:
    secrets = load_user_secrets()
    credentials = secrets.setdefault("credentials", {})
    credentials["cds"] = {"url": url, "key": key}
    save_user_secrets(secrets)


def clear_cds_credentials() -> None:
    secrets = load_user_secrets()
    credentials = secrets.setdefault("credentials", {})
    credentials["cds"] = {"url": "", "key": ""}
    save_user_secrets(secrets)


def save_ads_credentials(url: str, key: str) -> None:
    secrets = load_user_secrets()
    credentials = secrets.setdefault("credentials", {})
    credentials["ads"] = {"url": url, "key": key}
    save_user_secrets(secrets)


def clear_ads_credentials() -> None:
    secrets = load_user_secrets()
    credentials = secrets.setdefault("credentials", {})
    credentials["ads"] = {"url": "", "key": ""}
    save_user_secrets(secrets)


def save_earthdata_token(token: str) -> None:
    secrets = load_user_secrets()
    credentials = secrets.setdefault("credentials", {})
    credentials["earthdata"] = {"token": token}
    save_user_secrets(secrets)


def clear_earthdata_token() -> None:
    save_earthdata_token("")


def save_vision_api_key(api_key: str, base_url: str = "") -> None:
    secrets = load_user_secrets()
    vision = secrets.setdefault("vision", {})
    vision["api_key"] = api_key
    if base_url:
        vision["base_url"] = base_url
    save_user_secrets(secrets)


def save_email_smtp_password(smtp_password: str) -> None:
    secrets = load_user_secrets()
    email = secrets.setdefault("email", {})
    email["smtp_password"] = smtp_password
    save_user_secrets(secrets)


def clear_email_config() -> None:
    save_email_smtp_password("")


def _remove_secrets_from_config_data(data: dict) -> None:
    credentials = data.get("credentials")
    if isinstance(credentials, dict):
        cds = credentials.get("cds")
        if isinstance(cds, dict):
            cds["key"] = ""
        ads = credentials.get("ads")
        if isinstance(ads, dict):
            ads["key"] = ""
        earthdata = credentials.get("earthdata")
        if isinstance(earthdata, dict):
            earthdata["token"] = ""

    llm = data.get("llm")
    if isinstance(llm, dict):
        providers = llm.get("providers")
        if isinstance(providers, dict):
            for provider_data in providers.values():
                if isinstance(provider_data, dict):
                    provider_data["api_key"] = ""

    vision = data.get("vision")
    if isinstance(vision, dict):
        vision["api_key"] = ""

    email = data.get("email")
    if isinstance(email, dict):
        email["smtp_password"] = ""
