"""Turn-based play against a simulated game: Start toggles a pause menu that shows "Game is paused"."""

from relaymcp.device import turns


class Game:
    def __init__(self, paused=False, menu_open=False, start_in_menu="closes"):
        self.paused, self.menu_open, self.presses, self.frames = paused, menu_open, [], 0
        self.physical, self.start_in_menu, self.regions = None, start_in_menu, []

    def press(self, buttons):
        self.presses.append(tuple(buttons))
        if "b" in buttons:
            self.menu_open = False
        if "start" in buttons:
            if self.menu_open:  # some menus close on the first press; Minecraft's crafting screen ignores it
                if self.start_in_menu == "closes":
                    self.menu_open = False
            else:
                self.paused = not self.paused

    def grab(self):
        self.frames += 1
        return ("menu" if self.paused else "world", self.frames)

    def sees(self, text, region=None):
        self.regions.append(region)
        return self.paused and text == "Game is paused"


def make(game, text="Game is paused"):
    t = turns.Turns(game.press, game.grab, game.sees, lambda: game.physical)
    t.configure({"button": "start", "text": text, "settle_ms": 0})
    return t


def test_the_game_runs_only_during_calls_and_screens_show_the_world(monkeypatch):
    monkeypatch.setattr(turns, "RECHECK_S", 0)
    game = Game(paused=True)
    t = make(game)
    assert t.paused and t.frozen() is None  # paused before we had a frame: nothing frozen to show yet
    t.begin()
    assert not game.paused and not t.paused
    t.end()
    assert game.paused and t.paused and t.frozen()[0] == "world"  # the frame from before the pause menu opened
    t.begin()
    assert t.frozen() is None and not game.paused


def test_overlapping_calls_pause_only_when_the_last_one_ends(monkeypatch):
    monkeypatch.setattr(turns, "RECHECK_S", 0)
    game = Game()
    t = make(game)
    t.begin()
    t.begin()  # a behavior started while an act runs
    t.end()
    assert not game.paused
    t.end()
    assert game.paused and len(game.presses) == 1


def test_the_screen_is_the_truth_when_someone_else_paused_or_resumed(monkeypatch):
    monkeypatch.setattr(turns, "RECHECK_S", 0)
    game = Game()
    t = make(game)
    t.begin()
    t.end()
    game.paused = False  # the user resumed it by hand
    t.begin()
    assert not game.paused and game.presses == [("start",)]  # no press: it was already running
    t.end()
    game.paused = True  # the game paused itself (lost focus)
    t.paused = False
    t.begin()
    assert not game.paused


def test_a_menu_that_eats_the_first_press_gets_a_second(monkeypatch):
    game = Game(menu_open=True)
    t = make(game)
    t.begin()
    t.end()
    assert game.paused and t.paused and len(game.presses) == 2


def test_never_pauses_under_the_users_hands():
    game = Game()
    t = make(game)
    t.begin()
    game.physical = "slot 0"
    t.end()
    assert not game.paused and "in use" in t.status()["note"]


def test_without_a_pause_text_it_toggles_by_assumption(monkeypatch):
    monkeypatch.setattr(turns, "SETTLE_S", 0)
    game = Game()
    t = make(game, text="")
    t.begin()
    t.end()
    assert game.paused and t.paused
    t.begin()
    assert not game.paused
    assert t.configure(None) is None and t.frozen() is None


def test_a_menu_that_ignores_the_pause_button_is_closed_first():
    game = Game(menu_open=True, start_in_menu="ignores")
    t = turns.Turns(game.press, game.grab, game.sees, lambda: None)
    t.configure({"button": "start", "text": "Game is paused", "close": "b", "region": [1100, 30, 1560, 110],
                 "settle_ms": 0})
    t.begin()
    t.end()
    assert game.paused and t.paused and game.presses == [("start",), ("b",), ("start",)]
    assert "closed a menu" in t.status()["note"]
    assert game.regions and all(r == [1100, 30, 1560, 110] for r in game.regions)  # only the pause text's box is read


def test_without_close_a_menu_that_ignores_pausing_is_reported():
    game = Game(menu_open=True, start_in_menu="ignores")
    t = make(game)
    t.begin()
    t.end()
    assert not game.paused and not t.paused and "may still be running" in t.status()["note"]


class FadingGame(Game):
    """Resuming fades the menu out: its text is unreadable before the menu is gone (then it may read again)."""

    def __init__(self):
        super().__init__(paused=True)
        self.reads = 0

    def sees(self, text, region=None):
        self.reads += 1
        if self.paused:
            return True
        return self.reads == 4  # gone on the first read after resuming, then one late read of the fading text


def test_resume_waits_until_the_menu_is_really_gone_and_the_screen_settles(monkeypatch):
    monkeypatch.setattr(turns, "RECHECK_S", 0)
    game, settled = FadingGame(), []
    t = turns.Turns(game.press, game.grab, game.sees, lambda: None, settle=lambda: settled.append(True))
    t.configure({"button": "start", "text": "Game is paused", "settle_ms": 0})
    t.begin()
    assert not t.paused and settled == [True] and not game.paused
    assert game.reads >= 6 and len(game.presses) == 1  # read past the flicker; never pressed resume twice


def test_the_world_counts_as_running_only_between_switches(monkeypatch):
    monkeypatch.setattr(turns, "RECHECK_S", 0)
    game = Game()
    seen = []
    t = turns.Turns(game.press, game.grab, lambda text, region=None: (seen.append(t.running()), game.sees(text))[1],
                    lambda: None)
    t.configure({"button": "start", "text": "Game is paused", "settle_ms": 0})
    seen.clear()
    t.begin()
    t.end()  # pausing: the menu fades in while the pause text isn't readable yet
    assert seen and not any(seen), seen  # never "running" while switching
    assert not t.running() and t.paused
    t.begin()
    assert t.running()



def test_a_dropped_resume_press_is_tried_again(monkeypatch):
    """Seen on the Ally: Start didn't take, and a program played into the pause menu for its whole run."""
    monkeypatch.setattr(turns, "RECHECK_S", 0)
    monkeypatch.setattr(turns, "WAIT_S", 0.2)
    game = Game(paused=True)
    real = game.press
    dropped = [1]

    def flaky(buttons):
        if dropped[0]:
            dropped[0] -= 1
            game.presses.append(("dropped",) + tuple(buttons))
            return
        real(buttons)
    t = turns.Turns(flaky, game.grab, game.sees, lambda: game.physical)
    t.configure({"button": "start", "text": "Game is paused", "settle_ms": 0})
    t.begin()
    assert not game.paused and not t.paused and t.note is None, (game.presses, t.note)



def test_a_death_screen_is_left_alone():
    """Pausing on a death screen opens a menu over it, and the next presses land in that menu."""
    game = Game()
    dead = [False]
    real_sees = game.sees
    game.sees = lambda text, region=None: dead[0] if text == "Respawn" else real_sees(text, region)
    t = turns.Turns(game.press, game.grab, game.sees, lambda: game.physical)
    t.configure({"button": "start", "text": "Game is paused", "settle_ms": 0, "dead": "Respawn"})
    t.begin()
    dead[0] = True
    t.end()
    assert game.presses == [] and not game.paused and "died" in t.note, (game.presses, t.note)
