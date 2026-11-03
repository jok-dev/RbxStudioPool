--[[
	ROJOCONTROL FORK. Not part of stock Rojo.

	Lets other code drive this Rojo plugin through two CoreGui attributes, so anything that can run Luau in
	Studio (the Roblox Studio MCP's execute_luau, rbx-studio-pool, the command bar) can connect, pause and sync.

	RojoControlCommand (set by the controller), JSON:
		{ "seq": 1, "op": "connect", "port": 34872, "host": "localhost", "live": false }
		{ "seq": 2, "op": "sync_once" }
		{ "seq": 3, "op": "live", "on": true }
		{ "seq": 4, "op": "disconnect" }
	Each command needs a new seq; the same JSON twice does not fire a change.

	RojoControlState (written by the plugin), JSON:
		{ "seq": 2, "busy": false, "state": "paused", "live": false, "pending": 12, "lastSync": 1730000000,
		  "project": "My Game", "error": null }
	state is disconnected | connecting | paused | live | error. A command is finished when seq matches and
	busy is false. While paused, server changes are counted in pending instead of applied.
]]

local CoreGui = game:GetService("CoreGui")
local HttpService = game:GetService("HttpService")

local SoundPlayer = require(script.Parent.soundPlayer)

local COMMAND_ATTRIBUTE = "RojoControlCommand"
local STATE_ATTRIBUTE = "RojoControlState"
local DEFAULT_HOST = "localhost"
local PUBLISH_INTERVAL = 0.25
local UNMUTE_DELAY = 2 -- the disconnect notification (and its sound) comes a moment after endSession

local RojoControl = {}

function RojoControl.start(app)
	local seq = nil
	local busy = false
	local lastError = nil
	local connecting = false
	local published = nil

	local function currentState()
		local session = app.serveSession
		local appState = app.state or {}
		if session == nil then
			return if lastError then "error" else "disconnected", nil
		end

		local status = session:getStatus() -- "NotStarted" | "Connecting" | "Connected" | "Disconnected"
		if status == "NotStarted" or status == "Connecting" then
			return "connecting", nil
		elseif status == "Connected" then
			local sync = session:getSyncState()
			return if sync.live then "live" else "paused", sync
		end
		return if appState.errorMessage then "error" else "disconnected", nil
	end

	local function publish()
		local state, sync = currentState()
		if connecting and state ~= "connecting" then
			connecting = false
			busy = false
			if state == "error" or state == "disconnected" then
				lastError = lastError or (app.state and app.state.errorMessage) or "connection failed"
			end
		end

		local encoded = HttpService:JSONEncode({
			seq = seq,
			busy = busy,
			state = state,
			live = sync and sync.live or false,
			pending = sync and sync.pending or 0,
			lastSync = sync and sync.lastSync,
			project = app.state and app.state.projectName,
			error = lastError,
		})
		if encoded ~= published then
			published = encoded
			CoreGui:SetAttribute(STATE_ATTRIBUTE, encoded)
		end
	end

	local function finish(promise)
		busy = true
		promise
			:andThen(function()
				lastError = nil
			end)
			:catch(function(err)
				lastError = tostring(err)
			end)
			:finally(function()
				busy = false
				publish()
			end)
	end

	local handlers = {
		connect = function(command)
			-- sessions started from outside are silent: no connect/disconnect/notification sounds
			SoundPlayer.muted = true
			app:endSession()
			lastError = nil
			app.rojoControlSession = true
			app.rojoControlStartPaused = command.live == false
			app.setHost(command.host or DEFAULT_HOST)
			app.setPort(tostring(command.port))
			busy = true
			connecting = true
			app:startSession()
		end,
		disconnect = function()
			app:endSession()
			app.rojoControlSession = false
			lastError = nil
			task.delay(UNMUTE_DELAY, function()
				if not app.rojoControlSession then
					SoundPlayer.muted = false
				end
			end)
		end,
		live = function(command)
			local session = app.serveSession
			if session == nil then
				lastError = "Not connected"
				return
			end
			finish(session:setLive(command.on ~= false))
		end,
		sync_once = function()
			local session = app.serveSession
			if session == nil then
				lastError = "Not connected"
				return
			end
			finish(session:syncOnce())
		end,
	}

	local function onCommand()
		local raw = CoreGui:GetAttribute(COMMAND_ATTRIBUTE)
		if type(raw) ~= "string" then
			return
		end

		local ok, command = pcall(HttpService.JSONDecode, HttpService, raw)
		if not ok or type(command) ~= "table" then
			lastError = "Malformed " .. COMMAND_ATTRIBUTE
			publish()
			return
		end

		seq = command.seq
		local handler = handlers[command.op]
		if handler == nil then
			lastError = "Unknown op " .. tostring(command.op)
		else
			handler(command)
		end
		publish()
	end

	CoreGui:GetAttributeChangedSignal(COMMAND_ATTRIBUTE):Connect(function()
		task.spawn(onCommand)
	end)
	task.spawn(function()
		if CoreGui:GetAttribute(COMMAND_ATTRIBUTE) ~= nil then
			onCommand()
		end
		while true do
			publish()
			task.wait(PUBLISH_INTERVAL)
		end
	end)
end

return RojoControl
