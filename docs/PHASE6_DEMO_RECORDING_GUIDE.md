# Phase 6 — Demo Recording Guide

Goal, verbatim from Document 06 (Implementation Plan): a recording **under
4 minutes** showing requests flowing through the gateway with real-time
Grafana metrics, a simulated provider outage triggering fallback routing,
rate limiting kicking in for a high-volume team, and the circuit breaker
opening and recovering.

This guide turns that into a literal script: what's on screen, what you
type, and roughly when, so recording is mechanical rather than
improvised. Every command below is copy-pasteable against the Phase 5
stack exactly as delivered — nothing here requires new code.

---

## 0. Before you hit record

**Everything in this section happens off-camera.** The whole point of
Phase 5's `demo-seed` service is that nothing below needs to be configured
live — do it once, quietly, then record a clean take.

### 0.1 Bring the stack up and confirm it's healthy

```bash
docker compose up -d
./scripts/verify_stack_healthy.sh
docker compose logs demo-seed          # prints the demo team keys + curl examples
```

Expect `All services healthy after <N>s.` — budget 60–90s on a cold
machine (image pulls + builds included).

### 0.2 Capture real load-test numbers first

Document 06 Phase 6's own done criteria: *"every claim in the narrative
is traceable to a test or load-test result produced in Phase 5 — nothing
asserted that wasn't measured."* Run the load test **before** recording,
not after, so the numbers you narrate on camera (if you choose to show
any) are numbers you've already seen:

```bash
docker compose --profile load-test run --rm k6
```

Takes ~4–5 minutes. Copy the final `=== Gateway overhead (P99) ===` block
verbatim — you'll paste it into `docs/PHASE6_NARRATIVE.md` (this delivery
ships that file with `[PLACEHOLDER]` markers exactly where these numbers
go; see that doc's own instructions). Do **not** round up, average
against an old run, or restate from memory.

> If you'd rather capture this from CI instead of a laptop:
> `.github/workflows/integration.yml`'s `load-test` job (this Phase 6
> delivery) runs the identical command and uploads the output as a
> workflow artifact — trigger it manually with "Also run the k6 load test
> profile" checked, then download `k6-load-test-summary` from the run.

### 0.3 Open every window/tab you'll need, arranged so you never alt-tab mid-recording

| Window | Shows |
| --- | --- |
| Terminal 1 | `curl` commands you'll run live |
| Terminal 2 | `docker compose logs -f gateway --tail=0` (optional: live log scroll in the background) |
| Browser tab 1 | Grafana **Operations** dashboard — `http://localhost:3000/d/llm-gateway-operations`, admin/admin |
| Browser tab 2 | Grafana **Business** dashboard — `http://localhost:3000/d/llm-gateway-business` |
| Browser tab 3 | (Optional) Jaeger UI — `http://localhost:16686` — for a bonus trace close-up if time allows |

Set every Grafana dashboard's time range to **"Last 5 minutes"** with
auto-refresh **5s** before you start — this is what makes panels visibly
move during a ~4 minute take instead of looking static.

### 0.4 Reset state so the recording starts from a clean baseline

```bash
curl -s -X POST http://localhost:9000/_chaos/reset
curl -X PATCH http://localhost:8000/admin/limits/batch-devs \
  -H "X-Gateway-Admin-Key: dev-admin-key-change-me" \
  -H "Content-Type: application/json" \
  -d '{"rpm_cap": 20}'
```

(20 is `batch-devs`'s seeded default per `config/teams.yaml` — this just
guards against a prior manual test having left it patched to something
tiny.)

### 0.5 Pick and rehearse your recording tool

Any screen recorder works (OBS Studio, QuickTime on macOS, Windows Game
Bar). Settings that matter more than the specific tool:
- **1080p minimum**, 30fps is plenty (nothing here is fast motion).
- Record terminal + browser in the **same** capture, not a picture-in-
  picture — viewers need to see the curl command and the dashboard
  react in the same frame.
- **Increase your terminal font size** before recording (16–18pt) —
  this is the single most common portfolio-demo mistake; assume the
  viewer is on a laptop, not your 4K monitor.
- Do one silent dry run of the full script below, start to finish,
  before the take you keep. Timing drifts the first time through.

---

## 1. The script — four beats, ~4 minutes

Record in this exact order (Document 06's own instruction: "Record in
this exact order for narrative clarity").

### Beat 1 — Normal traffic on the Operations dashboard (~0:00–0:50)

**On screen:** Operations dashboard, then switch to terminal.

**Say (optional voiceover, or just type it as on-screen narration):**
"This is an LLM gateway sitting in front of OpenAI, Anthropic, and Ollama
— unified auth, rate limiting, and fallback for every team that calls
it."

**Do:**
```bash
for i in $(seq 1 8); do
  curl -s http://localhost:8000/v1/chat/completions \
    -H "X-Gateway-API-Key: sk-gw-datascience-demo-001" \
    -H "Content-Type: application/json" \
    -d '{"model": "tier-1-reasoning", "messages": [{"role": "user", "content": "Say hi in five words."}]}' \
    | jq -r '.choices[0].message.content, .provider'
  sleep 1
done
```

**Cut back to Grafana** for the last ~10s of this beat — the *Requests /
sec* and *P95/P99 Latency* panels on Operations should show a small,
clean bump from the 8 calls above.

### Beat 2 — Simulated outage → automatic fallback (~0:50–2:00)

**Say:** "If a provider goes down, the gateway retries, then fails over
automatically — the caller never sees it."

**Do — inject the outage:**
```bash
curl -s -X POST http://localhost:9000/_chaos/config \
  -H "Content-Type: application/json" \
  -d '{"provider": "openai", "error_rate": 1.0}'
```

**Do — send the same request again, on camera, and narrate the header:**
```bash
curl -s http://localhost:8000/v1/chat/completions \
  -H "X-Gateway-API-Key: sk-gw-datascience-demo-001" \
  -H "Content-Type: application/json" \
  -d '{"model": "tier-1-reasoning", "messages": [{"role": "user", "content": "Say hi in five words."}]}' \
  -D - -o /tmp/body.json | grep -i x-gateway-served-model
cat /tmp/body.json | jq .
```

Point out on screen: **still a 200**, `content` still populated, and
`X-Gateway-Served-Model: anthropic:claude-sonnet-5` — the fallback link,
not `openai:*`.

**Cut to Operations dashboard:** *Fallback Events* panel shows the spike;
*Error Rate by Provider* shows `openai` errors climbing while the overall
client-facing success rate stays flat. This is the single most important
shot in the whole recording — Document 03's Journey B, made visible.

**Do — clear the outage before moving on:**
```bash
curl -s -X POST http://localhost:9000/_chaos/reset
```

### Beat 3 — Rate limiting a high-volume team (~2:00–2:50)

**Say:** "Every team gets its own enforced limit — this one's capped at
20 requests a minute."

**Do:**
```bash
for i in $(seq 1 30); do
  curl -s -o /dev/null -w "%{http_code} " http://localhost:8000/v1/chat/completions \
    -H "X-Gateway-API-Key: sk-gw-batchdevs-demo-003" \
    -H "Content-Type: application/json" \
    -d '{"model": "ollama:llama3.2", "messages": [{"role": "user", "content": "hi"}]}'
done
echo
```

On screen: a run of `200`s followed by `429`s — narrate "twenty
succeeded, the rest were throttled with a `Retry-After` header, not
silently dropped."

**Cut to Business dashboard:** the *Rate-Limit and Budget Rejections*
panel shows `batch-devs` ticking up. If you also want the budget-bar
visual Document 06 calls out ("hammer one team's key ... show the
Business dashboard's budget bar move"), repeat a few calls against
`product-eng` after lowering its cap for the shot:

```bash
curl -X PATCH http://localhost:8000/admin/budgets/product-eng \
  -H "X-Gateway-Admin-Key: dev-admin-key-change-me" \
  -H "Content-Type: application/json" \
  -d '{"budget_cap_usd": 0.01}'
```
then one `product-eng` request — its *Budget Utilization by Team* stat
panel jumps toward red. **Restore the cap afterward** (`200.0`, the
seeded default) so the stack isn't left budget-locked for anyone re-
running this later.

### Beat 4 — Circuit breaker opens, then recovers (~2:50–3:50)

**Say:** "After enough consecutive failures, the gateway stops even
trying that provider — and probes it once, automatically, to recover."

**Do — force five consecutive failures against a single-link model
(isolates one circuit, per `CIRCUIT_BREAKER_FAILURE_THRESHOLD=5`):**
```bash
curl -s -X POST http://localhost:9000/_chaos/config \
  -H "Content-Type: application/json" \
  -d '{"provider": "gemini", "error_rate": 1.0}'

for i in $(seq 1 5); do
  curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8000/v1/chat/completions \
    -H "X-Gateway-API-Key: sk-gw-datascience-demo-001" \
    -H "Content-Type: application/json" \
    -d '{"model": "gemini:gemini-3.6-flash", "messages": [{"role": "user", "content": "hi"}]}'
done
```

**Cut to Operations dashboard:** *Circuit Breaker State* panel flips
`gemini/gemini-3.6-flash` from green (Closed) to red (Open) — this is a
`state-timeline` panel, so the transition itself is visible, not just a
before/after.

**Do — clear chaos, then wait out the cooldown (production default 60s;
if you patched `CIRCUIT_BREAKER_COOLDOWN_SECONDS` down for a shorter
demo, wait that long instead) and send one more request to trigger the
Half-Open probe:**
```bash
curl -s -X POST http://localhost:9000/_chaos/reset
sleep 60
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8000/v1/chat/completions \
  -H "X-Gateway-API-Key: sk-gw-datascience-demo-001" \
  -H "Content-Type: application/json" \
  -d '{"model": "gemini:gemini-3.6-flash", "messages": [{"role": "user", "content": "hi"}]}'
```

Panel flips back to green. **This is the one beat you may want to edit
for time** — a real 60-second wait is dead air on camera. Two honest
ways to handle it, pick one and say which in your narration:
1. **Speed up the clip** 8–10x during the wait, with an on-screen "⏩
   waiting for cooldown" label — this is standard practice for demo
   recordings and isn't misleading as long as it's labeled.
2. **Temporarily lower `CIRCUIT_BREAKER_COOLDOWN_SECONDS`** (e.g. to
   `10`) in `docker-compose.yml`'s `gateway` service just for the
   recording session, then restore it — real behavior, just a shorter
   wait. If you do this, say so in the narration ("cooldown shortened
   for this recording; production default is 60s") rather than let a
   viewer assume 10s is the real number.

---

## 2. Closing frame (~3:50–4:00)

End on the Grafana Business dashboard (or a title card) with three lines
on screen — the exact numbers from your own `docs/PHASE6_NARRATIVE.md`
once you've filled in the load-test placeholders:

```
<GATEWAY_P99_OVERHEAD_MS> ms P99 gateway overhead
<CONCURRENT_THROUGHPUT> concurrent requests sustained
Automatic multi-provider failover, per-team budgets, zero-downtime circuit recovery
```

---

## 3. Post-recording checklist

- [ ] Runtime is under 4:00 (trim the circuit-breaker wait first if over)
- [ ] Every number narrated on camera matches something in
      `docs/PHASE6_NARRATIVE.md` / the actual k6 output — nothing
      estimated or rounded up
- [ ] Captions/subtitles added if this goes on LinkedIn (most viewers
      watch muted)
- [ ] Re-run `curl .../admin/budgets/product-eng` and confirm the cap is
      back at `200.0` before you stop the stack — leave the demo
      environment in a re-runnable state for the next person
- [ ] `docker compose down -v` once you're done, so a stale chaos rule
      or drained bucket doesn't confuse the next recording session

---

## 4. If you'd rather not record narration live

A silent screen recording with on-screen text callouts (the `Say:` lines
above, shown as lower-third captions) is just as credible for a
portfolio piece as voiceover — arguably more so, since a viewer can read
faster than you can talk through four beats in under 4 minutes. Either
approach satisfies Document 06's own done criteria; pick whichever you
can execute cleanly in one or two takes.
