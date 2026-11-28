-- PoE2 Keyword-Free Full-Slot Unique Ranking Kernel (including Flask 1..2 and Charm 1..3)
-- + Sanctified / Vaal 0.78x..1.22x roll-range upside evaluation (ItemsTab.lua:2836).
local H = require("pob_host")
local now_ms, get_build = H.now_ms, H.get_build
local M = {}

local function primary_dps(out)
	if (out.FullDPS or 0) > 0 then return out.FullDPS, "FullDPS" end
	return out.CombinedDPS or out.TotalDPS or 0, "CombinedDPS"
end

local function is_item_valid_for_slot_poe2(items_tab, item, slot_name)
	if not item or not item.base then return false end
	if slot_name:match("^Flask") then
		return item.base.flask ~= nil and item.base.charm == nil
	end
	if slot_name:match("^Charm") then
		return item.base.charm ~= nil
	end
	if type(items_tab.IsItemValidForSlot) == "function" then
		local ok, valid = pcall(items_tab.IsItemValidForSlot, items_tab, item, slot_name)
		if ok then return valid and true or false end
	end
	return false
end

local function build_corrupted_roll_variant(item, scalar)
	if type(item) ~= "table" or type(item.raw) ~= "string" then return nil end
	local ok, clone = pcall(function()
		local c = new("Item", item.raw)
		if type(c.explicitModLines) == "table" then
			for _, ml in ipairs(c.explicitModLines) do
				ml.range = (scalar >= 1.0) and 1.0 or 0.0
				ml.corruptedRange = scalar
			end
		end
		if type(c.BuildAndParseRaw) == "function" then
			c:BuildAndParseRaw()
		elseif type(c.BuildModList) == "function" then
			c:BuildModList()
		end
		return c
	end)
	if ok and type(clone) == "table" and clone.base then return clone end
	return nil
end

local function eval_unique(calc_func, item, slot_name, use_full_dps)
	local arg
	if item.base and item.base.flask then
		arg = { toggleFlask = item }
	elseif item.base and item.base.charm then
		arg = { toggleCharm = item }
	else
		arg = { repSlotName = slot_name, repItem = item }
	end
	local ok, out = pcall(calc_func, arg, use_full_dps)
	if not ok or type(out) ~= "table" then return nil end
	return out
end

local function handle_rank_uniques_slot(params)
	params = type(params) == "table" and params or {}
	local build, t0 = get_build(), now_ms()
	local items_tab = build.itemsTab
	local slot_name = tostring(params.slot or "")
	if slot_name == "" then error("rank_uniques_slot requires params.slot", 0) end
	if not (items_tab and items_tab.slots and items_tab.slots[slot_name]) then
		error("unknown PoE2 item slot: " .. slot_name, 0)
	end

	local m = launch and launch.main
	local udb_obj = m and m.uniqueDB
	if type(udb_obj) ~= "table" then error("PoB2 uniqueDB is not loaded", 0) end
	local pumps = 0
	while udb_obj.loading and pumps < 2000 do
		H.frame()
		pumps = pumps + 1
	end
	local udb = udb_obj.list
	if type(udb) ~= "table" then error("PoB2 uniqueDB.list missing after load", 0) end

	local req_stat = tostring(params.stat or "")
	local top_n = math.max(1, math.min(tonumber(params.top) or 15, 50))
	local eval_sanctified = params.evalSanctified ~= false
	local max_scan = math.max(top_n, math.min(tonumber(params.maxScan) or 300, 600))

	local calc_func, calc_base = build.calcsTab:GetMiscCalculator(build)
	calc_base = calc_base or {}
	local base_dps, dps_key = primary_dps(calc_base)
	local target_stat = (req_stat ~= "") and req_stat or dps_key
	local use_full_dps = (target_stat == "FullDPS") or (dps_key == "FullDPS")
	local base_ehp = tonumber(calc_base.TotalEHP) or 0
	local base_val = (target_stat == dps_key) and base_dps or (tonumber(calc_base[target_stat]) or 0)

	local candidates = {}
	local scanned = 0
	for _, item in pairs(udb) do
		if scanned < max_scan and is_item_valid_for_slot_poe2(items_tab, item, slot_name) then
			scanned = scanned + 1
			local out = eval_unique(calc_func, item, slot_name, use_full_dps)
			if out then
				local new_dps = (dps_key == "FullDPS") and (tonumber(out.FullDPS) or 0) or (tonumber(out[dps_key]) or tonumber(out.CombinedDPS) or tonumber(out.TotalDPS) or 0)
				local new_ehp = tonumber(out.TotalEHP) or 0
				local new_val = (target_stat == dps_key) and new_dps or (tonumber(out[target_stat]) or 0)
				local d_val = new_val - base_val
				local d_dps = new_dps - base_dps
				local d_ehp = new_ehp - base_ehp
				candidates[#candidates + 1] = {
					_item = item,
					name = item.title or item.name or "Unique",
					baseName = item.baseName or (item.base and item.base.name) or "",
					slot = slot_name,
					statValue = new_val,
					deltaStat = d_val,
					deltaStatPct = (base_val > 1e-6) and (math.floor((d_val / base_val) * 1000 + 0.5) / 10) or 0,
					dps = new_dps,
					deltaDps = d_dps,
					deltaDpsPct = (base_dps > 1e-6) and (math.floor((d_dps / base_dps) * 1000 + 0.5) / 10) or 0,
					totalEhp = new_ehp,
					deltaEhp = d_ehp,
					deltaEhpPct = (base_ehp > 1e-6) and (math.floor((d_ehp / base_ehp) * 1000 + 0.5) / 10) or 0,
				}
			end
		end
	end

	table.sort(candidates, function(a, b)
		if a.deltaStat ~= b.deltaStat then return a.deltaStat > b.deltaStat end
		if a.deltaEhp ~= b.deltaEhp then return a.deltaEhp > b.deltaEhp end
		return a.name < b.name
	end)

	local top_list = {}
	for i = 1, math.min(top_n, #candidates) do
		local row = candidates[i]
		local item = row._item
		row._item = nil
		if eval_sanctified and item then
			local hi_item = build_corrupted_roll_variant(item, 1.22)
			local lo_item = build_corrupted_roll_variant(item, 0.78)
			local hi_out = hi_item and eval_unique(calc_func, hi_item, slot_name, use_full_dps)
			local lo_out = lo_item and eval_unique(calc_func, lo_item, slot_name, use_full_dps)
			if hi_out and lo_out then
				local hi_dps = (dps_key == "FullDPS") and (tonumber(hi_out.FullDPS) or 0) or (tonumber(hi_out[dps_key]) or tonumber(hi_out.CombinedDPS) or 0)
				local lo_dps = (dps_key == "FullDPS") and (tonumber(lo_out.FullDPS) or 0) or (tonumber(lo_out[dps_key]) or tonumber(lo_out.CombinedDPS) or 0)
				local hi_ehp = tonumber(hi_out.TotalEHP) or 0
				local lo_ehp = tonumber(lo_out.TotalEHP) or 0
				row.sanctifiedRollRange = {
					minScalar = 0.78,
					maxScalar = 1.22,
					minDps = lo_dps,
					maxDps = hi_dps,
					minEhp = lo_ehp,
					maxEhp = hi_ehp,
					maxDpsUpsidePct = (row.dps > 1e-6) and (math.floor(((hi_dps - row.dps) / row.dps) * 1000 + 0.5) / 10) or 0,
					maxEhpUpsidePct = (row.totalEhp > 1e-6) and (math.floor(((hi_ehp - row.totalEhp) / row.totalEhp) * 1000 + 0.5) / 10) or 0,
				}
			end
		end
		top_list[i] = row
	end

	return {
		game = "poe2",
		slot = slot_name,
		stat = target_stat,
		baseStat = base_val,
		baseDps = base_dps,
		baseEhp = base_ehp,
		scannedCount = scanned,
		results = top_list,
		elapsedMs = now_ms() - t0,
	}
end

M.rpc_handlers = {
	rank_uniques_slot = handle_rank_uniques_slot,
}

return M
