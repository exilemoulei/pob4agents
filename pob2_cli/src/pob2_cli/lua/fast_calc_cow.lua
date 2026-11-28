-- PoE2 Lua CoW snapshot/restore kernel:
--   * Node-level dual weapon-set allocMode (0 | 1 | 2) and spec.allocMode / spec.attributeIndex
--   * Travel attribute node (+5 Str/Dex/Int) hashOverrides + spec.nodes[nid] restoration
--   * Per-skill-group set1 / set2 / forcedBoth (#2498 weaponSetEnvs & weaponSetValidityCache)
--     plus gem-level enableGlobal1 / enableGlobal2
--   * PoE2 items, Flask 1..2, Charm 1..3, Weapon 1/2 & Swap, activeItemSet.useSecondWeaponSet
local H = require("pob_host")
local ok_sp, SP = pcall(require, "cow_spec_snapshot")
if not ok_sp or type(SP) ~= "table" then
	local src = (debug.getinfo(1, "S") or {}).source or ""
	local dir = (_G.__POB_LUA_DIR or src:gsub("\\", "/"):match("^@?(.*)/[^/]+$") or "."):gsub("\\", "/")
	SP = dofile(dir .. "/cow_spec_snapshot.lua")
end
local M = {}
local t_insert, t_remove = table.insert, table.remove
local shallow_copy, copy_array = SP.shallow_copy, SP.copy_array
local replace_arr, replace_tbl, tables_eq = SP.replace_arr, SP.replace_tbl, SP.tables_eq

local MAX_SNAPS = 16
local snapshots, snap_order, next_seq = {}, {}, 0

local function get_build()
	return H.get_build({ rebuild = false })
end

local function prims_eq(a, b)
	for k, v in pairs(a) do if type(v) ~= "table" and b[k] ~= v then return false end end
	for k, v in pairs(b) do if type(v) ~= "table" and a[k] ~= v then return false end end
	return true
end

local function copy_sg(sg)
	local out, gl = shallow_copy(sg), {}
	if type(sg.gemList) == "table" then
		for i = 1, #sg.gemList do gl[i] = shallow_copy(sg.gemList[i]) end
	end
	out.gemList = gl
	return out
end

local function clone_cmods(list)
	if type(list) ~= "table" then return nil end
	local out = {}
	for i = 1, #list do
		out[i] = { title = list[i].title, enabled = list[i].enabled, text = list[i].text }
	end
	return out
end

local function sync_slots(itab, snap)
	local aset, j_chg = itab.activeItemSet, false
	for sn, sl in pairs(itab.slots or {}) do
		local w = snap.slotSel[sn]
		if w ~= nil and sl.selItemId ~= w then
			sl.selItemId = w
			if sl.nodeId ~= nil then j_chg = true end
		elseif w == nil then
			sl.selItemId = 0
		end
		sl.active = snap.slotAct[sn]
	end
	if type(aset) == "table" then
		for sn, ss in pairs(aset) do
			if type(ss) == "table" and ss.selItemId ~= nil and snap.setSel[sn] == nil then aset[sn] = nil end
		end
		for sn, sel in pairs(snap.setSel) do
			local ss = aset[sn]
			if type(ss) ~= "table" then ss = {}; aset[sn] = ss end
			ss.selItemId, ss.active = sel, snap.setAct[sn]
		end
	end
	return j_chg
end

local function restore_items(b, snap)
	local itab = b.itemsTab
	if type(itab) ~= "table" then return false, false, false end
	local aset = itab.activeItemSet or {}
	local dirty, tbl_dirty, j_dirty = false, false, false

	for id in pairs(itab.items) do
		if snap.itemsRef[id] == nil then dirty, tbl_dirty = true, true; break end
	end
	if not tbl_dirty then
		for id, orig in pairs(snap.itemsRef) do
			if itab.items[id] ~= orig or (type(orig) == "table" and orig.raw ~= snap.itemRaw[id]) then
				dirty, tbl_dirty = true, true
				if snap.jewelIds[id] then j_dirty = true end
				break
			end
		end
	end
	if not dirty and not tables_eq(itab.itemOrderList, snap.itemOrderList) then
		dirty, tbl_dirty = true, true
	end
	for sn, sl in pairs(itab.slots or {}) do
		local ss = aset[sn]
		if sl.selItemId ~= snap.slotSel[sn] or sl.active ~= snap.slotAct[sn]
			or (ss and (ss.selItemId ~= snap.setSel[sn] or ss.active ~= snap.setAct[sn])) then
			dirty = true
			if sl.nodeId ~= nil then j_dirty = true end
		end
	end
	for sn in pairs(snap.slotSel) do
		if itab.slots[sn] == nil then dirty, j_dirty = true, true; break end
	end
	if not dirty then
		for sn, sel in pairs(snap.setSel) do
			local ss = aset[sn]
			if type(ss) ~= "table" or ss.selItemId ~= sel or ss.active ~= snap.setAct[sn] then dirty = true; break end
		end
	end
	if not dirty then return false, false, false end

	for id in pairs(itab.items) do
		if snap.itemsRef[id] == nil then itab.items[id] = nil end
	end
	for id, orig in pairs(snap.itemsRef) do
		if type(orig) == "table" and orig.raw ~= snap.itemRaw[id] and type(snap.itemRaw[id]) == "string" then
			local rb = H.make_obj("Item", snap.itemRaw[id])
			rb.id = id; rb:BuildModList()
			itab.items[id], snap.itemsRef[id] = rb, rb
			if snap.jewelIds[id] then j_dirty = true end
		elseif itab.items[id] ~= orig then
			itab.items[id] = orig
		end
	end
	replace_arr(itab.itemOrderList, snap.itemOrderList)
	sync_slots(itab, snap)
	return true, j_dirty, tbl_dirty
end

local function skills_differ(b, snap)
	local st = b.skillsTab
	if b.mainSocketGroup ~= snap.mainSG then return true end
	if (b.calcsTab and b.calcsTab.input and b.calcsTab.input.skill_number) ~= snap.calcsSkillNum then return true end
	local cur = st.socketGroupList or {}
	if #cur ~= #snap.groups then return true end
	for gi = 1, #snap.groups do
		local sg, ssg = cur[gi], snap.groups[gi]
		if sg.set1 ~= ssg.set1 or sg.set2 ~= ssg.set2 or sg.forcedBoth ~= ssg.forcedBoth then return true end
		if not prims_eq(sg, ssg) then return true end
		local cgl, sgl = sg.gemList or {}, ssg.gemList or {}
		if #cgl ~= #sgl then return true end
		for ji = 1, #sgl do
			if not prims_eq(cgl[ji], sgl[ji]) then return true end
		end
	end
	return false
end

local function restore_skills(b, snap)
	local st = b.skillsTab
	if type(st) ~= "table" or not skills_differ(b, snap) then return false end
	local list = st.socketGroupList
	for i = 1, #snap.groups do list[i] = copy_sg(snap.groups[i]) end
	while #list > #snap.groups do t_remove(list) end
	if st.skillSets and st.activeSkillSetId and st.skillSets[st.activeSkillSetId] then
		st.skillSets[st.activeSkillSetId].socketGroupList = list
	end
	-- Invalidate PoE2 0.5.5+ #2498 per-skill weapon-set validity cache & weaponSetEnvs
	if type(st.weaponSetValidityCache) == "table" then st.weaponSetValidityCache = {} end
	b.mainSocketGroup = snap.mainSG
	if b.calcsTab and b.calcsTab.input then b.calcsTab.input.skill_number = snap.calcsSkillNum end
	if b.calcsTab and type(b.calcsTab.mainEnv) == "table" then
		b.calcsTab.mainEnv.weaponSetEnvs = nil
	end
	if type(st.ReconcileSocketGroupWeaponSets) == "function" then
		pcall(st.ReconcileSocketGroupWeaponSets, st)
	end
	if type(st.UpdateSocketGroups) == "function" then
		st:UpdateSocketGroups()
	elseif type(st.ProcessSocketGroup) == "function" then
		for i = 1, #list do st:ProcessSocketGroup(list[i]) end
	end
	return true
end

local function cmods_differ(cur, saved)
	if cur == saved then return false end
	if type(cur) ~= "table" or type(saved) ~= "table" or #cur ~= #saved then return true end
	for i = 1, #saved do
		if cur[i].title ~= saved[i].title or cur[i].enabled ~= saved[i].enabled or cur[i].text ~= saved[i].text then
			return true
		end
	end
	return false
end

local function restore_config(b, snap)
	local ct = b.configTab
	if type(ct) ~= "table" then return false end
	local cs = ct.configSets and ct.activeConfigSetId and ct.configSets[ct.activeConfigSetId]
	local ci = (cs and cs.input) or ct.input
	if tables_eq(ci, snap.cfgIn) and not cmods_differ(cs and cs.customModsList, snap.cmods) then return false end
	replace_tbl(ci, snap.cfgIn)
	if ct.input and ct.input ~= ci then replace_tbl(ct.input, snap.cfgIn) end
	if cs and snap.cmods ~= nil then cs.customModsList = clone_cmods(snap.cmods) end
	if type(ct.UpdateControls) == "function" then ct:UpdateControls() end
	if type(ct.BuildModList) == "function" then ct:BuildModList() end
	return true
end

local function set_sig(b)
	local tt, it, st, ct = b.treeTab or {}, b.itemsTab or {}, b.skillsTab or {}, b.configTab or {}
	return table.concat({
		tostring(tt.activeSpec), #(tt.specList or {}),
		tostring(it.activeItemSetId), #(it.itemSetOrderList or {}),
		tostring(st.activeSkillSetId), #(st.skillSetOrderList or {}),
		tostring(ct.activeConfigSetId), #(ct.configSetOrderList or {}),
	}, ",")
end

local function forget(sid)
	snapshots[sid] = nil
	for i = #snap_order, 1, -1 do if snap_order[i] == sid then t_remove(snap_order, i) end end
end

local function remember(sid, snap)
	forget(sid)
	snapshots[sid] = snap
	t_insert(snap_order, sid)
	while #snap_order > MAX_SNAPS do snapshots[t_remove(snap_order, 1)] = nil end
end

local function rpc_cow_snapshot(_)
	local b = get_build()
	local sp, it, st, ct = b.spec, b.itemsTab, b.skillsTab, b.configTab
	local cs = ct.configSets and ct.activeConfigSetId and ct.configSets[ct.activeConfigSetId]
	local sl_s, sl_a, st_s, st_a, j_ids, i_ref, i_raw, grps = {}, {}, {}, {}, {}, {}, {}, {}
	for sn, sl in pairs(it.slots or {}) do
		sl_s[sn], sl_a[sn] = sl.selItemId, sl.active
		if sl.nodeId ~= nil and sl.selItemId and sl.selItemId ~= 0 then j_ids[sl.selItemId] = true end
	end
	for sn, ss in pairs(it.activeItemSet or {}) do
		if type(ss) == "table" and ss.selItemId ~= nil then st_s[sn], st_a[sn] = ss.selItemId, ss.active end
	end
	for id, o in pairs(it.items or {}) do i_ref[id], i_raw[id] = o, type(o) == "table" and o.raw or nil end
	for gi, sg in ipairs(st.socketGroupList or {}) do grps[gi] = copy_sg(sg) end
	next_seq = next_seq + 1
	local sid = "cow_" .. next_seq
	local snap = {
		build = b, treeTabRef = b.treeTab, specRef = sp, itemsTabRef = it, skillsTabRef = st, configTabRef = ct,
		sets = set_sig(b), charLvl = b.characterLevel, charLvlAuto = b.characterLevelAutoMode,
		mainSG = b.mainSocketGroup, calcsSkillNum = b.calcsTab and b.calcsTab.input and b.calcsTab.input.skill_number,
		slotSel = sl_s, slotAct = sl_a, setSel = st_s, setAct = st_a,
		weaponSwap = (it.activeItemSet or {}).useSecondWeaponSet and true or false,
		jewelIds = j_ids, itemsRef = i_ref, itemRaw = i_raw, itemOrderList = copy_array(it.itemOrderList),
		groups = grps, cfgIn = shallow_copy((cs and cs.input) or ct.input),
		cmods = clone_cmods(cs and cs.customModsList),
	}
	SP.capture_spec_into(snap, sp, j_ids)
	remember(sid, snap)
	return { snapId = sid }
end

local function req_sid(p, fn)
	local sid = type(p) == "table" and p.snapId or nil
	if type(sid) ~= "string" or sid == "" then error(fn .. " requires non-empty snapId", 0) end
	return sid
end

local function rpc_cow_restore(params)
	local sid = req_sid(params, "cow_restore")
	local snap = snapshots[sid] or error("Unknown cow snapshot: " .. sid, 0)
	local b = get_build()
	local stale = (b ~= snap.build or b.treeTab ~= snap.treeTabRef or b.itemsTab ~= snap.itemsTabRef
		or b.skillsTab ~= snap.skillsTabRef or b.configTab ~= snap.configTabRef) and "the build was replaced"
		or (set_sig(b) ~= snap.sets and "the active tree, item, skill or config set changed")
	if stale then forget(sid); error("Stale cow snapshot " .. sid .. ": " .. stale .. " since it was taken", 0) end

	local sc_d = b.characterLevel ~= snap.charLvl or b.characterLevelAutoMode ~= snap.charLvlAuto
	if sc_d then
		b.characterLevel, b.characterLevelAutoMode = snap.charLvl, snap.charLvlAuto
		local cl = b.controls and b.controls.characterLevel
		if cl and type(cl.SetText) == "function" then cl:SetText(tostring(snap.charLvl)) end
	end
	local it_d, j_d, tb_d = restore_items(b, snap)
	local aset = b.itemsTab and b.itemsTab.activeItemSet
	local sw_d = type(aset) == "table" and (aset.useSecondWeaponSet and true or false) ~= snap.weaponSwap
	if sw_d then aset.useSecondWeaponSet = snap.weaponSwap end
	local sp_d = SP.restore_spec(b, snap, j_d)
	local it = b.itemsTab
	if (it_d or tb_d or j_d or sp_d) and type(it) == "table" then
		if type(it.PopulateSlots) == "function" then it:PopulateSlots() end
		sync_slots(it, snap)
		if type(it.UpdateSockets) == "function" then it:UpdateSockets() end
	end
	local sk_d, cf_d = restore_skills(b, snap), restore_config(b, snap)
	if sc_d and not cf_d and b.configTab and type(b.configTab.BuildModList) == "function" then
		b.configTab:BuildModList()
	end
	if sc_d or sw_d or it_d or sp_d or sk_d or cf_d then
		if b.calcsTab and type(b.calcsTab.mainEnv) == "table" then b.calcsTab.mainEnv.weaponSetEnvs = nil end
		if type(wipeGlobalCache) == "function" then wipeGlobalCache() end
		b.buildFlag = true
		if params.rebuild ~= false then H.frame() end
	end
	if params.drop == true then forget(sid) else remember(sid, snap) end
	return { ok = true }
end

local function rpc_cow_drop(params)
	forget(req_sid(params, "cow_drop"))
	return { ok = true }
end

M.rpc_handlers = {
	cow_snapshot = rpc_cow_snapshot,
	cow_restore = rpc_cow_restore,
	cow_drop = rpc_cow_drop,
	set_gem = SP.rpc_set_gem,
	set_skill_weapon_set = SP.rpc_set_skill_weapon_set,
	get_weapon_set_state = SP.rpc_get_weapon_set_state,
}

return M
