import math

import pytest

from agent.actions import (ActionError, ActionExecutor, AskHuman, Click, Done, Select, Type, parse_action, redacted)
from agent.vision import extract_json

W, H = 1280, 800


def P(raw, mode="pixels"):
    return parse_action(raw, W, H, mode)


def test_valid_click_pixels():
    a = P({"action": "click", "x": 100, "y": 200, "rationale": "r", "target": "btn"})
    assert isinstance(a, Click) and (a.x, a.y) == (100, 200)


def test_valid_click_norm1000_converts_to_pixels():
    a = P({"action": "click", "x": 500, "y": 500}, "norm1000")
    assert (a.x, a.y) == (640, 400)


def test_numeric_strings_are_coerced_then_bounds_checked():
    assert P({"action": "click", "x": "10", "y": 5}).x == 10
    with pytest.raises(ActionError):
        P({"action": "click", "x": "abc", "y": 5})
    with pytest.raises(ActionError):
        P({"action": "click", "x": "9999", "y": 5})


def test_nulls_from_model_are_ignored():
    a = P({"action": "ask_human", "kind": "otp", "question": "OTP?", "x": None, "text": None})
    assert isinstance(a, AskHuman)


@pytest.mark.parametrize("raw", [[], "click", None, 5])
def test_non_object_rejected(raw):
    with pytest.raises(ActionError):
        P(raw)


def test_malformed_json_text():
    with pytest.raises(ActionError):
        extract_json("I think we should click the button")
    with pytest.raises(ActionError):
        extract_json('{"action": "click", ')
    assert extract_json('```json\n{"action":"screenshot"}\n```') == {"action": "screenshot"}


@pytest.mark.parametrize("kind", ["rm_rf", "eval", "navigate", "", None])
def test_unknown_action_types_rejected(kind):
    with pytest.raises(ActionError, match="unknown action"):
        P({"action": kind})


@pytest.mark.parametrize("raw", [
    {"action": "click", "x": 1300, "y": 10}, {"action": "click", "x": -1, "y": 10}, {"action": "click", "x": 10, "y": 801},
    {"action": "select", "x": 5000, "y": 5, "option": "a"},
])
def test_out_of_bounds_pixels(raw):
    with pytest.raises(ActionError):
        P(raw)


def test_out_of_bounds_norm_grid():
    with pytest.raises(ActionError):
        P({"action": "click", "x": 1001, "y": 5}, "norm1000")


@pytest.mark.parametrize("v", [math.nan, math.inf])
def test_non_finite_coords(v):
    with pytest.raises(ActionError):
        P({"action": "click", "x": v, "y": 10})


@pytest.mark.parametrize("raw", [
    {"action": "click", "x": 10}, {"action": "type"}, {"action": "select", "x": 1, "y": 1},
    {"action": "ask_human", "kind": "otp"}, {"action": "error"},     {"action": "type", "text": "a", "x": 5},
])
def test_missing_or_bad_fields(raw):
    with pytest.raises(ActionError):
        P(raw)


@pytest.mark.parametrize("raw", [
    {"action": "type", "text": "a\nb"}, {"action": "press", "key": "F12"}, {"action": "press", "key": "Control+Shift+i"},
    {"action": "scroll", "dy": 0}, {"action": "scroll", "dy": 99999}, {"action": "wait", "seconds": 60},
    {"action": "ask_human", "kind": "payment", "question": "q"},
])
def test_unsafe_values_rejected(raw):
    with pytest.raises(ActionError):
        P(raw)


def test_redaction_masks_sensitive_and_known_secrets():
    a = P({"action": "type", "text": "123412341234", "sensitive": True, "x": 5, "y": 5})
    assert redacted(a)["text"] == "•••"
    b = P({"action": "type", "text": "hello 9876543210", "x": 5, "y": 5})
    assert "9876543210" not in redacted(b, ["9876543210"])["text"]


async def test_executor_refuses_non_executable_actions():
    ex = ActionExecutor(page=None)
    with pytest.raises(ActionError):
        await ex.execute(Done(action="done", summary="x"))
    with pytest.raises(ActionError):
        await ex.execute(AskHuman(action="ask_human", kind="otp", question="q"))
    with pytest.raises(ActionError):
        await ex.execute({"action": "click"})


async def test_executor_select_on_non_dropdown_is_soft_error():
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        b = await pw.chromium.launch()
        page = await b.new_page(viewport={"width": W, "height": H})
        await page.set_content("<button style='position:absolute;left:10px;top:10px;width:100px;height:40px'>x</button>"
                               "<select style='position:absolute;left:10px;top:100px;width:100px;height:30px'><option>A</option><option>General (GEN)</option></select>")
        ex = ActionExecutor(page, 5)
        r = await ex.execute(Select(action="select", x=40, y=25, option="A"))
        assert not r.ok and "no dropdown" in r.detail
        r = await ex.execute(Select(action="select", x=40, y=115, option="gen"))
        assert r.ok and "General (GEN)" in r.detail
        await page.mouse.click(40, 115)  # the model clicks the dropdown, then selects with x,y that miss it
        r = await ex.execute(Select(action="select", x=400, y=400, option="A"))
        assert r.ok and "focused dropdown" in r.detail
        r = await ex.execute(Type(action="type", text="hi"))
        assert not r.ok and "no text field is focused" in r.detail
        await page.set_content("<input style='position:absolute;left:10px;top:10px;width:200px;height:30px'>")
        r = await ex.execute(Type(action="type", text="hi", x=5, y=300))
        assert not r.ok and "(5,300)" in r.detail
        r = await ex.execute(Type(action="type", text="hi", x=60, y=25))
        assert r.ok and await page.input_value("input") == "hi"
        r = await ex.execute(Type(action="type", text="yo", x=5, y=300))  # x,y misses, but the field is still focused
        assert r.ok and "already focused" in r.detail and await page.input_value("input") == "yo"
        await b.close()
