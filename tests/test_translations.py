"""Translations, icons, services.yaml and manifest stay consistent with the code."""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest
import yaml
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.homerunner import const
from custom_components.homerunner.const import DOMAIN, SUBENTRY_SCHEDULE

from .conftest import ENTRY_DATA

PKG = Path(__file__).parent.parent / "custom_components" / "homerunner"
PLATFORMS = ("sensor", "binary_sensor", "switch", "number", "button")


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


STRINGS = _load(PKG / "strings.json")
EN = _load(PKG / "translations" / "en.json")
DE = _load(PKG / "translations" / "de.json")
ICONS = _load(PKG / "icons.json")
SERVICES_YAML = yaml.safe_load((PKG / "services.yaml").read_text(encoding="utf-8"))


def _flatten(node, prefix=()):
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _flatten(v, (*prefix, k))
    else:
        yield prefix, node


def _paths(node) -> set[tuple[str, ...]]:
    return {p for p, _ in _flatten(node)}


def _keys(node, *path) -> set[str]:
    for p in path:
        node = node.get(p, {})
    return set(node)


# ------------------------------------------------------------ source scanning


def _module_constants(tree: ast.AST) -> dict[str, str]:
    return {
        t.id: n.value.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Assign)
        and isinstance(n.value, ast.Constant)
        and isinstance(n.value.value, str)
        for t in n.targets
        if isinstance(t, ast.Name)
    }


def _call_name(call: ast.Call) -> str:
    f = call.func
    return f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""


def _translation_keys() -> dict[str, dict[str, set[str]]]:
    """Static translation_key=... usages: {category: {file stem: keys}}."""
    found: dict[str, dict[str, set[str]]] = {
        "exception": {},
        "issue": {},
        "selector": {},
        "entity": {},
    }
    for path in PKG.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        consts = _module_constants(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg != "translation_key":
                    continue
                if isinstance(kw.value, ast.Constant):
                    key = kw.value.value
                elif isinstance(kw.value, ast.Name) and kw.value.id in consts:
                    key = consts[kw.value.id]
                else:
                    pytest.fail(f"{path.name}: translation_key is not a literal")
                name = _call_name(node)
                if name.endswith("Error"):
                    cat = "exception"
                elif name == "async_create_issue":
                    cat = "issue"
                elif name.endswith("SelectorConfig"):
                    cat = "selector"
                elif path.stem in PLATFORMS or name in {"server_device", "schedule_device"}:
                    cat = "entity"
                else:
                    cat = "device"
                found.setdefault(cat, {}).setdefault(path.stem, set()).add(key)
    return found


def _flow_keys() -> dict[str, set[str]]:
    """Steps, errors and aborts used by the three flow classes in config_flow.py."""
    tree = ast.parse((PKG / "config_flow.py").read_text(encoding="utf-8"))
    out: dict[str, set[str]] = {}
    for cls in (n for n in tree.body if isinstance(n, ast.ClassDef)):
        steps, errors, aborts = set(), set(), set()
        for n in ast.walk(cls):
            if isinstance(n, ast.AsyncFunctionDef) and n.name.startswith("async_step_"):
                steps.add(n.name.removeprefix("async_step_"))
            # variable step ids are covered by the async_step_* methods
            if (
                isinstance(n, ast.keyword)
                and n.arg == "step_id"
                and isinstance(n.value, ast.Constant)
            ):
                steps.add(n.value.value)
            if isinstance(n, ast.Call) and _call_name(n) in {
                "_show",
                "_show_server",
                "_show_reconfigure",
            }:
                for arg in n.args:
                    if isinstance(arg, ast.Dict):
                        errors |= {v.value for v in arg.values if isinstance(v, ast.Constant)}
            if (
                isinstance(n, ast.Assign)
                and any(
                    isinstance(t, ast.Subscript) and getattr(t.value, "id", "") == "errors"
                    for t in n.targets
                )
                and isinstance(n.value, ast.Constant)
            ):
                errors.add(n.value.value)
            if (
                isinstance(n, ast.Return)
                and isinstance(n.value, ast.Constant)
                and isinstance(n.value.value, str)
            ):
                errors.add(n.value.value)
            if isinstance(n, ast.Return) and isinstance(n.value, ast.Dict):
                errors |= {v.value for v in n.value.values if isinstance(v, ast.Constant)}
            if isinstance(n, ast.Call) and _call_name(n) == "async_abort":
                aborts |= {k.value.value for k in n.keywords if k.arg == "reason"}
            # HA-provided abort reasons raised by helpers
            if isinstance(n, ast.Call):
                name = _call_name(n)
                if name == "_abort_if_unique_id_configured":
                    aborts.add("already_configured")
        out[cls.name] = {"steps": steps, "errors": errors, "aborts": aborts}
    # module-level helper(s) returning error keys are used by the config flow
    for fn in (n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)):
        for n in ast.walk(fn):
            if isinstance(n, ast.Return) and isinstance(n.value, ast.Constant) and n.value.value:
                out["HomerunnerConfigFlow"]["errors"].add(n.value.value)
    return out


# ------------------------------------------------------------ file-level checks


def test_en_json_is_generated_from_strings_json() -> None:
    assert EN == STRINGS


def test_german_has_the_same_keys_as_english() -> None:
    en, de = _paths(STRINGS), _paths(DE)
    assert en - de == set(), f"missing in de.json: {sorted(en - de)}"
    assert de - en == set(), f"unknown in de.json: {sorted(de - en)}"


def test_no_empty_strings_and_placeholders_match() -> None:
    de = dict(_flatten(DE))
    for path, text in _flatten(STRINGS):
        assert isinstance(text, str) and text.strip(), path
        assert de[path].strip(), path
        assert set(re.findall(r"\{(\w+)\}", text)) == set(re.findall(r"\{(\w+)\}", de[path])), path


# ------------------------------------------------------------ code vs strings


def test_exception_issue_and_selector_keys_exist() -> None:
    found = _translation_keys()
    used_exc = set().union(*found["exception"].values())
    assert used_exc, "scanner found no exception keys"
    assert used_exc <= _keys(STRINGS, "exceptions")
    assert _keys(STRINGS, "exceptions") <= used_exc | {"force_off_failed"}, "unused exception text"
    used_issue = set().union(*found["issue"].values())
    assert used_issue == _keys(STRINGS, "issues")
    used_sel = set().union(*found["selector"].values())
    assert used_sel <= _keys(STRINGS, "selector")
    for stem, keys in found["entity"].items():
        assert keys <= _keys(STRINGS, "entity", stem), stem
    for key in found.get("device", {}).get("entity", set()):
        assert key in _keys(STRINGS, "device")


async def test_all_registered_entities_have_names_and_icons(hass, mocks) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Octo-Org",
        unique_id="octo-org",
        data=dict(ENTRY_DATA),
        options={"automatic": False, "dry_run": True},
        subentries_data=[
            ConfigSubentryData(
                data={
                    "name": "Nightly",
                    "repository": "octo-org/app",
                    "workflow": "nightly.yml",
                    "ref": "main",
                    "inputs": "{}",
                    "time": "03:00:00",
                    "weekdays": [0],
                    "timezone": "Europe/Berlin",
                    "prewake": True,
                    "prewake_minutes": 15,
                    "retries": 1,
                    "enabled": True,
                },
                subentry_type=SUBENTRY_SCHEDULE,
                title="Nightly",
                unique_id=None,
                subentry_id="sub1",
            )
        ],
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    entities = er.async_entries_for_config_entry(registry, entry.entry_id)
    assert len(entities) > 20
    used: dict[str, set[str]] = {}
    for ent in entities:
        assert ent.translation_key, ent.entity_id
        used.setdefault(ent.domain, set()).add(ent.translation_key)
    for domain, keys in used.items():
        assert keys == _keys(STRINGS, "entity", domain), domain
        for key in keys:
            assert "name" in STRINGS["entity"][domain][key], (domain, key)
        # only entities without a device-class icon strictly need one, but we give all
        assert keys == _keys(ICONS, "entity", domain), f"icons.json {domain}"
    assert set(STRINGS["entity"]) == set(used)
    assert set(ICONS["entity"]) == set(used)


def test_icons_are_mdi() -> None:
    for path, value in _flatten(ICONS):
        assert re.fullmatch(r"mdi:[a-z0-9-]+", value), (path, value)


def test_config_flow_keys_exist() -> None:
    flows = _flow_keys()
    config = flows["HomerunnerConfigFlow"]
    options = flows["HomerunnerOptionsFlow"]
    sub = flows["ScheduleSubentryFlow"]

    # "reauth" only forwards to reauth_confirm and shows no form of its own
    assert config["steps"] - {"reauth"} == _keys(STRINGS, "config", "step")
    assert options["steps"] == _keys(STRINGS, "options", "step")
    sub_strings = STRINGS["config_subentries"][SUBENTRY_SCHEDULE]
    assert sub["steps"] == set(sub_strings["step"])

    # flow-level errors and aborts (reauth/reconfigure/unique id results are set by HA helpers)
    assert config["errors"] <= _keys(STRINGS, "config", "error")
    assert _keys(STRINGS, "config", "error") <= config["errors"] | {"unknown"}
    assert sub["errors"] == set(sub_strings["error"])
    expected_aborts = {
        "already_configured",
        "reauth_successful",
        "reconfigure_successful",
    }
    assert expected_aborts == _keys(STRINGS, "config", "abort")
    assert set(sub_strings["abort"]) == {"reconfigure_successful"}
    assert set(sub_strings["initiate_flow"]) == {"user", "reconfigure"}
    assert not {e for e in options["errors"] if re.fullmatch(r"[a-z_]{3,}", e)}


def test_every_form_step_has_title_and_data_labels() -> None:
    schema_steps = {
        ("config", "user"): 5,
        ("config", "server"): 10,
        ("config", "reconfigure"): 10,
        ("options", "init"): 12,
    }
    for (section, step), count in schema_steps.items():
        node = STRINGS[section]["step"][step]
        assert node["title"]
        assert len(node["data"]) == count, (section, step)
    sub = STRINGS["config_subentries"][SUBENTRY_SCHEDULE]["step"]
    assert len(sub["user"]["data"]) == len(sub["reconfigure"]["data"]) == 13


# ------------------------------------------------------------ services


def _registered_services() -> set[str]:
    return {v for k, v in vars(const).items() if k.startswith("SERVICE_") and isinstance(v, str)}


def test_services_yaml_matches_registered_services_and_strings() -> None:
    registered = _registered_services()
    assert registered == set(SERVICES_YAML)
    assert registered == _keys(STRINGS, "services")
    assert registered == _keys(ICONS, "services")
    for name, spec in SERVICES_YAML.items():
        yaml_fields = set((spec or {}).get("fields", {}))
        string_fields = _keys(STRINGS, "services", name, "fields")
        assert yaml_fields == string_fields, name
        assert STRINGS["services"][name]["name"] and STRINGS["services"][name]["description"]
        for field in string_fields:
            assert STRINGS["services"][name]["fields"][field]["name"]


def test_service_names_registered_in_code() -> None:
    src = (PKG / "__init__.py").read_text(encoding="utf-8")
    for const_name in (k for k in vars(const) if k.startswith("SERVICE_")):
        assert f"DOMAIN, {const_name}" in src, const_name


# ------------------------------------------------------------ manifest


def test_manifest_key_order_for_hassfest() -> None:
    manifest = _load(PKG / "manifest.json")
    keys = list(manifest)
    assert keys[:2] == ["domain", "name"]
    assert keys[2:] == sorted(keys[2:])
    assert manifest["domain"] == DOMAIN
    assert manifest["config_flow"] is True
    assert re.fullmatch(r"\d+\.\d+\.\d+", manifest["version"])
