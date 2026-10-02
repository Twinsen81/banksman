import os
import pwd
from pathlib import Path

import pytest
from helpers import write_config

from banksman import config
from banksman.config import (
    DEFAULT_AGENTS,
    MAX_COUNT,
    Android,
    Config,
    ConfigError,
    Kind,
    default_config_path,
    load_config,
    parse_duration,
)
from banksman.lease import HARD_CAP, Holder, Timeouts
from banksman.store import RELEASED

OWNER = "/work/tree-a"


def test_without_a_file_there_are_no_kinds_and_the_default_timeouts():
    assert load_config() == Config()
    assert load_config().defaults == Timeouts(
        boot_timeout=5 * 60, owner_grace=5 * 60, idle_timeout=20 * 60, hard_cap=3 * 60 * 60
    )


def test_an_empty_file_is_the_default_configuration(config_path):
    write_config(config_path, "")
    assert load_config() == Config()


def test_the_path_does_not_depend_on_the_environment(monkeypatch):
    monkeypatch.delenv("BANKSMAN_CONFIG")
    monkeypatch.setenv("HOME", "/somewhere/else")
    monkeypatch.setenv("XDG_CONFIG_HOME", "/somewhere/else/.config")
    home = pwd.getpwuid(os.geteuid()).pw_dir
    assert default_config_path() == Path(home) / ".config" / "banksman" / "config.toml"


def test_the_defaults_apply_to_every_kind_and_a_kind_can_set_its_own(config_path):
    write_config(
        config_path,
        """
        [defaults]
        boot_timeout = "8m"
        hard_cap = "2h"

        [kinds.emulator]

        [kinds.build]
        count = 4
        owner_grace = "0s"
        idle_timeout = "off"
        hard_cap = "90m"
        """,
    )
    config = load_config()
    assert config.defaults == Timeouts(boot_timeout=8 * 60, hard_cap=2 * 60 * 60)
    assert config.kinds["emulator"] == Kind("emulator", timeouts=config.defaults)
    assert config.kinds["build"].timeouts == Timeouts(
        boot_timeout=8 * 60, owner_grace=0, idle_timeout=None, hard_cap=90 * 60
    )


def test_a_counted_kind_has_numbered_instances_and_a_named_kind_has_none(config_path):
    write_config(config_path, "[kinds.build]\ncount = 3\n[kinds.device]\n")
    kinds = load_config().kinds
    assert kinds["build"].instances() == ("build-0", "build-1", "build-2")
    assert kinds["device"].count is None
    assert kinds["device"].instances() == ()


def test_a_kind_can_list_its_instances_by_name(config_path, store):
    write_config(config_path, '[kinds.port]\ninstances = ["9101", "9102", "9103"]\n')
    port = load_config().kinds["port"]
    assert (port.count, port.instances()) == (None, ("9101", "9102", "9103"))
    lease = store.acquire(port.instances()[1], port.name, Holder(OWNER), timeouts=port.timeouts)
    assert (lease.resource, lease.kind) == ("9102", "port")


def test_stopping_is_off_unless_a_kind_turns_it_on(config_path):
    write_config(config_path, "[kinds.emulator]\nstop = true\n[kinds.device]\n")
    kinds = load_config().kinds
    assert (kinds["emulator"].stop, kinds["device"].stop) == (True, False)


def test_the_drain_timeout_is_a_timeout_like_the_others(config_path, store):
    write_config(
        config_path,
        '[defaults]\ndrain_timeout = "2m"\n'
        '[kinds.device]\n'
        '[kinds.emulator]\ndrain_timeout = "90s"\n',
    )
    kinds = load_config().kinds
    assert kinds["device"].timeouts.drain_timeout == 120
    assert kinds["emulator"].timeouts.drain_timeout == 90
    lease = store.acquire("emu-1", "emulator", Holder(OWNER), timeouts=kinds["emulator"].timeouts)
    assert lease.drain_timeout == 90


def test_on_void_is_a_program_and_its_arguments(config_path):
    write_config(config_path, '[kinds.emulator]\non_void = ["/opt/bin/stop-avd", "--now"]\n')
    assert load_config().kinds["emulator"].on_void == ("/opt/bin/stop-avd", "--now")


def test_a_new_counted_kind_needs_only_configuration(config_path, store, system):
    write_config(config_path, '[kinds.port]\ncount = 2\nidle_timeout = "off"\nhard_cap = "1h"\n')
    port = load_config().kinds["port"]
    lease = store.acquire(port.instances()[1], port.name, Holder(OWNER), timeouts=port.timeouts)
    assert (lease.resource, lease.idle_timeout) == ("port-1", None)
    system.advance(60 * 60 - 1)
    assert store.reap() == []
    system.advance(1)
    assert [(item.lease.void_reason, item.outcome) for item in store.reap()] == [
        (HARD_CAP, RELEASED)
    ]


def test_the_holder_rules_have_defaults_and_can_be_changed(config_path):
    assert load_config().holder.agents == DEFAULT_AGENTS == ("claude", "codex")
    assert load_config().holder.issue_pattern.search("xyz-183-paired").group(0) == "xyz-183"
    write_config(
        config_path, '[holder]\nagents = ["claude", "gemini"]\nissue_pattern = "#([0-9]+)"\n'
    )
    holder = load_config().holder
    assert holder.agents == ("claude", "gemini")
    assert holder.issue_pattern.pattern == "#([0-9]+)"


def test_a_kind_can_get_its_instances_from_discovery(config_path):
    write_config(
        config_path,
        "[kinds.emulator]\n"
        'preset = "android-emulator"\n'
        'preselect = ["qa_*"]\n'
        "[kinds.lab]\n"
        'discover = ["/usr/local/bin/find-lab-devices", "--json"]\n'
        "[accounts]\n"
        'preselect = ["*@example.test"]\n'
        "[android]\n"
        'sdk = "/opt/android-sdk"\n',
    )
    loaded = load_config()
    emulator, lab = loaded.kinds["emulator"], loaded.kinds["lab"]
    assert (emulator.preset, emulator.preselect, emulator.discovered) == (
        "android-emulator",
        ("qa_*",),
        True,
    )
    assert lab.discover == ("/usr/local/bin/find-lab-devices", "--json")
    assert lab.instances() == ()
    assert loaded.account_preselect == ("*@example.test",)
    assert loaded.android == Android(sdk=Path("/opt/android-sdk"))
    assert not Kind("build", count=2).discovered


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("90s", 90), ("20m", 20 * 60), ("3h", 3 * 60 * 60), ("0s", 0), ("1440m", 24 * 60 * 60)],
)
def test_durations(text, seconds):
    assert parse_duration(text) == seconds


@pytest.mark.parametrize(
    "text", ["", "20", "m", "-5m", "1.5h", "20 m", " 20m", "20M", "1h30m", "2d", "12345678s"]
)
def test_text_that_is_not_a_duration_is_refused(text):
    with pytest.raises(ValueError, match="not a duration"):
        parse_duration(text)


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("[timeouts]\nhard_cap = '3h'", "timeouts"),
        ("kinds = 3", "kinds"),
        ("[defaults]\ncount = 4", "defaults.count"),
        ("[defaults]\nhard_cap = 3", "defaults.hard_cap"),
        ("[defaults]\nhard_cap = 'off'", "defaults.hard_cap"),
        ("[defaults]\nboot_timeout = '0m'", "defaults.boot_timeout"),
        ("[defaults]\nidle_timeout = '0s'", "defaults.idle_timeout"),
        ("[defaults]\nidle_timeout = 'never'", "defaults.idle_timeout"),
        ("[kinds.build]\ncuont = 4", "kinds.build.cuont"),
        ("[kinds.Build]", "kinds.Build"),
        ("[kinds.'build slots']", "kinds.build slots"),
        ("[kinds]\nbuild = 4", "kinds.build"),
        ("[kinds.build]\ncount = 0", "kinds.build.count"),
        ("[kinds.build]\ncount = '4'", "kinds.build.count"),
        ("[kinds.build]\ncount = 4.0", "kinds.build.count"),
        ("[kinds.build]\ncount = true", "kinds.build.count"),
        (f"[kinds.build]\ncount = {MAX_COUNT + 1}", "kinds.build.count"),
        ("[kinds.build]\nowner_grace = '-1s'", "kinds.build.owner_grace"),
        ("[kinds.port]\ninstances = []", "kinds.port.instances"),
        ("[kinds.port]\ninstances = '9101'", "kinds.port.instances"),
        ("[kinds.port]\ninstances = [9101]", "kinds.port.instances"),
        ("[kinds.port]\ninstances = ['../9101']", "kinds.port.instances"),
        ("[kinds.port]\ninstances = ['91 01']", "kinds.port.instances"),
        ("[kinds.port]\ninstances = ['9101', '9102', '9101']", "kinds.port.instances"),
        (
            "[kinds.port]\ninstances = [" + ", ".join(f"'{n}'" for n in range(MAX_COUNT + 1)) + "]",
            "kinds.port.instances",
        ),
        ("[kinds.port]\ncount = 2\ninstances = ['9101']", "kinds.port.instances"),
        ("[kinds.build]\ncount = 2\n[kinds.port]\ninstances = ['build-1']", "kinds.port.instances"),
        ("[kinds.port]\ninstances = ['build-1']\n[kinds.build]\ncount = 2", "kinds.build.count"),
        (
            "[kinds.port]\ninstances = ['9101']\n[kinds.web]\ninstances = ['9101']",
            "kinds.web.instances",
        ),
        ("[kinds.emulator]\non_void = 'stop-avd'", "kinds.emulator.on_void"),
        ("[kinds.emulator]\non_void = []", "kinds.emulator.on_void"),
        ("[kinds.emulator]\non_void = ['', 'x']", "kinds.emulator.on_void"),
        ("[kinds.emulator]\non_void = ['stop-avd', 3]", "kinds.emulator.on_void"),
        ('[kinds.emulator]\non_void = ["stop\\u0000avd"]', "kinds.emulator.on_void"),
        ("[kinds.emulator]\non_void = ['stop-avd']", "kinds.emulator.on_void"),
        ("[kinds.emulator]\non_void = ['bin/stop-avd']", "kinds.emulator.on_void"),
        ("[kinds.emulator]\non_void = ['~/bin/stop-avd']", "kinds.emulator.on_void"),
        ("[kinds.emulator]\non_void = ['$HOME/bin/stop-avd']", "kinds.emulator.on_void"),
        ("[kinds.emulator]\nstop = 'yes'", "kinds.emulator.stop"),
        ("[kinds.emulator]\nstop = 1", "kinds.emulator.stop"),
        ("[defaults]\nstop = true", "defaults.stop"),
        ("[defaults]\ndrain_timeout = '0s'", "defaults.drain_timeout"),
        ("[kinds.emulator]\ndrain_timeout = 'off'", "kinds.emulator.drain_timeout"),
        ("[holder]\nagent = ['claude']", "holder.agent"),
        ("[holder]\nagents = 'claude'", "holder.agents"),
        ("[holder]\nagents = ['/usr/bin/claude']", "holder.agents"),
        ("[holder]\nissue_pattern = '('", "holder.issue_pattern"),
        ("[holder]\nissue_pattern = ''", "holder.issue_pattern"),
        ("[kinds.emulator]\npreset = 'ios-simulator'", "kinds.emulator.preset"),
        ("[kinds.emulator]\npreset = 'android-emulator'\ncount = 2", "kinds.emulator.preset"),
        ("[kinds.lab]\ndiscover = ['find-lab-devices']", "kinds.lab.discover"),
        ("[kinds.lab]\ninstances = ['a']\ndiscover = ['/bin/x']", "kinds.lab.discover"),
        ("[kinds.port]\ninstances = ['9101']\npreselect = ['9*']", "kinds.port.preselect"),
        ("[kinds.lab]\ndiscover = ['/bin/x']\npreselect = []", "kinds.lab.preselect"),
        ("[kinds.lab]\ndiscover = ['/bin/x']\npreselect = 'qa_*'", "kinds.lab.preselect"),
        ('[accounts]\npreselect = ["qa\\u001b@example.test"]', "accounts.preselect"),
        ("[accounts]\npattern = ['*']", "accounts.pattern"),
        ("[android]\nsdk = 'Library/Android/sdk'", "android.sdk"),
        ("[android]\nadb = '/opt/adb'", "android.adb"),
    ],
)
def test_a_configuration_that_is_not_valid_is_refused(config_path, text, key):
    write_config(config_path, text)
    with pytest.raises(ConfigError) as exc:
        load_config()
    assert str(exc.value).startswith(f"{config_path}: {key}: ")


def test_a_file_that_is_not_toml_is_refused(config_path):
    write_config(config_path, "[kinds.build\ncount = 4\n")
    with pytest.raises(ConfigError, match=r"line 1"):
        load_config()


def test_a_file_that_others_can_write_to_is_refused(config_path):
    write_config(config_path, "").chmod(0o666)
    with pytest.raises(ConfigError, match="can write"):
        load_config()


def test_a_file_of_root_is_accepted(config_path):
    write_config(config_path, "")
    info = os.stat(config_path)
    as_root = os.stat_result((info.st_mode, *info[1:4], 0, *info[5:]))
    assert as_root.st_uid == 0
    config._check_file(config_path, as_root)


def test_a_file_of_another_user_is_refused(config_path, monkeypatch):
    write_config(config_path, "")
    monkeypatch.setattr(os, "geteuid", lambda: os.getuid() + 1)
    with pytest.raises(ConfigError, match="another user"):
        load_config()


def test_a_path_that_is_not_a_regular_file_is_refused(config_path):
    config_path.mkdir()
    with pytest.raises(ConfigError, match="not a regular file"):
        load_config()


def test_a_fifo_is_refused_without_waiting_for_a_writer(config_path):
    os.mkfifo(config_path, 0o600)
    with pytest.raises(ConfigError, match="not a regular file"):
        load_config()
