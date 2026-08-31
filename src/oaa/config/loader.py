"""Load, merge and validate configuration.

Merge order (later wins):
    config/default.yaml
    config/<profile>.yaml        (profile from OAA_PROFILE or default.yaml)
    config/local.yaml            (gitignored, machine-specific)
    OAA_* environment variables  (double underscore = nesting)

Credentials never appear in YAML. They are resolved from the environment at
call time and selected by profile, so the judged account cannot be hit by a
dev run that forgot to switch.
"""

from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from oaa.config.schema import Config

_ENV_PREFIX = "OAA_"


def project_root() -> Path:
    """Repo root: the directory containing `config/` and `src/`."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "config").is_dir() and (parent / "src").is_dir():
            return parent
    return Path.cwd()


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping at the top level")
    return data


def _coerce(raw: str) -> Any:
    """Turn an env-var string into the obvious Python value."""
    lowered = raw.strip().lower()
    if lowered in {"true", "yes", "on"}:
        return True
    if lowered in {"false", "no", "off"}:
        return False
    if lowered in {"null", "none", ""}:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw


def _env_overlay(known_keys: set[str]) -> dict[str, Any]:
    """OAA_RISK__MAX_POSITIONS=4  ->  {"risk": {"max_positions": 4}}"""
    overlay: dict[str, Any] = {}
    for env_key, raw in os.environ.items():
        if not env_key.startswith(_ENV_PREFIX):
            continue
        path = env_key[len(_ENV_PREFIX) :].lower().split("__")
        if path[0] not in known_keys:
            continue
        cursor = overlay
        for part in path[:-1]:
            cursor = cursor.setdefault(part, {})
        cursor[path[-1]] = _coerce(raw)
    return overlay


def _apply_strategy_overrides(cfg: dict[str, Any]) -> dict[str, Any]:
    """Merge `strategy_overrides: {<name>: {...}}` into the strategies list.

    A variant patches strategies BY NAME rather than restating the list,
    because `_deep_merge` replaces a list wholesale: the first draft of
    config/variants/v2.yaml restated three strategies and silently dropped
    `earnings_event_directional`, which the baseline had. Naming a strategy
    that is not in the list is an error rather than a no-op, for the same
    reason - a typo should not look like a strategy that just does nothing.
    """
    overrides = cfg.pop("strategy_overrides", None) or {}
    if not overrides:
        return cfg
    refs = cfg.get("strategies") or []
    by_name = {ref.get("name"): ref for ref in refs}
    unknown = sorted(set(overrides) - set(by_name))
    if unknown:
        raise ValueError(
            f"strategy_overrides names no such strategy: {', '.join(unknown)}. "
            f"Configured: {', '.join(sorted(n for n in by_name if n))}"
        )
    for name, patch in overrides.items():
        by_name[name].update(_deep_merge(by_name[name], patch or {}))
    return cfg


def _load_strategy_params(cfg: dict[str, Any], root: Path) -> dict[str, Any]:
    """Inline each strategy's params_file so downstream code sees one object."""
    for ref in cfg.get("strategies") or []:
        params_file = ref.get("params_file")
        if not params_file:
            continue
        path = root / params_file
        if not path.exists():
            raise FileNotFoundError(
                f"strategy '{ref.get('name')}' references missing params file: {path}"
            )
        file_params = _read_yaml(path)
        ref["params"] = _deep_merge(file_params, ref.get("params") or {})
        # A variant's small diff, applied last so it beats both.
        overlay = ref.get("params_overlay") or {}
        if overlay:
            ref["params"] = _deep_merge(ref["params"], overlay)

    return cfg


def load_config(
    config_path: str | Path | None = None,
    profile: str | None = None,
    overrides: dict[str, Any] | None = None,
    variant: str | None = None,
) -> Config:
    """Build the validated Config object."""
    root = project_root()
    load_dotenv(root / ".env", override=False)

    base_path = Path(config_path) if config_path else root / os.getenv(
        "OAA_CONFIG", "config/default.yaml"
    )
    if not base_path.is_absolute():
        base_path = root / base_path
    if not base_path.exists():
        raise FileNotFoundError(
            f"config not found: {base_path}\n"
            "Run `make setup` (or copy config/default.yaml into place)."
        )

    merged = _read_yaml(base_path)

    active_profile = profile or os.getenv("OAA_PROFILE") or merged.get("profile") or "dev"
    profile_path = base_path.parent / f"{active_profile}.yaml"
    merged = _deep_merge(merged, _read_yaml(profile_path))
    merged["profile"] = active_profile

    # A VARIANT is a named strategy alternative - a different set of gates and
    # exits over the same code - applied after the profile so it is orthogonal
    # to which account is being traded, and before local.yaml so a machine
    # override still wins. `None` is the baseline strategy and touches nothing.
    active_variant = variant or os.getenv("OAA_VARIANT") or merged.get("variant")
    if active_variant:
        variant_path = base_path.parent / "variants" / f"{active_variant}.yaml"
        if not variant_path.exists():
            available = sorted(
                q.stem for q in (base_path.parent / "variants").glob("*.yaml")
            )
            raise FileNotFoundError(
                f"unknown variant '{active_variant}': {variant_path} does not exist. "
                f"Available: {', '.join(available) or 'none'}"
            )
        merged = _deep_merge(merged, _read_yaml(variant_path))
        merged = _apply_strategy_overrides(merged)
    merged["variant"] = active_variant or None

    merged = _deep_merge(merged, _read_yaml(base_path.parent / "local.yaml"))
    merged = _deep_merge(merged, _env_overlay(set(Config.model_fields)))
    if overrides:
        merged = _deep_merge(merged, overrides)

    # An explicit `--profile` beats OAA_PROFILE in .env, always. Without this
    # the env overlay silently wins and `oaa <cmd> --profile judged` quietly
    # keeps running on the dev account - the exact failure the profile split
    # exists to prevent, and one that leaves no trace in the output.
    if profile:
        merged["profile"] = profile
    # Same reasoning for the variant: an explicit --variant beats OAA_VARIANT.
    if variant:
        merged["variant"] = variant

    merged = _load_strategy_params(merged, root)
    return Config.model_validate(merged)


# --------------------------------------------------------------------------- #
# credentials
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Credentials:
    api_key: str
    secret_key: str
    paper: bool
    profile: str
    #: The JUDGED account, always - this is the submission field, and it stays
    #: the judged one whatever profile is active so the dashboard and
    #: `oaa doctor` never report a dev account as the thing being submitted.
    account_id: str | None = None
    #: The account the ACTIVE profile is expected to reach: the dev account on
    #: dev, the judged account on judged. Kept separate from `account_id`
    #: because they answer different questions - "what am I submitting?" versus
    #: "am I pointed at the right account right now?" - and conflating them
    #: made every dev-profile identity check compare against the judged ID and
    #: fail for the one reason that is not a problem.
    expected_account_id: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.secret_key)

    def masked(self) -> str:
        if not self.api_key:
            return "<unset>"
        return f"{self.api_key[:4]}...{self.api_key[-4:]}"


def resolve_credentials(cfg: Config) -> Credentials:
    """Pick the key pair matching the active profile.

    dev    -> ALPACA_DEV_API_KEY / ALPACA_DEV_SECRET_KEY (falls back to the
              primary pair if the dev pair is unset)
    judged -> ALPACA_API_KEY / ALPACA_SECRET_KEY

    Account IDs follow the same split: `ALPACA_DEV_ACCOUNT_ID` is what dev
    should reach, `ALPACA_JUDGED_ACCOUNT_ID` is what judged should reach AND
    what gets submitted.
    """
    load_dotenv(project_root() / ".env", override=False)

    primary = (os.getenv("ALPACA_API_KEY", ""), os.getenv("ALPACA_SECRET_KEY", ""))
    dev = (os.getenv("ALPACA_DEV_API_KEY", ""), os.getenv("ALPACA_DEV_SECRET_KEY", ""))

    if cfg.profile == "dev" and all(dev):
        key, secret = dev
    else:
        key, secret = primary

    judged_id = os.getenv("ALPACA_JUDGED_ACCOUNT_ID") or None
    dev_id = os.getenv("ALPACA_DEV_ACCOUNT_ID") or None
    return Credentials(
        api_key=key,
        secret_key=secret,
        paper=cfg.broker.paper,
        profile=cfg.profile,
        account_id=judged_id,
        # Unset on dev is fine and stays fine: the check that uses this skips
        # rather than fails when there is nothing to compare against. Setting
        # it turns "am I on the dev account?" into something verifiable, which
        # is worth having precisely because the dev account is where careless
        # experiments are supposed to land.
        expected_account_id=dev_id if cfg.profile == "dev" else judged_id,
    )


@dataclass
class Settings:
    """Config plus resolved credentials plus resolved paths. One object to pass."""

    config: Config
    credentials: Credentials
    root: Path

    def path(self, relative: str | Path) -> Path:
        p = Path(relative)
        return p if p.is_absolute() else self.root / p

    def ensure_run_dirs(self) -> None:
        t = self.config.telemetry
        for target in (t.run_dir, Path(t.journal).parent, Path(t.equity_curve).parent,
                       Path(t.db).parent, self.config.data.cache.dir):
            self.path(target).mkdir(parents=True, exist_ok=True)


def load_settings(
    config_path: str | Path | None = None,
    profile: str | None = None,
    overrides: dict[str, Any] | None = None,
    variant: str | None = None,
) -> Settings:
    cfg = load_config(config_path, profile, overrides, variant)
    return Settings(config=cfg, credentials=resolve_credentials(cfg), root=project_root())
