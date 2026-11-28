-- Cross-platform (Windows / macOS / Linux) native LuaJIT headless runner for PoB2 + PobTools bridge.lua.
-- Usage:
--   Windows: pob2 _lua-host headless_runner.lua <game_dir> <bridge_lua_path>  (zero pob-zh.exe)
--   POSIX:   <engine>/bin/luajit headless_runner.lua <game_dir> <bridge_lua_path> (pinned 2460b3f)
local ffi, bit = require("ffi"), require("bit")

local RUNNER_DIR = (_G.__POB_LUA_DIR or (((debug.getinfo(1, "S") or {}).source or ""):gsub("\\", "/")
	:match("^@?(.*)/[^/]+$")) or "."):gsub("\\", "/")
local H = dofile(RUNNER_DIR .. "/pob_host.lua")
package.loaded.pob_host = H
local norm_path = H.norm_path
RUNNER_DIR = norm_path(RUNNER_DIR)

local GAME_DIR = norm_path(arg and arg[1] or ".")
local BRIDGE_PATH = (arg and arg[2] or ""):gsub("\\", "/")
local IS_WIN, IS_OSX, IS_X64 = (ffi.os == "Windows"), (ffi.os == "OSX"), (ffi.arch == "x64")

local RAW_STDOUT, RAW_STDERR = io.stdout, io.stderr
local function log_stderr(msg) RAW_STDERR:write(tostring(msg), "\n"); RAW_STDERR:flush() end
function print(...)
	local t, n = {}, select("#", ...)
	for i = 1, n do t[i] = tostring(select(i, ...)) end
	RAW_STDERR:write(table.concat(t, "\t"), "\n"); RAW_STDERR:flush()
end

local engine_root = os.getenv("POB2_ENGINE_ROOT") or os.getenv("POB_ENGINE_ROOT")
if not engine_root or engine_root == "" then engine_root = GAME_DIR:match("^(.*)/[^/]+$") or "." end
engine_root = norm_path(engine_root)

local data_root = os.getenv("POB2_DATA_ROOT")
if not data_root or data_root == "" then data_root = engine_root .. "/Data" end
data_root = norm_path(data_root)

local USER_DIR = os.getenv("POB2_USER_PATH")
if not USER_DIR or USER_DIR == "" then USER_DIR = GAME_DIR end
USER_DIR = norm_path(USER_DIR)
local READONLY_GAME_DIR = os.getenv("POB2_READONLY_GAME_DIR") == "1"

package.path = table.concat({
	GAME_DIR .. "/lua/?.lua", GAME_DIR .. "/lua/?/init.lua",
	GAME_DIR .. "/?.lua", GAME_DIR .. "/?/init.lua", package.path or "", RUNNER_DIR .. "/?.lua",
}, ";")
package.cpath = table.concat(IS_WIN
	and { GAME_DIR .. "/?.dll", engine_root .. "/bin/?.dll", package.cpath or "" }
	or { engine_root .. "/bin/?.so", engine_root .. "/bin/?.dylib", GAME_DIR .. "/?.so", GAME_DIR .. "/?.dylib", package.cpath or "" }, ";")

local CAPS, EXTRA_HANDLERS = { game = "poe2" }, {}
local function load_ext(filename)
	local chunk, err = loadfile(RUNNER_DIR .. "/" .. filename)
	if not chunk then return nil, err end
	local ok, mod = pcall(chunk)
	if not ok then log_stderr("[" .. filename .. "] " .. tostring(mod)); return nil, mod end
	return mod
end
local function register_handlers(tbl)
	if type(tbl) ~= "table" then return end
	for k, v in pairs(tbl) do if type(k) == "string" and type(v) == "function" then EXTRA_HANDLERS[k] = v end end
end

-- Platform FFI: Windows (win32_ffi.lua) vs POSIX (libc + libz)
if IS_WIN then
	local w32, werr = load_ext("win32_ffi.lua")
	if type(w32) ~= "table" or type(w32.install) ~= "function" then
		error("win32_ffi.lua load failed: " .. tostring(werr))
	end
	local plat = w32.install(GAME_DIR)
	if _G.__POB2_WIN_LUA_HOST or _G.__POB_WIN_LUA_HOST or (type(plat) == "table" and plat.installed) then
		CAPS.winLuaHost = true
	end
else
	ffi.cdef[[
		int chdir(const char *p); char *getcwd(char *b, size_t n);
		int mkdir(const char *p, unsigned int m); int rmdir(const char *p); int unlink(const char *p);
		typedef struct { int64_t tv_sec; int32_t tv_usec; int32_t _pad; } pob_timeval_t;
		int gettimeofday(pob_timeval_t *tv, void *tz);
		void *opendir(const char *n); int closedir(void *d);
	]]
	if IS_OSX then
		ffi.cdef[[
			typedef struct { uint64_t d_ino; uint64_t d_seekoff; uint16_t d_reclen; uint16_t d_namlen; uint8_t d_type; char d_name[1024]; } pob_dirent_t;
			typedef struct { uint8_t _head[48]; int64_t st_mtime; int64_t st_mtime_nsec; uint8_t _tail[256]; } pob_stat_t;
		]]
		if IS_X64 then
			ffi.cdef[[ pob_dirent_t *pob_readdir(void *dirp) asm("readdir$INODE64"); int pob_stat(const char *path, pob_stat_t *buf) asm("stat$INODE64"); ]]
		else
			ffi.cdef[[ pob_dirent_t *pob_readdir(void *dirp) asm("readdir"); int pob_stat(const char *path, pob_stat_t *buf) asm("stat"); ]]
		end
	else
		ffi.cdef[[
			typedef struct { uint64_t d_ino; int64_t d_off; uint16_t d_reclen; uint8_t d_type; char d_name[256]; } pob_dirent_t;
			typedef struct { uint8_t _head[88]; int64_t st_mtime; int64_t st_mtime_nsec; uint8_t _tail[256]; } pob_stat_t;
			pob_dirent_t *pob_readdir(void *dirp) asm("readdir");
			int pob_stat(const char *path, pob_stat_t *buf) asm("stat");
			int pob_xstat(int ver, const char *path, pob_stat_t *buf) asm("__xstat");
		]]
	end
	local _stat_fn, _xstat_fn, _xstat_ver = nil, nil, IS_X64 and 1 or 0
	do
		local ok, fn = pcall(function() return ffi.C.pob_stat end)
		if ok then _stat_fn = fn
		elseif not IS_OSX then
			local ok2, xfn = pcall(function() return ffi.C.pob_xstat end)
			if ok2 then _xstat_fn = xfn end
		end
	end
	local function posix_file_mtime(path)
		if type(path) ~= "string" or path == "" then return 0 end
		local st = ffi.new("pob_stat_t")
		local rc = _stat_fn and _stat_fn(path, st) or (_xstat_fn and _xstat_fn(_xstat_ver, path, st) or -1)
		if rc ~= 0 then return 0 end
		local sec = tonumber(st.st_mtime) or 0
		return sec < 0 and 0 or sec
	end
	local function posix_is_dir(p) local d = ffi.C.opendir(p); if d ~= nil then ffi.C.closedir(d); return true end; return false end
	local function posix_getcwd() local b = ffi.new("char[4096]"); local r = ffi.C.getcwd(b, 4096); return r == nil and GAME_DIR or ffi.string(r) end
	local function posix_chdir(p) if type(p) == "string" and p ~= "" then ffi.C.chdir(p) end end
	local function posix_mkdir_p(p)
		if type(p) ~= "string" or p == "" then return nil, "empty path" end
		local n = p:gsub("\\", "/"); local acc = (n:sub(1, 1) == "/") and "" or "."
		for seg in n:gmatch("[^/]+") do
			acc = (acc == "") and ("/" .. seg) or ((acc == ".") and seg or (acc .. "/" .. seg))
			if not posix_is_dir(acc) and ffi.C.mkdir(acc, 493) ~= 0 and not posix_is_dir(acc) then return nil, "mkdir failed: " .. acc end
		end
		return true
	end
	local function posix_list_entries(dir)
		local out, dp = {}, ffi.C.opendir(dir)
		if dp == nil then return out end
		while true do
			local ent = ffi.C.pob_readdir(dp); if ent == nil then break end
			local nm = ffi.string(ent.d_name)
			if nm ~= "." and nm ~= ".." then
				local full = (dir == "/" and "/" or (dir .. "/")) .. nm
				local dt = ent.d_type
				out[#out + 1] = { name = nm, full = full, is_dir = (dt == 4) or ((dt == 0 or dt == 10) and posix_is_dir(full)) }
			end
		end
		ffi.C.closedir(dp); return out
	end
	local function posix_remove_dir(p, rec)
		if type(p) ~= "string" or p == "" then return false, "empty path" end
		local n = norm_path(p)
		if n == "/" or n == "." or n == ".." then return false, "refusing to remove root/dot directory" end
		if rec then
			for _, e in ipairs(posix_list_entries(n)) do
				if e.is_dir then local ok, err = posix_remove_dir(e.full, true); if not ok then return false, err end
				else ffi.C.unlink(e.full) end
			end
		end
		return ffi.C.rmdir(n) == 0 and true or false, "rmdir failed: " .. n
	end
	posix_chdir(GAME_DIR)
	local ZLIB
	for _, nm in ipairs(IS_OSX and { "z", "/usr/lib/libz.dylib", "libz.dylib" } or { "z", "libz.so.1", "libz.so" }) do
		local ok, lib = pcall(ffi.load, nm); if ok and lib then ZLIB = lib; break end
	end
	if not ZLIB then error("Unable to load system zlib library via LuaJIT FFI") end
	Inflate, Deflate = H.zlib_codec(ZLIB)
	local START_TV = ffi.new("pob_timeval_t"); ffi.C.gettimeofday(START_TV, nil)
	function GetTime()
		local tv = ffi.new("pob_timeval_t"); ffi.C.gettimeofday(tv, nil)
		return math.floor(tonumber(tv.tv_sec - START_TV.tv_sec) * 1000 + tonumber(tv.tv_usec - START_TV.tv_usec) / 1000)
	end
	function GetWorkDir() return posix_getcwd() end
	function SetWorkDir(p) posix_chdir(p) end
	function MakeDir(p) return posix_mkdir_p(p) end
	function RemoveDir(p, r) return posix_remove_dir(p, r) end
	local fsMT = {}; fsMT.__index = fsMT
	function fsMT:NextFile() self._idx = self._idx + 1; return self._idx <= #self._items end
	function fsMT:GetFileName() local it = self._items[self._idx]; return it and it.name or nil end
	function fsMT:GetFileSize()
		local it = self._items[self._idx]; if not it or it.is_dir then return 0 end
		local f = io.open(it.full, "rb"); if not f then return 0 end
		local sz = f:seek("end") or 0; f:close(); return sz
	end
	function fsMT:GetFileModifiedTime() local it = self._items[self._idx]; return it and posix_file_mtime(it.full) or 0 end
	function NewFileSearch(spec, findDirs)
		if type(spec) ~= "string" or spec == "" then return nil end
		local n = spec:gsub("\\", "/"); local dir, pat = n:match("^(.*)/([^/]*)$")
		if not dir then dir, pat = ".", n elseif dir == "" then dir = "/" end
		if pat == "" then pat = "*" end
		local lp = "^" .. pat:gsub("[%(%)%.%%%+%-%[%]%^%$]", "%%%1"):gsub("%*", ".*"):gsub("%?", ".") .. "$"
		local want, matched = findDirs and true or false, {}
		for _, e in ipairs(posix_list_entries(dir)) do
			if e.is_dir == want and e.name:match(lp) then matched[#matched + 1] = e end
		end
		if #matched == 0 then return nil end
		return setmetatable({ _items = matched, _idx = 1 }, fsMT)
	end
end

-- Zero-write guard: prevent PoB2 from writing Settings.xml, Builds/, or Data/ModCache.lua into GAME_DIR
if READONLY_GAME_DIR and USER_DIR ~= GAME_DIR then
	local g_low = GAME_DIR:lower()
	local function is_inside_game_dir(p)
		if type(p) ~= "string" or p == "" then return false end
		local n = norm_path(p)
		local is_abs = n:sub(1, 1) == "/" or n:match("^%a:/") ~= nil
		if not is_abs then return true end
		local nl = n:lower()
		return nl == g_low or nl:sub(1, #g_low + 1) == (g_low .. "/")
	end
	local orig_io_open = io.open
	io.open = function(path, mode, ...)
		if type(mode) == "string" and mode:find("[wa+]") and is_inside_game_dir(path) then
			return nil, "read-only PoE2 game directory guard blocked write to " .. tostring(path)
		end
		return orig_io_open(path, mode, ...)
	end
	local orig_mkdir = MakeDir
	MakeDir = function(path)
		if is_inside_game_dir(path) then return true end
		return orig_mkdir(path)
	end
	local orig_rmdir = RemoveDir
	RemoveDir = function(path, rec)
		if is_inside_game_dir(path) then return false, "read-only PoE2 game directory" end
		return orig_rmdir(path, rec)
	end
	CAPS.readonlyGameDir = true
end

-- JSON serialization
local ESC = { ['"'] = '\\"', ['\\'] = '\\\\', ['\b'] = '\\b', ['\f'] = '\\f', ['\n'] = '\\n', ['\r'] = '\\r', ['\t'] = '\\t' }
local function jstr(s) return '"' .. s:gsub('[%z\1-\31\\"]', function(c) return ESC[c] or string.format("\\u%04x", c:byte()) end) .. '"' end
local function is_arr(t)
	local mt = getmetatable(t); if type(mt) == "table" and mt.__object then return false, 0 end
	local n, c = #t, 0
	for k in pairs(t) do c = c + 1; if type(k) ~= "number" or k ~= math.floor(k) or k < 1 or k > n then return false, 0 end end
	return c == n, n
end
local function to_json(v, d)
	d = d or 0; local ty = type(v)
	if ty == "nil" then return "null"
	elseif ty == "boolean" then return v and "true" or "false"
	elseif ty == "number" then
		if v ~= v or v == math.huge or v == -math.huge then return "null" end
		return (v == math.floor(v) and math.abs(v) < 9007199254740992.0) and string.format("%.0f", v) or string.format("%.14g", v)
	elseif ty == "string" then return jstr(v)
	elseif ty == "table" then
		if d > 32 then return '"<too deep>"' end
		local arr, n = is_arr(v)
		if arr then
			local p = {}; for i = 1, n do p[i] = to_json(v[i], d + 1) end
			return "[" .. table.concat(p, ",") .. "]"
		end
		local p = {}
		for k, x in pairs(v) do
			local xt = type(x)
			if xt ~= "function" and xt ~= "userdata" and xt ~= "thread" then
				local kt, ks = type(k), nil
				if kt == "string" then ks = k
				elseif kt == "number" then ks = (k == math.floor(k) and math.abs(k) < 9007199254740992.0) and string.format("%.0f", k) or string.format("%.14g", k) end
				if ks ~= nil then p[#p + 1] = jstr(ks) .. ":" .. to_json(x, d + 1) end
			end
		end
		return "{" .. table.concat(p, ",") .. "}"
	end
	return "null"
end

local function send_raw(line) RAW_STDOUT:write(line, "\n"); RAW_STDOUT:flush() end
local function send_event(ev, data) send_raw('{"event":' .. jstr(tostring(ev)) .. ',"data":' .. ((data == nil) and "null" or to_json(data, 0)) .. '}') end
local function send_result(id, res) send_raw('{"id":' .. to_json(id, 0) .. ',"result":' .. ((res == nil) and "null" or to_json(res, 0)) .. '}') end
local function send_error(id, code, msg) send_raw('{"id":' .. to_json(id, 0) .. ',"error":{"code":' .. jstr(tostring(code)) .. ',"message":' .. jstr(tostring(msg)) .. '}}') end

-- SimpleGraphic headless API stubs
local SW, SH = tonumber(os.getenv("POB_ZH_HEADLESS_W")) or 1920, tonumber(os.getenv("POB_ZH_HEADLESS_H")) or 1080
__callbackTable__, __mainObject__ = {}, nil
function SetCallback(n, f) __callbackTable__[n] = f end
function GetCallback(n) return __callbackTable__[n] end
function SetMainObject(o) __mainObject__ = o end
local function runCallback(n, ...)
	if __callbackTable__[n] then return __callbackTable__[n](...)
	elseif __mainObject__ and __mainObject__[n] then return __mainObject__[n](__mainObject__, ...) end
end

local imgMT = { __index = {
	Load = function(s) s.valid = true end, LoadArtRectangle = function(s) s.valid = true end,
	LoadArtArcBand = function(s) s.valid = true end, Unload = function(s) s.valid = false end,
	IsValid = function(s) return s.valid and true or false end, IsLoading = function() return false end,
	SetLoadingPriority = function() end, ImageSize = function() return 1, 1 end,
} }
function NewImageHandle() return setmetatable({ valid = false }, imgMT) end
function NewArtHandle() return setmetatable({}, { __index = { Size = function() return 1, 1 end } }) end
local texMT = { __index = {
	Allocate = function() return true end, Load = function() return true end, Save = function() return true end,
	Info = function() return { formatStr = "RGBA", width = 1, height = 1, layerCount = 1, mipCount = 1 } end,
	IsValid = function() return true end, StackTextures = function() return true end,
} }
Texture = { new = function() return setmetatable({}, texMT) end }

local function noop() end
function GetScreenSize() return SW, SH end
function GetVirtualScreenSize() return SW, SH end
function GetScreenScale() return 1 end
function GetDrawLayer() return 0, 0 end
function GetDrawColor() return 1, 1, 1, 1 end
function GetDPIScaleOverridePercent() return 0 end
function DrawStringCursorIndex() return 1 end
function GetAsyncCount() return 0 end
function GetCloudProvider() return nil, nil, nil end
function GetCursorPos() return 0, 0 end
function IsKeyDown() return false end
function Paste() return nil end
function LaunchSubScript() return nil end
function IsSubScriptRunning() return false end
SetClearColor, SetDrawLayer, SetViewport, SetBlendMode, SetDrawColor = noop, noop, noop, noop, noop
SetDPIScaleOverridePercent, DrawImage, DrawImageQuad, DrawString, RenderInit = noop, noop, noop, noop, noop
SetWindowTitle, SetCursorPos, ShowCursor, Copy, ConPrintTable = noop, noop, noop, noop, noop
ConExecute, ConClear, SpawnProcess, OpenURL, SetProfiling = noop, noop, noop, noop, noop
TakeScreenshot, Restart, Exit, SetForeground, AbortSubScript = noop, noop, noop, noop, noop

function StripEscapes(t) return type(t) == "string" and (t:gsub("%^%d", ""):gsub("%^x%x%x%x%x%x%x", "")) or "" end
function DrawStringWidth(h, _, t)
	if type(t) ~= "string" then return 0 end
	local s, w, i = StripEscapes(t), 0, 1
	while i <= #s do
		local b = s:byte(i)
		if b < 0x80 then w = w + 0.55; i = i + 1
		elseif bit.band(b, 0xE0) == 0xC0 then w = w + 0.6; i = i + 2
		elseif bit.band(b, 0xF0) == 0xE0 then w = w + 1.0; i = i + 3
		elseif bit.band(b, 0xF8) == 0xF0 then w = w + 1.0; i = i + 4
		else i = i + 1 end
	end
	return w * (tonumber(h) or 14)
end

function GetScriptPath() return GAME_DIR end
function GetRuntimePath() return GAME_DIR end
function GetUserPath() return USER_DIR end
function PCall(fn, ...) local r = { pcall(fn, ...) }; if r[1] then table.remove(r, 1); return nil, unpack(r) else return r[2] end end
function LoadModule(nm, ...)
	if not nm:match("%.lua$") then nm = nm .. ".lua" end
	local f, e = loadfile(nm); if f then return f(...) else error("LoadModule() error loading '" .. nm .. "': " .. tostring(e)) end
end
function PLoadModule(nm, ...)
	if not nm:match("%.lua$") then nm = nm .. ".lua" end
	local f, e = loadfile(nm); if f then return PCall(f, ...) else error("PLoadModule() error loading '" .. nm .. "': " .. tostring(e)) end
end
function ConPrintf(fmt, ...) local ok, m = pcall(string.format, tostring(fmt), ...); log_stderr(ok and m or tostring(fmt)) end
local orig_require = require
function require(nm) if nm == "lcurl.safe" or nm == "lcurl" then return nil end; return orig_require(nm) end

-- PobTools bridge host hooks (explicitly PoE2)
local bridge_dispatcher = nil
function PobToolsBridgeSetDispatcher(fn) if type(fn) ~= "function" then error("PobToolsBridgeSetDispatcher expects a function") end; bridge_dispatcher = fn end
function PobToolsBridgeEmit(ev, d) send_event(ev, d) end
function PobToolsHeadless() return true end
function PobToolsGame() return "poe2" end
function PobToolsLogError(tag, msg) log_stderr("[" .. tostring(tag) .. "] " .. tostring(msg)) end

local dkjson = require("dkjson")
arg = {}
dofile("Launch.lua")
runCallback("OnInit")
if __mainObject__ and __mainObject__.main and READONLY_GAME_DIR then
	__mainObject__.main.SaveSettings = noop
end
runCallback("OnFrame")
if __mainObject__ and __mainObject__.promptMsg then
	send_event("gate_result", { ok = false, failed = { "PoB2 Launch promptMsg: " .. tostring(__mainObject__.promptMsg) } })
	os.exit(1)
end

-- Install PoE2 zh translation + reverse item-text modules
do
	local zt = load_ext("zh_translate.lua")
	if type(zt) == "table" and type(zt.install_pre_bridge) == "function" then
		local ok, res = pcall(zt.install_pre_bridge, data_root)
		if ok and res ~= false then CAPS.localeSwitch = true; register_handlers(zt.rpc_handlers)
		elseif not ok then log_stderr("[zh_translate] " .. tostring(res)) end
	end
	local zr = load_ext("zh_reverse.lua")
	if type(zr) == "table" and pcall(zr.install, data_root) then CAPS.zhReverse = true end
end

-- Load PobTools bridge.lua
if BRIDGE_PATH == "" then BRIDGE_PATH = data_root .. "/bridge/bridge.lua" end
local b_chunk, b_err = loadfile(BRIDGE_PATH)
if not b_chunk then send_event("gate_result", { ok = false, failed = { "bridge.lua load failed (" .. BRIDGE_PATH .. "): " .. tostring(b_err) } }); os.exit(1) end
local ok_b, b_run_err = xpcall(b_chunk, debug.traceback)
if not ok_b then send_event("gate_result", { ok = false, failed = { "bridge.lua runtime error: " .. tostring(b_run_err) } }); os.exit(1) end

-- Install PoE2 #2529 (mainOutput nil) & #2582 (missing base drop) shields + optional M3 kernels
local shields_mod = nil
do
	local sh = load_ext("poe2_shields.lua")
	if type(sh) == "table" then
		shields_mod = sh
		sh.ensure_hooks()
		register_handlers(sh.rpc_handlers)
		CAPS.mainOutputShield = true
		CAPS.missingBaseDetector = true
	end
	local cow = load_ext("fast_calc_cow.lua")
	if type(cow) == "table" and type(cow.rpc_handlers) == "table" then
		register_handlers(cow.rpc_handlers)
		if cow.rpc_handlers.cow_snapshot and cow.rpc_handlers.cow_restore then CAPS.cowSnapshot = true end
	end
	local npk = load_ext("node_power_kernel.lua")
	if type(npk) == "table" and type(npk.rpc_handlers) == "table" then
		register_handlers(npk.rpc_handlers)
		CAPS.nodePowerKernel = true
	end
	local ap = load_ext("agent_probes.lua")
	if type(ap) == "table" and type(ap.rpc_handlers) == "table" then
		register_handlers(ap.rpc_handlers)
		CAPS.agentProbes = true
	end
	local dap = load_ext("deep_audit_probe.lua")
	if type(dap) == "table" and type(dap.rpc_handlers) == "table" then
		register_handlers(dap.rpc_handlers)
		CAPS.deepAuditProbe = true
	end
	local urk = load_ext("unique_rank_kernel.lua")
	if type(urk) == "table" and type(urk.rpc_handlers) == "table" then
		register_handlers(urk.rpc_handlers)
		CAPS.uniqueRankKernel = true
	end
end

-- Blocking NDJSON stdin/stdout RPC loop
while true do
	local line = io.stdin:read("*l")
	if not line then break end
	line = line:gsub("\r$", "")
	if line ~= "" then
		local req, _, dec_err = dkjson.decode(line, 1, nil)
		if type(req) ~= "table" then send_error(-1, "bad_json", dec_err or "invalid JSON")
		else
			local id, method = req.id, req.method; if id == nil then id = -1 end
			if type(method) ~= "string" or method == "" then send_error(id, "bad_request", "missing method")
			elseif type(bridge_dispatcher) ~= "function" then send_error(id, "no_bridge", "bridge.lua did not register a dispatcher")
			else
				if SetWorkDir then SetWorkDir(GAME_DIR) end
				local params = type(req.params) == "table" and req.params or {}
				local ok, res = xpcall(function()
					if shields_mod then shields_mod.ensure_hooks() end
					local fn = EXTRA_HANDLERS[method]
					local out = fn and fn(params, bridge_dispatcher) or bridge_dispatcher(method, params)
					if shields_mod then
						shields_mod.ensure_hooks()
						out = shields_mod.attach_diagnostics(method, out)
					end
					if method == "version" and type(out) == "table" then
						if type(out.caps) ~= "table" then out.caps = {} end
						for k, v in pairs(CAPS) do out.caps[k] = v end
					end
					return out
				end, debug.traceback)
				if ok then send_result(id, res) else send_error(id, "lua_error", tostring(res)) end
			end
		end
	end
end
