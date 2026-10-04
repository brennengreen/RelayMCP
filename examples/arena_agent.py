"""A starting point for the Spinal Score: a plain reactive agent with no model and no memory. Beat it, then beat Spinal.

    spinal arena run --agent plugin:examples/arena_agent.py:Agent --maps MAP01      # a quick try (unranked)
    spinal arena run --board reflex --agent plugin:examples/arena_agent.py:Agent --name "My agent"

Every tic, `state` (a dict) has: health, ammo, kills, secrets, x, y, angle (degrees), damage (taken), dealt; the
monsters and items on screen, nearest first (name, distance, bearing in degrees with left positive, x, y, id; items
also have a kind: health, ammo, weapon or armor); screen (the pixels, 3 x H x W); and layout (the level's sectors:
floor_height, ceiling_height, and lines with x1, y1, x2, y2, is_blocking). Nothing behind a wall: remember it yourself.
"""


class Agent:
    def __init__(self):
        self.last, self.stuck = None, 0

    def act(self, state, tic):
        """-> [attack, speed, forward, back, left, right, turn (degrees, left positive), use]"""
        pos = (round(state["x"]), round(state["y"]))
        self.stuck = self.stuck + 1 if pos == self.last else 0
        self.last = pos
        if state["monsters"]:  # aim at the nearest, shoot when it's in the crosshair, keep strafing
            m = state["monsters"][0]
            side = tic // 30 % 2
            return [int(abs(m["bearing"]) < 4), 1, 0, 0, side, 1 - side, max(-10.0, min(10.0, m["bearing"] * 0.5)), 0]
        if self.stuck > 8:  # a wall or a door: try to open it, back off and turn
            return [0, 1, 0, 1, 0, 0, 12.0, 1]
        return [0, 1, 1, 0, 0, 0, 0.0, int(tic % 20 == 0)]
