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

local function apply(player, cmd, dtime)
    if cmd.yaw then player:set_look_horizontal(math.rad(cmd.yaw)) end
    if cmd.pitch then player:set_look_vertical(math.rad(cmd.pitch)) end
    local v = player:get_velocity()
    local mv = cmd.move or { 0, 0 }
    player:add_velocity({ x = mv[1] - v.x, y = 0, z = mv[2] - v.z })
    if cmd.jump and math.abs(v.y) < 0.05 then player:add_velocity({ x = 0, y = 6.5, z = 0 }) end
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
        if cmd.slot then player:set_wield_index(cmd.slot) end
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
        if ok and type(cmd) == "table" then apply(player, cmd, dtime) end
    end
    if acc < 0.05 then return end
    acc, tick = 0, tick + 1
    local p, v = player:get_pos(), player:get_velocity()
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
        tod = minetest.get_timeofday(), dig = dig.pos and dig.t or 0,
    }) .. "\n")
    out:flush()
end)
