"""Synth-time validation of the parameter file. Every problem is collected and reported at once, and
``cdk synth`` fails before any construct is created.

The region tables below are the result of the checks recorded in docs/architecture.md ("Fact
check"); ``make preflight`` re-checks the model in the target region with the live API.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

PREFIX_RE = re.compile(r"^[a-z][a-z0-9-]{1,22}[a-z0-9]$")
STACK_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,63}$")
REGION_RE = re.compile(r"^[a-z]{2}(-gov)?-[a-z]+-\d$")
TAG_RE = re.compile(r"^[A-Za-z0-9 _.:/=+@-]{1,128}$")
ACCOUNT_RE = re.compile(r"(?<!\d)\d{12}(?!\d)")
KIOSK_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
VERSION_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
ORIGIN_RE = re.compile(r"^https://[a-z0-9.-]+(:\d{2,5})?$")
COUNTRY_RE = re.compile(r"^[A-Z]{2}$")
LOG_RETENTION_DAYS = (1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365)
REQUIRED_TAGS = {"Project", "Purpose", "Teardown"}

# Regions where Bedrock Guardrails image content filters are generally available (preview regions
# are not accepted: the prototype does not depend on preview features).
IMAGE_GUARDRAIL_REGIONS = frozenset({"us-east-1", "us-west-2", "eu-central-1", "ap-northeast-1"})

# Geographic and global cross-Region inference profile prefixes. A profile can route a request to
# another region of its geography (or worldwide), so it needs an explicit opt-in.
GEO_PREFIXES = ("us.", "eu.", "apac.", "jp.", "au.", "ca.", "us-gov.")
GLOBAL_PREFIX = "global."

# Image models this prototype has request code for, and the regions where the foundation model was
# listed with ON_DEMAND inference (in-region call, no inference profile).
MODELS: dict[str, dict[str, Any]] = {
    "stability.sd3-5-large-v1:0": {"family": "sd35-image-to-image", "on_demand_regions": {"us-west-2"}},
    "stability.stable-image-control-structure-v1:0": {"family": "control-structure", "on_demand_regions": set()},
}

# CloudWatch Logs managed data identifiers that need no country code.
DATA_IDENTIFIERS = frozenset({"Address", "AwsSecretKey", "CreditCardExpiration", "CreditCardNumber",
                              "CreditCardSecurityCode", "EmailAddress", "IpAddress", "LatLong", "Name",
                              "OpenSshPrivateKey", "PgpPrivateKey", "PkcsPrivateKey", "PuttyPrivateKey",
                              "VehicleIdentificationNumber"})
COUNTRY_IDENTIFIER_RE = re.compile(r"^[A-Za-z]{2,40}-[A-Z]{2}$")


class ParamsInvalid(ValueError):
    """The parameter file has one or more invalid values."""


def foundation_model_id(model_id: str) -> str:
    for p in (*GEO_PREFIXES, GLOBAL_PREFIX):
        if model_id.startswith(p):
            return model_id[len(p):]
    return model_id


def is_cross_region_profile(model_id: str) -> bool:
    return model_id.startswith((*GEO_PREFIXES, GLOBAL_PREFIX))


def _int(sec: dict, name: str, key: str, lo: int, hi: int, errors: list[str]) -> int | None:
    v = sec.get(key)
    if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
        errors.append(f"{name}.{key} must be an integer between {lo} and {hi} (got {v!r})")
        return None
    return v


def validate(params: dict[str, Any]) -> None:
    errors: list[str] = []
    sections = ("deploy", "frontend", "session", "image_model", "moderation", "logging")
    if not isinstance(params, dict) or not all(isinstance(params.get(s), dict) for s in sections):
        raise ParamsInvalid(f"parameter file needs the objects {', '.join(sections)}")
    d, f, s, m, mod, lg = (params[x] for x in sections)

    if ACCOUNT_RE.search(json.dumps(params)):
        errors.append("parameter file contains a 12-digit number; account ids come from the environment, "
                      "never from the file")

    # deploy
    if not isinstance(d.get("stack_name"), str) or not STACK_RE.match(d["stack_name"]):
        errors.append("deploy.stack_name must match [A-Za-z][A-Za-z0-9-]{0,63}")
    prefix = d.get("resource_prefix")
    if not isinstance(prefix, str) or not PREFIX_RE.match(prefix):
        errors.append("deploy.resource_prefix must be 3-24 chars of a-z, 0-9 and '-', starting with a letter")
    elif isinstance(d.get("stack_name"), str) and not d["stack_name"].startswith(prefix):
        errors.append("deploy.stack_name must start with deploy.resource_prefix (cleanup finds resources by it)")
    region = d.get("region")
    if not isinstance(region, str) or not REGION_RE.match(region):
        errors.append("deploy.region is required and must look like us-east-1 (the stack is pinned to it)")
        region = None
    if d.get("removal_policy") not in ("destroy", "retain"):
        errors.append("deploy.removal_policy must be 'destroy' or 'retain'")
    if d.get("log_retention_days") not in LOG_RETENTION_DAYS:
        errors.append(f"deploy.log_retention_days must be one of {list(LOG_RETENTION_DAYS)}")
    if not isinstance(d.get("image_bucket_versioning"), bool):
        errors.append("deploy.image_bucket_versioning must be true or false")
    _int(d, "deploy", "lambda_memory_mb", 512, 4096, errors)
    _int(d, "deploy", "processor_timeout_s", 30, 900, errors)
    rate = _int(d, "deploy", "api_throttle_rate", 1, 1000, errors)
    burst = _int(d, "deploy", "api_throttle_burst", 1, 2000, errors)
    if rate is not None and burst is not None and burst < rate:
        errors.append("deploy.api_throttle_burst must be >= deploy.api_throttle_rate")
    origins = d.get("cors_extra_origins", [])
    if not isinstance(origins, list) or not all(isinstance(o, str) and ORIGIN_RE.match(o) for o in origins):
        errors.append("deploy.cors_extra_origins must be a list of https:// origins (no wildcards)")
    tags = d.get("tags")
    if not isinstance(tags, dict) or not REQUIRED_TAGS <= set(tags) or not all(
            isinstance(k, str) and isinstance(v, str) and TAG_RE.match(k) and TAG_RE.match(v)
            for k, v in tags.items()):
        errors.append("deploy.tags must be string pairs and include Project, Purpose and Teardown")

    # frontend
    if not isinstance(f.get("enabled"), bool):
        errors.append("frontend.enabled must be true or false")
    geo = f.get("geo_allowlist", [])
    if not isinstance(geo, list) or not all(isinstance(c, str) and COUNTRY_RE.match(c) for c in geo):
        errors.append("frontend.geo_allowlist must be a list of ISO 3166-1 alpha-2 codes")
    if f.get("enabled") is False and not origins:
        errors.append("frontend.enabled=false needs deploy.cors_extra_origins (where the kiosk page is served)")

    # session
    if not isinstance(s.get("kiosk_id"), str) or not KIOSK_RE.match(s["kiosk_id"]):
        errors.append("session.kiosk_id must be 1-32 chars of a-z, 0-9 and '-' (it is written to audit events)")
    if not isinstance(s.get("notice_version"), str) or not VERSION_RE.match(s["notice_version"]):
        errors.append("session.notice_version must be 1-32 chars of A-Z, a-z, 0-9, '.', '_' and '-'")
    retention = _int(s, "session", "retention_seconds", 60, 86400, errors)
    link = _int(s, "session", "share_link_ttl_seconds", 30, 3600, errors)
    _int(s, "session", "ttl_grace_seconds", 60, 86400, errors)
    max_bytes = _int(s, "session", "max_upload_bytes", 10_000, 4_000_000, errors)
    max_side = _int(s, "session", "max_side", 256, 2048, errors)
    min_side = _int(s, "session", "min_side", 64, 512, errors)
    _int(s, "session", "max_pixels", 1_000_000, 40_000_000, errors)
    aspect = s.get("max_aspect")
    if not isinstance(aspect, int | float) or isinstance(aspect, bool) or not math.isfinite(aspect) \
            or not 1.0 <= aspect <= 2.5:
        errors.append("session.max_aspect must be a number between 1.0 and 2.5")
    if retention is not None and link is not None and link > retention:
        errors.append("session.share_link_ttl_seconds must not exceed session.retention_seconds")
    if max_side is not None and min_side is not None and min_side >= max_side:
        errors.append("session.min_side must be smaller than session.max_side")
    if max_bytes is not None and d.get("lambda_memory_mb", 0) and max_bytes * 8 > d["lambda_memory_mb"] * 1024 * 1024:
        errors.append("session.max_upload_bytes is too large for deploy.lambda_memory_mb")
    contact = s.get("operator_contact")
    if not isinstance(contact, str) or not 5 <= len(contact) <= 200:
        errors.append("session.operator_contact must be a 5-200 character string")

    # image model
    provider = m.get("provider")
    if provider not in ("transform", "bedrock"):
        errors.append("image_model.provider must be 'transform' or 'bedrock'")
    strength = m.get("strength")
    if not isinstance(strength, int | float) or isinstance(strength, bool) or not math.isfinite(strength) \
            or not 0.0 <= strength <= 1.0:
        errors.append("image_model.strength must be a number between 0 and 1")
    if not isinstance(m.get("allow_cross_region_inference"), bool):
        errors.append("image_model.allow_cross_region_inference must be true or false")
    if provider == "bedrock":
        model_id = m.get("model_id")
        if not isinstance(model_id, str) or not model_id:
            errors.append("image_model.model_id is required when provider=bedrock")
        else:
            base = foundation_model_id(model_id)
            known = MODELS.get(base)
            if model_id.startswith(GLOBAL_PREFIX):
                errors.append("image_model.model_id: global inference profiles route worldwide and are not "
                              "accepted (the stack is pinned to one region or geography)")
            elif known is None:
                errors.append(f"image_model.model_id {model_id!r} has no request code here; known: {sorted(MODELS)}")
            else:
                if m.get("family") != known["family"]:
                    errors.append(f"image_model.family must be {known['family']!r} for {base}")
                if is_cross_region_profile(model_id):
                    if m.get("allow_cross_region_inference") is not True:
                        errors.append(f"image_model.model_id {model_id!r} is a cross-Region inference profile; "
                                      "set image_model.allow_cross_region_inference=true to accept that requests "
                                      "may be processed in other regions of its geography (see "
                                      "docs/compliance/third-parties-and-cross-border.md)")
                elif region and region not in known["on_demand_regions"]:
                    errors.append(f"image_model.model_id {model_id!r} is not listed for in-region on-demand "
                                  f"inference in {region}; choose another region or an inference profile")

    # moderation
    mprov = mod.get("provider")
    if mprov not in ("guardrails", "mock", "none"):
        errors.append("moderation.provider must be 'guardrails', 'mock' or 'none'")
    if mod.get("filter_strength") not in ("LOW", "MEDIUM", "HIGH"):
        errors.append("moderation.filter_strength must be LOW, MEDIUM or HIGH")
    if mprov == "guardrails" and region and region not in IMAGE_GUARDRAIL_REGIONS:
        errors.append(f"moderation.provider=guardrails: image content filters are not generally available in "
                      f"{region} (accepted: {sorted(IMAGE_GUARDRAIL_REGIONS)})")
    if mprov == "none" and mod.get("allow_unmoderated") is not True:
        errors.append("moderation.provider=none needs moderation.allow_unmoderated=true")

    # logging
    ids = lg.get("data_identifiers")
    if not isinstance(ids, list) or not ids or not all(
            isinstance(i, str) and (i in DATA_IDENTIFIERS or COUNTRY_IDENTIFIER_RE.match(i)) for i in ids):
        errors.append("logging.data_identifiers must be a non-empty list of CloudWatch Logs managed data "
                      "identifiers (country-specific ones as Name-CC, for example PhoneNumber-US)")

    if errors:
        raise ParamsInvalid("invalid parameter file:\n  - " + "\n  - ".join(errors))
