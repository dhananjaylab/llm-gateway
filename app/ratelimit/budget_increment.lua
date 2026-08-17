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
-- ARGV[2] = cap_usd -- the team's CURRENT budget_cap_usd (team_config,
--           hot-reloaded via the Admin API), passed fresh on every call
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
-- pushed spend over the cap -- see budget.py's module docstring for the
-- full reasoning on precheck-before-spend ordering.
--
-- BUGFIX (caught during Phase 5 integration testing, not Phase 2): this
-- script used to seed `cap_usd` into the hash ONLY on the first write of
-- a period and reuse that stale stored value on every later call within
-- the same period -- so an admin PATCHing a team's budget cap mid-period
-- had NO enforcement effect until the period rolled over, directly
-- contradicting the PRD's "a policy change never requires a deploy or a
-- restart" user story and Document 03 Journey D. Phase 2's own test
-- suite never caught this because every unit test PATCHes the cap
-- BEFORE any spend has been recorded in that test's fresh fakeredis
-- period -- it never exercised "PATCH after spend already happened this
-- period," which is exactly what a real, long-lived container hit
-- immediately. Fix: always trust the freshly-passed ARGV[2] (the live
-- team_config value) as the enforcement cap, never the stale stored one
-- -- `cap_usd` is still persisted into the hash (useful for
-- observability / the Business dashboard's per-request snapshot), it
-- just no longer WINS over a live value on read.

local key = KEYS[1]
local delta = tonumber(ARGV[1])
local cap = tonumber(ARGV[2])
local warn_fraction = tonumber(ARGV[3])
local ttl = tonumber(ARGV[4])

local data = redis.call('HMGET', key, 'spend_usd', 'warned_80pct')
local spend = tonumber(data[1]) or 0
local warned = data[2]

if warned == false or warned == nil then
    warned = '0'
end

local after = spend + delta

local crossed_warning = 0
if cap > 0 and after >= cap * warn_fraction and warned == '0' then
    crossed_warning = 1
    warned = '1'
end

redis.call('HMSET', key, 'spend_usd', after, 'cap_usd', cap, 'warned_80pct', warned)
redis.call('EXPIRE', key, ttl)

return {tostring(after), tostring(cap), crossed_warning}
