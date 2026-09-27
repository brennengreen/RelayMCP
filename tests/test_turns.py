"""Turn-based play against a simulated game: Start toggles a pause menu that shows "Game is paused"."""

from relaymcp.device import turns


class Game:
    def __init__(self, paused=False, menu_open=False):
        self.paused, self.menu_open, self.presses, self.frames = paused, menu_open, [], 0
        self.physical = None

    def press(self, buttons):
        self.presses.append(tuple(buttons))
        if "start" in buttons:
            if self.menu_open:  # like Minecraft's inventory: the first press only closes it
                self.menu_open = False
            else:
                self.paused = not self.paused

    def grab(self):
        self.frames += 1
        return ("menu" if self.paused else "world", self.frames)

    def sees(self, text):
        return self.paused and text == "Game is paused"


def make(game, text="Game is paused"):
    t = turns.Turns(game.press, game.grab, game.sees, lambda: game.physical)
    t.configure({"button": "start", "text": text, "settle_ms": 0})
    return t


def test_the_game_runs_only_during_calls_and_screens_show_the_world(monkeypatch):
    monkeypatch.setattr(turns, "FADE_S", 0)
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
    monkeypatch.setattr(turns, "FADE_S", 0)
    game = Game()
    t = make(game)
    t.begin()
    t.begin()  # a behavior started while an act runs
    t.end()
    assert not game.paused
    t.end()
    assert game.paused and len(game.presses) == 1


def test_the_screen_is_the_truth_when_someone_else_paused_or_resumed(monkeypatch):
    monkeypatch.setattr(turns, "FADE_S", 0)
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
