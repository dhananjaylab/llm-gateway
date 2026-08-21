-- circuit_record_error_rate.lua
--
-- Phase 7: time-windowed sibling of circuit_record.lua, selected via
-- CIRCUIT_BREAKER_MODE=error_rate (default remains "fixed_count" — this
-- script is opt-in, never a silent behavior change). circuit_check.lua
-- is reused UNCHANGED for both modes: the Closed/Open/Half-Open
-- transition decision only reads state/opened_at/probe_in_flight from
-- KEYS[1], which is identical regardless of how failures are counted —
-- only the failure-counting window itself differs between modes.
--
-- Where circuit_record.lua counts failures over the last N *requests*
-- (a fixed-size LIST, LPUSH+LTRIM), this counts failures over the last
-- M *seconds* (a ZSET scored by timestamp — the same rolling-window
-- pattern app/resilience/health.py's HealthTracker already established)
-- — answering "how bad has the last 30 seconds been" instead of "how
-- bad were the last 10 calls," which behave very differently under
-- bursty or sparse traffic. This is the v2 extension the TRD's own
-- Appendix A anticipated ("move to a rolling error-rate percentage only
-- if the fixed threshold proves too twitchy").
--
-- Uses a DIFFERENT window key (KEYS[2] = circuit:{provider}:{model}:er_window,
-- a ZSET) than circuit_record.lua's KEYS[2] (circuit:{provider}:{model}:window,
-- a LIST) — so a deployment that ever switches CIRCUIT_BREAKER_MODE at
-- runtime never hits a Redis WRONGTYPE error against leftover data from
-- the other mode.
--
-- KEYS[1] = circuit:{provider}:{model}             (HASH: state, opened_at, probe_in_flight)
-- KEYS[2] = circuit:{provider}:{model}:er_window    (ZSET: score=timestamp, member=outcome-tagged)
--
-- ARGV[1] = outcome (1 = failure, 0 = success)
-- ARGV[2] = now (unix seconds, float)
-- ARGV[3] = window_seconds (CIRCUIT_BREAKER_ERROR_RATE_WINDOW_SECONDS, default 30)
-- ARGV[4] = error_rate_threshold (CIRCUIT_BREAKER_ERROR_RATE_THRESHOLD, a
--           fraction 0..1, default 0.5 — matches fixed_count's default
--           5-in-10 = 50% so switching modes isn't a surprising behavior
--           change)
-- ARGV[5] = a caller-supplied unique suffix (e.g. uuid4 hex) — ZSET
--           members must be unique or Redis silently dedupes identical
--           values and the window undercounts; circuit_record.lua's LIST
--           doesn't have this problem since LPUSH always appends
--           regardless of value.
-- ARGV[6] = minimum_samples (CIRCUIT_BREAKER_ERROR_RATE_MINIMUM_SAMPLES,
--           default 5) — the circuit stays Closed until at least this
--           many calls have landed in the window, regardless of error
--           rate. Without this floor, a single failure in a freshly-
--           reset or sparse window reads as "100% error rate" and trips
--           the circuit on one blip — exactly the "too twitchy" failure
--           mode a percentage threshold is supposed to avoid, not
--           reintroduce.
--
-- Returns: {previous_state, new_state} — same shape as circuit_record.lua.

local outcome = tonumber(ARGV[1])
local now = tonumber(ARGV[2])
local window_seconds = tonumber(ARGV[3])
local threshold = tonumber(ARGV[4])
local minimum_samples = tonumber(ARGV[6])
local member = (outcome == 1 and "f:" or "s:") .. tostring(now) .. ":" .. ARGV[5]

local state = redis.call('HGET', KEYS[1], 'state')
if state == false then
    state = 'closed'
end

if state == 'half_open' then
    redis.call('DEL', KEYS[2])
    if outcome == 0 then
        redis.call('HSET', KEYS[1], 'state', 'closed', 'probe_in_flight', '0')
        redis.call('HDEL', KEYS[1], 'opened_at')
        return {'half_open', 'closed'}
    else
        redis.call('HSET', KEYS[1], 'state', 'open', 'opened_at', now, 'probe_in_flight', '0')
        return {'half_open', 'open'}
    end
end

-- Closed (or unset/other, treated as closed) — append this outcome into
-- the time window, trim anything older than window_seconds, then decide.
redis.call('ZADD', KEYS[2], now, member)
redis.call('ZREMRANGEBYSCORE', KEYS[2], 0, now - window_seconds)
redis.call('EXPIRE', KEYS[2], window_seconds * 4)

local total = redis.call('ZCARD', KEYS[2])
local members = redis.call('ZRANGE', KEYS[2], 0, -1)
local failures = 0
for _, m in ipairs(members) do
    if string.sub(m, 1, 2) == 'f:' then
        failures = failures + 1
    end
end

local error_rate = 0
if total > 0 then
    error_rate = failures / total
end

if total >= minimum_samples and error_rate >= threshold then
    redis.call('HSET', KEYS[1], 'state', 'open', 'opened_at', now, 'probe_in_flight', '0')
    return {'closed', 'open'}
end

if state ~= 'closed' then
    redis.call('HSET', KEYS[1], 'state', 'closed')
end

return {'closed', 'closed'}
