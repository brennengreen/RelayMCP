-- RelayMCP test bed for Luanti (Minecraft-like games such as Mineclonia): every 0.05 s it writes the player's state as
-- one JSON line to relay_telemetry.jsonl in the world folder, and applies the agent's controls from relay_cmd.json
-- (written by the agent, replaced whole): look (yaw, pitch in degrees), a horizontal velocity, jump, dig (held; a
-- block breaks after the game's dig time for the wielded tool), attack, place and hotbar slot (one-shots, by seq).
local wp = minetest.get_worldpath()
local out = io.open(wp .. "/relay_telemetry.jsonl", "w")
local acc, tick, last_seq = 0, 0, -1
local dig = { pos = nil, t = 0 }
local ids, next_id = setmetatable({}, { __mode = "k" }), 1
local trees, tree_tick = {}, -100
local deaths = 0
minetest.register_on_dieplayer(function(player)
    deaths = deaths + 1
    minetest.after(2, function() if player:get_hp() == 0 then pcall(function() player:respawn() end) end end)
end)

local function hotbar(player)
    local out = {}
    for i, st in ipairs(player:get_inventory():get_list("main") or {}) do
        if i > 9 then break end
        if not st:is_empty() then out[#out + 1] = { i, st:get_name(), st:get_count() } end
    end
    return out
end

local function oid(obj)
    local ref = obj:get_luaentity() or obj
    if not ids[ref] then ids[ref], next_id = next_id, next_id + 1 end
    return ids[ref]
end

local function eye(player)
    local p = player:get_pos()
    return vector.add(p, { x = 0, y = player:get_properties().eye_height or 1.5, z = 0 })
end

local function pointed(player, objects)
    local e = eye(player)
    local ray = minetest.raycast(e, vector.add(e, vector.multiply(player:get_look_dir(), 5)), objects, false)
    for pt in ray do
        if pt.type == "object" and pt.ref ~= player then return pt end
        if pt.type == "node" then return pt end
    end
end

local function caps(player)
    -- an empty hand digs and punches with the game's hand item (its "hand" list), not the engine's default
    local w = player:get_wielded_item()
    local def = w:get_definition()
    if w:is_empty() or not (def and def.tool_capabilities) then  -- a block or item held digs like the hand
        local hand = player:get_inventory():get_stack("hand", 1)
        if not hand:is_empty() then return hand:get_tool_capabilities() end
    end
    return w:get_tool_capabilities()
end

local function inventory(player)
    local counts = {}
    for _, st in ipairs(player:get_inventory():get_list("main") or {}) do
        if not st:is_empty() then counts[st:get_name()] = (counts[st:get_name()] or 0) + st:get_count() end
    end
    return counts
end

local body = { vy = 0, vx = 0, vz = 0, set = false }

local function solid(x, y, z)
    local n = minetest.get_node_or_nil({ x = math.floor(x + 0.5), y = math.floor(y + 0.5), z = math.floor(z + 0.5) })
    if not n then return true end
    local d = minetest.registered_nodes[n.name]
    return d == nil or d.walkable ~= false
end

local function blocked(x, feet, z)  -- the body's feet and head cells
    return solid(x, feet + 0.3, z) or solid(x, feet + 1.3, z)
end

local function move_body(player, mv, dtime)
    -- The server moves the agent's body (walking, collisions, one-block steps, falling): the client's own keys and
    -- physics are off, so a focused game window (its mouse and keyboard) can't fight the agent's controls.
    if not body.set then
        player:set_physics_override({ speed = 0, jump = 0, gravity = 0, sneak = false })
        body.set = true
    end
    local p = player:get_pos()
    local ground = solid(p.x, p.y - 0.1, p.z)
    body.vy = ground and 0 or math.max(-20, body.vy - 9.81 * dtime)
    local nx, nz, ny = p.x + mv[1] * dtime, p.z + mv[2] * dtime, p.y + body.vy * dtime
    local px = nx + (mv[1] > 0.01 and 0.3 or (mv[1] < -0.01 and -0.3 or 0))
    local pz = nz + (mv[2] > 0.01 and 0.3 or (mv[2] < -0.01 and -0.3 or 0))
    if (mv[1] ~= 0 or mv[2] ~= 0) and blocked(px, p.y, pz) then
        if ground and not blocked(px, p.y + 1, pz) and not solid(p.x, p.y + 2.3, p.z) then
            ny = p.y + 1  -- step up onto the block (Minecraft's jump-and-walk)
        else
            nx, nz = p.x, p.z
        end
    end
    if body.vy < 0 and solid(nx, ny, nz) then  -- land on the block below
        ny = math.floor(ny + 0.5) + 0.5
        body.vy = 0
    end
    body.vx, body.vz = (nx - p.x) / math.max(dtime, 1e-3), (nz - p.z) / math.max(dtime, 1e-3)
    if nx ~= p.x or ny ~= p.y or nz ~= p.z then player:move_to({ x = nx, y = ny, z = nz }, true) end
end

local function apply(player, cmd, dtime)
    if cmd.yaw then player:set_look_horizontal(math.rad(cmd.yaw)) end
    if cmd.pitch then player:set_look_vertical(math.rad(cmd.pitch)) end
    move_body(player, cmd.move or { 0, 0 }, dtime)
    if cmd.dig then
        local pt = pointed(player, false)
        if pt and pt.type == "node" then
            if not dig.pos or not vector.equals(dig.pos, pt.under) then dig.pos, dig.t = pt.under, 0 end
            dig.t = dig.t + dtime
            local node = minetest.get_node(pt.under)
            local def = minetest.registered_nodes[node.name]
            local params = minetest.get_dig_params(def and def.groups or {}, caps(player))
            if params.diggable and dig.t >= params.time then
                local on_dig = def and def.on_dig or minetest.node_dig  -- the game's own (drops, tool wear)
                on_dig(pt.under, node, player)
                dig.pos = nil
            end
        end
    else
        dig.pos = nil
    end
    if cmd.seq and cmd.seq ~= last_seq then
        last_seq = cmd.seq
        if cmd.say then minetest.chat_send_all("<agent> " .. tostring(cmd.say)) end
        if cmd.seal then  -- put the wielded block into the cell above the head (a roof over a dug-in hole)
            local p = vector.round(player:get_pos())
            local above = { x = p.x, y = p.y + 2, z = p.z }
            if minetest.get_node(above).name == "air" then
                local st = player:get_wielded_item()
                local left = minetest.item_place_node(st, player, { type = "node", under = { x = p.x + 1, y = p.y + 2, z = p.z }, above = above })
                if left then player:set_wielded_item(left) end
            end
        end
        if cmd.slot then  -- bring that hotbar stack into the held slot (no set_wield_index in this engine)
            local inv, wi = player:get_inventory(), player:get_wield_index()
            if cmd.slot ~= wi then
                local held, want = inv:get_stack("main", wi), inv:get_stack("main", cmd.slot)
                inv:set_stack("main", wi, want)
                inv:set_stack("main", cmd.slot, held)
            end
        end
        if cmd.attack then
            local pt = pointed(player, true)
            if pt and pt.type == "object" then
                pt.ref:punch(player, 1.0, caps(player), player:get_look_dir())
            end
        end
        if cmd.place then
            local pt = pointed(player, false)
            if pt and pt.type == "node" then
                local left = minetest.item_place(player:get_wielded_item(), player, pt)
                if left then player:set_wielded_item(left) end
            end
        end
    end
end

minetest.register_globalstep(function(dtime)
    acc = acc + dtime
    local player = minetest.get_connected_players()[1]
    if not player then return end
    local f = io.open(wp .. "/relay_cmd.json", "r")
    if f then
        local ok, cmd = pcall(minetest.parse_json, f:read("*a"))
        f:close()
        if ok and type(cmd) == "table" then
            local fine, err = pcall(apply, player, cmd, dtime)  -- a bad command is logged, never crashes the game
            if not fine then minetest.log("error", "[relay] " .. tostring(err)) end
        end
    end
    if acc < 0.05 then return end
    acc, tick = 0, tick + 1
    local p, v = player:get_pos(), { x = body.vx, y = body.vy, z = body.vz }
    local mobs, drops = {}, {}
    for _, obj in ipairs(minetest.get_objects_inside_radius(p, 24)) do
        local ent = obj:get_luaentity()
        if ent and ent.name then
            local q = obj:get_pos()
            if ent.name == "__builtin:item" then
                drops[#drops + 1] = { ent.itemstring or "", q.x, q.y, q.z }
            elseif obj ~= player and not ent.name:find("wieldview") then
                mobs[#mobs + 1] = { ent.name, q.x, q.y, q.z, obj:get_hp(), oid(obj) }
            end
        end
    end
    if tick - tree_tick >= 10 then  -- trunks near the player, twice a second
        tree_tick, trees = tick, {}
        local found = minetest.find_nodes_in_area(vector.add(p, { x = -32, y = -6, z = -32 }),
            vector.add(p, { x = 32, y = 10, z = 32 }), { "group:tree" })
        table.sort(found, function(a, b) return vector.distance(a, p) < vector.distance(b, p) end)
        for i = 1, math.min(12, #found) do trees[i] = { found[i].x, found[i].y, found[i].z } end
    end
    local look = nil
    local pt = pointed(player, false)
    if pt and pt.type == "node" then
        look = { pt.under.x, pt.under.y, pt.under.z, minetest.get_node(pt.under).name }
    end
    out:write(minetest.write_json({
        tick = tick, t = minetest.get_us_time() / 1000, x = p.x, y = p.y, z = p.z, vx = v.x, vy = v.y, vz = v.z,
        yaw = math.deg(player:get_look_horizontal()), pitch = math.deg(player:get_look_vertical()),
        hp = player:get_hp(), slot = player:get_wield_index(), wield = player:get_wielded_item():get_name(),
        inv = inventory(player), look = look, mobs = mobs, drops = drops, trees = trees,
        tod = minetest.get_timeofday(), dig = dig.pos and dig.t or 0, deaths = deaths, hotbar = hotbar(player),
    }) .. "\n")
    out:flush()
end)
