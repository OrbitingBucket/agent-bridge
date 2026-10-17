"""Terminal tabs: one tab per agent, attached to a private view of the tmux session."""
import json
import re
import subprocess
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_bridge import cli, config, spawn, terminal, tmux  # noqa: E402


def fake_tmux(sessions):
    """A tmux stand-in with one agent window `relay:capital-prices` at pane %9 and the given live sessions."""
    calls = []

    def run(*args):
        calls.append(args)
        if args[0] == "display-message" and "#{session_name}:#{window_name}" in args:
            return subprocess.CompletedProcess(args, 0, "relay:capital-prices\n", "")
        if args[0] == "has-session":
            return subprocess.CompletedProcess(args, 0 if args[2] in sessions else 1, "", "")
        if args[0] == "new-session":
            sessions.add(args[args.index("-s") + 1])
        if args[0] == "kill-session":
            sessions.discard(args[2])
        return subprocess.CompletedProcess(args, 0, "", "")
    return calls, run


def fake_kitty(tabs, fail=False):
    calls = []

    def run(*args):
        calls.append(args)
        if args[0] == "ls":
            return subprocess.CompletedProcess(args, 0, json.dumps([{"tabs": [{"title": t} for t in tabs]}]), "")
        return subprocess.CompletedProcess(args, 1 if fail else 0, "", "kitty: no socket" if fail else "")
    return calls, run


class DetectTests(unittest.TestCase):
    def test_a_configured_terminal_wins_over_the_environment(self):
        self.assertEqual(terminal.detect(config.Config({"TERMINAL": "kitty"}), env={}), "kitty")
        self.assertEqual(terminal.detect(config.Config({"TERMINAL": "none"}), env={"KITTY_LISTEN_ON": "unix:/tmp/k"}), "none")
        self.assertEqual(terminal.detect(config.Config({"TERMINAL": " iTerm "}), env={}), "iterm")

    def test_auto_picks_the_terminal_running_the_spawner(self):
        auto = config.Config({"TERMINAL": "auto"})
        with mock.patch.object(terminal.shutil, "which", return_value="/usr/bin/kitty"):
            self.assertEqual(terminal.detect(auto, env={"KITTY_LISTEN_ON": "unix:/tmp/k"}), "kitty")
        with mock.patch.object(terminal.shutil, "which", return_value=None):
            self.assertEqual(terminal.detect(auto, env={"KITTY_LISTEN_ON": "unix:/tmp/k"}), "none")
        self.assertEqual(terminal.detect(auto, env={"TERM_PROGRAM": "iTerm.app"}), "iterm")
        self.assertEqual(terminal.detect(auto, env={}), "none")

    def test_auto_is_the_default(self):
        self.assertEqual(config.DEFAULTS["TERMINAL"], "auto")


class KittyTabTests(unittest.TestCase):
    def test_opens_a_tab_attached_to_a_private_view_of_the_agent_window(self):
        tcalls, trun = fake_tmux({"relay"})
        kcalls, krun = fake_kitty(tabs=["nvim"])
        with mock.patch.object(tmux, "_run", side_effect=trun), mock.patch.object(terminal, "_kitty", side_effect=krun), \
                mock.patch.object(tmux, "binary", return_value="/usr/bin/tmux"):
            self.assertTrue(terminal.open_tab("capital-prices", "%9", focus=True, terminal="kitty"))
        self.assertIn(("new-session", "-d", "-t", "relay", "-s", "view-capital-prices"), tcalls)
        self.assertIn(("select-window", "-t", "view-capital-prices:capital-prices"), tcalls)
        self.assertEqual(kcalls[-1], ("launch", "--type=tab", "--tab-title", "capital-prices",
                                      "/usr/bin/tmux", "attach-session", "-t", "view-capital-prices"))

    def test_an_existing_tab_is_focused_not_duplicated(self):
        tcalls, trun = fake_tmux({"relay", "view-capital-prices"})
        kcalls, krun = fake_kitty(tabs=["capital-prices"])
        with mock.patch.object(tmux, "_run", side_effect=trun), mock.patch.object(terminal, "_kitty", side_effect=krun):
            self.assertTrue(terminal.open_tab("capital-prices", "%9", focus=True, terminal="kitty"))
        self.assertFalse([c for c in tcalls if c[0] == "new-session"])
        self.assertIn(("select-window", "-t", "view-capital-prices:capital-prices"), tcalls)
        self.assertEqual([c[0] for c in kcalls], ["ls", "focus-tab"])
        self.assertEqual(kcalls[-1], ("focus-tab", "--match", "title:^" + re.escape("capital-prices") + "$"))

    def test_no_focus_keeps_the_human_where_they_are(self):
        _, trun = fake_tmux({"relay"})
        kcalls, krun = fake_kitty(tabs=[])
        with mock.patch.object(tmux, "_run", side_effect=trun), mock.patch.object(terminal, "_kitty", side_effect=krun):
            self.assertTrue(terminal.open_tab("capital-prices", "%9", focus=False, terminal="kitty"))
        self.assertIn("--keep-focus", kcalls[-1])
        kcalls, krun = fake_kitty(tabs=["capital-prices"])
        with mock.patch.object(tmux, "_run", side_effect=trun), mock.patch.object(terminal, "_kitty", side_effect=krun):
            terminal.open_tab("capital-prices", "%9", focus=False, terminal="kitty")
        self.assertEqual([c[0] for c in kcalls], ["ls"])

    def test_an_unreachable_terminal_is_a_note_not_a_failure(self):
        _, trun = fake_tmux({"relay"})
        kcalls, krun = fake_kitty(tabs=[], fail=True)
        with mock.patch.object(tmux, "_run", side_effect=trun), mock.patch.object(terminal, "_kitty", side_effect=krun), \
                mock.patch.object(terminal, "say") as say:
            self.assertFalse(terminal.open_tab("capital-prices", "%9", focus=True, terminal="kitty"))
        self.assertIn("kitty", say.call_args[0][0])

    def test_none_and_a_closed_window_open_nothing(self):
        tcalls, trun = fake_tmux({"relay"})
        kcalls, krun = fake_kitty(tabs=[])
        with mock.patch.object(tmux, "_run", side_effect=trun), mock.patch.object(terminal, "_kitty", side_effect=krun):
            self.assertFalse(terminal.open_tab("capital-prices", "%9", terminal="none"))
            with mock.patch.object(tmux, "window_target", return_value=""):
                self.assertFalse(terminal.open_tab("capital-prices", "%9", terminal="kitty"))
        self.assertEqual(tcalls, [])
        self.assertEqual(kcalls, [])


class CloseTabTests(unittest.TestCase):
    def test_close_kills_the_view_session_only_while_it_exists(self):
        tcalls, trun = fake_tmux({"relay", "view-capital-prices"})
        with mock.patch.object(tmux, "_run", side_effect=trun):
            self.assertTrue(terminal.close_tab("capital-prices"))
            self.assertFalse(terminal.close_tab("capital-prices"))
        self.assertEqual([c for c in tcalls if c[0] == "kill-session"], [("kill-session", "-t", "view-capital-prices")])


class SpawnHookTests(unittest.TestCase):
    def test_spawn_opens_the_tab_with_the_focus_setting(self):
        with mock.patch.object(terminal, "open_tab", return_value=True) as open_tab:
            spawn._open_tab("x", "%9", False)
        open_tab.assert_called_once_with("x", "%9", focus=False)

    def test_stop_and_a_failed_launch_close_the_tab(self):
        agent = types.SimpleNamespace(name="x", spawned_by="", pane_id="%9")
        with mock.patch.object(spawn.registry, "get", return_value=agent), mock.patch.object(spawn.registry, "remove"), \
                mock.patch.object(spawn.tmux, "pane_alive", return_value=False), mock.patch.object(spawn.events, "emit"), \
                mock.patch.object(spawn, "whoami", return_value=None), mock.patch.object(terminal, "close_tab") as close:
            spawn.stop("x", force=True)
        close.assert_called_once_with("x")
        with mock.patch.object(spawn.tmux, "kill"), mock.patch.object(spawn.registry, "release"), \
                mock.patch.object(terminal, "close_tab") as close:
            err = spawn._fail("y", "%9", None, "boom")
        self.assertIsInstance(err, spawn.SpawnError)
        close.assert_called_once_with("y")

    def test_gc_closes_the_tabs_of_dead_agents(self):
        dead = [types.SimpleNamespace(name="old", runtime="codex", pid=1)]
        with mock.patch.object(cli.registry, "gc", return_value=dead), mock.patch.object(cli.events, "emit"), \
                mock.patch.object(terminal, "close_tab") as close, mock.patch("sys.stdout"):
            cli.cmd_gc(types.SimpleNamespace(dry_run=False))
        close.assert_called_once_with("old")


if __name__ == "__main__":
    unittest.main()
