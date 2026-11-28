-- PoE2 0.5.5+ Engine Crash & Silent-Drop Shields:
--   1. #2529 `mainOutput is nil` shield: intercepts weapon-set edge-case calculation errors or
--      nil mainOutput in CalcsTab:BuildOutput so headless PoB2 returns structured diagnostics
--      instead of crashing with `attempt to index field 'mainOutput' (a nil value)`.
--   2. #2582 `0.5.5` missing base silent item-drop detector: intercepts ItemsTab:Load / AddItem
--      when an item base is missing from data.itemBases and PoB2 silently drops the item (causing
--      weapons to disappear and DPS to fall to 0), emitting structured warnings.
local H = require("pob_host")
local M = {}

local state = {
	mainOutputShield = {
		installed = false,
		triggered = false,
		triggerCount = 0,
		lastError = nil,
		lastTimestampMs = nil,
		recovered = false,
	},
	droppedItems = {},
	missingBaseInstalled = false,
}

local function trim(s)
	return type(s) == "string" and (s:gsub("^%s+", ""):gsub("%s+$", "")) or ""
end

local function parse_raw_header(raw_text)
	if type(raw_text) ~= "string" then return "Normal", "", "" end
	local rarity, lines = "Normal", {}
	for line in (raw_text:gsub("\r", "") .. "\n"):gmatch("([^\n]*)\n") do
		local t = trim(line)
		if t ~= "" then
			if t:sub(1, 8) == "--------" then break end
			local rv = t:match("^Rarity:%s*(.+)$")
			if rv then
				rarity = trim(rv)
			elseif not t:match("^(Item Class|Unique ID|Item Level|Quality|Sockets|LevelReq|Implicits):") then
				lines[#lines + 1] = t
				if #lines >= 2 then break end
			end
		end
	end
	local rl = rarity:lower()
	if (rl == "rare" or rl == "unique") and #lines >= 2 then
		return rarity, lines[1], lines[2]
	end
	return rarity, "", lines[1] or ""
end

local function collect_xml_slot_refs(xml)
	local refs = {}
	local function add_ref(iid, slot_name)
		if not iid or iid <= 0 or not slot_name or slot_name == "" then return end
		local list = refs[iid]
		if not list then list = {}; refs[iid] = list end
		for _, existing in ipairs(list) do if existing == slot_name then return end end
		list[#list + 1] = slot_name
	end
	for _, node in ipairs(type(xml) == "table" and xml or {}) do
		if type(node) == "table" then
			if node.elem == "Slot" and type(node.attrib) == "table" then
				add_ref(tonumber(node.attrib.itemId), node.attrib.name)
			elseif node.elem == "ItemSet" then
				for _, child in ipairs(node) do
					if type(child) == "table" and child.elem == "Slot" and type(child.attrib) == "table" then
						add_ref(tonumber(child.attrib.itemId), child.attrib.name)
					end
				end
			end
		end
	end
	return refs
end

local function is_weapon_slot(slots)
	for _, sn in ipairs(slots or {}) do
		if type(sn) == "string" and sn:match("^Weapon") then return true end
	end
	return false
end

local function record_dropped_item(item_id, raw_text, slots)
	local rarity, title, base_line = parse_raw_header(raw_text)
	local wpn = is_weapon_slot(slots)
	local slot_str = (#slots > 0) and table.concat(slots, ", ") or "unequipped"
	state.droppedItems[#state.droppedItems + 1] = {
		issue = "#2582",
		code = "missing_item_base_dropped",
		severity = wpn and "critical" or "warning",
		itemId = item_id,
		rarity = rarity,
		title = title,
		missingBase = base_line,
		affectedSlots = slots,
		isWeaponSlot = wpn,
		message = string.format(
			"Item #%s (base='%s', slots=%s) was silently dropped by PoB2 because its base type is missing from the 0.5.5 item table.",
			tostring(item_id or "?"), base_line, slot_str
		),
	}
end

local function ensure_safe_env_output(calcs_tab, reason)
	state.mainOutputShield.triggered = true
	state.mainOutputShield.triggerCount = state.mainOutputShield.triggerCount + 1
	state.mainOutputShield.lastError = tostring(reason or "mainOutput was nil after BuildOutput")
	state.mainOutputShield.lastTimestampMs = H.now_ms()
	state.mainOutputShield.recovered = true
	if type(calcs_tab.mainEnv) ~= "table" then
		calcs_tab.mainEnv = { player = { output = {} } }
	elseif type(calcs_tab.mainEnv.player) ~= "table" then
		calcs_tab.mainEnv.player = { output = {} }
	elseif type(calcs_tab.mainEnv.player.output) ~= "table" then
		calcs_tab.mainEnv.player.output = {}
	end
	if type(calcs_tab.mainOutput) ~= "table" then
		calcs_tab.mainOutput = calcs_tab.mainEnv.player.output
	end
	if type(calcs_tab.calcsOutput) ~= "table" then
		calcs_tab.calcsOutput = calcs_tab.mainOutput
	end
end

local function wrap_calcs_tab_class()
	local cls = type(CalcsTabClass) == "table" and CalcsTabClass or nil
	if not cls then
		local b = H.get_build({ rebuild = false, optional = true })
		local ct = b and b.calcsTab
		local mt = ct and getmetatable(ct)
		cls = (type(mt) == "table" and type(mt.__index) == "table") and mt.__index or ct
	end
	if type(cls) ~= "table" or type(cls.BuildOutput) ~= "function" then return false end
	if cls._pob2_2529_shield then
		state.mainOutputShield.installed = true
		return true
	end
	local orig_build_output = cls.BuildOutput
	cls.BuildOutput = function(self, ...)
		state.mainOutputShield.triggered = false
		state.mainOutputShield.recovered = false
		state.mainOutputShield.lastError = nil
		local ok, err = xpcall(function() return orig_build_output(self) end, debug.traceback)
		if not ok then
			ensure_safe_env_output(self, err)
			return
		end
		if type(self.mainOutput) ~= "table" then
			ensure_safe_env_output(self, "CalcsTab:BuildOutput completed with mainOutput == nil (#2529)")
		end
	end
	cls._pob2_2529_shield = true
	state.mainOutputShield.installed = true
	return true
end

local function wrap_items_tab_class()
	local cls = type(ItemsTabClass) == "table" and ItemsTabClass or nil
	if not cls then
		local b = H.get_build({ rebuild = false, optional = true })
		local it = b and b.itemsTab
		local mt = it and getmetatable(it)
		cls = (type(mt) == "table" and type(mt.__index) == "table") and mt.__index or it
	end
	if type(cls) ~= "table" or type(cls.Load) ~= "function" then return false end
	if cls._pob2_2582_shield then
		state.missingBaseInstalled = true
		return true
	end
	local orig_load = cls.Load
	cls.Load = function(self, xml, dbFileName)
		state.droppedItems = {}
		local expected = {}
		local slot_refs = collect_xml_slot_refs(xml)
		for _, node in ipairs(type(xml) == "table" and xml or {}) do
			if type(node) == "table" and node.elem == "Item" and type(node.attrib) == "table" then
				local iid = tonumber(node.attrib.id)
				local raw_chunks = {}
				for _, ch in ipairs(node) do
					if type(ch) == "string" then raw_chunks[#raw_chunks + 1] = ch end
				end
				if iid then expected[#expected + 1] = { id = iid, raw = table.concat(raw_chunks, "\n") } end
			end
		end
		local res = orig_load(self, xml, dbFileName)
		local loaded = type(self.items) == "table" and self.items or {}
		for _, rec in ipairs(expected) do
			if loaded[rec.id] == nil then
				record_dropped_item(rec.id, rec.raw, slot_refs[rec.id] or {})
			end
		end
		return res
	end
	if type(cls.AddItem) == "function" then
		local orig_add = cls.AddItem
		cls.AddItem = function(self, item, ...)
			if type(item) == "table" and item.base == nil then
				record_dropped_item(item.id or 0, item.raw or "", {})
			end
			return orig_add(self, item, ...)
		end
	end
	cls._pob2_2582_shield = true
	state.missingBaseInstalled = true
	return true
end

function M.ensure_hooks()
	local ok1 = wrap_calcs_tab_class()
	local ok2 = wrap_items_tab_class()
	return ok1, ok2
end

function M.get_diagnostics()
	local warnings = {}
	if state.mainOutputShield.triggered then
		warnings[#warnings + 1] = {
			issue = "#2529",
			code = "main_output_nil_recovered",
			severity = "warning",
			recovered = true,
			reason = state.mainOutputShield.lastError,
		}
	end
	for _, d in ipairs(state.droppedItems) do
		warnings[#warnings + 1] = d
	end
	return {
		ok = true,
		game = "poe2",
		mainOutputShield = {
			installed = state.mainOutputShield.installed,
			triggered = state.mainOutputShield.triggered,
			triggerCount = state.mainOutputShield.triggerCount,
			recovered = state.mainOutputShield.recovered,
			lastError = state.mainOutputShield.lastError,
			status = state.mainOutputShield.triggered and "degraded_nil_main_output" or "healthy",
		},
		missingBaseDetector = {
			installed = state.missingBaseInstalled,
			droppedCount = #state.droppedItems,
			droppedItems = state.droppedItems,
		},
		engineWarnings = warnings,
	}
end

function M.attach_diagnostics(method, result)
	if type(result) ~= "table" then return result end
	if method == "import_code" or method == "load_xml" or method == "list_items" or method == "get_build_info" or method == "get_stats" or method == "get_calcs" then
		if #state.droppedItems > 0 then
			result.droppedItems = state.droppedItems
		end
		if state.mainOutputShield.triggered then
			result.mainOutputShield = M.get_diagnostics().mainOutputShield
		end
		local diag = M.get_diagnostics()
		if #diag.engineWarnings > 0 then
			result.engineWarnings = diag.engineWarnings
		end
	end
	return result
end

-- Controlled self-test RPC verifying the #2529 shield catches a nil mainOutput without crashing.
local function rpc_probe_main_output_shield(params)
	M.ensure_hooks()
	local b = H.get_build({ rebuild = false })
	local ct = b.calcsTab
	local orig_calcs_build = ct.calcs.buildOutput
	local simulate_error = type(params) == "table" and params.mode == "error"
	ct.calcs.buildOutput = function()
		if simulate_error then error("simulated #2529 weapon-set nil mainEnv failure", 0) end
		return { player = { output = nil } }
	end
	local ok, err = pcall(function() ct:BuildOutput() end)
	ct.calcs.buildOutput = orig_calcs_build
	local after_shield = M.get_diagnostics().mainOutputShield
	local main_output_is_table = type(ct.mainOutput) == "table"
	-- Restore clean real calculation pass immediately
	ct:BuildOutput()
	return {
		ok = ok and main_output_is_table and after_shield.triggered == true,
		callOk = ok,
		callErr = not ok and tostring(err) or nil,
		mainOutputIsTable = main_output_is_table,
		shieldDuringFault = after_shield,
		recoveredAfterRestore = type(ct.mainOutput) == "table",
	}
end

M.rpc_handlers = {
	get_engine_diagnostics = function(_p) return M.get_diagnostics() end,
	probe_main_output_shield = rpc_probe_main_output_shield,
}

return M
