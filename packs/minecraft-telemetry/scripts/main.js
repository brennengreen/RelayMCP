// RelayMCP Telemetry: the player's exact pose every tick (20 Hz) and nearby mobs every other tick, as one
// "RELAY {json}" line in the content log. Yaw/pitch are the game's (yaw 0 = south/+z, 90 = west; pitch +90 = down).
// Commands for benchmarks (typed as chat commands between trials): /scriptevent relay:<name> {json}
//   relay:tp {"x","y","z","yaw","pitch"}      exact start pose        relay:fill {"from","to","block"}  reset an area
//   relay:summon {"type","at":[x,y,z]}         a target                relay:clear {"radius"}           remove mobs
//   relay:blocks {"from":[x,y,z],"to":[x,y,z]} non-air blocks in a region (a build's check), at most 4096 cells
// Each replies with a "RELAY {"reply": name, ...}" line.
import { world, system } from "@minecraft/server";

const R = (v, d = 3) => Math.round(v * 10 ** d) / 10 ** d;

function pose(p) {
  const l = p.location, r = p.getRotation(), v = p.getVelocity(), h = p.getHeadLocation();
  const out = { t: Date.now(), tick: system.currentTick, x: R(l.x), y: R(l.y), z: R(l.z), ey: R(h.y),
                yaw: R(r.y, 2), pitch: R(r.x, 2), vx: R(v.x), vy: R(v.y), vz: R(v.z),
                ground: p.isOnGround, sneak: p.isSneaking, slot: p.selectedSlotIndex };
  try {
    const hit = p.getBlockFromViewDirection({ maxDistance: 7 });
    if (hit) {
      const b = hit.block.location;
      out.look = [b.x, b.y, b.z, hit.face, hit.block.typeId.replace("minecraft:", "")];
    }
  } catch (e) {}
  try {
    const hp = p.getComponent("minecraft:health");
    if (hp) out.hp = hp.currentValue;
  } catch (e) {}
  return out;
}

function mobs(p) {
  const out = [];
  const near = p.dimension.getEntities({ location: p.location, maxDistance: 32,
                                         excludeTypes: ["minecraft:player", "minecraft:item", "minecraft:xp_orb"] });
  for (const e of near) {
    const l = e.location, h = e.getHeadLocation();
    out.push([e.typeId.replace("minecraft:", ""), R(l.x, 2), R(l.y, 2), R(l.z, 2), R(h.y, 2), e.id]);
    if (out.length >= 24) break;
  }
  return out;
}

system.runInterval(() => {
  const p = world.getAllPlayers()[0];
  if (!p) return;
  const msg = pose(p);
  if (system.currentTick % 2 === 0) msg.mobs = mobs(p);
  console.log("RELAY " + JSON.stringify(msg));
}, 1);

function reply(id, data) {
  console.log("RELAY " + JSON.stringify(Object.assign({ reply: id.replace("relay:", ""), t: Date.now() }, data)));
}

function box(a, b) {
  return [Math.min(a[0], b[0]), Math.min(a[1], b[1]), Math.min(a[2], b[2]),
          Math.max(a[0], b[0]), Math.max(a[1], b[1]), Math.max(a[2], b[2])];
}

system.afterEvents.scriptEventReceive.subscribe((ev) => {
  if (!ev.id.startsWith("relay:")) return;
  let a = {};
  try {
    a = ev.message && ev.message.trim() ? JSON.parse(ev.message) : {};
  } catch (e) {
    reply(ev.id, { error: "arguments are not JSON" });
    return;
  }
  const p = world.getAllPlayers()[0];
  const dim = p ? p.dimension : world.getDimension("overworld");
  try {
    if (ev.id === "relay:tp") {
      p.teleport({ x: a.x, y: a.y, z: a.z }, { rotation: { x: a.pitch || 0, y: a.yaw || 0 } });
      reply(ev.id, { ok: true });
    } else if (ev.id === "relay:fill") {
      const [x1, y1, z1, x2, y2, z2] = box(a.from, a.to);
      dim.runCommand(`fill ${x1} ${y1} ${z1} ${x2} ${y2} ${z2} ${a.block || "air"}`);
      reply(ev.id, { ok: true });
    } else if (ev.id === "relay:summon") {
      const e = dim.spawnEntity(a.type.includes(":") ? a.type : "minecraft:" + a.type,
                                { x: a.at[0], y: a.at[1], z: a.at[2] });
      reply(ev.id, { ok: true, id: e.id });
    } else if (ev.id === "relay:clear") {
      let n = 0;
      for (const e of dim.getEntities({ location: p.location, maxDistance: a.radius || 32,
                                        excludeTypes: ["minecraft:player"] })) {
        e.remove();
        n++;
      }
      reply(ev.id, { ok: true, removed: n });
    } else if (ev.id === "relay:blocks") {
      const [x1, y1, z1, x2, y2, z2] = box(a.from, a.to);
      if ((x2 - x1 + 1) * (y2 - y1 + 1) * (z2 - z1 + 1) > 4096) {
        reply(ev.id, { error: "region over 4096 cells" });
        return;
      }
      const found = [];
      for (let x = x1; x <= x2; x++)
        for (let y = y1; y <= y2; y++)
          for (let z = z1; z <= z2; z++) {
            const b = dim.getBlock({ x, y, z });
            if (b && !b.isAir) found.push([x, y, z, b.typeId.replace("minecraft:", "")]);
          }
      reply(ev.id, { blocks: found });
    } else {
      reply(ev.id, { error: "unknown command" });
    }
  } catch (e) {
    reply(ev.id, { error: String(e) });
  }
});
