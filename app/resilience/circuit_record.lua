-- circuit_record.lua
--
-- Atomically records the outcome of a call that circuit_check.lua just
-- admitted, updates the rolling failure window, and performs the
-- resulting state transition:
--   Closed:    push outcome into the rolling window; if failures in the
--              window reach the threshold, open the circuit.
--   Half-Open: this call WAS the single probe (see circuit_check.lua) —
--              success closes the circuit, failure reopens it. Either
--              way the rolling window is cleared, so a stale pre-outage
--              failure count never counts against a freshly-recovered
--              (or freshly-reopened) circuit.
--
-- KEYS[1] = circuit:{provider}:{model}          (HASH: state, opened_at, probe_in_flight)
-- KEYS[2] = circuit:{provider}:{model}:window   (LIST: rolling outcomes, "1"=failure "0"=success)
--
-- ARGV[1] = outcome (1 = failure, 0 = success)
-- ARGV[2] = now (unix seconds, float)
-- ARGV[3] = window_size (Document 06 Appendix A default: 10)
-- ARGV[4] = failure_threshold (default: 5, i.e. a 50% failure rate)
--
-- Returns: {previous_state, new_state} — both plain strings
-- ("closed"/"open"/"half_open"), so the caller (CircuitBreaker in
-- app/resilience/circuit_breaker.py) can log a structured transition
-- event only when they actually differ, per the TRD's "log every circuit
-- state transition" instruction, without this script itself doing any
-- logging (Lua scripts have no logger — that's Python's job).

local outcome = tonumber(ARGV[1])
local now = tonumber(ARGV[2])
local window_size = tonumber(ARGV[3])
local threshold = tonumber(ARGV[4])

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

-- Closed (or any other/unset state, treated as closed — record_* is only
-- ever called after circuit_check.lua admitted the request, and it never
-- admits a request while genuinely Open, so this branch is the normal
-- path for the overwhelming majority of calls).
redis.call('LPUSH', KEYS[2], tostring(outcome))
redis.call('LTRIM', KEYS[2], 0, window_size - 1)

local entries = redis.call('LRANGE', KEYS[2], 0, -1)
local failures = 0
for _, v in ipairs(entries) do
    if v == '1' then
        failures = failures + 1
    end
end

if failures >= threshold then
    redis.call('HSET', KEYS[1], 'state', 'open', 'opened_at', now, 'probe_in_flight', '0')
    return {'closed', 'open'}
end

if state ~= 'closed' then
    redis.call('HSET', KEYS[1], 'state', 'closed')
end

return {'closed', 'closed'}
