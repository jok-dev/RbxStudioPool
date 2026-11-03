local fs = require("@lune/fs")
local net = require("@lune/net")
local process = require("@lune/process")
local serde = require("@lune/serde")

local Base64 = require("../lib/Base64")
local Relay = require("../lib/Relay")

local PORT = 34991
-- "localhost" stalls Lune's client after ~10 requests on Windows, so the test addresses the relay by IP
local BASE_URL = `http://127.0.0.1:{PORT}`
local OUT_DIR = "spikes/place/lune/tests/.relay-test-out"
local CHANGESET_FILE = `{OUT_DIR}-changeset.json`
local CHANGESET_BODY = '{"changes":[]}'
local CHUNK_PATH = "Workspace.Map"
local BINARY_BODY = "\0\1\2\255\254<roblox!\r\n\0"
local MANIFEST_BODY = '{"roots":["Workspace"],"chunks":["Workspace.Map"],"skipped":[]}'
local OUTSIDE_FILE = "spikes/place/lune/tests/.relay-test-escape.rbxm"

local ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"

local failures = 0

local function encodeBase64(data: string): string
	local output = {}
	for start = 1, #data, 3 do
		local first, second, third = string.byte(data, start, start + 2)
		local combined = first * 65536 + (second or 0) * 256 + (third or 0)
		local characters = {}
		for position = 3, 0, -1 do
			local index = math.floor(combined / 64 ^ position) % 64 + 1
			table.insert(characters, string.sub(ALPHABET, index, index))
		end
		if not second then
			characters[3] = "="
		end
		if not third then
			characters[4] = "="
		end
		table.insert(output, table.concat(characters))
	end
	return table.concat(output)
end

local function allBytes(): string
	local bytes = {}
	for value = 0, 255 do
		table.insert(bytes, string.char(value))
	end
	return table.concat(bytes)
end

local function check(name: string, condition: boolean)
	if condition then
		print(`PASS {name}`)
	else
		failures += 1
		warn(`FAIL {name}`)
	end
end

local function getDoneList(baseUrl: string): { string }
	return serde.decode("json", net.request({ url = `{baseUrl}/done-list` }).body)
end

local function send(method: string, path: string, body: string?): net.FetchResponse
	return net.request({ url = `{BASE_URL}{path}`, method = method, body = body })
end

if fs.isDir(OUT_DIR) then
	fs.removeDir(OUT_DIR)
end
fs.writeFile(CHANGESET_FILE, CHANGESET_BODY)

local serveHandle = net.serve(PORT, Relay.createHandler({ outDir = OUT_DIR, changesetPath = CHANGESET_FILE }))
local noChangesetHandle = net.serve(PORT + 1, Relay.createHandler({ outDir = `{OUT_DIR}/nochangeset` }))

check("done-list empty before upload", #getDoneList(BASE_URL) == 0)

local postResponse = send("POST", `/chunk?path={CHUNK_PATH}`, encodeBase64(BINARY_BODY))
check("chunk POST ok", postResponse.ok)
check("chunk written with exact bytes", fs.readFile(`{OUT_DIR}/{CHUNK_PATH}.rbxm`) == BINARY_BODY)
check("done-list lists chunk", table.concat(getDoneList(BASE_URL), ",") == CHUNK_PATH)
check("no temp file left behind", not fs.isFile(`{OUT_DIR}/{CHUNK_PATH}.rbxm.tmp`))

for length = 0, 4 do
	local sample = string.sub(allBytes(), 250 - length, 253)
	check(`base64 round trip length {#sample}`, Base64.decode(encodeBase64(sample)) == sample)
end
check("base64 decode rejects garbage", Base64.decode("a*b") == nil)
check("every byte value survives base64 upload", send("POST", "/chunk?path=AllBytes", encodeBase64(allBytes())).ok and fs.readFile(`{OUT_DIR}/AllBytes.rbxm`) == allBytes())
check("non-base64 body rejected", send("POST", "/chunk?path=Garbage", "a*b").statusCode == 400)
check("rejected body writes nothing", not fs.isFile(`{OUT_DIR}/Garbage.rbxm`))

fs.writeFile(`{OUT_DIR}/Interrupted.rbxm.tmp`, "partial")
check("leftover .tmp not in done-list", not table.find(getDoneList(BASE_URL), "Interrupted") and not table.find(getDoneList(BASE_URL), "Interrupted.rbxm"))

local trickyPaths = { "Workspace.Foo#1", "Workspace.Foo#2", "Workspace.A&B", "Workspace.100%", "Workspace.A+B" }
for _, trickyPath in trickyPaths do
	local encodedPath = net.urlEncode(trickyPath)
	check(`encoded path accepted: {trickyPath}`, send("POST", `/chunk?path={encodedPath}`, encodeBase64(trickyPath)).ok)
	check(`encoded path round trips: {trickyPath}`, fs.readFile(`{OUT_DIR}/{trickyPath}.rbxm`) == trickyPath)
	check(`encoded path in done-list: {trickyPath}`, table.find(getDoneList(BASE_URL), trickyPath) ~= nil)
end

check("manifest POST ok", send("POST", "/manifest", MANIFEST_BODY).ok)
check("manifest.json written", fs.readFile(`{OUT_DIR}/manifest.json`) == MANIFEST_BODY)

local changesetResponse = send("GET", "/changeset")
check("changeset served", changesetResponse.ok and changesetResponse.body == CHANGESET_BODY)
local missingChangeset = net.request({ url = `http://127.0.0.1:{PORT + 1}/changeset` })
check("changeset 404 when not given", missingChangeset.statusCode == 404)

for label, unsafePath in
	{
		parent = "..%2Fescape",
		doubleDot = "A..B",
		slash = "a%2Fb",
		backslash = "a%5Cb",
		leadingDot = ".hidden",
		trailingDot = "trailing.",
		empty = "",
		colon = "a%3Ab",
		star = "a%2Ab",
		question = "a%3Fb",
		quote = "a%22b",
		angle = "a%3Cb",
		pipe = "a%7Cb",
		control = "a%01b",
	}
do
	local response = send("POST", `/chunk?path={unsafePath}`, encodeBase64("evil"))
	check(`unsafe path rejected: {label}`, response.statusCode == 400)
end
check("nothing written outside out dir", not fs.isFile(OUTSIDE_FILE) and not fs.isFile(`{OUT_DIR}/../escape.rbxm`))

check("Relay.isSafePath accepts dot path", Relay.isSafePath("ReplicatedStorage.Modules.Shared"))
check("Relay.isSafePath rejects traversal", not Relay.isSafePath("../x") and not Relay.isSafePath("a/b"))

serveHandle.stop()
noChangesetHandle.stop()
fs.removeDir(OUT_DIR)
fs.removeFile(CHANGESET_FILE)

if failures > 0 then
	warn(`{failures} relay check(s) failed`)
	process.exit(1)
end
print("All relay checks passed")
