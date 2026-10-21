import sys
from pathlib import Path

import pytest

from banksman import android
from banksman.android import (
    AndroidError,
    Transport,
    parse_accounts,
    parse_input_age,
    parse_properties,
    parse_transports,
)
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


def test_a_port_forward_is_not_offered_as_a_device():
    adb = FakeAdb({"devices": "List of devices attached\nlocalhost:49920\tdevice\n"})
    document = android.devices(Android(sdk=SDK), run=adb)
    assert document["instances"] == []
    assert document["notes"] == [
        "localhost:49920 is a port forward, so it is not offered as a device; a kind with the"
        " android-remote preset offers it"
    ]
    assert adb.commands == [["devices"]]


# The serial that adb gives the device R5CR1234ABC over Wi-Fi after `adb pair`.
WIFI = "adb-R5CR1234ABC-aYi6h4._adb-tls-connect._tcp"
SERIAL_NUMBER = "[ro.serialno]: [R5CR1234ABC]\n"


def test_a_device_over_usb_and_wifi_is_one_instance_with_the_serial_over_usb():
    adb = FakeAdb(
        {
            "devices": f"List of devices attached\n{WIFI}\tdevice\nR5CR1234ABC\tdevice\n",
            f"-s {WIFI} shell getprop": SERIAL_NUMBER,
            "-s R5CR1234ABC shell getprop": SERIAL_NUMBER + GETPROP,
            "-s R5CR1234ABC shell dumpsys account": "  Accounts: 0\n",
        }
    )
    document = android.devices(Android(sdk=SDK), run=adb)
    (instance,) = document["instances"]
    assert instance["name"] == "R5CR1234ABC"
    assert instance["facts"]["serial"] == "R5CR1234ABC"
    assert instance["facts"]["model"] == "SM-S926B"
    assert document["notes"] == []
    assert adb.commands == [
        ["devices"],
        ["-s", WIFI, "shell", "getprop"],
        ["-s", "R5CR1234ABC", "shell", "getprop"],
        ["-s", "R5CR1234ABC", "shell", "dumpsys", "account"],
    ]


@pytest.mark.parametrize("serial", [WIFI, "192.0.2.7:37123"])
def test_a_device_over_wifi_only_is_named_by_its_serial_number(serial):
    adb = FakeAdb(
        {
            "devices": f"List of devices attached\n{serial}\tdevice\n",
            f"-s {serial} shell getprop": SERIAL_NUMBER + GETPROP,
            f"-s {serial} shell dumpsys account": "  Accounts: 0\n",
        }
    )
    (instance,) = android.devices(Android(sdk=SDK), run=adb)["instances"]
    assert instance["name"] == "R5CR1234ABC"
    assert instance["facts"]["serial"] == serial
    assert instance["facts"]["model"] == "SM-S926B"


def test_a_device_over_the_network_without_a_serial_number_is_not_offered():
    adb = FakeAdb(
        {
            "devices": f"List of devices attached\n{WIFI}\tdevice\n192.0.2.7:37123\tdevice\n",
            "-s 192.0.2.7:37123 shell getprop": GETPROP,
        }
    )
    document = android.devices(Android(sdk=SDK), run=adb)
    assert document["instances"] == []
    assert document["notes"] == [
        f"{serial} is a connection over the network without a serial number that banksman can"
        " read, so it is not offered"
        for serial in (WIFI, "192.0.2.7:37123")
    ]


def test_a_device_over_usb_keeps_its_serial_as_its_name_without_a_valid_serial_number():
    adb = FakeAdb(
        {
            "devices": "List of devices attached\nR5CR1234ABC\tdevice\n",
            "-s R5CR1234ABC shell getprop": "[ro.serialno]: [not a name]\n",
        }
    )
    (instance,) = android.devices(Android(sdk=SDK), run=adb, accounts=False)["instances"]
    assert instance["name"] == "R5CR1234ABC"


@pytest.mark.parametrize(
    ("serial", "number"),
    [
        (WIFI, "R5CR1234ABC"),
        (WIFI + ".", "R5CR1234ABC"),
        # After adb tcpip, the name has no random characters.
        ("adb-R5CR1234ABC._adb._tcp", "R5CR1234ABC"),
        ("adb-R5CR1234ABC._adb._tcp.", "R5CR1234ABC"),
        ("adb-0A1B-2C3D._adb._tcp", "0A1B-2C3D"),
        ("adb-0A1B-2C3D-Zz09aB._adb-tls-connect._tcp", "0A1B-2C3D"),
        ("adb-R5CR1234ABC-aYi6h._adb-tls-connect._tcp", None),
        ("adb-R5CR1234ABC-aYi6h4._adb-tls-pairing._tcp", None),
        ("R5CR1234ABC", None),
        ("192.0.2.7:37123", None),
    ],
)
def test_the_serial_number_in_a_serial_that_adb_found_by_mdns(serial, number):
    assert android.mdns_serial_number(serial) == number


@pytest.mark.parametrize(
    ("serial", "network"),
    [
        (WIFI, True),
        ("adb-R5CR1234ABC._adb._tcp", True),
        ("192.0.2.7:37123", True),
        ("[2001:db8::7]:37123", True),
        ("R5CR1234ABC", False),
        ("emulator-5554", False),
    ],
)
def test_a_serial_over_the_network(serial, network):
    assert android.network(serial) is network


def test_a_remote_device_keeps_a_form_that_its_properties_do_not_name():
    adb = FakeAdb({"-s localhost:49920 shell getprop": "[ro.product.device]: [gts9wifi]\n"})
    settings = Android(sdk=SDK)
    assert android.describe(settings, "localhost:49920", form=None, run=adb) == {
        "codename": "gts9wifi"
    }
    tablet = "[ro.build.characteristics]: [tablet,nosdcard]\n"
    adb = FakeAdb({"-s localhost:49920 shell getprop": tablet})
    assert android.describe(settings, "localhost:49920", form=None, run=adb) == {"form": "tablet"}


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
# The 7.6in Foldable of the AVD manager: as wide as a tablet when it is unfolded.
FOLDABLE = {
    **TABLET,
    "hw.lcd.width": "1768",
    "hw.lcd.height": "2208",
    "hw.lcd.density": "420",
    "hw.sensor.hinge": "yes",
}


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


@pytest.mark.parametrize(
    ("config", "form"),
    [
        (FOLDABLE, "foldable"),
        ({**FOLDABLE, "hw.sensor.hinge": "no"}, "tablet"),
        ({**PHONE, "hw.sensor.hinge": "no"}, "phone"),
    ],
)
def test_an_avd_with_a_hinge_is_a_foldable(tmp_path, config, form):
    home = tmp_path / "avd"
    write_avd(home, "qa_fold_api36", config)
    adb = FakeAdb({"devices": "List of devices attached\n"})
    (instance,) = android.emulators(Android(sdk=SDK, avd_home=home), run=adb)["instances"]
    assert instance["facts"]["form"] == form


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


def test_an_emulator_that_boots_has_its_serial_from_its_console(tmp_path):
    home = tmp_path / "avd"
    write_avd(home, "qa_phone_api35", PHONE)
    write_avd(home, "qa_tablet_api33", TABLET)
    # adb lists an emulator as offline until its adb daemon starts. The console answers before.
    adb = FakeAdb(
        {
            "devices": "List of devices attached\nemulator-5556\toffline\nemulator-5558\toffline\n",
            "-s emulator-5556 emu avd name": "qa_phone_api35\r\nOK\r\n",
        }
    )
    document = android.emulators(Android(sdk=SDK, avd_home=home), run=adb)
    phone, tablet = document["instances"]
    assert (phone["facts"]["running"], phone["facts"]["serial"]) == (True, "emulator-5556")
    # Its accounts cannot be read yet, so they are not known.
    assert "accounts" not in phone
    assert tablet["facts"]["running"] is False
    # The properties need the adb daemon, so they are not asked for.
    assert ["-s", "emulator-5558", "shell", "getprop"] not in adb.commands
    assert document["notes"] == ["cannot read the AVD name of the running emulator-5558"]


def test_the_presets_can_leave_out_the_accounts(tmp_path):
    home = tmp_path / "avd"
    write_avd(home, "qa_phone_api35", PHONE)
    adb = FakeAdb(
        {
            "devices": DEVICES,
            "-s emulator-5554 emu avd name": "qa_phone_api35\r\nOK\r\n",
            "-s R5CR1234ABC shell getprop": GETPROP,
        }
    )
    settings = Android(sdk=SDK, avd_home=home)
    (emulator,) = android.emulators(settings, run=adb, accounts=False)["instances"]
    (device,) = android.devices(settings, run=adb, accounts=False)["instances"]
    assert emulator["facts"]["serial"] == "emulator-5554"
    assert "accounts" not in emulator and "accounts" not in device
    assert not [command for command in adb.commands if "dumpsys" in command]


def test_the_avd_name_of_one_emulator(tmp_path):
    adb = FakeAdb({"-s emulator-5554 emu avd name": "qa_phone_api35\r\nOK\r\n"})
    assert android.avd_name(Android(sdk=SDK), "emulator-5554", run=adb) == "qa_phone_api35"
    assert android.avd_name(Android(sdk=SDK), "emulator-5556", run=adb) is None
    # A serial that is not one of an emulator needs no question.
    assert android.avd_name(Android(sdk=SDK), "R5CR1234ABC", run=adb) is None
    assert adb.commands == [
        ["-s", "emulator-5554", "emu", "avd", "name"],
        ["-s", "emulator-5556", "emu", "avd", "name"],
        ["-s", "emulator-5556", "shell", "getprop"],
    ]


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


def test_the_devices_of_adb_devices_l():
    text = (
        "* daemon not running; starting now at tcp:5037\n"
        "List of devices attached\n"
        "R5CR1234ABC            device usb:1-1 product:e2sxxx model:SM_S926B device:e2s"
        " transport_id:3\n"
        "emulator-5554          offline transport_id:4\n"
        "192.0.2.7:5555         device product:husky model:Pixel_8 device:husky transport_id:5\n"
        "0A1B2C3D               no permissions (missing udev rules?) usb:1-2 transport_id:6\n"
        "\n"
    )
    assert parse_transports(text) == [
        Transport("R5CR1234ABC", "device", "usb:1-1", "e2sxxx", "SM_S926B", "e2s", "3"),
        Transport("emulator-5554", "offline", transport_id="4"),
        Transport("192.0.2.7:5555", "device", "", "husky", "Pixel_8", "husky", "5"),
        Transport("0A1B2C3D", "no", "usb:1-2", transport_id="6"),
    ]


def test_the_devices_come_from_the_adb_server_that_the_call_names():
    adb = FakeAdb({"-P 5038 devices -l": "List of devices attached\nemulator-5556 device\n"})
    found = android.transports(Android(sdk=SDK), ("-P", "5038"), run=adb)
    assert found == [Transport("emulator-5556", "device")]
    assert adb.commands == [["-P", "5038", "devices", "-l"]]


# The input age: the uptime of the device, then what the power manager says.

INPUT_AGE_CALL = "-s emulator-5554 shell cat /proc/uptime; dumpsys power"
# As Android 16 prints it, measured on an emulator: 24879 s of uptime, and the last input
# 14171.127 s after the boot.
POWER_NEW = """\
24879.42 98112.07
POWER MANAGER (dumpsys power)

Power Manager State:
  mDirty=0x0
  mWakefulness=Asleep
  mLastUserActivityTime(excludingAttention)=14171127
  mLastUserActivityTimeNoChangeLights=0
"""
# As earlier versions print it, with how long ago the input was in the clock of the input.
POWER_AGO = """\
1100.00 4000.00
Power Manager State:
  mLastWakeTime=1084416 (11011 ms ago)
  mLastUserActivityTime=1090322 (5105 ms ago)
  mLastUserActivityTimeNoChangeLights=0 (1095427 ms ago)
"""


def test_the_input_age_is_the_uptime_minus_the_time_of_the_last_input():
    assert parse_input_age(POWER_NEW) == 10708
    # Earlier versions print the name without the suffix.
    assert parse_input_age(POWER_NEW.replace("(excludingAttention)", "")) == 10708


def test_the_input_age_that_the_power_manager_gives_counts_first():
    # 1100 s of uptime would give 9 s; the time that the device slept does not count.
    assert parse_input_age(POWER_AGO) == 5
    assert parse_input_age(POWER_AGO.replace("(5105 ms ago)", "(now)")) == 0


@pytest.mark.parametrize(
    "text",
    [
        "",
        "24879.42 98112.07\n",
        "24879.42 98112.07\n  mLastUserActivityTimeNoChangeLights=0\n",
        "24879.42 98112.07\n  mLastUserActivityTime=soon\n",
        "cat: /proc/uptime: Permission denied\n  mLastUserActivityTime=14171127\n",
        "Can't find service: power\n",
    ],
)
def test_an_output_without_a_known_value_gives_no_input_age(text):
    assert parse_input_age(text) is None


def test_an_input_during_the_call_and_several_displays_give_the_latest_input():
    text = (
        "100.00 300.00\n"
        "  mLastUserActivityTime=40000\n"
        "  mLastUserActivityTime=100500\n"
    )
    assert parse_input_age(text) == 0


def test_a_running_emulator_has_its_input_age(tmp_path):
    home = tmp_path / "avd"
    write_avd(home, "qa_phone_api35", PHONE)
    answers = {
        "devices": "List of devices attached\nemulator-5554\tdevice\n",
        "-s emulator-5554 emu avd name": "qa_phone_api35\r\nOK\r\n",
        "-s emulator-5554 shell dumpsys account": "  Accounts: 0\n",
        INPUT_AGE_CALL: POWER_NEW,
    }
    settings = Android(sdk=SDK, avd_home=home)
    (instance,) = android.emulators(settings, run=FakeAdb(answers))["instances"]
    assert instance["facts"]["input_age"] == 10708
    # Without the accounts, the input age is left out too, unless the caller asks for it.
    adb = FakeAdb(answers)
    (instance,) = android.emulators(settings, run=adb, accounts=False)["instances"]
    assert "input_age" not in instance["facts"]
    assert ["-s", "emulator-5554", "shell", "cat /proc/uptime; dumpsys power"] not in adb.commands
    adb = FakeAdb(answers)
    (instance,) = android.emulators(settings, run=adb, accounts=False, input_age=True)[
        "instances"
    ]
    assert (instance["facts"]["input_age"], "accounts" in instance) == (10708, False)


def test_an_emulator_that_boots_or_does_not_answer_has_no_input_age(tmp_path):
    home = tmp_path / "avd"
    write_avd(home, "qa_phone_api35", PHONE)
    adb = FakeAdb(
        {
            "devices": "List of devices attached\nemulator-5554\toffline\n",
            "-s emulator-5554 emu avd name": "qa_phone_api35\r\nOK\r\n",
            INPUT_AGE_CALL: POWER_NEW,
        }
    )
    settings = Android(sdk=SDK, avd_home=home)
    (instance,) = android.emulators(settings, run=adb)["instances"]
    assert "input_age" not in instance["facts"]
    assert ["-s", "emulator-5554", "shell", "cat /proc/uptime; dumpsys power"] not in adb.commands
    adb.answers["devices"] = "List of devices attached\nemulator-5554\tdevice\n"
    del adb.answers[INPUT_AGE_CALL]
    (instance,) = android.emulators(settings, run=adb)["instances"]
    assert instance["facts"]["serial"] == "emulator-5554"
    assert "input_age" not in instance["facts"]
