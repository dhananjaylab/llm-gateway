-- budget_increment.lua
--
-- Atomically record actual spend against a team's budget period and
-- report whether this call just crossed the soft-warning threshold or the
-- hard cap, so the caller (app/ratelimit/budget.py) can fire each event
-- exactly once per threshold crossing (per the Phase 2 test plan:
-- "spend crossing 80% triggers the warning header/webhook exactly once
-- per threshold crossing").
--
-- KEYS[1] = budget:{team_id}:{period}   (HASH: spend_usd, cap_usd, warned_80pct)
--
-- ARGV[1] = cost_usd to add for this call
-- ARGV[2] = cap_usd (seeds the hash if this is the first write of the period)
-- ARGV[3] = warn_fraction (e.g. 0.8)
-- ARGV[4] = key_ttl_seconds (until period rollover; caller computes this)
--
-- Returns: {new_spend_usd (string), cap_usd (string), crossed_warning (0/1)}
--
-- "crossed_cap" is deliberately NOT returned here: the hard cap is
-- enforced by BudgetEnforcer.precheck() BEFORE the provider call is ever
-- made (Document 03, Journey C: "the gateway rejects before any upstream
-- provider call is made"), so by the time this script runs the call has
-- already happened and billing it is correct regardless of whether it
-- pushed spend over the cap — see budget.py's module docstring for the
-- full reasoning on precheck-before-spend ordering.

local key = KEYS[1]
local delta = tonumber(ARGV[1])
local cap = tonumber(ARGV[2])
local warn_fraction = tonumber(ARGV[3])
local ttl = tonumber(ARGV[4])

local data = redis.call('HMGET', key, 'spend_usd', 'cap_usd', 'warned_80pct')
local spend = tonumber(data[1]) or 0
local existing_cap = tonumber(data[2])
local warned = data[3]

if existing_cap == nil then
    existing_cap = cap
end
if warned == false or warned == nil then
    warned = '0'
end

local after = spend + delta

local crossed_warning = 0
if existing_cap > 0 and after >= existing_cap * warn_fraction and warned == '0' then
    crossed_warning = 1
    warned = '1'
end

redis.call('HMSET', key, 'spend_usd', after, 'cap_usd', existing_cap, 'warned_80pct', warned)
redis.call('EXPIRE', key, ttl)

return {tostring(after), tostring(existing_cap), crossed_warning}
