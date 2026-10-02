import sys
from pathlib import Path

import pytest

from banksman import android
from banksman.android import AndroidError, parse_accounts, parse_properties
from banksman.config import Android

# No test runs adb. A fake answers each adb command that the presets run.

SDK = Path("/opt/android-sdk")
ADB = str(SDK / "platform-tools" / "adb")

DEVICES = """\
* daemon not running; starting now at tcp:5037
* daemon started successfully
List of devices attached
R5CR1234ABC	device
emulator-5554	device
0A1B2C3D	unauthorized

"""

GETPROP = """\
[ro.build.characteristics]: [nosdcard]
[ro.build.version.sdk]: [36]
[ro.product.cpu.abi]: [arm64-v8a]
[ro.product.device]: [e2s]
[ro.product.manufacturer]: [samsung]
[ro.product.model]: [SM-S926B]
"""

DUMPSYS = """\
User UserInfo{0:Owner:c13}:
  Accounts: 3
    Account {name=qa-primary@example.test, type=com.google}
    Account {name=someone, type=com.example.chat}
    Account {name=qa-secondary@example.test, type=com.google}

  AccountId, Action_Type, timestamp, UID, TableName, Key
    Account {name=old@example.test, type=com.google}
User UserInfo{10:Work profile:1030}:
  Accounts: 1
    Account {name=work@example.test, type=com.google}
"""


class FakeAdb:
    def __init__(self, answers):
        self.answers = answers
        self.commands = []

    def __call__(self, command, timeout):
        assert command[0] == ADB
        self.commands.append(command[1:])
        answer = self.answers.get(" ".join(command[1:]))
        if answer is None:
            raise AndroidError(f"{' '.join(command[1:])} failed with exit status 1")
        if isinstance(answer, Exception):
            raise answer
        return answer


def test_a_device_has_its_facts_and_its_google_accounts():
    adb = FakeAdb(
        {
            "devices": DEVICES,
            "-s R5CR1234ABC shell getprop": GETPROP,
            "-s R5CR1234ABC shell dumpsys account": DUMPSYS,
        }
    )
    document = android.devices(Android(sdk=SDK), run=adb)
    assert document == {
        "schema": 1,
        "instances": [
            {
                "name": "R5CR1234ABC",
                "facts": {
                    "serial": "R5CR1234ABC",
                    "running": True,
                    "manufacturer": "samsung",
                    "model": "SM-S926B",
                    "codename": "e2s",
                    "api": 36,
                    "abi": "arm64-v8a",
                    "form": "phone",
                },
                "accounts": [
                    "qa-primary@example.test",
                    "qa-secondary@example.test",
                    "work@example.test",
                ],
            }
        ],
        "notes": ["0A1B2C3D is unauthorized, so it is not offered"],
    }
    # Only reads: the device list, the properties, and the accounts.
    assert adb.commands == [
        ["devices"],
        ["-s", "R5CR1234ABC", "shell", "getprop"],
        ["-s", "R5CR1234ABC", "shell", "dumpsys", "account"],
    ]


def test_the_manufacturer_can_come_from_the_vendor_properties():
    adb = FakeAdb(
        {
            "devices": "List of devices attached\nR5CR1234ABC\tdevice\n",
            "-s R5CR1234ABC shell getprop": "[ro.product.vendor.manufacturer]: [Google]\n",
            "-s R5CR1234ABC shell dumpsys account": "  Accounts: 0\n",
        }
    )
    (instance,) = android.devices(Android(sdk=SDK), run=adb)["instances"]
    assert instance["facts"]["manufacturer"] == "Google"


def test_accounts_that_cannot_be_read_are_not_known():
    adb = FakeAdb(
        {
            "devices": "List of devices attached\nR5CR1234ABC\tdevice\n",
            "-s R5CR1234ABC shell getprop": "[ro.build.characteristics]: [tablet]\n",
            "-s R5CR1234ABC shell dumpsys account": "Permission Denial: can't dump\n",
        }
    )
    (instance,) = android.devices(Android(sdk=SDK), run=adb)["instances"]
    assert instance["facts"]["form"] == "tablet"
    assert instance["accounts"] is None
    assert "cannot read the accounts" in instance["note"]


def test_a_device_whose_properties_cannot_be_read_still_has_its_serial():
    adb = FakeAdb({"devices": "List of devices attached\nR5CR1234ABC\tdevice\n"})
    (instance,) = android.devices(Android(sdk=SDK), run=adb)["instances"]
    assert instance["facts"] == {"serial": "R5CR1234ABC", "running": True}
    assert instance["accounts"] is None
    assert instance["note"].startswith("cannot read the properties")


def test_without_adb_there_are_no_devices_and_a_note():
    adb = FakeAdb({"devices": AndroidError("adb does not exist")})
    document = android.devices(Android(sdk=SDK), run=adb)
    assert document["instances"] == []
    assert document["notes"] == ["cannot list the devices: adb does not exist"]


def write_avd(home, name, config, target="android-35"):
    directory = home / f"{name}.avd"
    directory.mkdir(parents=True)
    (home / f"{name}.ini").write_text(f"path={directory}\ntarget={target}\n")
    lines = [f"{key}={value}\n" for key, value in config.items()]
    (directory / "config.ini").write_text("".join(lines))


PHONE = {
    "avd.ini.displayname": "Pixel 7 API 35",
    "hw.device.manufacturer": "Google",
    "image.sysdir.1": "system-images/android-35/google_apis/arm64-v8a/",
    "tag.id": "google_apis",
    "abi.type": "arm64-v8a",
    "hw.lcd.width": "1080",
    "hw.lcd.height": "2400",
    "hw.lcd.density": "420",
}
TABLET = {
    "image.sysdir.1": "system-images/android-33/google_apis/arm64-v8a/",
    "tag.id": "google_apis",
    "abi.type": "arm64-v8a",
    "hw.lcd.width": "2560",
    "hw.lcd.height": "1600",
    "hw.lcd.density": "320",
}
WATCH = {"tag.id": "android-wear", "abi.type": "arm64-v8a"}


def test_emulators_are_the_avds_and_a_running_one_has_its_serial_and_accounts(tmp_path):
    home = tmp_path / "avd"
    write_avd(home, "qa_phone_api35", PHONE)
    write_avd(home, "qa_tablet_api33", TABLET)
    write_avd(home, "Wear_OS", WATCH, target="android-34")
    adb = FakeAdb(
        {
            "devices": DEVICES,
            "-s emulator-5554 emu avd name": "qa_phone_api35\r\nOK\r\n",
            "-s emulator-5554 shell dumpsys account": "User:\n  Accounts: 0\n",
        }
    )
    document = android.emulators(Android(sdk=SDK, avd_home=home), run=adb)
    assert document["notes"] == []
    assert document["instances"] == [
        {
            "name": "Wear_OS",
            "facts": {
                "avd": "Wear_OS",
                "api": 34,
                "image": "android-wear",
                "abi": "arm64-v8a",
                "form": "watch",
                "running": False,
            },
        },
        {
            "name": "qa_phone_api35",
            "facts": {
                "avd": "qa_phone_api35",
                "display_name": "Pixel 7 API 35",
                "manufacturer": "Google",
                "api": 35,
                "image": "google_apis",
                "abi": "arm64-v8a",
                "form": "phone",
                "running": True,
                "serial": "emulator-5554",
            },
            "accounts": [],
        },
        {
            "name": "qa_tablet_api33",
            "facts": {
                "avd": "qa_tablet_api33",
                "api": 33,
                "image": "google_apis",
                "abi": "arm64-v8a",
                "form": "tablet",
                "running": False,
            },
        },
    ]


def test_an_avd_with_a_relative_path_is_found(tmp_path):
    home = tmp_path / ".android" / "avd"
    directory = home / "moved.avd"
    directory.mkdir(parents=True)
    (home / "qa_phone.ini").write_text("path=/no/such/place\npath.rel=avd/moved.avd\n")
    (directory / "config.ini").write_text("tag.id=google_apis\n")
    adb = FakeAdb({"devices": "List of devices attached\n"})
    (instance,) = android.emulators(Android(sdk=SDK, avd_home=home), run=adb)["instances"]
    assert instance["facts"] == {"avd": "qa_phone", "image": "google_apis", "running": False}


def test_without_adb_it_is_not_known_whether_an_emulator_runs(tmp_path):
    home = tmp_path / "avd"
    write_avd(home, "qa_phone_api35", PHONE)
    adb = FakeAdb({"devices": AndroidError("adb does not exist")})
    document = android.emulators(Android(sdk=SDK, avd_home=home), run=adb)
    (instance,) = document["instances"]
    assert "serial" not in instance["facts"]
    assert "running" not in instance["facts"]
    assert document["notes"][0].startswith("cannot list the running emulators")


def test_the_avd_name_of_an_older_emulator_comes_from_its_properties(tmp_path):
    home = tmp_path / "avd"
    write_avd(home, "qa_phone_api35", PHONE)
    adb = FakeAdb(
        {
            "devices": "List of devices attached\nemulator-5556\tdevice\n",
            "-s emulator-5556 shell getprop": "[ro.kernel.qemu.avd_name]: [qa_phone_api35]\n",
            "-s emulator-5556 shell dumpsys account": "  Accounts: 0\n",
        }
    )
    (instance,) = android.emulators(Android(sdk=SDK, avd_home=home), run=adb)["instances"]
    assert instance["facts"]["serial"] == "emulator-5556"


def test_a_missing_avd_home_has_no_emulators(tmp_path):
    adb = FakeAdb({"devices": "List of devices attached\n"})
    document = android.emulators(Android(sdk=SDK, avd_home=tmp_path / "none"), run=adb)
    assert document["instances"] == []
    assert document["notes"] == [f"{tmp_path / 'none'} does not exist, so there are no AVDs"]


def test_an_avd_whose_files_cannot_be_read_is_left_out(tmp_path):
    home = tmp_path / "avd"
    home.mkdir()
    (home / "broken.ini").write_text("path=/no/such/place\n")
    adb = FakeAdb({"devices": "List of devices attached\n"})
    document = android.emulators(Android(sdk=SDK, avd_home=home), run=adb)
    assert document["instances"] == []
    assert document["notes"] == ["cannot read the files of the AVD broken"]


@pytest.mark.parametrize(
    ("text", "accounts"),
    [
        (DUMPSYS, ["qa-primary@example.test", "qa-secondary@example.test", "work@example.test"]),
        ("  Accounts: 0\n", []),
        ("Accounts: 1\n  Account {name=a@example.test, type=com.google}\n", ["a@example.test"]),
        ("Account {name=a@example.test, type=com.google}\n", None),
        ("", None),
    ],
)
def test_the_google_accounts_in_the_output_of_dumpsys(text, accounts):
    assert parse_accounts(text)[0] == accounts


def test_properties():
    properties = parse_properties(GETPROP + "not a property\n[a]: [b]\n")
    assert properties["ro.product.model"] == "SM-S926B"


def test_the_sdk_and_the_avd_home_do_not_depend_on_the_environment(monkeypatch):
    monkeypatch.setenv("ANDROID_HOME", "/somewhere/else")
    monkeypatch.setenv("ANDROID_AVD_HOME", "/somewhere/else/avd")
    settings = Android()
    expected = "Library/Android/sdk" if sys.platform == "darwin" else "Android/Sdk"
    assert str(android.sdk(settings)).endswith(expected)
    assert android.avd_home(settings).parts[-2:] == (".android", "avd")
    assert not str(android.sdk(settings)).startswith("/somewhere")
    assert android.sdk(Android(sdk=SDK)) == SDK


def test_a_program_that_does_not_exist_is_an_error(tmp_path):
    with pytest.raises(AndroidError, match="does not exist; set sdk in \\[android\\]"):
        android.run_program([str(tmp_path / "adb"), "devices"], 5)


def test_a_program_that_fails_or_hangs_is_an_error():
    with pytest.raises(AndroidError, match="failed with exit status 3"):
        android.run_program([sys.executable, "-c", "raise SystemExit(3)"], 30)
    with pytest.raises(AndroidError, match="did not end within 0.5 s"):
        android.run_program([sys.executable, "-c", "import time; time.sleep(30)"], 0.5)


def test_a_program_that_leaves_a_process_running_does_not_keep_the_command_waiting():
    # Like `adb devices` when it starts the adb server: the server keeps the output open.
    code = (
        "import subprocess, sys\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(3)'])\n"
        "print('List of devices attached')\n"
    )
    assert android.run_program([sys.executable, "-c", code], 2) == "List of devices attached\n"

