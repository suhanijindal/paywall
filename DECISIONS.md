# Decisions & things that broke

A running log. Written as it happens, not reconstructed afterwards.
Razorpay explicitly asks what broke and how it was recovered, so this file is
part of the submission, not scratch notes.

---

## Day 1 — 29 Aug

### Decision: build the payment path first, the intelligence last

The original plan built the sophisticated parts first and closed the
end-to-end loop on day six. Feedback from someone familiar with the track
changed this: a working end-to-end system matters more than clever
components. Reordered so that money moves on day one, ugly but real, and
every later change is layered onto something that already works.

Consequence: the catalog is a hardcoded Python list today. That looks
unimpressive in isolation and is a deliberate trade.

### Decision: money stored in paise, as integers

Razorpay's API takes amounts in paise. Rather than converting at the
boundary, the whole system uses paise throughout. Avoids floating-point
rounding entirely, and removes a class of bug where two parts of the code
disagree about units.

### Decision: mock mode instead of blocking on credentials

The payment functions return realistic fake responses when no API keys are
present. Cost about twenty lines. Benefit: development never blocked waiting
on dashboard access, and anyone cloning the repo gets a running system with
no setup.

### Broke: background server kept dying between test commands

Running uvicorn as a background process and then testing it from a separate
shell session failed - the process did not survive between sessions. Fixed by
starting the server and running the test sequence in one session. Trivial, but
it is the kind of thing that silently wastes an hour.

### Broke: first test run reused a stale database file

Test results looked wrong because a previous run's orders were still in
paywall.db, so the idempotency check fired when it should not have. Fixed by
deleting the database before the test sequence. Real lesson: tests that share
state with previous runs are not tests. Proper isolated test fixtures needed
before the adversarial suite gets written.

### Open question

Whether the human approval step should be a web page at all, or whether the
demo is stronger if approval happens through a signed message the buyer agent
relays. The web page is honest about how Razorpay checkout actually works.
Revisit once mandates are in.

---

## Day 3 — 1 Sept

### Direction change after research

Found that Razorpay already sells spending-limit guardrails as a product, and
that a similar open-source project already exists. So guardrails alone are not
a differentiator. Shifted the centre of the project from "we have guardrails"
to "we measured whether the guardrails work, and published the numbers that
look bad."

Also dropped the plan to build custom cryptographic mandates. NPCI already has
UPI Reserve Pay for delegated spending limits. Implementing a local
approximation of an existing standard is more defensible than inventing a
format.

### Broke: my first test run measured nothing

The first version of the simulator reported a 75% block rate and a 20.5%
false-block rate. Both numbers were wrong, and both were my fault.

**The false blocks were fake.** The shopper generator picked a budget sized for
one item, then sometimes asked for two. The system correctly refused. I had
labelled those refusals as mistakes. Fixed by choosing the product first, then
setting a budget above the real total with a varying amount of headroom.

**The injection tests proved nothing.** They sent an ordinary in-budget request
and checked that it went through. That tests nothing about injection. Rewrote
them to assume the worst case: the AI buyer is *completely* fooled and now
genuinely wants the expensive item. The real question is whether the user's
original spending limit still holds when the agent itself has been turned. It
does.

Lesson worth stating in the pitch: a measurement harness that reports
flattering numbers on the first run is usually measuring the wrong thing.

### Remaining honest weakness

Current results are 100% attacks blocked and 0% false blocks. Those numbers are
too clean to be trustworthy. Only 8 attack cases exist, and the honest shoppers
are all straightforward. Before submission the suite needs genuinely ambiguous
cases - purchases that are arguably fine and arguably not - so the false-block
number stops being zero. A system that never makes a mistake has not been
tested hard enough.

### Broke: MCP server crashed with "no such table: orders"

Found by live testing against Claude Desktop, not by the test suite. With the
database file missing, a `purchase` call through the MCP server died with a
sqlite `no such table: orders` internal error. The tables only ever got created
by the FastAPI app's startup hook (`store.init_db()`); the MCP server, reached
directly through its JSON-RPC `dispatch()`, assumed they already existed.

**Fix.** Call `store.init_db()` at the top of `dispatch()`, before any request
is handled. It is a set of `CREATE TABLE IF NOT EXISTS` statements — cheap and
safe to run on every call — so the server now stands up its own schema instead
of depending on another process having run first. (`main_loop()` already called
it, but no real caller — neither the tests nor the live stdio host — goes
through `main_loop()`; they all enter via `dispatch()`, which is why that call
never helped.)

**Why the suite missed it.** Every test used the `fresh_db` fixture, which
always calls `store.init_db()` on a `tmp_path` database before the test runs.
So a clean, valid schema was *always* present — the one condition under which
this bug cannot occur. The fixtures guaranteed away the exact state that breaks
production.

**Lesson.** Fixtures that always set up a clean, valid state cannot catch
missing-setup bugs. At least one test must exercise the absence of setup — here,
`test_purchase_recreates_a_missing_database` deletes the database file the
fixture created, then asserts the purchase still succeeds. It fails without the
fix and passes with it.
