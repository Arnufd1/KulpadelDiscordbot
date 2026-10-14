import json
import subprocess
import types
from urllib.parse import parse_qs

import httpx
import pytest
from click.testing import CliRunner

from padelbot import android, auth, idp
from padelbot.cli import cli
from padelbot.config import settings
from padelbot.store import TokenBundle, TokenStore


LINK = "https://icts.kuleuven.be/apps/authenticator/fake-challenge_123"
ORIGIN = "https://idp.kuleuven.be"


@pytest.mark.parametrize("link", [
    "http://icts.kuleuven.be/apps/authenticator/token",
    "https://attacker.example/apps/authenticator/token",
    "https://icts.kuleuven.be@attacker.example/apps/authenticator/token",
    "https://icts.kuleuven.be/apps/authenticator/token?cmd=anything",
    "https://icts.kuleuven.be/apps/authenticator/token#anything",
    "https://icts.kuleuven.be/apps/authenticator/token;reboot",
    "https://icts.kuleuven.be/apps/authenticator/../token",
    "https://icts.kuleuven.be/apps/authenticator/",
])
def test_untrusted_link_is_rejected_before_device_actions(link, monkeypatch):
    monkeypatch.setattr(android, "selected_device", lambda: pytest.fail("queried phone for rejected link"))
    with pytest.raises(RuntimeError):
        android.open_challenge(link)


def test_adb_device_states_and_models(monkeypatch):
    monkeypatch.setattr(settings, "padelbot_android_device_id", "")
    monkeypatch.setattr(android, "run_adb", lambda *a: "List of devices attached\nfirst\tunauthorized\nsecond\tdevice product:demo model:Pixel_7 transport_id:1\n")
    assert android.list_devices() == [
        {"serial": "first", "state": "unauthorized", "model": ""},
        {"serial": "second", "state": "device", "model": "Pixel_7"},
    ]
    monkeypatch.setattr(settings, "padelbot_android_serial", "first")
    with pytest.raises(RuntimeError, match="accept"):
        android.selected_device()
    monkeypatch.setattr(settings, "padelbot_android_serial", "second")
    assert android.selected_device()["serial"] == "second"
    monkeypatch.setattr(settings, "padelbot_android_serial", "")
    with pytest.raises(RuntimeError, match="Several"):
        android.selected_device()


def test_physical_device_selection_survives_wifi_port_changes_and_duplicate_transports(monkeypatch):
    monkeypatch.setattr(settings, "padelbot_android_serial", "")
    monkeypatch.setattr(settings, "padelbot_android_device_id", "registered-phone")
    devices = [
        {"serial": "192.168.0.20:40000", "state": "device"},
        {"serial": "phone._adb-tls-connect._tcp", "state": "device"},
        {"serial": "other-phone", "state": "device"},
    ]
    monkeypatch.setattr(android, "list_devices", lambda: devices)
    monkeypatch.setattr(android, "run_adb", lambda *a, serial: "different-phone" if serial == "other-phone" else "registered-phone")
    assert android.selected_device()["serial"] == "192.168.0.20:40000"
    devices[0]["serial"] = "192.168.0.20:45000"
    assert android.selected_device()["serial"] == "192.168.0.20:45000"
    devices[:] = [devices[-1]]
    with pytest.raises(RuntimeError, match="No matching"):
        android.selected_device()


def test_adb_errors_do_not_expose_challenge_or_pin(monkeypatch, tmp_path):
    monkeypatch.setattr(android, "find_adb", lambda: tmp_path / "adb.exe")
    monkeypatch.setattr(android.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout=LINK, stderr="secret-pin"))
    with pytest.raises(RuntimeError) as error:
        android.run_adb("shell", "am", "start", "-d", LINK)
    assert LINK not in str(error.value)
    assert "secret-pin" not in str(error.value)


def test_hierarchy_hides_pin_account_and_unrelated_apps():
    xml = f'''<hierarchy>
    <node package="{android.PACKAGE}" class="android.widget.EditText" text="123456" password="true" content-desc="PIN 123456"/>
    <node package="{android.PACKAGE}" class="android.widget.Button" text="Log in r0123456 user@example.com https://icts.kuleuven.be/apps/authenticator/secret" resource-id="{android.PACKAGE}:id/login" clickable="true" enabled="true"/>
    <node package="other.app" text="private message"/>
    </hierarchy>'''
    nodes = android.describe_hierarchy(xml)
    assert len(nodes) == 2
    encoded = json.dumps(nodes)
    assert "123456" not in encoded and "r0123456" not in encoded
    assert "user@example.com" not in encoded and "private message" not in encoded
    assert "/secret" not in encoded
    assert nodes[0]["text"] == "[hidden]"
    assert nodes[1]["resource_id"].endswith(":id/login")


def test_pin_is_encrypted_and_validated(tmp_path):
    tokens = TokenStore(tmp_path / "key", tmp_path / "tokens")
    tokens.init_key()
    store = android.AndroidPinStore(tokens.key_file, tmp_path / "pin.enc")
    store.save("123456")
    assert b"123456" not in store.blob_file.read_bytes()
    assert store.load() == "123456"
    with pytest.raises(ValueError): store.save("not-a-pin")
    assert store.load() == "123456"


def test_profile_refuses_arbitrary_actions_and_unbounded_selectors(tmp_path, monkeypatch):
    path = tmp_path / "profile.json"
    monkeypatch.setattr(settings, "padelbot_android_profile_file", path)
    profile = {"version": 1, "serial": "test-device", "app_version": "1", "steps": [{"action": "shell", "selector": {"text": "Log in"}, "guard": {"text": "Confirm"}}]}
    path.write_text(json.dumps(profile))
    with pytest.raises(RuntimeError, match="Unsupported"):
        android.read_profile()
    profile["steps"][0]["action"] = "click"
    profile["steps"][0]["selector"] = {}
    path.write_text(json.dumps(profile))
    with pytest.raises(RuntimeError, match="explicit"):
        android.read_profile()


def test_passwordless_flow_binds_exact_link_and_never_submits_password(monkeypatch):
    import websocket
    events = []
    class Socket:
        def send(self, value): events.append(value)
        def settimeout(self, value): pass
        def recv(self): return "LOGIN approved"
        def close(self): events.append("closed")
    monkeypatch.setattr(websocket, "create_connection", lambda *a, **k: Socket())
    callback = settings.kul_redirect_uri + "?code=code&state=test"
    selector = '<form id="nextauthLoginForm" action="?execution=random"><input name="csrf_token" value="csrf"></form><form><input name="username"><input name="password"></form>'
    qr = f'''<body onload="nextauthwsinit('server','register',false,'wss://authenticator-wss.icts.kuleuven.be')"><form id="loginformnextauth" action="?execution=next"><input name="csrf_token" value="qr-csrf"><input name="_eventId_proceed"></form><a href="{LINK}">Open app</a><a href="{LINK}">QR</a></body>'''
    requests = []
    def handler(request):
        requests.append(request)
        data = parse_qs(request.content.decode(), keep_blank_values=True)
        assert "password" not in data and "nextauthAccountId" not in data
        if request.method == "GET": return httpx.Response(200, text=selector)
        if "_eventId_UseNextauth" in data:
            assert data["csrf_token"] == ["csrf"]
            return httpx.Response(200, text=qr)
        assert events == ["REGISTER register server", LINK]
        assert data["csrf_token"] == ["qr-csrf"]
        return httpx.Response(302, headers={"Location": callback})
    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        result = idp.walk_oidc(client, ORIGIN + "/idp/profile/oidc/authorize", settings.kul_redirect_uri,
                               {"cookies": [], "origins": []}, authenticate_link=lambda link: events.append(link))
    assert result == callback
    assert len(requests) == 3
    assert events[-1] == "closed"


def test_profile_rejects_repeated_pin_entry(tmp_path, monkeypatch):
    path = tmp_path / "profile.json"
    monkeypatch.setattr(settings, "padelbot_android_profile_file", path)
    step = {"action": "pin", "selector": {"resourceId": "pin"}, "guard": {"resourceId": "pin-screen"}}
    path.write_text(json.dumps({"version": 1, "serial": "test-device", "app_version": "1", "steps": [step, step]}))
    with pytest.raises(RuntimeError, match="only one"):
        android.read_profile()


def test_android_profile_is_bound_to_serial_and_app_version(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "padelbot_android_failure_file", tmp_path / "failure")
    monkeypatch.setattr(android, "doctor", lambda: {"serial": "different-device", "app_version": "1"})
    monkeypatch.setattr(android, "read_profile", lambda: {"serial": "known-device", "app_version": "1", "steps": [{"action": "click"}]})
    with pytest.raises(RuntimeError, match="Recalibrate"):
        android.AndroidApprover()


def test_android_profile_follows_same_handset_but_rejects_different_device_id(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "padelbot_android_failure_file", tmp_path / "failure")
    info = {"serial": "wifi:new-port", "device_id": "registered-phone", "app_version": "1"}
    monkeypatch.setattr(android, "doctor", lambda: info)
    monkeypatch.setattr(android, "read_profile", lambda: {"device_id": "registered-phone", "app_version": "1", "steps": [{"action": "click"}]})
    assert android.AndroidApprover().device_info["serial"] == "wifi:new-port"
    info["device_id"] = "different-phone"
    with pytest.raises(RuntimeError, match="Recalibrate"):
        android.AndroidApprover()


def test_inspection_does_not_report_success_for_app_behind_lock_screen(tmp_path, monkeypatch):
    device = types.SimpleNamespace(
        app_current=lambda: {"package": android.PACKAGE},
        dump_hierarchy=lambda: '<hierarchy><node package="com.android.systemui" text="PIN"/></hierarchy>',
    )
    monkeypatch.setattr(android, "connect_device", lambda: device)
    output = tmp_path / "screen.json"
    with pytest.raises(RuntimeError, match="Unlock the phone"):
        android.inspect_app(output)
    assert not output.exists()


def test_profile_controls_are_scoped_to_authenticator_and_pin_runs_once(monkeypatch, tmp_path):
    events = []
    step = {"action": "pin", "selector": {"resourceId": "pin"}, "guard": {"resourceId": "pin-screen"}}
    profile = {"serial": "test-device", "app_version": "1", "steps": [step]}
    monkeypatch.setattr(settings, "padelbot_android_failure_file", tmp_path / "failure")
    monkeypatch.setattr(android, "doctor", lambda: {"serial": "test-device", "app_version": "1"})
    monkeypatch.setattr(android, "read_profile", lambda: profile)
    monkeypatch.setattr(android.AndroidPinStore, "load", lambda s: "123456")
    class Element:
        count = 1
        def wait(self, **kwargs): return not getattr(self, "biometric", False)
        def set_text(self, pin): events.append("pin-attempt")
    class Device:
        def app_stop(self, package):
            assert package == android.PACKAGE
            events.append("stop-own-app")
        def screen_on(self): events.append("screen-on")
        def app_current(self): return {"package": android.PACKAGE}
        def __call__(self, **kwargs):
            element = Element()
            element.biometric = kwargs["packageName"] == android.SAMSUNG_BIOMETRIC_PACKAGE
            if not element.biometric:
                assert kwargs["packageName"] == android.PACKAGE
            return element
    monkeypatch.setattr(android, "connect_device", lambda serial: Device())
    monkeypatch.setattr(android, "open_challenge", lambda link, **k: events.append("opened-link"))
    monkeypatch.setattr("time.sleep", lambda s: None)
    approver = android.AndroidApprover()
    approver(LINK)
    assert events == ["stop-own-app", "screen-on", "opened-link", "pin-attempt"]
    with pytest.raises(RuntimeError, match="already been attempted"):
        approver(LINK)
    assert events == ["stop-own-app", "screen-on", "opened-link", "pin-attempt"]
    approver.on_failure()
    assert settings.padelbot_android_failure_file.exists()
    with pytest.raises(RuntimeError, match="earlier"):
        android.AndroidApprover()


def test_cold_sdk_waits_for_own_confirmation_activity_before_foreground_check(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "padelbot_android_failure_file", tmp_path / "failure")
    monkeypatch.setattr(android, "doctor", lambda: {"serial": "test-device", "app_version": "1"})
    monkeypatch.setattr(android, "read_profile", lambda: {"serial": "test-device", "app_version": "1", "steps": [
        {"action": "click", "guard": {"resourceId": "own-confirmation"}, "selector": {"resourceId": "own-next"}}
    ]})
    events = []
    class Element:
        count = 1
        def wait(self, **kwargs):
            device.foreground = android.PACKAGE
            events.append("own-screen-ready")
            return True
        def click(self): events.append("clicked-own-next")
    class Device:
        foreground = "com.android.launcher3"
        def app_stop(self, package): pass
        def screen_on(self): pass
        def app_current(self): return {"package": self.foreground}
        def __call__(self, **kwargs):
            assert kwargs["packageName"] == android.PACKAGE
            return Element()
    device = Device()
    monkeypatch.setattr(android, "connect_device", lambda serial: device)
    monkeypatch.setattr(android, "open_challenge", lambda link, **kwargs: None)
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    android.AndroidApprover()(LINK)
    assert events == ["own-screen-ready", "clicked-own-next"]


def test_profile_startup_guard_initializes_registered_account_before_opening_link(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "padelbot_android_failure_file", tmp_path / "failure")
    monkeypatch.setattr(android, "doctor", lambda: {"serial": "test-device", "app_version": "1"})
    monkeypatch.setattr(android, "read_profile", lambda: {"serial": "test-device", "app_version": "1",
        "startup_guard": {"resourceId": "own-account"}, "steps": [
            {"action": "click", "guard": {"resourceId": "own-confirmation"}, "selector": {"resourceId": "own-next"}}
        ]})
    events = []
    class Element:
        count = 1
        def __init__(self, resource): self.resource = resource
        def wait(self, **kwargs):
            events.append(self.resource + "-ready")
            return True
        def click(self): events.append("clicked")
    class Device:
        def app_stop(self, package): pass
        def screen_on(self): pass
        def app_start(self, package):
            assert package == android.PACKAGE
            events.append("initialize-own-app")
        def app_current(self): return {"package": android.PACKAGE}
        def __call__(self, **kwargs):
            assert kwargs["packageName"] == android.PACKAGE
            return Element(kwargs["resourceId"])
    monkeypatch.setattr(android, "connect_device", lambda serial: Device())
    monkeypatch.setattr(android, "open_challenge", lambda link, **kwargs: events.append("open-own-link"))
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    android.AndroidApprover()(LINK)
    assert events == ["initialize-own-app", "own-account-ready", "open-own-link", "own-confirmation-ready", "clicked"]


def test_mirror_launches_only_selected_device_without_audio(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(android, "selected_device", lambda: {"serial": "test-device"})
    monkeypatch.setattr(android.shutil, "which", lambda name: "scrcpy.exe")
    monkeypatch.setattr(android, "find_adb", lambda: tmp_path / "adb.exe")
    commands = []
    monkeypatch.setattr(android.subprocess, "Popen", lambda command, **k: (commands.append(command) or types.SimpleNamespace(pid=42)))
    assert android.start_mirror() == 42
    assert "--serial" in commands[0] and "test-device" in commands[0]
    assert "--no-audio" in commands[0]


def test_cli_requires_android_for_manual_approval():
    result = CliRunner().invoke(cli, ["login", "--manual-approval"])
    assert result.exit_code == 2
    assert "requires --android" in result.output


def test_fresh_android_login_ignores_saved_session_and_marks_pin_failure(monkeypatch):
    calls = []
    class Provider:
        def on_failure(self): calls.append("failure")
    def http_login(path, **kwargs):
        assert kwargs["use_saved_session"] is False
        raise RuntimeError("app did not approve")
    monkeypatch.setattr(auth, "_http_login", http_login)
    with pytest.raises(RuntimeError):
        auth.login_via_android(Provider(), fresh=True)
    assert calls == ["failure"]


@pytest.mark.parametrize("enabled", [False, True])
def test_expired_session_uses_android_only_when_enabled(tmp_path, monkeypatch, enabled):
    store = TokenStore(tmp_path / "key", tmp_path / "tokens")
    store.init_key()
    store.save(TokenBundle("old", "", None, "Bearer", 0, "openid"))
    monkeypatch.setattr(settings, "padelbot_android_autologin", enabled)
    def expired(path):
        raise idp.SessionExpiredError("expired")
    monkeypatch.setattr(auth, "silent_refresh_via_session", expired)
    provider = object()
    monkeypatch.setattr(android, "AndroidApprover", lambda: provider if enabled else pytest.fail("Android disabled"))
    def login(approver):
        assert approver is provider
        return TokenBundle("new", "", None, "Bearer", 600, "openid")
    monkeypatch.setattr(auth, "login_via_android", login)
    if enabled:
        assert auth.renew_access_token(store) == "new"
        assert store.load().access_token == "new"
    else:
        with pytest.raises(idp.SessionExpiredError):
            auth.renew_access_token(store)
        assert store.load().access_token == "old"


def test_network_failure_does_not_trigger_android_approval(tmp_path, monkeypatch):
    store = TokenStore(tmp_path / "key", tmp_path / "tokens")
    store.init_key()
    store.save(TokenBundle("old", "", None, "Bearer", 0, "openid"))
    monkeypatch.setattr(settings, "padelbot_android_autologin", True)
    def disconnected(path):
        raise httpx.ConnectError("network unavailable")
    monkeypatch.setattr(auth, "silent_refresh_via_session", disconnected)
    monkeypatch.setattr(android, "AndroidApprover", lambda: pytest.fail("phone invoked on network error"))
    with pytest.raises(httpx.ConnectError):
        auth.renew_access_token(store)
    assert store.load().access_token == "old"


def keypad_step():
    return {
        "action": "pin", "length": 4,
        "guard": {"resourceId": "pin-screen"},
        "selector": {"resourceId": "pin-container"},
        "empty_selector": {"resourceId": "empty-dot"},
        "keypad": {digit: {"resourceId": "button" + digit, "text": digit} for digit in "0123456789"},
    }


@pytest.mark.parametrize("starting_empty, change_screen, expected", [(4, False, list("0012")), (3, False, []), (4, True, ["0"])])
def test_keypad_enters_exact_pin_once_and_stops_on_partial_entry_or_screen_change(starting_empty, change_screen, expected):
    clicked = []
    class Element:
        def __init__(self, selector): self.selector = selector
        @property
        def count(self):
            if self.selector["resourceId"] == "empty-dot": return starting_empty - len(clicked)
            return 1
        def click(self): clicked.append(self.selector["text"])
    class Device:
        def app_current(self): return {"package": "other.app" if change_screen and clicked else android.PACKAGE}
        def __call__(self, **kwargs):
            assert kwargs.pop("packageName") == android.PACKAGE
            return Element(kwargs)
    approver = android.AndroidApprover.__new__(android.AndroidApprover)
    approver.pin, approver.attempted_pin = "0012", False
    if starting_empty == 4 and not change_screen:
        approver._enter_keypad(Device(), keypad_step())
    else:
        with pytest.raises(RuntimeError):
            approver._enter_keypad(Device(), keypad_step())
    assert clicked == expected
    assert approver.attempted_pin == bool(expected)


def test_profile_requires_correct_digit_labels_and_all_keypad_buttons(tmp_path, monkeypatch):
    path = tmp_path / "profile.json"
    monkeypatch.setattr(settings, "padelbot_android_profile_file", path)
    step = keypad_step()
    profile = {"version": 1, "device_id": "registered-phone", "app_version": "1", "steps": [step]}
    path.write_text(json.dumps(profile))
    assert android.read_profile()["steps"][0]["length"] == 4
    step["keypad"]["0"]["text"] = "1"
    path.write_text(json.dumps(profile))
    with pytest.raises(RuntimeError, match="digit labels"):
        android.read_profile()
    del step["keypad"]["0"]
    path.write_text(json.dumps(profile))
    with pytest.raises(RuntimeError, match="all ten"):
        android.read_profile()


def test_wrong_stored_pin_length_stops_before_phone_ui_actions(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "padelbot_android_failure_file", tmp_path / "failure")
    monkeypatch.setattr(android, "doctor", lambda: {"device_id": "registered-phone", "app_version": "1"})
    monkeypatch.setattr(android, "read_profile", lambda: {"device_id": "registered-phone", "app_version": "1", "steps": [keypad_step()]})
    monkeypatch.setattr(android.AndroidPinStore, "load", lambda s: "123456")
    monkeypatch.setattr(android, "connect_device", lambda *a: pytest.fail("UI started with incompatible PIN length"))
    with pytest.raises(RuntimeError, match="PIN length"):
        android.AndroidApprover()


@pytest.mark.parametrize("activity, subject_matches, clicked", [
    ("com.nextauth.authenticator.authentication.SecondFactorActivity", True, True),
    ("other.Activity", True, False),
    ("com.nextauth.authenticator.authentication.SecondFactorActivity", False, False),
])
def test_samsung_pin_option_requires_own_second_factor_and_kul_subject(activity, subject_matches, clicked):
    events = []
    class Element:
        def __init__(self, kwargs): self.kwargs = kwargs
        def wait(self, **kwargs): return True
        @property
        def count(self): return 0 if self.kwargs["text"] == "to: KU Leuven" and not subject_matches else 1
        def click(self): events.append("Use PIN")
    class Device:
        def __call__(self, **kwargs):
            assert kwargs["packageName"] == android.SAMSUNG_BIOMETRIC_PACKAGE
            return Element(kwargs)
        def app_current(self): return {"package": android.PACKAGE, "activity": activity}
    approver = android.AndroidApprover.__new__(android.AndroidApprover)
    if clicked:
        approver._select_pin_fallback(Device())
    else:
        with pytest.raises(RuntimeError, match="does not match"):
            approver._select_pin_fallback(Device())
    assert events == (["Use PIN"] if clicked else [])
