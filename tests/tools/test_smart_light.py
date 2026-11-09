"""smart_light (tools/smart_light.py): Tuya bulbs over the LAN."""

from __future__ import annotations

import json
import stat

import pytest

from openjarvis.agents.fast_paths import match_fast_path
from openjarvis.agents.session_guard import TRUSTED_OUTPUT_TOOLS
from openjarvis.core.types import ToolResult
from openjarvis.tools import smart_light
from openjarvis.tools.smart_light import LightError, SmartLightTool

SECRET = "super-secret-local-key"
WHITE = {"20": True, "21": "white", "22": 1000, "23": 28, "24": "000003e803e8"}
BLUE = {"20": True, "21": "colour", "22": 1000, "23": 28, "24": "00f003e801f4"}


class FakeBulb:
    def __init__(self, dps=None, error=None):
        self.dps = dict(WHITE if dps is None else dps)
        self.error = error
        self.sent = []

    def status(self):
        return {"Error": self.error} if self.error else {"dps": self.dps}

    def set_multiple_values(self, values):
        if self.error:
            return {"Error": self.error}
        self.sent.append(values)
        return {"dps": values}


@pytest.fixture
def lights_file(tmp_path, monkeypatch):
    path = tmp_path / "smart_lights.json"
    light = {
        "id": "abc",
        "name": "Bombillo",
        "local_key": SECRET,
        "ip": "192.168.2.10",
        "version": "3.5",
    }
    path.write_text(json.dumps({"lights": [light]}))
    monkeypatch.setattr(smart_light, "LIGHTS_PATH", path)
    return path


@pytest.fixture
def bulb(lights_file, monkeypatch):
    fake = FakeBulb()
    monkeypatch.setattr(smart_light, "_device", lambda light: fake)
    monkeypatch.setattr(smart_light, "_rediscover", lambda light: False)
    return fake


def test_on_and_off(bulb):
    assert SmartLightTool().execute(action="off").success
    assert SmartLightTool().execute(action="on").success
    assert bulb.sent == [{"20": False}, {"20": True}]


def test_brightness_in_white_mode_uses_the_bright_data_point(bulb):
    assert SmartLightTool().execute(action="brightness", value="50%").success
    assert bulb.sent == [{"20": True, "22": 500}]


def test_low_brightness_stays_within_the_bulb_minimum(bulb):
    SmartLightTool().execute(action="brightness", value="1")
    assert bulb.sent == [{"20": True, "22": 10}]


def test_brightness_in_colour_mode_keeps_the_colour(bulb):
    bulb.dps = dict(BLUE)
    SmartLightTool().execute(action="brightness", value="25")
    assert bulb.sent == [{"20": True, "24": "00f003e800fa"}]


def test_colour_name_keeps_current_brightness(bulb):
    bulb.dps = dict(WHITE, **{"22": 400})
    assert SmartLightTool().execute(action="color", value="Azul").success
    assert bulb.sent == [{"20": True, "21": "colour", "24": "00f003e80190"}]


def test_hex_colour(bulb):
    SmartLightTool().execute(action="color", value="#ff0000")
    assert bulb.sent[0]["24"] == "000003e803e8"


def test_white_word_given_as_colour_selects_white_mode(bulb):
    SmartLightTool().execute(action="color", value="cálida")
    assert bulb.sent == [{"20": True, "21": "white", "23": 0}]


def test_unknown_colour_is_refused_without_contacting_the_bulb(bulb):
    result = SmartLightTool().execute(action="color", value="bonita")
    assert not result.success and "bonita" in result.content
    assert bulb.sent == []


def test_status_never_returns_the_key(bulb):
    result = SmartLightTool().execute(action="status")
    assert result.success and "on, white, brightness 100%" in result.content
    assert SECRET not in result.content


def test_status_of_a_light_that_is_off(bulb):
    bulb.dps = dict(WHITE, **{"20": False})
    assert "Bombillo: off" in SmartLightTool().execute(action="status").content


def test_unreachable_bulb_is_reported(lights_file, monkeypatch):
    monkeypatch.setattr(
        smart_light, "_device", lambda light: FakeBulb(error="Unreachable")
    )
    monkeypatch.setattr(smart_light, "_rediscover", lambda light: False)
    result = SmartLightTool().execute(action="on")
    assert not result.success and "did not answer" in result.content


def test_moved_bulb_is_retried_at_its_new_address(lights_file, monkeypatch):
    good = FakeBulb()

    def device(light):
        return good if light["ip"] == "192.168.2.50" else FakeBulb(error="gone")

    monkeypatch.setattr(smart_light, "_device", device)
    monkeypatch.setattr(
        smart_light,
        "_rediscover",
        lambda light: smart_light._store_ip(light, "192.168.2.50"),
    )
    assert SmartLightTool().execute(action="on").success
    assert good.sent == [{"20": True}]
    saved = json.loads(lights_file.read_text())["lights"][0]
    assert saved["ip"] == "192.168.2.50" and saved["local_key"] == SECRET
    assert stat.S_IMODE(lights_file.stat().st_mode) == 0o600


def test_missing_config_is_explained(tmp_path, monkeypatch):
    monkeypatch.setattr(smart_light, "LIGHTS_PATH", tmp_path / "nope.json")
    result = SmartLightTool().execute(action="on")
    assert not result.success and "No lights configured" in result.content


def test_check_raises_on_tinytuya_error_payload():
    with pytest.raises(LightError):
        smart_light._checked({"Error": "Network Error"})


def test_output_is_trusted():
    assert "smart_light" in TRUSTED_OUTPUT_TOOLS


@pytest.mark.parametrize(
    ("text", "arguments"),
    [
        ("Prende la luz", {"action": "on"}),
        ("Jarvis, apaga el bombillo, por favor.", {"action": "off"}),
        ("apágame las luces", {"action": "off"}),
        ("enciende la lámpara", {"action": "on"}),
        ("pon la luz azul", {"action": "color", "value": "azul"}),
        ("cambia la luz a color verde", {"action": "color", "value": "verde"}),
        ("pon el brillo al 50%", {"action": "brightness", "value": "50"}),
        ("baja la luz al 30 por ciento", {"action": "brightness", "value": "30"}),
    ],
)
def test_fast_path_matches(text, arguments):
    path = match_fast_path(text, {"smart_light"})
    assert path is not None and path.tool == "smart_light"
    assert path.arguments == arguments


@pytest.mark.parametrize(
    "text",
    ["apaga la luz y pon música", "pon el brillo al 500", "apaga el computador"],
)
def test_fast_path_leaves_the_rest_to_the_model(text):
    assert match_fast_path(text, {"smart_light"}) is None


def test_fast_path_failure_goes_back_to_the_model():
    path = match_fast_path("prende la luz", {"smart_light"})
    failed = ToolResult(tool_name="smart_light", content="x", success=False)
    assert path.reply(failed) is None
