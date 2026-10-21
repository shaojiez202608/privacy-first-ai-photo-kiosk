import copy
import json
from pathlib import Path

import pytest
from kiosk_infra.params import ParamsInvalid, foundation_model_id, validate

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = sorted((ROOT / "config").glob("*.example.json"))


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_examples_are_valid(path):
    validate(json.loads(path.read_text()))


def test_three_examples_exist():
    assert {p.name for p in EXAMPLES} == {"params.example.json", "params.handover.example.json",
                                          "params.cris-bedrock.example.json"}


def test_sandbox_default_has_no_cross_region_inference(params):
    assert params["image_model"]["allow_cross_region_inference"] is False
    assert params["deploy"]["removal_policy"] == "destroy"
    handover = json.loads((ROOT / "config" / "params.handover.example.json").read_text())
    assert handover["deploy"]["removal_policy"] == "retain"
    assert not handover["image_model"]["model_id"].startswith(("us.", "eu.", "global."))


def _bad(params, mutate, needle):
    p = copy.deepcopy(params)
    mutate(p)
    with pytest.raises(ParamsInvalid) as e:
        validate(p)
    assert needle in str(e.value)


def _set(path, value):
    def f(p):
        cur = p
        for k in path[:-1]:
            cur = cur[k]
        cur[path[-1]] = value
    return f


@pytest.mark.parametrize("path,value,needle", [
    (("deploy", "region"), "mars-1", "deploy.region"),
    (("deploy", "removal_policy"), "keep", "removal_policy"),
    (("deploy", "log_retention_days"), 0, "log_retention_days"),
    (("deploy", "stack_name"), "other-stack", "must start with deploy.resource_prefix"),
    (("deploy", "cors_extra_origins"), ["*"], "cors_extra_origins"),
    (("deploy", "cors_extra_origins"), ["http://example.com"], "cors_extra_origins"),
    (("deploy", "tags"), {"Project": "x"}, "Purpose and Teardown"),
    (("deploy", "api_throttle_burst"), 1, "api_throttle_burst"),
    (("session", "retention_seconds"), 30, "retention_seconds"),
    (("session", "share_link_ttl_seconds"), 600, "must not exceed"),
    (("session", "max_aspect"), float("nan"), "max_aspect"),
    (("session", "kiosk_id"), "Kiosk With Spaces", "kiosk_id"),
    (("session", "operator_contact"), "x", "operator_contact"),
    (("image_model", "strength"), float("inf"), "strength"),
    (("moderation", "provider"), "none", "allow_unmoderated"),
    (("moderation", "filter_strength"), "MAX", "filter_strength"),
    (("logging", "data_identifiers"), ["Faces"], "data_identifiers"),
    (("frontend", "geo_allowlist"), ["usa"], "geo_allowlist"),
])
def test_rules(params, path, value, needle):
    _bad(params, _set(path, value), needle)


def test_account_id_in_file_rejected(params):
    _bad(params, _set(("session", "operator_contact"), "call 111122223333"), "12-digit")


def test_cross_region_profile_needs_explicit_opt_in(params):
    def f(p):
        p["image_model"].update(provider="bedrock", model_id="us.stability.stable-image-control-structure-v1:0",
                                family="control-structure")
    _bad(params, f, "allow_cross_region_inference=true")


def test_global_profile_rejected_even_with_opt_in(params):
    def f(p):
        p["image_model"].update(provider="bedrock", model_id="global.stability.stable-image-control-structure-v1:0",
                                family="control-structure", allow_cross_region_inference=True)
    _bad(params, f, "global inference profiles")


def test_in_region_model_must_be_listed_for_region(params):
    def f(p):
        p["image_model"].update(provider="bedrock", model_id="stability.sd3-5-large-v1:0",
                                family="sd35-image-to-image")
    _bad(params, f, "not listed for in-region on-demand inference in us-east-1")


def test_family_must_match(params):
    def f(p):
        p["deploy"]["region"] = "us-west-2"
        p["image_model"].update(provider="bedrock", model_id="stability.sd3-5-large-v1:0", family="control-structure")
    _bad(params, f, "image_model.family")


def test_guardrails_only_where_image_filters_are_ga(params):
    _bad(params, _set(("deploy", "region"), "eu-west-1"), "image content filters are not generally available")


def test_frontend_disabled_needs_origin(params):
    _bad(params, _set(("frontend", "enabled"), False), "cors_extra_origins")


def test_all_errors_reported_at_once(params):
    p = copy.deepcopy(params)
    p["deploy"]["removal_policy"] = "x"
    p["session"]["retention_seconds"] = 1
    with pytest.raises(ParamsInvalid) as e:
        validate(p)
    assert str(e.value).count("\n  - ") == 2


def test_foundation_model_id():
    assert foundation_model_id("us.stability.x-v1:0") == "stability.x-v1:0"
    assert foundation_model_id("stability.x-v1:0") == "stability.x-v1:0"
