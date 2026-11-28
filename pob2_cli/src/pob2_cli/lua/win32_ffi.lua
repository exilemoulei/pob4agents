-- Windows FFI platform & PoE2/zlib1.dll bindings for headless LuaJIT host.
local ffi = require("ffi")
local bit = require("bit")
local H = require("pob_host")

local M = {}
local _cdef_done = false

local function ensure_cdef()
	if _cdef_done then return end
	ffi.cdef[[
		typedef int WIN_BOOL;
		typedef unsigned long WIN_DWORD;
		typedef unsigned short WIN_WORD;
		typedef unsigned short WIN_WCHAR;
		typedef int64_t WIN_LONGLONG;
		typedef void* WIN_HANDLE;

		typedef struct { WIN_DWORD dwLowDateTime; WIN_DWORD dwHighDateTime; } WIN_FILETIME;
		typedef struct {
			WIN_DWORD dwFileAttributes; WIN_FILETIME ftCreationTime;
			WIN_FILETIME ftLastAccessTime; WIN_FILETIME ftLastWriteTime;
			WIN_DWORD nFileSizeHigh; WIN_DWORD nFileSizeLow;
			WIN_DWORD dwReserved0; WIN_DWORD dwReserved1;
			WIN_WCHAR cFileName[260]; WIN_WCHAR cAlternateFileName[14];
		} WIN_FIND_DATAW;
		typedef struct {
			WIN_DWORD dwFileAttributes; WIN_FILETIME ftCreationTime;
			WIN_FILETIME ftLastAccessTime; WIN_FILETIME ftLastWriteTime;
			WIN_DWORD nFileSizeHigh; WIN_DWORD nFileSizeLow;
		} WIN_FILE_ATTR_DATA;

		WIN_BOOL QueryPerformanceCounter(WIN_LONGLONG *lpPerformanceCount);
		WIN_BOOL QueryPerformanceFrequency(WIN_LONGLONG *lpFrequency);
		WIN_BOOL CreateDirectoryW(const WIN_WCHAR *lpPathName, void *lpSecurityAttributes);
		WIN_BOOL RemoveDirectoryW(const WIN_WCHAR *lpPathName);
		WIN_BOOL DeleteFileW(const WIN_WCHAR *lpFileName);
		WIN_BOOL SetCurrentDirectoryW(const WIN_WCHAR *lpPathName);
		WIN_DWORD GetCurrentDirectoryW(WIN_DWORD nBufferLength, WIN_WCHAR *lpBuffer);
		WIN_DWORD GetFileAttributesW(const WIN_WCHAR *lpPathName);
		WIN_BOOL GetFileAttributesExW(const WIN_WCHAR *lpPathName, int fInfoLevelId, void *lpFileInformation);
		WIN_HANDLE FindFirstFileW(const WIN_WCHAR *lpFileName, WIN_FIND_DATAW *lpFindFileData);
		WIN_BOOL FindNextFileW(WIN_HANDLE hFindFile, WIN_FIND_DATAW *lpFindFileData);
		WIN_BOOL FindClose(WIN_HANDLE hFindFile);
	]]
	_cdef_done = true
end

local FILE_ATTR_DIRECTORY = 0x10
local FILE_ATTR_REPARSE_POINT = 0x400
local INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF
local utf8_to_wide, wide_to_utf8, strip_slash = H.utf8_to_wide, H.wide_to_utf8, H.norm_path

local function filetime_to_unix(ft)
	local ticks = tonumber(ft.dwHighDateTime) * 4294967296.0 + tonumber(ft.dwLowDateTime)
	local sec = ticks / 10000000.0 - 11644473600.0
	return sec < 0 and 0 or sec
end

local function load_zlib(game_dir)
	for _, name in ipairs({ game_dir .. "/zlib1.dll", "zlib1.dll", "zlib1", "z" }) do
		local ok, lib = pcall(ffi.load, name)
		if ok and lib then return lib end
	end
	error("win32_ffi: unable to load zlib1.dll from '" .. tostring(game_dir) .. "'")
end

function M.install(game_dir)
	ensure_cdef()
	local gdir = strip_slash(tostring(game_dir or "."))
	local kernel32 = ffi.C
	local invalid_handle = ffi.cast("WIN_HANDLE", -1)

	local cpath_entry = gdir .. "/?.dll"
	if not (package.cpath or ""):find(cpath_entry, 1, true) then
		package.cpath = cpath_entry .. ";" .. (package.cpath or "")
	end

	local qpc_freq, qpc_start, qpc_now = ffi.new("WIN_LONGLONG[1]"), ffi.new("WIN_LONGLONG[1]"), ffi.new("WIN_LONGLONG[1]")
	kernel32.QueryPerformanceFrequency(qpc_freq)
	kernel32.QueryPerformanceCounter(qpc_start)
	local qpc_div = tonumber(qpc_freq[0]) / 1000.0
	if qpc_div <= 0 then qpc_div = 1.0 end

	local function win_get_time()
		kernel32.QueryPerformanceCounter(qpc_now)
		return math.floor(tonumber(qpc_now[0] - qpc_start[0]) / qpc_div)
	end

	local function win_is_dir(path)
		if type(path) ~= "string" or path == "" then return false end
		local attrs = kernel32.GetFileAttributesW(utf8_to_wide(path))
		return attrs ~= INVALID_FILE_ATTRIBUTES and bit.band(attrs, FILE_ATTR_DIRECTORY) ~= 0
	end

	local function win_getcwd()
		local buf = ffi.new("WIN_WCHAR[4096]")
		local n = kernel32.GetCurrentDirectoryW(4096, buf)
		if n == 0 or n >= 4096 then return gdir end
		return wide_to_utf8(buf, n):gsub("\\", "/")
	end

	local function win_chdir(path)
		if type(path) == "string" and path ~= "" then kernel32.SetCurrentDirectoryW(utf8_to_wide(path)) end
	end

	local function win_mkdir_p(path)
		if type(path) ~= "string" or path == "" then return nil, "empty path" end
		local norm = strip_slash(path)
		if win_is_dir(norm) then return true end
		local acc, rest = "", norm
		local unc_root, unc_tail = norm:match("^(//[^/]+/[^/]+)(.*)$")
		if unc_root then acc, rest = unc_root, unc_tail or ""
		elseif norm:sub(1, 1) == "/" then acc, rest = "/", norm:sub(2) end
		for seg in rest:gmatch("[^/]+") do
			acc = (acc == "" and seg) or (acc == "/" and ("/" .. seg)) or (acc .. "/" .. seg)
			if not acc:match("^%a:$") and not win_is_dir(acc) then
				local ok = kernel32.CreateDirectoryW(utf8_to_wide(acc), nil)
				if ok == 0 and not win_is_dir(acc) then return nil, "mkdir failed: " .. acc end
			end
		end
		return true
	end

	local function win_file_mtime(path)
		if type(path) ~= "string" or path == "" then return 0 end
		local info = ffi.new("WIN_FILE_ATTR_DATA")
		if kernel32.GetFileAttributesExW(utf8_to_wide(path), 0, info) == 0 then return 0 end
		return filetime_to_unix(info.ftLastWriteTime)
	end

	local function win_remove_dir(path, recurse)
		if type(path) ~= "string" or path == "" then return false, "empty path" end
		local norm = strip_slash(path)
		if norm == "/" or norm == "." or norm == ".." or norm:match("^%a:/?$") then
			return false, "refusing to remove root/dot directory"
		end
		local top_attr = kernel32.GetFileAttributesW(utf8_to_wide(norm))
		local is_reparse = (top_attr ~= INVALID_FILE_ATTRIBUTES) and (bit.band(top_attr, FILE_ATTR_REPARSE_POINT) ~= 0)
		if recurse and not is_reparse then
			local fd = ffi.new("WIN_FIND_DATAW")
			local h = kernel32.FindFirstFileW(utf8_to_wide(norm .. "/*"), fd)
			if h ~= invalid_handle then
				repeat
					local name = wide_to_utf8(fd.cFileName)
					if name ~= "." and name ~= ".." then
						local child = norm .. "/" .. name
						local attr = fd.dwFileAttributes
						if bit.band(attr, FILE_ATTR_DIRECTORY) ~= 0 then
							local ok, err
							if bit.band(attr, FILE_ATTR_REPARSE_POINT) ~= 0 then
								ok = kernel32.RemoveDirectoryW(utf8_to_wide(child)) ~= 0
								err = not ok and ("rmdir failed: " .. child) or nil
							else
								ok, err = win_remove_dir(child, true)
							end
							if not ok then kernel32.FindClose(h); return false, err end
						else
							kernel32.DeleteFileW(utf8_to_wide(child))
						end
					end
				until kernel32.FindNextFileW(h, fd) == 0
				kernel32.FindClose(h)
			end
		end
		if kernel32.RemoveDirectoryW(utf8_to_wide(norm)) ~= 0 then return true end
		return false, "rmdir failed: " .. norm
	end

	local function win_new_file_search(pattern, dirs_only)
		if type(pattern) ~= "string" or pattern == "" then return nil end
		local want_dirs = dirs_only and true or false
		local find_data = ffi.new("WIN_FIND_DATAW")
		local handle = kernel32.FindFirstFileW(utf8_to_wide(pattern), find_data)
		if handle == invalid_handle then return nil end

		local function valid_entry()
			local name = wide_to_utf8(find_data.cFileName)
			if name == "." or name == ".." then return false end
			return (bit.band(find_data.dwFileAttributes, FILE_ATTR_DIRECTORY) ~= 0) == want_dirs
		end

		while not valid_entry() do
			if kernel32.FindNextFileW(handle, find_data) == 0 then
				kernel32.FindClose(handle); return nil
			end
		end

		local closed = false
		local gc_owner = ffi.gc(handle, function(h)
			if not closed then closed = true; kernel32.FindClose(h) end
		end)
		local obj = { _gc = gc_owner }
		function obj:NextFile()
			if closed then return false end
			while true do
				if kernel32.FindNextFileW(handle, find_data) == 0 then
					closed = true; ffi.gc(gc_owner, nil); kernel32.FindClose(handle); return false
				end
				if valid_entry() then return true end
			end
		end
		function obj:GetFileName() return wide_to_utf8(find_data.cFileName) end
		function obj:GetFileSize() return tonumber(find_data.nFileSizeHigh) * 4294967296.0 + tonumber(find_data.nFileSizeLow) end
		function obj:GetFileModifiedTime() return filetime_to_unix(find_data.ftLastWriteTime) end
		return obj
	end

	local win_inflate, win_deflate = H.zlib_codec(load_zlib(gdir))
	win_chdir(gdir)

	GetTime = win_get_time
	GetWorkDir = win_getcwd
	SetWorkDir = win_chdir
	MakeDir = win_mkdir_p
	RemoveDir = win_remove_dir
	NewFileSearch = win_new_file_search
	Inflate = win_inflate
	Deflate = win_deflate

	M.chdir, M.getcwd, M.mkdir_p, M.remove_dir = win_chdir, win_getcwd, win_mkdir_p, win_remove_dir
	M.file_mtime, M.is_dir, M.get_time = win_file_mtime, win_is_dir, win_get_time
	M.new_file_search, M.inflate, M.deflate = win_new_file_search, win_inflate, win_deflate
	M.installed = true
	return M
end

return M
