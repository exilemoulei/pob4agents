-- PoE2 PassiveSpec / allocMode / hashOverrides CoW snapshot, restore, and weapon-set RPC helpers.
local H = require("pob_host")
local M = {}
local t_insert, t_remove = table.insert, table.remove

local function shallow_copy(t)
	local out = {}
	if type(t) == "table" then
		for k, v in pairs(t) do out[k] = v end
	end
	return out
end

local function copy_array(a)
	local out = {}
	if type(a) == "table" then
		for i = 1, #a do out[i] = a[i] end
	end
	return out
end

local function replace_arr(dst, src)
	if type(dst) ~= "table" or type(src) ~= "table" then return end
	for i = 1, #src do dst[i] = src[i] end
	while #dst > #src do t_remove(dst) end
end

local function replace_tbl(dst, src)
	if type(dst) ~= "table" then return end
	for k in pairs(dst) do
		if src[k] == nil then dst[k] = nil end
	end
	for k, v in pairs(src) do dst[k] = v end
end

local function tables_eq(a, b)
	if a == b then return true end
	if type(a) ~= "table" or type(b) ~= "table" then return false end
	for k, v in pairs(a) do if b[k] ~= v then return false end end
	for k in pairs(b) do if a[k] == nil then return false end end
	return true
end

local function keys_eq(a, b)
	if a == b then return true end
	if type(a) ~= "table" or type(b) ~= "table" then return false end
	for k in pairs(a) do if b[k] == nil then return false end end
	for k in pairs(b) do if a[k] == nil then return false end end
	return true
end

local function override_sig(nd)
	if type(nd) ~= "table" then return "" end
	local sd = type(nd.sd) == "table" and table.concat(nd.sd, "\n") or ""
	return tostring(nd.dn or "") .. "|" .. sd
end

local function node_modes_differ(spec, snap)
	if (spec.allocMode or 0) ~= snap.allocMode then return true end
	if (spec.attributeIndex or 1) ~= snap.attrIndex then return true end
	for nid, nd in pairs(spec.allocNodes or {}) do
		if (nd.allocMode or 0) ~= (snap.nodeModes[nid] or 0) then return true end
	end
	return false
end

local function overrides_differ(spec, snap)
	local cur_ov = spec.hashOverrides or {}
	if not keys_eq(cur_ov, snap.hashOvr) then return true end
	for nid, saved_nd in pairs(snap.hashOvr) do
		local cur_nd = cur_ov[nid]
		if cur_nd ~= saved_nd or override_sig(cur_nd) ~= snap.hashOvrSig[nid] then return true end
		local live = spec.nodes and spec.nodes[nid]
		if live and override_sig(live) ~= snap.hashOvrSig[nid] then return true end
	end
	for nid, base_sig in pairs(snap.allocAttrSig) do
		local live = spec.nodes and spec.nodes[nid]
		if live and override_sig(live) ~= base_sig then return true end
	end
	return false
end

local function restore_hash_overrides(spec, snap)
	local touched = {}
	for nid in pairs(spec.hashOverrides or {}) do touched[nid] = true end
	for nid in pairs(snap.hashOvr) do touched[nid] = true end
	for nid in pairs(snap.allocAttrSig) do touched[nid] = true end

	if type(spec.hashOverrides) ~= "table" then spec.hashOverrides = {} end
	replace_tbl(spec.hashOverrides, snap.hashOvr)

	local tree = spec.tree
	for nid in pairs(touched) do
		local live = spec.nodes and spec.nodes[nid]
		if live then
			local target = snap.hashOvr[nid] or (tree and tree.nodes and tree.nodes[nid])
			if target and type(spec.ReplaceNode) == "function" then
				spec:ReplaceNode(live, target)
				if tree and type(tree.ProcessStats) == "function" then
					pcall(tree.ProcessStats, tree, live)
				end
				live.finalModList = live.modList
			end
		end
	end
end

local function sync_alloc_nodes_poe2(spec, snap)
	local an = spec.allocNodes
	for nid in pairs(an) do
		if not snap.allocNodes[nid] then
			an[nid] = nil
			local nd = spec.nodes[nid]
			if nd then nd.alloc = false; nd.allocMode = 0 end
		end
	end
	for nid in pairs(snap.allocNodes) do
		local nd = spec.nodes[nid]
		if nd then
			an[nid] = nd
			nd.alloc = true
			nd.allocMode = snap.nodeModes[nid] or 0
		end
	end
	spec.allocMode = snap.allocMode
	spec.attributeIndex = snap.attrIndex
end

function M.capture_spec_into(snap, sp, j_ids)
	local a_ids, n_modes, attr_sigs = {}, {}, {}
	for nid, nd in pairs(sp.allocNodes or {}) do
		t_insert(a_ids, nid)
		n_modes[nid] = (type(nd) == "table" and nd.allocMode) or 0
		if type(nd) == "table" and nd.isAttribute then attr_sigs[nid] = override_sig(nd) end
	end
	local h_ovr, h_sig = {}, {}
	for nid, ov in pairs(sp.hashOverrides or {}) do
		h_ovr[nid] = ov
		h_sig[nid] = override_sig(ov)
	end
	for _, iid in pairs(sp.jewels or {}) do
		if iid and iid ~= 0 then j_ids[iid] = true end
	end
	snap.classId = sp.curClassId
	snap.ascId = sp.curAscendClassId
	snap.secAscId = sp.curSecondaryAscendClassId or sp.secondaryAscendClassId or 0
	snap.treeVer = sp.treeVersion
	snap.allocMode = sp.allocMode or 0
	snap.attrIndex = sp.attributeIndex or 1
	snap.allocNodes = shallow_copy(sp.allocNodes)
	snap.allocIds = a_ids
	snap.nodeModes = n_modes
	snap.allocExt = copy_array(sp.allocExtendedNodes)
	snap.jewels = shallow_copy(sp.jewels)
	snap.notableCnt = sp.allocatedNotableCount
	snap.keystoneCnt = sp.allocatedKeystoneCount
	snap.masterySel = shallow_copy(sp.masterySelections)
	snap.hashOvr = h_ovr
	snap.hashOvrSig = h_sig
	snap.allocAttrSig = attr_sigs
end

function M.restore_spec(b, snap, j_dirty)
	local spec = b.spec
	if type(spec) ~= "table" then return false end

	local cur_sec = spec.curSecondaryAscendClassId or spec.secondaryAscendClassId or 0
	local spec_rep = (spec ~= snap.specRef)
	local cls_dirty = (spec.curClassId ~= snap.classId)
		or (spec.curAscendClassId ~= snap.ascId)
		or (cur_sec ~= snap.secAscId)
		or (spec.treeVersion ~= snap.treeVer)

	if not j_dirty then
		if not tables_eq(spec.jewels or {}, snap.jewels) then
			j_dirty = true
		elseif spec.tree and type(spec.tree.sockets) == "table" then
			for sid in pairs(spec.tree.sockets) do
				if (spec.allocNodes[sid] ~= nil) ~= (snap.allocNodes[sid] ~= nil) then j_dirty = true; break end
			end
		end
	end

	local mode_dirty = node_modes_differ(spec, snap)
	local ovr_dirty = overrides_differ(spec, snap)
	local alloc_dirty = not keys_eq(spec.allocNodes, snap.allocNodes)
	local dirty = spec_rep or cls_dirty or j_dirty or mode_dirty or ovr_dirty or alloc_dirty
		or (not tables_eq(spec.masterySelections or {}, snap.masterySel))
	if not dirty then return false end

	if spec_rep or cls_dirty then
		local ws_map = {}
		for nid, mode in pairs(snap.nodeModes) do
			if mode and mode ~= 0 then ws_map[nid] = mode end
		end
		-- PoE2 PassiveSpec:ImportFromNodeList takes weaponSets as 6th arg before hashOverrides!
		spec:ImportFromNodeList(
			nil, snap.classId, snap.ascId, snap.secAscId,
			copy_array(snap.allocIds), ws_map, shallow_copy(snap.hashOvr),
			shallow_copy(snap.masterySel), snap.treeVer
		)
		spec.allocMode = snap.allocMode
		spec.attributeIndex = snap.attrIndex
		replace_tbl(spec.jewels, snap.jewels)
	else
		if type(spec.masterySelections) == "table" then replace_tbl(spec.masterySelections, snap.masterySel) end
		if type(spec.jewels) == "table" then replace_tbl(spec.jewels, snap.jewels) end
		if ovr_dirty then restore_hash_overrides(spec, snap) end
		sync_alloc_nodes_poe2(spec, snap)
		spec.allocatedNotableCount = snap.notableCnt
		spec.allocatedKeystoneCount = snap.keystoneCnt
	end
	if type(spec.allocExtendedNodes) == "table" then replace_arr(spec.allocExtendedNodes, snap.allocExt) end
	if type(spec.BuildAllDependsAndPaths) == "function" then spec:BuildAllDependsAndPaths() end
	if b.itemsTab and type(b.itemsTab.UpdateSockets) == "function" then b.itemsTab:UpdateSockets() end
	return true
end

-- Compatible wrapper for bridge.lua `set_gem` accepting both `index` and legacy `gem` with 1-based bounds checks.
function M.rpc_set_gem(params, bridge_dispatcher)
	params = type(params) == "table" and shallow_copy(params) or {}
	local b = H.get_build({ rebuild = false })
	local st = b.skillsTab
	local grp_idx = tonumber(params.group or b.mainSocketGroup or 1)
	local sg = st and st.socketGroupList and grp_idx and st.socketGroupList[grp_idx]
	if not sg then error("no socket group at index " .. tostring(grp_idx), 0) end
	local raw_idx = params.index ~= nil and params.index or params.gem
	local gem_idx = tonumber(raw_idx)
	local gl = type(sg.gemList) == "table" and sg.gemList or {}
	if not gem_idx or gem_idx < 1 or gem_idx > #gl or not gl[gem_idx] then
		error("gem index " .. tostring(raw_idx) .. " out of range (1-based; group " .. tostring(grp_idx) .. " has " .. tostring(#gl) .. " gems)", 0)
	end
	params.group = grp_idx
	params.index = gem_idx
	return bridge_dispatcher("set_gem", params)
end

-- PoE2 0.5.5+ (#2498) per-skill weapon-set binding mutator & state reader (works on both 0.23.1 & 0.5.5+ dev).
function M.rpc_set_skill_weapon_set(params)
	params = type(params) == "table" and params or {}
	local b = H.get_build({ rebuild = false })
	local st = b.skillsTab
	local idx = tonumber(params.index or b.mainSocketGroup or 1)
	local sg = st and st.socketGroupList and st.socketGroupList[idx]
	if not sg then error("no socket group at index " .. tostring(idx), 0) end
	local gl = type(sg.gemList) == "table" and sg.gemList or {}
	local target_gems = {}
	if params.gemIndex ~= nil then
		local gi = tonumber(params.gemIndex)
		if not gi or gi < 1 or gi > #gl or not gl[gi] then
			error("gem index " .. tostring(params.gemIndex) .. " out of range (1-based; group " .. tostring(idx) .. " has " .. tostring(#gl) .. " gems)", 0)
		end
		target_gems[1] = gl[gi]
	else
		for i = 1, #gl do target_gems[i] = gl[i] end
	end

	local prev_s1, prev_s2, prev_fb = sg.set1, sg.set2, sg.forcedBoth
	local prev_g1, prev_g2 = {}, {}
	for i, gem in ipairs(target_gems) do
		prev_g1[i] = gem.enableGlobal1
		prev_g2[i] = gem.enableGlobal2
	end

	if params.set1 ~= nil then
		local v1 = params.set1 and true or false
		sg.set1 = v1
		if params.enableGlobal1 == nil then
			for _, gem in ipairs(target_gems) do gem.enableGlobal1 = v1 end
		end
	end
	if params.set2 ~= nil then
		local v2 = params.set2 and true or false
		sg.set2 = v2
		if params.enableGlobal2 == nil then
			for _, gem in ipairs(target_gems) do gem.enableGlobal2 = v2 end
		end
	end
	if params.forcedBoth ~= nil then sg.forcedBoth = params.forcedBoth and true or false end
	if params.enableGlobal1 ~= nil or params.enableGlobal2 ~= nil then
		for _, gem in ipairs(target_gems) do
			if params.enableGlobal1 ~= nil then gem.enableGlobal1 = params.enableGlobal1 and true or false end
			if params.enableGlobal2 ~= nil then gem.enableGlobal2 = params.enableGlobal2 and true or false end
		end
	end
	if type(st.weaponSetValidityCache) == "table" then st.weaponSetValidityCache = {} end
	if type(st.ReconcileSocketGroupWeaponSets) == "function" then pcall(st.ReconcileSocketGroupWeaponSets, st) end
	if type(st.ProcessSocketGroup) == "function" then st:ProcessSocketGroup(sg) end
	if b.calcsTab and type(b.calcsTab.mainEnv) == "table" then b.calcsTab.mainEnv.weaponSetEnvs = nil end
	b.buildFlag = true
	H.frame()

	local changed = (sg.set1 ~= prev_s1) or (sg.set2 ~= prev_s2) or (sg.forcedBoth ~= prev_fb)
	if not changed then
		for i, gem in ipairs(target_gems) do
			if gem.enableGlobal1 ~= prev_g1[i] or gem.enableGlobal2 ~= prev_g2[i] then
				changed = true
				break
			end
		end
	end
	local g1 = gl[1]
	return {
		ok = true, index = idx, set1 = sg.set1, set2 = sg.set2, forcedBoth = sg.forcedBoth,
		gem1EnableGlobal1 = g1 and g1.enableGlobal1, gem1EnableGlobal2 = g1 and g1.enableGlobal2,
		changed = changed, noOp = not changed,
	}
end

function M.rpc_get_weapon_set_state(_)
	local b = H.get_build({ rebuild = true })
	local sp, it, st = b.spec, b.itemsTab, b.skillsTab
	local node_modes, attr_overrides = H.json_obj({}), H.json_obj({})
	local ws1_cnt, ws2_cnt = 0, 0
	for nid, nd in pairs((sp and sp.allocNodes) or {}) do
		local m = (type(nd) == "table" and nd.allocMode) or 0
		node_modes[tostring(nid)] = m
		if m == 1 then ws1_cnt = ws1_cnt + 1 elseif m == 2 then ws2_cnt = ws2_cnt + 1 end
	end
	for nid, ov in pairs((sp and sp.hashOverrides) or {}) do
		attr_overrides[tostring(nid)] = {
			name = type(ov) == "table" and ov.dn or nil,
			sd = type(ov) == "table" and ov.sd or nil,
		}
	end
	local skill_sets = {}
	for i, sg in ipairs((st and st.socketGroupList) or {}) do
		local g1 = sg.gemList and sg.gemList[1]
		skill_sets[i] = {
			index = i, label = sg.label or "", enabled = sg.enabled and true or false,
			set1 = sg.set1, set2 = sg.set2, forcedBoth = sg.forcedBoth,
			gem1EnableGlobal1 = g1 and g1.enableGlobal1, gem1EnableGlobal2 = g1 and g1.enableGlobal2,
		}
	end
	local aset = it and it.activeItemSet
	return {
		ok = true, game = "poe2",
		allocMode = sp and (sp.allocMode or 0) or 0,
		attributeIndex = sp and (sp.attributeIndex or 1) or 1,
		useSecondWeaponSet = type(aset) == "table" and (aset.useSecondWeaponSet and true or false) or false,
		weaponSet1Used = ws1_cnt, weaponSet2Used = ws2_cnt,
		nodeModes = node_modes, hashOverrides = attr_overrides, skillWeaponSets = skill_sets,
	}
end

M.shallow_copy = shallow_copy
M.copy_array = copy_array
M.replace_arr = replace_arr
M.replace_tbl = replace_tbl
M.tables_eq = tables_eq
M.keys_eq = keys_eq

return M
