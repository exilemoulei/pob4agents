-- PoE2 Node Power Ranking Kernel:
--   * Strictly PoE2 8-class model (integerIds: 1, 2, 6, 7, 8, 9, 10, 11)
--   * Dual weapon-set allocMode (0 | 1 | 2) metadata per node and weapon-set summary
--   * PoE2 passive jewel sockets (no PoE1 cluster jewel subgraphs, 6-class quadrants, or masteries)
local calcs = require("Modules.Calcs")
local H = require("pob_host")
local now_ms, get_build = H.now_ms, H.get_build

local M = {}

local POE2_CLASS_IDS = { 1, 2, 6, 7, 8, 9, 10, 11 }
local POE2_CLASSES = {
	[1] = "Witch",
	[2] = "Ranger",
	[6] = "Warrior",
	[7] = "Sorceress",
	[8] = "Huntress",
	[9] = "Mercenary",
	[10] = "Monk",
	[11] = "Druid",
}

local function list_power_stat_names()
	local names = {}
	for _, entry in ipairs((data and data.powerStatList) or {}) do
		if entry.stat then names[#names + 1] = entry.stat end
	end
	return names
end

local function find_power_stat_exact(stat_name)
	for _, entry in ipairs((data and data.powerStatList) or {}) do
		if entry.stat == stat_name then return entry end
	end
	return nil
end

local function pick_power_stat(build, preferred, strict)
	if preferred and preferred ~= "" then
		local exact = find_power_stat_exact(preferred)
		if exact then return exact end
		if strict then error("unknown power stat: " .. tostring(preferred), 0) end
	end
	local out = (build.calcsTab and build.calcsTab.mainOutput) or {}
	if (out.FullDPS or 0) > 0 then
		local full = find_power_stat_exact("FullDPS")
		if full then return full end
	end
	return find_power_stat_exact("CombinedDPS")
		or find_power_stat_exact("TotalDPS")
		or (data and data.powerStatList and (data.powerStatList[3] or data.powerStatList[1]))
end

local function wrap_calcs_power_builder(calcs_tab)
	if not calcs_tab or calcs_tab._pob2_pb_wrapped then return end
	local mt = getmetatable(calcs_tab)
	local host = (type(mt) == "table" and type(mt.__index) == "table" and mt.__index.PowerBuilder)
		and mt.__index or calcs_tab
	if host._pob2_pb_wrapped or type(host.PowerBuilder) ~= "function" then return end
	local orig_pb = host.PowerBuilder
	host.PowerBuilder = function(self, ...)
		local b = self and self.build
		local saved_view = b and b.viewMode
		local stat_name = self and self.powerStat and self.powerStat.stat
		if b and stat_name and stat_name ~= "FullDPS" then b.viewMode = "CALCS" end
		local ok, err = pcall(orig_pb, self, ...)
		if b and saved_view ~= nil then b.viewMode = saved_view end
		if not ok then error(err, 0) end
	end
	host._pob2_pb_wrapped = true
	calcs_tab._pob2_pb_wrapped = true
end

local function resolve_poe2_class(spec)
	local cid = spec and spec.curClassId
	local cname = (cid and POE2_CLASSES[cid]) or (spec and spec.curClassName) or "Unknown"
	local valid = (cid ~= nil and POE2_CLASSES[cid] ~= nil)
	return cid, cname, valid
end

local function collect_weapon_set_meta(build)
	local spec = build and build.spec
	local aset = build and build.itemsTab and build.itemsTab.activeItemSet
	local ws1, ws2 = 0, 0
	if spec and type(spec.CountAllocNodes) == "function" then
		local _, _, _, _, u1, u2 = spec:CountAllocNodes()
		ws1, ws2 = tonumber(u1) or 0, tonumber(u2) or 0
	else
		for _, nd in pairs((spec and spec.allocNodes) or {}) do
			if nd.allocMode == 1 then ws1 = ws1 + 1 elseif nd.allocMode == 2 then ws2 = ws2 + 1 end
		end
	end
	return {
		activeAllocMode = spec and (spec.allocMode or 0) or 0,
		useSecondWeaponSet = type(aset) == "table" and (aset.useSecondWeaponSet and true or false) or false,
		weaponSet1Used = ws1,
		weaponSet2Used = ws2,
	}
end

local function is_poe2_jewel_socket(node)
	if node.type ~= "Socket" then return false end
	if node.charm or node.charmSocket or node.asc or node.ascendancyName then return false end
	if node.name == "Charm Socket" or node.dn == "Charm Socket" then return false end
	return true
end

local function collect_nearby_sockets(build, max_dist)
	local summary = {
		allocatedTotal = 0,
		allocatedOccupied = 0,
		allocatedEmptyIds = {},
	}
	local nearby = {}
	local spec = build and build.spec
	if not spec or type(spec.nodes) ~= "table" then return summary, nearby end
	local items_tab = build.itemsTab
	local sock_map = items_tab and items_tab.sockets or {}
	local lim = max_dist and math.min(math.max(math.floor(max_dist), 2), 8) or 4

	for id, node in pairs(spec.nodes) do
		if type(id) == "number" and is_poe2_jewel_socket(node) then
			local alloc = node.alloc and true or false
			local sock_ctrl = sock_map[id]
			local sel_id = sock_ctrl and tonumber(sock_ctrl.selItemId) or 0
			local occupied = sel_id > 0
			if alloc then
				summary.allocatedTotal = summary.allocatedTotal + 1
				if occupied then
					summary.allocatedOccupied = summary.allocatedOccupied + 1
				else
					summary.allocatedEmptyIds[#summary.allocatedEmptyIds + 1] = id
					nearby[#nearby + 1] = {
						nodeId = id,
						kind = "jewel_socket",
						allocMode = node.allocMode or 0,
						allocated = true,
						occupied = false,
						cost = 0,
						pathIds = {},
						travelStats = {},
					}
				end
			else
				local dist = tonumber(node.pathDist)
				if dist and dist >= 1 and dist <= lim then
					local path_ids, travel_stats, seen_st = {}, {}, {}
					local raw_path = type(node.path) == "table" and node.path or {}
					for j = #raw_path, 1, -1 do
						local pn = raw_path[j]
						if type(pn) == "table" and type(pn.id) == "number" then
							path_ids[#path_ids + 1] = pn.id
							if pn.id ~= id and type(pn.sd) == "table" then
								for _, ln in ipairs(pn.sd) do
									local clean = tostring(ln or ""):gsub("%s*\r?\n%s*", " ")
									if clean ~= "" and not seen_st[clean] and #travel_stats < 4 then
										seen_st[clean] = true
										travel_stats[#travel_stats + 1] = clean
									end
								end
							end
						end
					end
					if #path_ids == 0 then path_ids[1] = id end
					nearby[#nearby + 1] = {
						nodeId = id,
						kind = "jewel_socket",
						allocMode = 0,
						allocated = false,
						occupied = false,
						cost = math.floor(dist),
						pathIds = path_ids,
						travelStats = travel_stats,
					}
				end
			end
		end
	end
	table.sort(summary.allocatedEmptyIds)
	table.sort(nearby, function(a, b)
		if a.cost ~= b.cost then return a.cost < b.cost end
		return a.nodeId < b.nodeId
	end)
	return summary, nearby
end

local function primary_dps_pair(out)
	if (out.FullDPS or 0) > 0 then return out.FullDPS, "FullDPS" end
	return out.CombinedDPS or out.TotalDPS or 0, "CombinedDPS"
end

local function find_display_stat_for(build, stat_name)
	for _, ds in ipairs((build and build.displayStats) or {}) do
		if ds and ds.stat == stat_name then return ds end
	end
	return nil
end

local function format_power_val(val, display_stat)
	local fmt = (display_stat and display_stat.fmt) or ".1f"
	local ok, s = pcall(string.format, "%" .. fmt, val or 0)
	return ok and s or string.format("%.1f", val or 0)
end

local function eval_subset_node_power(build, power_stat, raw_ids, include_path_power)
	local spec = build.spec
	local spec_nodes = (spec and spec.nodes) or {}
	local main_env = build.calcsTab and build.calcsTab.mainEnv
	local granted = (main_env and main_env.grantedPassives) or {}
	local use_full_dps = (power_stat.stat == "FullDPS")
	local calc_func, calc_base
	if build.calcsTab and type(build.calcsTab.GetMiscCalculator) == "function" then
		calc_func, calc_base = build.calcsTab:GetMiscCalculator()
	end
	if type(calc_func) ~= "function" then
		calc_func, calc_base = calcs.getMiscCalculator(build)
	end
	calc_base = calc_base or {}

	local display_stat = find_display_stat_for(build, power_stat.stat)
	local scale = (display_stat and (display_stat.pc or display_stat.mod)) and 100 or 1
	local report, seen, requested, missing = {}, {}, {}, {}

	for _, raw_id in ipairs(raw_ids) do
		local nid = tonumber(raw_id)
		if nid and not seen[nid] then
			seen[nid] = true
			requested[#requested + 1] = nid
			local node = spec_nodes[nid]
			if not node then
				missing[#missing + 1] = nid
			else
				local is_alloc = (node.alloc or granted[nid]) and true or false
				local single_out
				if is_alloc then
					single_out = calc_func({ removeNodes = { [node] = true } }, use_full_dps) or {}
				else
					single_out = calc_func({ addNodes = { [node] = true } }, use_full_dps) or {}
				end
				local raw_single = build.calcsTab:CalculatePowerStat(power_stat, single_out, calc_base) or 0
				local path_dist
				local raw_path = raw_single
				if is_alloc then
					path_dist = (type(node.depends) == "table" and #node.depends > 0) and #node.depends or 1
					if include_path_power and type(node.depends) == "table" and #node.depends > 1 and not node.ascendancyName then
						local dep_set = {}
						for _, dn in ipairs(node.depends) do dep_set[dn] = true end
						raw_path = build.calcsTab:CalculatePowerStat(power_stat, calc_func({ removeNodes = dep_set }, use_full_dps) or {}, calc_base) or raw_single
					end
				else
					path_dist = tonumber(node.pathDist) or (type(node.path) == "table" and #node.path > 0 and #node.path) or 1
					if include_path_power and type(node.path) == "table" and #node.path > 1 and not node.ascendancyName then
						local path_set = {}
						for _, pn in ipairs(node.path) do path_set[pn] = true end
						raw_path = build.calcsTab:CalculatePowerStat(power_stat, calc_func({ addNodes = path_set }, use_full_dps) or {}, calc_base) or raw_single
					end
				end
				local node_power = raw_single * scale
				local path_power = (raw_path / math.max(path_dist, 1)) * scale
				report[#report + 1] = {
					id = nid,
					name = node.dn,
					type = node.type,
					allocated = is_alloc,
					allocMode = node.allocMode or 0,
					isAttribute = node.isAttribute and true or false,
					power = node_power,
					powerStr = format_power_val(node_power, display_stat),
					pathPower = path_power,
					pathPowerStr = format_power_val(path_power, display_stat),
					pathDist = path_dist,
					sd = node.sd,
					x = node.x,
					y = node.y,
				}
			end
		end
	end
	table.sort(report, function(a, b)
		if a.power ~= b.power then return a.power > b.power end
		return a.id < b.id
	end)
	return report, requested, missing
end

local function extract_stat_snapshot(out)
	local dps, dps_key = primary_dps_pair(out)
	return {
		primaryDpsKey = dps_key,
		primaryDps = dps,
		totalEhp = tonumber(out.TotalEHP) or 0,
		life = tonumber(out.Life) or 0,
		energyShield = tonumber(out.EnergyShield) or 0,
		spirit = tonumber(out.Spirit) or 0,
		spiritUnreserved = tonumber(out.SpiritUnreserved) or 0,
		fireResist = tonumber(out.FireResist) or 0,
		coldResist = tonumber(out.ColdResist) or 0,
		lightningResist = tonumber(out.LightningResist) or 0,
		chaosResist = tonumber(out.ChaosResist) or 0,
		str = tonumber(out.Str) or 0,
		dex = tonumber(out.Dex) or 0,
		int = tonumber(out.Int) or 0,
	}
end

local function handle_eval_isolated_node(params)
	params = type(params) == "table" and params or {}
	local build = get_build()
	local spec = build.spec
	local nid = tonumber(params.id)
	local node = nid and spec and spec.nodes and spec.nodes[nid]
	if not node then error("no node " .. tostring(params.id), 0) end

	local was_alloc = node.alloc and true or false
	local req_action = tostring(params.action or "auto")
	local op = (req_action == "add" or req_action == "remove") and req_action or (was_alloc and "remove" or "add")
	local calc_func, calc_base
	if build.calcsTab and type(build.calcsTab.GetMiscCalculator) == "function" then
		calc_func, calc_base = build.calcsTab:GetMiscCalculator()
	end
	if type(calc_func) ~= "function" then
		calc_func, calc_base = calcs.getMiscCalculator(build)
	end
	calc_base = calc_base or {}
	local use_full_dps = (calc_base.FullDPS or 0) > 0
	local mod_req = (op == "remove") and { removeNodes = { [node] = true } } or { addNodes = { [node] = true } }
	local after_out = calc_func(mod_req, use_full_dps) or {}

	local before_s = extract_stat_snapshot(calc_base)
	local after_s = extract_stat_snapshot(after_out)
	local ddps = after_s.primaryDps - before_s.primaryDps
	local dehp = after_s.totalEhp - before_s.totalEhp
	local path_ids = {}
	for _, pn in ipairs(node.path or {}) do
		if type(pn) == "table" and type(pn.id) == "number" then path_ids[#path_ids + 1] = pn.id end
	end
	local is_reachable = was_alloc or (type(node.path) == "table" and #node.path > 0)
	local path_dist = tonumber(node.pathDist)
	if path_dist and path_dist >= 1000 then path_dist = nil end
	return {
		nodeId = nid,
		name = node.dn,
		type = node.type,
		sd = node.sd or {},
		allocated = was_alloc,
		op = op,
		isReachable = is_reachable,
		connectedToStart = node.connectedToStart and true or false,
		pathDist = path_dist,
		pathIds = path_ids,
		before = before_s,
		after = after_s,
		delta = {
			primaryDps = ddps,
			primaryDpsPct = before_s.primaryDps > 1e-6 and (ddps / before_s.primaryDps * 100) or 0,
			totalEhp = dehp,
			totalEhpPct = before_s.totalEhp > 1e-6 and (dehp / before_s.totalEhp * 100) or 0,
			life = after_s.life - before_s.life,
			energyShield = after_s.energyShield - before_s.energyShield,
			spirit = after_s.spirit - before_s.spirit,
		},
	}
end

local function handle_toggle_isolated_node(params)
	params = type(params) == "table" and params or {}
	local build = get_build()
	local spec = build.spec
	local nid = tonumber(params.id)
	local node = nid and spec and spec.nodes and spec.nodes[nid]
	if not node then error("no node " .. tostring(params.id), 0) end
	local was_alloc = node.alloc and true or false
	local op = was_alloc and "remove" or "add"
	if was_alloc then
		node.alloc = false
		node.allocMode = 0
		spec.allocNodes[nid] = nil
	else
		node.alloc = true
		node.allocMode = 0
		spec.allocNodes[nid] = node
	end
	if type(wipeGlobalCache) == "function" then wipeGlobalCache() end
	build.buildFlag = true
	H.frame()
	return {
		ok = true,
		nodeId = nid,
		name = node.dn,
		type = node.type,
		op = op,
		allocated = node.alloc and true or false,
	}
end

local function handle_node_power(params)
	params = type(params) == "table" and params or {}
	local build = get_build()
	wrap_calcs_power_builder(build.calcsTab)

	local req_stat = params.stat
	local power_stat = pick_power_stat(build, req_stat, req_stat ~= nil and req_stat ~= "")
	if not power_stat then error("unknown power stat: " .. tostring(req_stat), 0) end

	local max_depth = tonumber(params.maxDepth or params.max_depth)
	local raw_node_ids = params.nodeIds or params.node_ids
	local has_subset = type(raw_node_ids) == "table" and #raw_node_ids > 0

	local saved_view = build.viewMode
	local saved_depth = build.calcsTab.nodePowerMaxDepth
	if power_stat.stat ~= "FullDPS" then build.viewMode = "CALCS" end
	build.calcsTab.powerStat = power_stat
	build.calcsTab.nodePowerMaxDepth = max_depth

	local t0 = now_ms()
	local report, requested_ids, missing_ids
	if has_subset then
		local ok, r1, r2, r3 = pcall(eval_subset_node_power, build, power_stat, raw_node_ids, params.includePathPower ~= false)
		build.viewMode = saved_view
		build.calcsTab.nodePowerMaxDepth = saved_depth
		if not ok then error(r1, 0) end
		report, requested_ids, missing_ids = r1, r2, r3
	else
		local ok, report_or_err = pcall(function()
			build.calcsTab:PowerBuilder()
			return build.treeTab:BuildPowerReportList(power_stat)
		end)
		build.viewMode = saved_view
		build.calcsTab.nodePowerMaxDepth = saved_depth
		if not ok then error(report_or_err, 0) end
		local raw_report = report_or_err or {}
		local spec_nodes = (build.spec and build.spec.nodes) or {}
		report = {}
		for i, row in ipairs(raw_report) do
			local nd = spec_nodes[row.id]
			report[i] = {
				id = row.id,
				name = row.name,
				type = row.type,
				allocated = row.allocated and true or false,
				allocMode = (nd and nd.allocMode) or 0,
				isAttribute = (nd and nd.isAttribute) and true or false,
				power = row.power or 0,
				powerStr = row.powerStr,
				pathPower = row.pathPower or 0,
				pathPowerStr = row.pathPowerStr,
				pathDist = row.pathDist,
				sd = row.sd,
				x = row.x,
				y = row.y,
			}
		end
	end
	local elapsed_ms = now_ms() - t0

	local cid, cname, valid_cls = resolve_poe2_class(build.spec)
	local sock_summary, nearby_sockets = collect_nearby_sockets(build, max_depth)
	local out = {
		game = "poe2",
		stat = power_stat.stat,
		stats = list_power_stat_names(),
		classId = cid,
		className = cname,
		validPoe2Class = valid_cls,
		validClassIds = POE2_CLASS_IDS,
		weaponSet = collect_weapon_set_meta(build),
		report = report,
		socketSummary = sock_summary,
		nearbySockets = nearby_sockets,
		elapsedMs = elapsed_ms,
	}
	if has_subset then
		out.filteredNodeIds = requested_ids
		if #missing_ids > 0 then out.missingNodeIds = missing_ids end
	end
	return out
end

M.POE2_CLASS_IDS = POE2_CLASS_IDS
M.POE2_CLASSES = POE2_CLASSES
M.list_power_stat_names = list_power_stat_names
M.find_power_stat_exact = find_power_stat_exact
M.pick_power_stat = pick_power_stat
M.collect_nearby_sockets = collect_nearby_sockets
M.handle_node_power = handle_node_power
M.handle_eval_isolated_node = handle_eval_isolated_node
M.handle_toggle_isolated_node = handle_toggle_isolated_node
M.rpc_handlers = {
	node_power = handle_node_power,
	eval_isolated_node = handle_eval_isolated_node,
	toggle_isolated_node = handle_toggle_isolated_node,
}

return M
